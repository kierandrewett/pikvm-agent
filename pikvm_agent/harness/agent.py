"""Checkpointed provider-neutral task harness over the raw PiKVM MCP tools."""

from __future__ import annotations

import hashlib
import json
import re
import time
import uuid
from pathlib import Path
from typing import Any, Protocol

from PIL import Image, ImageDraw, ImageOps, UnidentifiedImageError

from pikvm_agent.executor.burst import BurstError, validate_actions
from pikvm_agent.harness.agent_models import (
    ComputerObservation,
    ControllerDecision,
    HarnessConfig,
    ModelRequest,
    ModelRole,
    PendingAction,
    PlanDecision,
    RunModelRoute,
    RunSnapshot,
    RunStatus,
    TERMINAL_RUN_STATUSES,
    VerificationDecision,
    VerificationImageArtifact,
)
from pikvm_agent.harness.agent_store import RunStore
from pikvm_agent.harness.model_budget import (
    DurableRunModelBudget,
    ModelBudgetExceeded,
    ModelBudgetPolicy,
)
from pikvm_agent.harness.model_pool import ModelPool, ModelPoolError
from pikvm_agent.harness.redaction import redact_secrets


class ComputerDriver(Protocol):
    async def open(self, label: str) -> ComputerObservation: ...

    async def refresh(self, *, session_id: str) -> ComputerObservation: ...

    async def burst(
        self,
        *,
        session_id: str,
        actions: list[dict[str, Any]],
        based_on_world_version: int | None,
        based_on_control_epoch: int | None,
        idempotency_key: str,
    ) -> ComputerObservation: ...

    async def resolve_approval(
        self, *, session_id: str, approval_id: str, decision: dict[str, Any]
    ) -> ComputerObservation: ...

    async def abort(self, *, session_id: str, reason: str) -> ComputerObservation: ...


_REASONER_SYSTEM = """\
You are the deliberate planner for a physical-computer task. Produce a short,
durable plan and observable completion criteria. The target is accessible only
through screenshots and guarded keyboard/mouse actions. Never propose base64,
large scripts, heredocs, compressed payloads, clipboard APIs, SSH, or hidden
side channels. Preserve existing/default values unless the user explicitly
asked to change them. Do not invent exact values, delays, quantities, formats,
or preferences absent from the task. Every success criterion must be necessary
to satisfy the user's literal request, not a nicer or stricter task the planner
made up. When the exact label is unavailable, a semantically equivalent visible
control may be used only when its effect satisfies the literal request and can
be verified. Do not add approval-request steps to the plan. The controller
proposes the next bounded action; the independent daemon policy decides whether
that exact action requires human approval and exits the model loop if it does."""

_CONTROLLER_SYSTEM = """\
You are the fast controller for a physical computer. Choose one small logical
burst against the supplied frame and checkpointed plan. Use only supported HID
actions. Keep text short and inspectable. Do not submit/send/delete/purchase/
install/change permissions in the same burst that prepares the action. Never
claim success; the independent verifier decides. If evidence is unclear, ask
for replan instead of guessing or repeating input. Do not wait for human
approval and do not claim that approval is missing: propose the bounded action,
then let the independent daemon policy create an exact approval request if the
action is consequential. Never infer keyboard focus from appearance, a window
being foreground, or a visible caret alone. If focus is not established by the
last verified action, first propose a separate bounded focus action and verify
it before typing. Once the verifier has established focus, proceed with the
requested bounded input. Local unsaved typing is not a commit; do not emit
pointer-only no-ops, repeated moves, or waits merely to preserve focus. After
any focus-lost or type-unverified result, do not repeat
the text; re-observe and establish focus first. After a verifier failure, do
not repeat the original action unchanged. Use the verifier evidence to propose
only a bounded correction, or block if the current pixels cannot support one.
Treat trajectory_signals as durable evidence. If the same exact search query
already produced visible no results, do not repeat it unless the application
or search scope visibly changed. Try one semantically equivalent visible
control or a different bounded navigation strategy, then replan or block
instead of cycling. If ungrounded_navigation_replans is nonzero, do not repeat
the same coordinate-only click: use a visibly grounded target, a safe keyboard
navigation action, or request a replan. Treat ungrounded_navigation_history as
explicitly refused pointer targets: do not revisit them or another blank/icon-
only target that cannot be independently read."""

_VERIFIER_SYSTEM = """\
You are the independent verifier. Compare the plan, intended action, before
state, and after state. When an action has before/after screenshots, the
attached image is a labelled comparison: the left panel is BEFORE and the
right panel is AFTER. Compare control geometry first (knob side, checkmark,
selection, text, enabled/disabled shape, and position); never assume that a
particular accent colour is required. Return complete only when visible
evidence proves every success criterion. Return one criteria assessment for
every zero-based success-criterion index; complete requires every assessment
to be satisfied by specific visible evidence. Return verified when the intended
action and its expected evidence are visibly proven but more task steps remain.
Do not return uncertain merely because the overall task is not complete. Return
uncertain only when the intended action result itself is ambiguous: OCR
ambiguity, unexpected focus, stale frames, missing characters,
transition/hover styling, or an unexplained UI change. Never infer success from
the controller's claim and never call a state-changing toggle failed merely
because its colour has not settled when its geometry visibly changed to the
intended state."""


class AgentHarness:
    """Deep module owning planning, action checkpoints, retries and approvals."""

    def __init__(
        self,
        *,
        computer: ComputerDriver,
        models: ModelPool,
        store: RunStore,
        config: HarnessConfig | None = None,
        budget_policy: ModelBudgetPolicy | None = None,
    ) -> None:
        self.computer = computer
        self.models = models
        self.store = store
        self.config = config or HarnessConfig()
        self.budget_policy = budget_policy or ModelBudgetPolicy(
            max_provider_attempts=self.config.max_provider_attempts_per_run,
        )

    async def start(self, task: str) -> RunSnapshot:
        run = await self.create(task)
        if run.status is RunStatus.FAILED:
            return run
        return await self._advance(run)

    async def create(
        self,
        task: str,
        *,
        caller: dict[str, Any] | None = None,
        model_provider: str | None = None,
        model_route: RunModelRoute | None = None,
    ) -> RunSnapshot:
        """Create/open a run without waiting for a model.

        UI/API callers use this to render the first frame immediately, then call
        ``continue_run`` in a background task while streaming checkpoints.
        """
        task = task.strip()
        if not task:
            raise ValueError("task must not be empty")
        run = RunSnapshot(
            run_id=str(uuid.uuid4()),
            task=task,
            status=RunStatus.PLANNING,
            caller=dict(caller or {}),
            model_provider=model_provider,
            model_route=model_route,
        )
        run.model_budget.provider_attempt_limit = (
            self.budget_policy.max_provider_attempts
        )
        run.model_budget.max_cost_microusd = (
            self.budget_policy.max_cost_microusd
        )
        run.model_budget.pricing_version = self.budget_policy.pricing_version
        run.record(
            "run.created",
            interface=run.caller.get("interface"),
            caller_label=run.caller.get("label"),
            model_provider=run.model_provider,
            model_route=(
                run.model_route.model_dump(mode="json", exclude_none=True)
                if run.model_route is not None
                else None
            ),
        )
        await self.store.save(run)
        try:
            observation = await self.computer.open(task)
        except Exception as exc:  # transport failed before any action
            run.status = RunStatus.FAILED
            run.error = f"computer open failed: {exc}"
            run.record("computer.open_failed", error=str(exc))
            await self.store.save(run)
            return run
        run.session_id = observation.session_id
        run.observation = observation
        run.status = RunStatus.RUNNING
        run.record(
            "computer.opened",
            session_id=observation.session_id,
            frame_id=observation.frame_id,
            world_version=observation.world_version,
        )
        await self.store.save(run)
        return run

    async def status(self, run_id: str) -> RunSnapshot:
        return await self.store.get_control(run_id)

    async def continue_run(self, run_id: str) -> RunSnapshot:
        run = await self.store.get_control(run_id)
        retry_provider_cooldown = (
            run.status is RunStatus.PAUSED
            and bool(run.events)
            and run.events[-1].kind == "model.failed"
        )
        if run.status is RunStatus.FAILED and self._recoverable_failure(
            run.observation
        ):
            # Compatibility for checkpoints created before recoverable
            # computer failures were modelled as pauses. The failed MCP result
            # is definitive, so advance the action index and never replay it.
            run.next_action_index += 1
            run.plan = None
            run.status = RunStatus.RUNNING
            run.error = None
            run.record("run.recovering_computer_failure")
            await self.store.save(run)
        if (
            run.status is RunStatus.FAILED
            and run.last_verification is not None
            and run.last_verification.verdict == "failed"
        ):
            # Compatibility for older checkpoints where verifier disagreement
            # ended the run. The action result is already definitive and its
            # index advanced, so replan a correction without replaying it.
            run.plan = None
            run.status = RunStatus.RUNNING
            run.error = None
            run.record("run.recovering_verification_failure")
            await self.store.save(run)
        if run.status is RunStatus.BLOCKED:
            if (
                run.events
                and run.events[-1].kind == "target.identity_changed"
            ):
                return run
            # A model/environment block contains no accepted HID action. Replan
            # from the durable observation so an operator can retry after the
            # condition changes without creating a new run.
            run.plan = None
            run.status = RunStatus.RUNNING
            run.error = None
            run.record("run.replanning_after_block")
            await self.store.save(run)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        if run.status is RunStatus.NEEDS_APPROVAL:
            return run
        run.status = RunStatus.RUNNING
        run.error = None
        return await self._advance(
            run,
            retry_provider_cooldown=retry_provider_cooldown,
        )

    async def pause(
        self, run_id: str, reason: str = "paused by operator"
    ) -> RunSnapshot:
        """Pause model progress without discarding a durable pending action."""

        run = await self.store.get_control(run_id)
        if run.status in TERMINAL_RUN_STATUSES or run.status is RunStatus.NEEDS_APPROVAL:
            return run
        run.status = RunStatus.PAUSED
        run.record("run.paused", reason=reason, source="operator")
        await self.store.save(run)
        return run

    async def steer(self, run_id: str, instruction: str) -> RunSnapshot:
        """Checkpoint operator guidance and force a fresh managed plan."""

        instruction = instruction.strip()
        if not instruction:
            raise ValueError("steering instruction must not be empty")
        if len(instruction) > 2_000:
            raise ValueError("steering instruction exceeds 2000 characters")
        run = await self.store.get_control(run_id)
        if run.origin != "managed":
            raise ValueError("only managed runs accept operator steering")
        if run.status in {
            RunStatus.COMPLETED,
            RunStatus.REJECTED,
            RunStatus.ABORTED,
            RunStatus.FAILED,
        }:
            raise ValueError("terminal run cannot be steered")
        if run.status is RunStatus.NEEDS_APPROVAL:
            raise ValueError(
                "pending approval must be resolved before steering"
            )
        if (
            run.status is RunStatus.BLOCKED
            and run.events
            and run.events[-1].kind == "target.identity_changed"
        ):
            raise ValueError(
                "target identity change must be resolved outside the model loop"
            )
        if run.pending_action is not None:
            raise ValueError(
                "pending action must settle or be aborted before steering"
            )
        if len(run.operator_guidance) >= 20:
            raise ValueError("operator steering history limit reached")
        run.operator_guidance.append(instruction)
        run.plan = None
        run.status = RunStatus.PAUSED
        run.error = None
        run.record(
            "run.steered",
            instruction=instruction,
            guidance_count=len(run.operator_guidance),
            source="operator",
        )
        await self.store.save(run)
        return run

    async def resolve_approval(
        self, run_id: str, approval_id: str, decision: dict[str, Any]
    ) -> RunSnapshot:
        run = await self.store.get_control(run_id)
        pending = run.pending_approval or {}
        if run.status is not RunStatus.NEEDS_APPROVAL:
            raise ValueError("run is not waiting for approval")
        if approval_id != pending.get("approval_id"):
            raise ValueError("approval_id does not match the pending approval")
        if not run.session_id:
            raise ValueError("run has no computer session")
        decision_type = decision.get("type")
        if decision_type not in {"approve", "reject", "take_over"}:
            raise ValueError("decision.type must be approve, reject, or take_over")
        run.record("approval.resolving", approval_id=approval_id, decision=decision_type)
        await self.store.save(run)
        observation = await self.computer.resolve_approval(
            session_id=run.session_id,
            approval_id=approval_id,
            decision=decision,
        )
        run.pending_approval = None
        if decision_type != "approve":
            run.pending_action = None
            run.observation = observation
            if decision_type == "reject":
                try:
                    run.observation = await self.computer.abort(
                        session_id=run.session_id,
                        reason="approval rejected by operator",
                    )
                    run.record(
                        "computer.aborted_after_rejection",
                        session_id=run.session_id,
                    )
                except Exception as exc:
                    run.error = (
                        "approval was rejected, but computer-session abort "
                        f"could not be confirmed: {exc}"
                    )
                    run.record(
                        "computer.abort_after_rejection_failed",
                        session_id=run.session_id,
                        error=str(exc),
                    )
            run.status = (
                RunStatus.REJECTED
                if decision_type == "reject"
                else RunStatus.ABORTED
            )
            run.record("approval.not_approved", decision=decision_type)
            await self.store.save(run)
            return run
        await self._accept_action_observation(run, observation)
        if run.status in TERMINAL_RUN_STATUSES or run.status is RunStatus.NEEDS_APPROVAL:
            return run
        return await self._advance(run)

    async def abort(self, run_id: str, reason: str = "aborted by caller") -> RunSnapshot:
        run = await self.store.get_control(run_id)
        if run.status in TERMINAL_RUN_STATUSES:
            return run
        if run.session_id:
            with_abort = await self.computer.abort(
                session_id=run.session_id, reason=reason
            )
            run.observation = with_abort
        run.pending_action = None
        run.pending_approval = None
        run.status = RunStatus.ABORTED
        run.record("run.aborted", reason=reason)
        await self.store.save(run)
        return run

    async def _advance(
        self,
        run: RunSnapshot,
        *,
        retry_provider_cooldown: bool = False,
    ) -> RunSnapshot:
        actions_this_call = 0
        while actions_this_call < self.config.max_actions_per_advance:
            if run.next_action_index >= self.config.max_total_actions:
                run.status = RunStatus.BLOCKED
                run.error = "maximum total action budget reached"
                run.record("run.budget_exhausted")
                await self.store.save(run)
                return run

            if run.pending_action is not None:
                outcome = await self._execute_pending(run)
                if outcome != "continue":
                    return run
                actions_this_call += 1
                if run.status in TERMINAL_RUN_STATUSES:
                    return run
                continue

            if run.plan is None:
                if not await self._plan(
                    run,
                    bypass_cooldown=retry_provider_cooldown,
                ):
                    return run
                retry_provider_cooldown = False

            previous_controller = run.last_controller
            controller = await self._control(
                run,
                bypass_cooldown=retry_provider_cooldown,
            )
            retry_provider_cooldown = False
            if controller is None:
                return run
            if self._repeats_ungrounded_navigation(run, controller):
                rejected = self._last_ungrounded_navigation(run) or {}
                rejected_actions = rejected.get("rejected_actions") or []
                run.record(
                    "controller.ungrounded_repeat_rejected",
                    actions=self._visible_actions(rejected_actions),
                    reason=(
                        "the same or near-identical coordinate-only navigation "
                        "was already rejected before HID"
                    ),
                )
                await self.store.save(run)
                controller = await self._control(
                    run,
                    controller_feedback={
                        "reason": (
                            "This same or near-identical coordinate-only "
                            "navigation was already rejected before HID "
                            "because its target could not be independently "
                            "grounded."
                        ),
                        "rejected_actions": self._visible_actions(
                            rejected_actions
                        ),
                        "instruction": (
                            "Do not repeat or slightly perturb these "
                            "coordinates. Choose a safe keyboard navigation "
                            "action, a visibly grounded text target, or replan."
                        ),
                    },
                )
                if controller is None:
                    return run
                if self._repeats_ungrounded_navigation(run, controller):
                    run.plan = None
                    run.status = RunStatus.PAUSED
                    run.error = (
                        "controller repeated an ungrounded coordinate action "
                        "after explicit correction feedback"
                    )
                    run.record(
                        "controller.ungrounded_correction_failed",
                        actions=self._visible_actions(rejected_actions),
                    )
                    await self.store.save(run)
                    return run
            if self._unsafe_non_idempotent_retry(
                previous_controller,
                controller,
                verification=run.last_verification,
            ):
                run.plan = None
                run.status = RunStatus.BLOCKED
                run.error = (
                    "unsafe retry of a state-changing toggle after ambiguous "
                    "verification"
                )
                run.record(
                    "controller.non_idempotent_retry_stopped",
                    previous_intent=(
                        previous_controller.intent
                        if previous_controller is not None
                        else None
                    ),
                    proposed_intent=controller.intent,
                    actions=self._visible_actions(
                        [
                            item.model_dump(mode="json", exclude_none=True)
                            for item in controller.actions
                        ]
                    ),
                )
                await self.store.save(run)
                return run
            run.last_controller = controller
            if controller.outcome == "blocked":
                run.status = RunStatus.BLOCKED
                run.error = controller.reason or controller.intent
                run.record("controller.blocked", reason=run.error)
                await self.store.save(run)
                return run
            if controller.outcome == "replan":
                run.plan = None
                run.status = RunStatus.PAUSED
                run.record("controller.requested_replan", reason=controller.reason)
                await self.store.save(run)
                return run
            if controller.outcome == "done":
                await self._verify(run, action=None, before=run.observation)
                if run.status is RunStatus.RUNNING:
                    run.status = RunStatus.PAUSED
                    run.record("run.paused", reason="verifier requires more work")
                    await self.store.save(run)
                return run

            actions = [
                action.model_dump(mode="json", exclude_none=True)
                for action in controller.actions
            ]
            previous_actions = (
                [
                    action.model_dump(mode="json", exclude_none=True)
                    for action in previous_controller.actions
                ]
                if previous_controller is not None
                and previous_controller.outcome == "act"
                else None
            )
            if previous_actions == actions:
                # Exact repeated HID is almost always an agent stall and is
                # especially dangerous for text. Refuse it before checkpoint
                # creation so a retry cannot duplicate accepted input.
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = "controller repeated the previous action unchanged"
                run.record(
                    "controller.repeated_actions",
                    actions=self._visible_actions(actions),
                )
                await self.store.save(run)
                return run
            if self._repeated_unsuccessful_text_input(run, actions):
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = (
                    "controller repeated text input after unsuccessful verification"
                )
                run.record(
                    "controller.repeated_unsuccessful_text",
                    action_types=[str(action.get("type") or "") for action in actions],
                )
                await self.store.save(run)
                return run
            try:
                validate_actions(actions)
            except BurstError as exc:
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = f"controller proposed invalid actions: {exc}"
                run.record("controller.invalid_actions", error=str(exc))
                await self.store.save(run)
                return run
            if len(actions) > self.config.max_actions_per_burst:
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = (
                    f"controller proposed {len(actions)} actions; "
                    f"limit is {self.config.max_actions_per_burst}"
                )
                run.record("controller.action_limit", count=len(actions))
                await self.store.save(run)
                return run
            if actions and all(
                action.get("type") == "move" for action in actions
            ):
                run.plan = None
                run.status = RunStatus.PAUSED
                run.error = "controller proposed a pointer-only no-op"
                run.record(
                    "controller.pointer_noop_rejected",
                    actions=self._visible_actions(actions),
                )
                await self.store.save(run)
                return run
            run.pending_action = self._pending_action(
                run, controller, actions
            )
            run.record(
                "action.checkpointed",
                index=run.pending_action.index,
                idempotency_key=run.pending_action.idempotency_key,
                intent=controller.intent,
                actions=self._visible_actions(actions),
                expected_evidence=controller.expected_evidence,
            )
            # Critical ordering: the durable pending action exists before HID.
            await self.store.save(run)

        run.status = RunStatus.PAUSED
        run.record("run.paused", reason="per-call action budget reached")
        await self.store.save(run)
        return run

    async def _plan(
        self,
        run: RunSnapshot,
        *,
        bypass_cooldown: bool = False,
    ) -> bool:
        request = self._model_request(run, "reasoner", PlanDecision, _REASONER_SYSTEM)
        run.record(
            "model.started",
            role="reasoner",
            candidates=self.models.route_names(
                "reasoner",
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "reasoner"),
            ),
        )
        await self.store.save(run)
        try:
            plan, response = await self.models.complete(
                request,
                PlanDecision,
                on_event=self._model_event_sink(run, "reasoner"),
                bypass_cooldown=bypass_cooldown,
                budget=self._model_budget(run),
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "reasoner"),
            )
        except ModelBudgetExceeded as exc:
            await self._model_budget_exhausted(run, "reasoner", exc)
            return False
        except ModelPoolError as exc:
            await self._model_failed(run, "reasoner", exc)
            return False
        run.plan = plan
        run.record(
            "model.completed",
            role="reasoner",
            provider=response.provider,
            model=response.model,
            latency_ms=response.latency_ms,
            usage=response.usage,
            plan=plan.model_dump(mode="json"),
        )
        await self.store.save(run)
        return True

    async def _control(
        self,
        run: RunSnapshot,
        *,
        bypass_cooldown: bool = False,
        controller_feedback: dict[str, Any] | None = None,
    ) -> ControllerDecision | None:
        request = self._model_request(
            run,
            "controller",
            ControllerDecision,
            _CONTROLLER_SYSTEM,
            extra=(
                {"controller_feedback": controller_feedback}
                if controller_feedback is not None
                else None
            ),
        )
        run.record(
            "model.started",
            role="controller",
            candidates=self.models.route_names(
                "controller",
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "controller"),
            ),
        )
        await self.store.save(run)
        try:
            decision, response = await self.models.complete(
                request,
                ControllerDecision,
                on_event=self._model_event_sink(run, "controller"),
                bypass_cooldown=bypass_cooldown,
                budget=self._model_budget(run),
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "controller"),
            )
        except ModelBudgetExceeded as exc:
            await self._model_budget_exhausted(run, "controller", exc)
            return None
        except ModelPoolError as exc:
            await self._model_failed(run, "controller", exc)
            return None
        run.record(
            "model.completed",
            role="controller",
            provider=response.provider,
            model=response.model,
            outcome=decision.outcome,
            latency_ms=response.latency_ms,
            usage=response.usage,
            intent=decision.intent,
        )
        await self.store.save(run)
        return decision

    async def _verify(
        self,
        run: RunSnapshot,
        *,
        action: PendingAction | None,
        before: ComputerObservation | None,
    ) -> None:
        after = run.observation
        after_image = Path(after.image_path) if after and after.image_path else None
        if after_image is None:
            try:
                refreshed = await self.computer.refresh(
                    session_id=run.session_id
                    or (after.session_id if after is not None else "")
                )
            except Exception as exc:
                run.status = RunStatus.PAUSED
                run.error = (
                    "computer action completed, but its verification image "
                    f"could not be refreshed: {exc}"
                )
                run.record(
                    "verification.evidence_unavailable",
                    error=str(exc),
                )
                await self.store.save(run)
                return
            previous_fingerprint = str(
                (after.machine if after is not None else {}).get(
                    "fingerprint"
                )
                or ""
            )
            refreshed_fingerprint = str(
                refreshed.machine.get("fingerprint") or ""
            )
            if (
                previous_fingerprint
                and refreshed_fingerprint
                and previous_fingerprint != refreshed_fingerprint
            ):
                run.observation = refreshed
                run.plan = None
                run.status = RunStatus.BLOCKED
                run.error = (
                    "target identity changed while refreshing verification "
                    "evidence"
                )
                run.record(
                    "target.identity_changed",
                    previous_fingerprint=previous_fingerprint,
                    current_fingerprint=refreshed_fingerprint,
                    source="harness_verification_refresh",
                )
                await self.store.save(run)
                return
            refreshed_image = (
                Path(refreshed.image_path)
                if refreshed.image_path
                else None
            )
            if refreshed_image is None:
                run.observation = refreshed
                run.status = RunStatus.PAUSED
                run.error = (
                    "computer action completed, but no readable verification "
                    "image was returned"
                )
                run.record("verification.evidence_unavailable")
                await self.store.save(run)
                return
            run.observation = refreshed
            run.record(
                "verification.evidence_refreshed",
                frame_id=refreshed.frame_id,
                world_version=refreshed.world_version,
            )
            await self.store.save(run)
        comparison_image = self._verification_composite(
            before=before,
            after=run.observation,
            run_id=run.run_id,
            action_index=run.next_action_index,
        )
        if comparison_image:
            run.latest_verification_image_path = comparison_image
            run.latest_verification_image_revision += 1
            evidence = VerificationImageArtifact(
                revision=run.latest_verification_image_revision,
                action_index=run.next_action_index,
                before_frame_id=before.frame_id if before else None,
                after_frame_id=(
                    run.observation.frame_id
                    if run.observation is not None
                    else None
                ),
                path=comparison_image,
            )
            run.verification_images = [
                *run.verification_images[-63:],
                evidence,
            ]
            run.record(
                "verification.evidence_captured",
                revision=evidence.revision,
                action_index=evidence.action_index,
                before_frame_id=evidence.before_frame_id,
                after_frame_id=evidence.after_frame_id,
            )
            await self.store.save(run)
        request = self._model_request(
            run,
            "verifier",
            VerificationDecision,
            _VERIFIER_SYSTEM,
            image_path=comparison_image,
            extra={
                "action": action.model_dump(mode="json") if action else None,
                "before": before.model_dump(mode="json") if before else None,
                "verification_image": (
                    {
                        "layout": "left panel is BEFORE; right panel is AFTER",
                        "path": comparison_image,
                    }
                    if comparison_image
                    else {
                        "layout": "AFTER frame only; no readable BEFORE frame",
                        "path": (
                            run.observation.image_path
                            if run.observation is not None
                            else None
                        ),
                    }
                ),
            },
        )
        run.record(
            "model.started",
            role="verifier",
            candidates=self.models.route_names(
                "verifier",
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "verifier"),
            ),
        )
        await self.store.save(run)
        try:
            verdict, response = await self.models.complete(
                request,
                VerificationDecision,
                on_event=self._model_event_sink(run, "verifier"),
                budget=self._model_budget(run),
                preferred_provider=run.model_provider,
                provider_route=self._provider_route(run, "verifier"),
            )
        except ModelBudgetExceeded as exc:
            await self._model_budget_exhausted(run, "verifier", exc)
            return
        except ModelPoolError as exc:
            await self._model_failed(run, "verifier", exc)
            return
        reported_verdict = verdict.verdict
        completion_rejection = self._completion_rejection_reason(run, verdict)
        if completion_rejection is not None:
            verdict = verdict.model_copy(update={"verdict": "verified"})
        run.last_verification = verdict
        run.record(
            "model.completed",
            role="verifier",
            provider=response.provider,
            model=response.model,
            verdict=verdict.verdict,
            reported_verdict=reported_verdict,
            latency_ms=response.latency_ms,
            usage=response.usage,
            summary=verdict.summary,
            evidence=verdict.evidence,
        )
        if completion_rejection is not None:
            run.record(
                "verification.complete_rejected",
                reason=completion_rejection,
                summary=verdict.summary,
            )
        if verdict.verdict == "complete":
            run.status = RunStatus.COMPLETED
            run.error = None
            run.record("run.completed", summary=verdict.summary)
        elif verdict.verdict == "verified":
            run.status = RunStatus.RUNNING
            run.error = None
        elif verdict.verdict == "uncertain":
            run.plan = None
            run.status = RunStatus.PAUSED
            run.error = verdict.summary
            run.record("verification.uncertain", summary=verdict.summary)
        else:
            run.plan = None
            run.status = RunStatus.PAUSED
            run.error = verdict.summary
            run.record("verification.failed", summary=verdict.summary)
        await self.store.save(run)

    async def _execute_pending(self, run: RunSnapshot) -> str:
        action = run.pending_action
        if action is None or not run.session_id:
            raise RuntimeError("pending action requires a computer session")
        before = run.observation
        action.attempts += 1
        tool = "pikvm_run_burst"
        call_id = (
            f"{action.idempotency_key}:attempt:{action.attempts}"
        )
        started = time.monotonic()
        run.record(
            "action.attempted",
            index=action.index,
            attempt=action.attempts,
            idempotency_key=action.idempotency_key,
            tool=tool,
            call_id=call_id,
            arguments={
                "session_id": run.session_id,
                "actions": self._visible_actions(action.actions),
                "based_on_world_version": action.based_on_world_version,
                "based_on_control_epoch": action.based_on_control_epoch,
                "idempotency_key": action.idempotency_key,
            },
        )
        await self.store.save(run)
        try:
            observation = await self.computer.burst(
                session_id=run.session_id,
                actions=action.actions,
                based_on_world_version=action.based_on_world_version,
                based_on_control_epoch=action.based_on_control_epoch,
                idempotency_key=action.idempotency_key,
            )
        except Exception as exc:
            # Ambiguous transport result: retain the exact pending action/key.
            latency_ms = round((time.monotonic() - started) * 1000)
            run.status = RunStatus.PAUSED
            run.error = f"computer transport failed; safe to retry same action: {exc}"
            run.record(
                "action.transport_uncertain",
                index=action.index,
                idempotency_key=action.idempotency_key,
                tool=tool,
                call_id=call_id,
                latency_ms=latency_ms,
                status="transport_uncertain",
                error=str(exc),
            )
            await self.store.save(run)
            return "stop"
        latency_ms = round((time.monotonic() - started) * 1000)
        accepted = await self._accept_action_observation(
            run,
            observation,
            before=before,
            tool=tool,
            call_id=call_id,
            latency_ms=latency_ms,
        )
        return "continue" if accepted else "stop"

    async def _accept_action_observation(
        self,
        run: RunSnapshot,
        observation: ComputerObservation,
        *,
        before: ComputerObservation | None = None,
        tool: str | None = None,
        call_id: str | None = None,
        latency_ms: int | None = None,
    ) -> bool:
        action = run.pending_action
        tool_outcome = {
            key: value
            for key, value in {
                "tool": tool,
                "call_id": call_id,
                "latency_ms": latency_ms,
            }.items()
            if value is not None
        }
        input_receipts = self._public_input_receipts(
            observation.raw,
            action.actions if action is not None else [],
        )
        receipt_outcome = (
            {"input_receipts": input_receipts}
            if input_receipts
            else {}
        )
        continuity_before = before or run.observation
        previous_machine = (
            continuity_before.machine
            if continuity_before is not None
            else {}
        )
        previous_fingerprint = str(
            previous_machine.get("fingerprint") or ""
        )
        current_fingerprint = str(
            observation.machine.get("fingerprint") or ""
        )
        run.observation = observation
        if (
            previous_fingerprint
            and current_fingerprint
            and previous_fingerprint != current_fingerprint
        ):
            run.pending_action = None
            run.pending_approval = None
            run.plan = None
            run.status = RunStatus.BLOCKED
            run.error = "target identity changed during computer action"
            run.record(
                "target.identity_changed",
                previous_fingerprint=previous_fingerprint,
                current_fingerprint=current_fingerprint,
                previous_alias=previous_machine.get("alias"),
                current_alias=observation.machine.get("alias"),
                source="harness",
                status=observation.status,
                **receipt_outcome,
                **tool_outcome,
            )
            await self.store.save(run)
            return False
        if observation.status == "needs_approval":
            approval_request = observation.approval_request or {}
            if self._is_ungrounded_navigation(approval_request):
                approval_id = str(
                    approval_request.get("approval_id") or ""
                )
                try:
                    dismissed = await self.computer.resolve_approval(
                        session_id=run.session_id or observation.session_id,
                        approval_id=approval_id,
                        decision={
                            "type": "reject",
                            "reason": (
                                "managed harness rejected an ungrounded "
                                "navigation proposal"
                            ),
                        },
                    )
                except Exception:
                    # If the exact daemon hold cannot be cleared, keep the
                    # visible approval boundary rather than guessing that no
                    # input can occur.
                    pass
                else:
                    prior_recoveries = sum(
                        event.kind == "action.ungrounded_refreshed"
                        for event in run.events
                    )
                    if (
                        prior_recoveries
                        >= self.config.max_ungrounded_navigation_replans
                    ):
                        run.pending_action = None
                        run.pending_approval = None
                        run.observation = dismissed
                        run.plan = None
                        run.status = RunStatus.BLOCKED
                        run.error = (
                            "click targets could not be independently grounded "
                            "after the bounded navigation replan budget"
                        )
                        run.record(
                            "action.ungrounded_budget_exhausted",
                            approval_id=approval_id,
                            risk=approval_request.get("risk"),
                            reason=approval_request.get("reason"),
                            dismissal_status=dismissed.status,
                            recovery_count=prior_recoveries,
                            recovery_limit=(
                                self.config.max_ungrounded_navigation_replans
                            ),
                            error=run.error,
                            **tool_outcome,
                        )
                        await self.store.save(run)
                        return False
                    try:
                        reopened = await self.computer.open(run.task)
                    except Exception as exc:
                        run.pending_action = None
                        run.pending_approval = None
                        run.observation = dismissed
                        run.status = RunStatus.PAUSED
                        run.error = (
                            "ungrounded navigation was rejected, but a fresh "
                            f"managed session could not be opened: {exc}"
                        )
                        run.record(
                            "action.ungrounded_refresh_failed",
                            approval_id=approval_id,
                            risk=approval_request.get("risk"),
                            reason=approval_request.get("reason"),
                            dismissal_status=dismissed.status,
                            **tool_outcome,
                        )
                        await self.store.save(run)
                        return False
                    reopened_fingerprint = str(
                        reopened.machine.get("fingerprint") or ""
                    )
                    if (
                        previous_fingerprint
                        and reopened_fingerprint
                        and previous_fingerprint != reopened_fingerprint
                    ):
                        run.pending_action = None
                        run.pending_approval = None
                        run.plan = None
                        run.observation = reopened
                        run.status = RunStatus.BLOCKED
                        run.error = (
                            "target identity changed while rejecting "
                            "ungrounded navigation"
                        )
                        run.record(
                            "target.identity_changed",
                            previous_fingerprint=previous_fingerprint,
                            current_fingerprint=reopened_fingerprint,
                            previous_alias=previous_machine.get("alias"),
                            current_alias=reopened.machine.get("alias"),
                            source="harness_ungrounded_refresh",
                            **tool_outcome,
                        )
                        await self.store.save(run)
                        return False
                    previous_session_id = run.session_id
                    run.session_id = reopened.session_id
                    run.observation = reopened
                    run.pending_action = None
                    run.pending_approval = None
                    run.last_controller = None
                    run.status = RunStatus.PAUSED
                    run.error = None
                    run.record(
                        "action.ungrounded_refreshed",
                        approval_id=approval_id,
                        risk=approval_request.get("risk"),
                        reason=approval_request.get("reason"),
                        refused_frame_id=observation.frame_id,
                        previous_session_id=previous_session_id,
                        fresh_session_id=reopened.session_id,
                        fresh_frame_id=reopened.frame_id,
                        fresh_world_version=reopened.world_version,
                        plan_preserved=run.plan is not None,
                        recovery_count=prior_recoveries + 1,
                        recovery_limit=(
                            self.config.max_ungrounded_navigation_replans
                        ),
                        **tool_outcome,
                    )
                    await self.store.save(run)
                    return False
            run.pending_approval = approval_request
            run.status = RunStatus.NEEDS_APPROVAL
            run.record(
                "approval.required",
                approval_id=run.pending_approval.get("approval_id"),
                risk=run.pending_approval.get("risk"),
                request=run.pending_approval,
                status=observation.status,
                **tool_outcome,
            )
            await self.store.save(run)
            return False
        if observation.status in {"stale_world", "control_changed"}:
            refused_status = observation.status
            # A stale frame proves only that this specific HID proposal is no
            # longer authorized. The high-level task plan remains useful; the
            # controller will receive the fresh frame and must author a new
            # action against it. Human/concurrent control changes are different:
            # discard the plan and re-reason after authority is reacquired.
            if refused_status == "control_changed":
                run.plan = None
            run.status = RunStatus.PAUSED
            try:
                refreshed = await self.computer.refresh(
                    session_id=run.session_id or observation.session_id
                )
            except Exception as exc:
                run.pending_action = None
                run.error = (
                    f"action refused: {refused_status}; "
                    f"fresh observation failed: {exc}"
                )
                run.record(
                    "action.refused_stale",
                    status=refused_status,
                    refresh_error=str(exc),
                    **tool_outcome,
                )
                await self.store.save(run)
                return False
            refreshed_fingerprint = str(
                refreshed.machine.get("fingerprint") or ""
            )
            if (
                previous_fingerprint
                and refreshed_fingerprint
                and previous_fingerprint != refreshed_fingerprint
            ):
                run.pending_approval = None
                run.status = RunStatus.BLOCKED
                run.error = "target identity changed during stale-world refresh"
                run.record(
                    "target.identity_changed",
                    previous_fingerprint=previous_fingerprint,
                    current_fingerprint=refreshed_fingerprint,
                    previous_alias=previous_machine.get("alias"),
                    current_alias=refreshed.machine.get("alias"),
                    source="harness_stale_refresh",
                    **tool_outcome,
                )
                await self.store.save(run)
                return False
            run.observation = refreshed
            run.pending_action = None
            run.last_controller = None
            run.error = f"action refused: {refused_status}; world refreshed"
            run.record(
                "action.stale_world_refreshed",
                status=refused_status,
                refused_world_version=observation.world_version,
                fresh_world_version=refreshed.world_version,
                plan_preserved=run.plan is not None,
                fresh_controller_decision_required=True,
                **tool_outcome,
            )
            await self.store.save(run)
            return False
        if observation.status == "unverified":
            run.pending_action = None
            if action is not None:
                run.next_action_index = max(
                    run.next_action_index,
                    action.index + 1,
                )
            run.error = observation.error
            run.plan = None
            run.status = RunStatus.PAUSED
            run.record(
                "action.completed_unverified",
                index=action.index if action else None,
                frame_id=observation.frame_id,
                world_version=observation.world_version,
                reason=observation.raw.get("reason"),
                status=observation.status,
                **receipt_outcome,
                **tool_outcome,
            )
            await self.store.save(run)
            return False
        if observation.status not in {"completed", "paused", "done"}:
            run.pending_action = None
            run.error = observation.error or f"computer returned {observation.status}"
            if self._recoverable_failure(observation):
                if action is not None:
                    run.next_action_index = max(
                        run.next_action_index,
                        action.index + 1,
                    )
                run.plan = None
                run.status = RunStatus.PAUSED
                run.record(
                    "action.recoverable_failure",
                    index=action.index if action else None,
                    status=observation.status,
                    reason=observation.raw.get("reason"),
                    error=run.error,
                    **receipt_outcome,
                    **tool_outcome,
                )
            else:
                run.status = RunStatus.FAILED
                run.record(
                    "action.failed",
                    index=action.index if action else None,
                    status=observation.status,
                    error=run.error,
                    **receipt_outcome,
                    **tool_outcome,
                )
            await self.store.save(run)
            return False
        run.pending_action = None
        if action is not None:
            run.next_action_index = max(run.next_action_index, action.index + 1)
        run.error = None
        run.status = RunStatus.RUNNING
        run.record(
            "action.completed",
            index=action.index if action else None,
            frame_id=observation.frame_id,
            world_version=observation.world_version,
            status=observation.status,
            **receipt_outcome,
            **tool_outcome,
        )
        await self.store.save(run)
        await self._verify(run, action=action, before=before)
        return run.status is RunStatus.RUNNING

    @staticmethod
    def _is_ungrounded_navigation(
        approval_request: dict[str, Any],
    ) -> bool:
        """Return whether managed mode should discard and replan a click.

        The proposal is never executed. Direct mode still fails closed in the
        daemon because an external controller may not own a recovery loop.
        """

        return (
            approval_request.get("kind") == "direct_burst"
            and approval_request.get("risk") == "unknown"
            and approval_request.get("reason")
            == "coordinate click target could not be independently read"
        )

    def _pending_action(
        self,
        run: RunSnapshot,
        controller: ControllerDecision,
        actions: list[dict[str, Any]],
    ) -> PendingAction:
        canonical = json.dumps(
            actions, sort_keys=True, separators=(",", ":")
        ).encode()
        digest = hashlib.sha256(canonical).hexdigest()[:16]
        observation = run.observation
        return PendingAction(
            index=run.next_action_index,
            intent=controller.intent,
            actions=actions,
            expected_evidence=controller.expected_evidence,
            based_on_world_version=(
                observation.world_version if observation is not None else None
            ),
            based_on_control_epoch=(
                observation.control_epoch if observation is not None else None
            ),
            idempotency_key=(
                f"{run.run_id}:action:{run.next_action_index}:{digest}"
            ),
        )

    @staticmethod
    def _provider_route(
        run: RunSnapshot,
        role: ModelRole,
    ) -> list[str] | None:
        if run.model_route is None:
            return None
        return run.model_route.for_role(role)

    def _model_request(
        self,
        run: RunSnapshot,
        role: ModelRole,
        output_type: type[Any],
        system: str,
        *,
        extra: dict[str, Any] | None = None,
        image_path: str | None = None,
    ) -> ModelRequest:
        context: dict[str, Any] = {
            "task": run.task,
            "operator_guidance": run.operator_guidance,
            "plan": run.plan.model_dump(mode="json") if run.plan else None,
            "action_index": run.next_action_index,
            "last_controller": (
                run.last_controller.model_dump(mode="json")
                if run.last_controller
                else None
            ),
            "last_verification": (
                run.last_verification.model_dump(mode="json")
                if run.last_verification
                else None
            ),
            "observation": (
                run.observation.model_dump(mode="json")
                if run.observation
                else None
            ),
            "trajectory_signals": self._trajectory_signals(run),
        }
        if extra:
            context.update(extra)
        prompt = (
            f"{system}\n\nReturn only JSON matching the supplied schema.\n\n"
            f"RUN CONTEXT:\n{json.dumps(context, ensure_ascii=False, sort_keys=True)}"
        )
        return ModelRequest(
            role=role,
            prompt=prompt,
            output_schema=output_type.model_json_schema(),
            image_path=(
                image_path
                if image_path is not None
                else run.observation.image_path if run.observation else None
            ),
            run_id=run.run_id,
            metadata={"action_index": run.next_action_index},
        )

    @staticmethod
    def _trajectory_signals(run: RunSnapshot) -> dict[str, Any]:
        """Aggregate prior outcomes without exporting historical text or screen data."""
        action_type_counts: dict[str, int] = {}
        verifier_verdict_counts: dict[str, int] = {}
        recoverable_failures = 0
        repeated_unsuccessful_text_stops = 0
        ungrounded_navigation_replans = 0
        for event in run.events:
            data = event.data
            if event.kind == "action.checkpointed":
                actions = data.get("actions")
                if isinstance(actions, list):
                    for action in actions:
                        if not isinstance(action, dict):
                            continue
                        kind = str(action.get("type") or "unknown")
                        action_type_counts[kind] = action_type_counts.get(kind, 0) + 1
            elif (
                event.kind == "model.completed"
                and data.get("role") == "verifier"
            ):
                verdict = str(data.get("verdict") or "unknown")
                if verdict in {"verified", "complete", "uncertain", "failed"}:
                    verifier_verdict_counts[verdict] = (
                        verifier_verdict_counts.get(verdict, 0) + 1
                    )
            elif event.kind == "action.recoverable_failure":
                recoverable_failures += 1
            elif event.kind == "controller.repeated_unsuccessful_text":
                repeated_unsuccessful_text_stops += 1
            elif event.kind == "action.ungrounded_refreshed":
                ungrounded_navigation_replans += 1
        return {
            "action_type_counts": action_type_counts,
            "verifier_verdict_counts": verifier_verdict_counts,
            "recoverable_failures": recoverable_failures,
            "repeated_unsuccessful_text_stops": repeated_unsuccessful_text_stops,
            "ungrounded_navigation_replans": ungrounded_navigation_replans,
            "last_ungrounded_navigation": (
                AgentHarness._last_ungrounded_navigation(run)
            ),
            "ungrounded_navigation_history": (
                AgentHarness._ungrounded_navigation_history(run)
            ),
        }

    @staticmethod
    def _ungrounded_navigation_history(
        run: RunSnapshot,
    ) -> list[dict[str, Any]]:
        last_checkpointed_actions: list[dict[str, Any]] | None = None
        history: list[dict[str, Any]] = []
        for event in run.events:
            if event.kind == "action.checkpointed":
                actions = event.data.get("actions")
                if isinstance(actions, list):
                    last_checkpointed_actions = [
                        dict(action)
                        for action in actions
                        if (
                            isinstance(action, dict)
                            and action.get("type")
                            in {
                                "click",
                                "double_click",
                                "move",
                                "scroll",
                                "wait",
                                "wait_for_stable_screen",
                                "wait_for_change",
                            }
                        )
                    ]
            elif event.kind == "action.ungrounded_refreshed":
                history.append(
                    {
                        "reason": (
                            event.data.get("reason")
                            or (
                                "coordinate click target could not be "
                                "independently read"
                            )
                        ),
                        "rejected_actions": list(
                            last_checkpointed_actions or []
                        ),
                        "refused_frame_id": event.data.get("refused_frame_id"),
                        "fresh_frame_id": event.data.get("fresh_frame_id"),
                    }
                )
        return history[-16:]

    @staticmethod
    def _last_ungrounded_navigation(
        run: RunSnapshot,
    ) -> dict[str, Any] | None:
        history = AgentHarness._ungrounded_navigation_history(run)
        return history[-1] if history else None

    @staticmethod
    def _repeats_ungrounded_navigation(
        run: RunSnapshot,
        controller: ControllerDecision,
    ) -> bool:
        if controller.outcome != "act":
            return False
        rejection_history = AgentHarness._ungrounded_navigation_history(run)
        if not rejection_history:
            return False
        proposed_actions = [
            action.model_dump(mode="json", exclude_none=True)
            for action in controller.actions
        ]
        navigation_action_types = {
            "click",
            "double_click",
            "move",
            "scroll",
            "wait",
            "wait_for_stable_screen",
            "wait_for_change",
        }
        if not proposed_actions or not all(
            str(action.get("type") or "") in navigation_action_types
            for action in proposed_actions
        ):
            return False

        def click_signature(
            actions: list[dict[str, Any]],
        ) -> list[tuple[str, int, int, str]]:
            return [
                (
                    str(action.get("type") or ""),
                    int(action.get("x") or 0),
                    int(action.get("y") or 0),
                    str(action.get("button") or "left"),
                )
                for action in actions
                if action.get("type") in {"click", "double_click"}
            ]

        proposed_clicks = click_signature(proposed_actions)
        return any(
            isinstance(rejected_actions, list)
            and bool(rejected_clicks := click_signature(rejected_actions))
            and len(rejected_clicks) == len(proposed_clicks)
            and all(
                rejected_type == proposed_type
                and rejected_button == proposed_button
                and abs(rejected_x - proposed_x) <= 4
                and abs(rejected_y - proposed_y) <= 4
                for (
                    rejected_type,
                    rejected_x,
                    rejected_y,
                    rejected_button,
                ), (
                    proposed_type,
                    proposed_x,
                    proposed_y,
                    proposed_button,
                ) in zip(rejected_clicks, proposed_clicks, strict=True)
            )
            for rejected_actions in (
                rejection.get("rejected_actions")
                for rejection in rejection_history
            )
        )

    @staticmethod
    def _repeated_unsuccessful_text_input(
        run: RunSnapshot,
        proposed_actions: list[dict[str, Any]],
    ) -> bool:
        """Stop a locally detected failed query loop without sending its text anywhere."""

        def signatures(actions: Any) -> set[tuple[str, str, bool]]:
            if not isinstance(actions, list):
                return set()
            return {
                (
                    str(action.get("text") or ""),
                    str(action.get("context") or ""),
                    bool(action.get("code")),
                )
                for action in actions
                if (
                    isinstance(action, dict)
                    and action.get("type") == "type_text"
                    and not action.get("secret")
                )
            }

        proposed = signatures(proposed_actions)
        if not proposed:
            return False
        active_match = False
        for event in run.events:
            if event.kind == "action.checkpointed":
                active_match = bool(
                    proposed.intersection(signatures(event.data.get("actions")))
                )
            elif (
                active_match
                and event.kind == "model.completed"
                and event.data.get("role") == "verifier"
                and event.data.get("verdict") in {"failed", "uncertain"}
            ):
                return True
        return False

    @staticmethod
    def _verification_composite(
        *,
        before: ComputerObservation | None,
        after: ComputerObservation | None,
        run_id: str,
        action_index: int,
    ) -> str | None:
        """Persist a labelled, full-resolution visual delta for the verifier."""

        before_path = Path(before.image_path) if before and before.image_path else None
        after_path = Path(after.image_path) if after and after.image_path else None
        if (
            before_path is None
            or after_path is None
            or not before_path.is_file()
            or not after_path.is_file()
        ):
            return None
        output = after_path.with_name(
            f"{after_path.stem}.before-after-{run_id}-{action_index}.png"
        )
        try:
            with (
                Image.open(before_path) as before_source,
                Image.open(after_path) as after_source,
            ):
                before_image = ImageOps.exif_transpose(before_source).convert("RGB")
                after_image = ImageOps.exif_transpose(after_source).convert("RGB")
                panel_width = max(before_image.width, after_image.width)
                panel_height = max(before_image.height, after_image.height)
                label_height = 32
                composite = Image.new(
                    "RGB",
                    (panel_width * 2, panel_height + label_height),
                    "#202124",
                )
                composite.paste(
                    before_image,
                    (
                        (panel_width - before_image.width) // 2,
                        label_height + (panel_height - before_image.height) // 2,
                    ),
                )
                composite.paste(
                    after_image,
                    (
                        panel_width
                        + (panel_width - after_image.width) // 2,
                        label_height + (panel_height - after_image.height) // 2,
                    ),
                )
                draw = ImageDraw.Draw(composite)
                draw.text((8, 9), "BEFORE", fill="white")
                draw.text((panel_width + 8, 9), "AFTER", fill="white")
                composite.save(output, format="PNG", optimize=True)
        except (OSError, UnidentifiedImageError):
            return None
        return str(output)

    @classmethod
    def _unsafe_non_idempotent_retry(
        cls,
        previous: ControllerDecision | None,
        proposed: ControllerDecision,
        *,
        verification: VerificationDecision | None,
    ) -> bool:
        """Stop a nearby second click that could undo a successful toggle."""

        if (
            previous is None
            or previous.outcome != "act"
            or proposed.outcome != "act"
            or verification is None
            or verification.verdict not in {"failed", "uncertain"}
            or not cls._toggle_intent(previous.intent)
            or not cls._toggle_intent(proposed.intent)
            or len(previous.actions) != 1
            or len(proposed.actions) != 1
        ):
            return False
        prior = previous.actions[0]
        retry = proposed.actions[0]
        if prior.type not in {"click", "double_click"} or retry.type not in {
            "click",
            "double_click",
        }:
            return False
        return (prior.x - retry.x) ** 2 + (prior.y - retry.y) ** 2 <= 50**2

    @staticmethod
    def _toggle_intent(intent: str) -> bool:
        normalized = " ".join(intent.casefold().split())
        return any(
            marker in normalized
            for marker in (
                "toggle",
                "enabl",
                "disabl",
                "turn on",
                "turn off",
                "switch on",
                "switch off",
            )
        )

    @staticmethod
    def _completion_rejection_reason(
        run: RunSnapshot,
        verdict: VerificationDecision,
    ) -> str | None:
        if verdict.verdict != "complete":
            return None
        expected = len(run.plan.success_criteria) if run.plan is not None else 0
        assessments = {
            item.criterion_index: item for item in verdict.criteria
        }
        if expected and set(assessments) != set(range(expected)):
            return (
                "complete verdict did not assess every success criterion "
                f"(expected indexes 0..{expected - 1})"
            )
        for index in range(expected):
            if not assessments[index].satisfied:
                return f"criterion {index} was explicitly reported unsatisfied"
            if not assessments[index].evidence.strip():
                return f"criterion {index} has no specific visible evidence"
        claim = " ".join([verdict.summary, *verdict.evidence]).casefold()
        contradiction = re.search(
            r"\b(?:not yet|has not|have not|not been|not complete|incomplete|"
            r"still needs?|remains? to be|overall task[^.]{0,80}\bnot\b)",
            claim,
        )
        if contradiction is not None:
            return (
                "complete verdict contradicts its own evidence near "
                f"{contradiction.group(0)!r}"
            )
        return None

    async def _model_failed(
        self,
        run: RunSnapshot,
        role: str,
        exc: Exception,
    ) -> None:
        # No HID action is accepted at a model boundary. Treat provider
        # exhaustion as a resumable operational outage rather than turning a
        # transient OAuth/API/CLI failure into a terminal computer run.
        run.status = RunStatus.PAUSED
        run.error = str(exc)
        run.record("model.failed", role=role, error=str(exc))
        await self.store.save(run)

    async def _model_budget_exhausted(
        self,
        run: RunSnapshot,
        role: str,
        exc: ModelBudgetExceeded,
    ) -> None:
        run.status = RunStatus.PAUSED
        run.error = str(exc)
        run.record(
            "model.budget_exhausted",
            role=role,
            reason=str(exc),
            provider_attempts=run.model_budget.provider_attempts,
            provider_attempt_limit=self.budget_policy.max_provider_attempts,
            committed_cost_microusd=run.model_budget.committed_cost_microusd,
            outstanding_cost_microusd=(
                run.model_budget.outstanding_cost_microusd
            ),
            max_cost_microusd=self.budget_policy.max_cost_microusd,
        )
        await self.store.save(run)

    def _model_budget(self, run: RunSnapshot) -> DurableRunModelBudget:
        return DurableRunModelBudget(
            run=run,
            store=self.store,
            policy=self.budget_policy,
        )

    def _model_event_sink(self, run: RunSnapshot, role: str):
        async def record(kind: str, data: dict[str, object]) -> None:
            run.record(f"model.{kind}", role=role, **data)
            await self.store.save(run)

        return record

    @staticmethod
    def _visible_actions(actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
        return redact_secrets(actions)

    @staticmethod
    def _public_input_receipts(
        raw: dict[str, Any],
        actions: list[dict[str, Any]],
    ) -> list[dict[str, Any]]:
        """Copy bounded watched-typing evidence across the public event boundary."""

        candidates = raw.get("action_receipts")
        if not isinstance(candidates, list):
            return []
        output: list[dict[str, Any]] = []
        seen: set[int] = set()
        allowed_strings = {
            "status": {
                "verified_exact",
                "verified_safe_normalized",
                "verified_with_warnings",
                "unverified_ambiguous",
                "unverified_wrong_region",
                "unverified_truncated",
                "failed_symbol_mismatch",
                "failed_case_mismatch",
                "failed_keyboard_layout",
                "failed_focus_lost",
                "failed_stale_frame",
                "blocked_by_policy",
                "needs_human",
                "delivered_unverified",
            },
            "verdict": {"match", "contains", "mismatch", "unverified"},
            "focus_evidence": {
                "focus_lost",
                "read_back_verified",
                "read_back_unverified",
                "read_back_mismatch",
                "read_back_not_retained",
                "read_back_unavailable",
            },
        }
        integer_limits = {
            "typed_characters": 480,
            "intended_characters": 480,
            "correction_count": 20,
            "delivery_retries": 20,
            "edit_distance": 960,
        }
        for candidate in candidates[:20]:
            if not isinstance(candidate, dict):
                continue
            index = candidate.get("index")
            if (
                not isinstance(index, int)
                or isinstance(index, bool)
                or index < 0
                or index >= len(actions)
                or index in seen
            ):
                continue
            action = actions[index]
            if action.get("type") != "type_text":
                continue
            seen.add(index)
            secret = action.get("secret") is True
            redacted = secret or candidate.get("observed_text_redacted") is True
            receipt: dict[str, Any] = {
                "index": index,
                "type": "type_text",
            }
            for key, allowed in allowed_strings.items():
                value = candidate.get(key)
                if isinstance(value, str) and value in allowed:
                    receipt[key] = value
            for key, limit in integer_limits.items():
                value = candidate.get(key)
                if (
                    isinstance(value, int)
                    and not isinstance(value, bool)
                    and 0 <= value <= limit
                ):
                    receipt[key] = value
            for key in ("used_fast_path",):
                value = candidate.get(key)
                if isinstance(value, bool):
                    receipt[key] = value
            for key in (
                "intended_sha256",
                "acknowledged_prefix_sha256",
                "observed_sha256",
            ):
                value = candidate.get(key)
                if (
                    isinstance(value, str)
                    and re.fullmatch(r"[0-9a-f]{64}", value)
                ):
                    receipt[key] = value
            exact_sha256_match = candidate.get("exact_sha256_match")
            if isinstance(exact_sha256_match, bool):
                receipt["exact_sha256_match"] = exact_sha256_match
            receipt["observed_text_redacted"] = redacted
            if redacted:
                for key in (
                    "intended_sha256",
                    "acknowledged_prefix_sha256",
                    "observed_sha256",
                    "exact_sha256_match",
                ):
                    receipt.pop(key, None)
                receipt.update(
                    {
                        "status": "delivered_unverified",
                        "verdict": "unverified",
                        "focus_evidence": "read_back_not_retained",
                    }
                )
            else:
                observed_text = candidate.get("observed_text")
                if isinstance(observed_text, str) and len(observed_text) <= 960:
                    receipt["observed_text"] = observed_text
                summary = candidate.get("summary")
                if isinstance(summary, str) and 0 < len(summary) <= 320:
                    receipt["summary"] = summary
            output.append(receipt)
        return output

    @staticmethod
    def _recoverable_failure(
        observation: ComputerObservation | None,
    ) -> bool:
        if observation is None or observation.status != "failed":
            return False
        return str(observation.raw.get("reason") or "") in {
            "type_unverified",
            "focus_lost",
        }
