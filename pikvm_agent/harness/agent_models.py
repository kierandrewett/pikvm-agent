"""Public value objects for the provider-neutral computer-use harness.

The harness deliberately exposes a small task/checkpoint interface.  Model
providers and the raw PiKVM MCP transport are adapters behind that interface;
neither owns run state, retries, approvals, or success.
"""

from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Annotated, Any, Literal

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    field_validator,
    model_validator,
)

from pikvm_agent.core.spreadsheet_grid import (
    SpreadsheetGridError,
    validate_spreadsheet_grid,
)

ModelRole = Literal["reasoner", "controller", "verifier"]
RunMode = Literal["assistant", "computer"]
ModelActivityPhase = Literal[
    "queued",
    "provider_selected",
    "request_sent",
    "output_received",
    "validating",
    "schema_repair",
    "failover",
]
ProviderAlias = Annotated[
    str,
    Field(pattern=r"^[A-Za-z0-9_.:-]{1,128}$"),
]


def utc_now() -> datetime:
    return datetime.now(UTC)


class RunStatus(str, Enum):
    PLANNING = "planning"
    RUNNING = "running"
    PAUSED = "paused"
    NEEDS_APPROVAL = "needs_approval"
    COMPLETED = "completed"
    BLOCKED = "blocked"
    REJECTED = "rejected"
    ABORTED = "aborted"
    FAILED = "failed"


class ArtifactAcceptanceState(str, Enum):
    PENDING = "pending"
    CAPTURING = "capturing"
    PASSED = "passed"
    FAILED = "failed"


class MediaTransactionState(str, Enum):
    """Durable public state of an approval-gated virtual-media transaction."""

    PREPARED = "prepared"
    AWAITING_APPROVAL = "awaiting_approval"
    UPLOADING = "uploading"
    SELECTED = "selected"
    ATTACHED = "attached"
    VERIFIED = "verified"
    DETACHING = "detaching"
    ROLLING_BACK = "rolling_back"
    RELEASED = "released"
    REJECTED = "rejected"
    CLEANUP_REQUIRED = "cleanup_required"
    UNSUPPORTED = "unsupported"


TERMINAL_RUN_STATUSES = {
    RunStatus.COMPLETED,
    RunStatus.BLOCKED,
    RunStatus.REJECTED,
    RunStatus.ABORTED,
    RunStatus.FAILED,
}


class ArtifactAcceptance(BaseModel):
    """Host-owned artifact evidence attached after model-visible completion."""

    model_config = ConfigDict(extra="forbid")

    kind: Literal["office_artifact"]
    label: str = Field(min_length=1, max_length=200)
    state: ArtifactAcceptanceState
    artifact_format: Literal["docx", "xlsx"] | None = None
    checks_passed: int = Field(default=0, ge=0)
    checks_total: int = Field(default=0, ge=0)
    byte_count: int | None = Field(default=None, ge=0)
    sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    error_class: str | None = Field(default=None, max_length=100)
    updated_at: datetime = Field(default_factory=utc_now)

    @model_validator(mode="after")
    def evidence_matches_state(self) -> "ArtifactAcceptance":
        if self.checks_passed > self.checks_total:
            raise ValueError("passed checks cannot exceed declared checks")
        if self.state is ArtifactAcceptanceState.PASSED:
            if self.checks_total < 1 or self.checks_passed != self.checks_total:
                raise ValueError(
                    "passed acceptance requires all declared checks"
                )
            if (
                self.artifact_format is None
                or self.byte_count is None
                or self.sha256 is None
            ):
                raise ValueError(
                    "passed acceptance requires artifact format, bytes, and hash"
                )
            if self.error_class is not None:
                raise ValueError(
                    "passed acceptance cannot carry an error class"
                )
        return self


class MediaFileEvidence(BaseModel):
    """Non-secret guest-file receipt safe to project into the operator UI."""

    model_config = ConfigDict(extra="forbid")

    name: str = Field(min_length=1, max_length=64)
    size: int = Field(ge=0)
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")


class MediaTransaction(BaseModel):
    """Public receipt and state; media bytes and host paths never live here."""

    model_config = ConfigDict(extra="forbid")

    transaction_id: str = Field(min_length=1, max_length=100)
    state: MediaTransactionState
    approval_id: str = Field(min_length=1, max_length=100)
    purpose: str = Field(min_length=1, max_length=500)
    session_id: str = Field(min_length=1, max_length=200)
    machine_fingerprint: str = Field(min_length=1, max_length=500)
    control_epoch: int = Field(ge=0)
    adapter: str = Field(min_length=1, max_length=100)
    media_name: str = Field(min_length=1, max_length=100)
    image_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    image_bytes: int = Field(ge=1)
    manifest_sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    files: list[MediaFileEvidence] = Field(min_length=1, max_length=32)
    read_only: bool = True
    lease_expires_at: datetime
    attached_at: datetime | None = None
    released_at: datetime | None = None
    failure_reason: str | None = Field(default=None, max_length=500)
    cleanup_reason: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def require_read_only_media(self) -> "MediaTransaction":
        if not self.read_only:
            raise ValueError("virtual media transactions must be read-only")
        return self


class HarnessConfig(BaseModel):
    """Budgets owned by the harness, never by a model response."""

    max_actions_per_advance: int = Field(default=4, ge=1, le=32)
    max_actions_per_burst: int = Field(default=8, ge=1, le=32)
    parallel_post_action_control: bool = True
    max_total_actions: int = Field(default=100, ge=1)
    max_ungrounded_navigation_replans: int = Field(default=3, ge=1, le=16)
    max_provider_attempts_per_run: int = Field(default=500, ge=1, le=100_000)
    interactive_action_preview_ms: int = Field(
        default=300,
        ge=0,
        le=2_000,
    )


class RunModelBudgetState(BaseModel):
    """Durable model accounting owned by the harness, not provider prose."""

    provider_attempts: int = Field(default=0, ge=0)
    provider_attempt_limit: int | None = Field(default=None, ge=1)
    committed_cost_microusd: int = Field(default=0, ge=0)
    max_cost_microusd: int | None = Field(default=None, ge=1)
    pricing_version: str | None = Field(default=None, max_length=100)
    reservations_microusd: dict[str, int] = Field(default_factory=dict)
    provider_cost_microusd: dict[str, int] = Field(default_factory=dict)

    @computed_field
    @property
    def outstanding_cost_microusd(self) -> int:
        return sum(self.reservations_microusd.values())


class RunModelRoute(BaseModel):
    """Durable per-role candidates selected for one managed run."""

    model_config = ConfigDict(extra="forbid")

    reasoner: list[ProviderAlias] | None = Field(
        default=None,
        min_length=1,
        max_length=16,
    )
    controller: list[ProviderAlias] | None = Field(
        default=None,
        min_length=1,
        max_length=16,
    )
    verifier: list[ProviderAlias] | None = Field(
        default=None,
        min_length=1,
        max_length=16,
    )

    @field_validator("reasoner", "controller", "verifier")
    @classmethod
    def candidates_are_unique(
        cls,
        value: list[str] | None,
    ) -> list[str] | None:
        if value is not None and len(value) != len(set(value)):
            raise ValueError("provider route candidates must be unique")
        return value

    @model_validator(mode="after")
    def at_least_one_role_is_selected(self) -> "RunModelRoute":
        if not any((self.reasoner, self.controller, self.verifier)):
            raise ValueError("model route must select at least one role")
        return self

    def for_role(self, role: ModelRole) -> list[str] | None:
        value = getattr(self, role)
        return list(value) if value is not None else None


class ComputerObservation(BaseModel):
    session_id: str
    status: str
    machine: dict[str, Any] = Field(default_factory=dict)
    frame_id: int | None = None
    world_version: int | None = None
    control_epoch: int | None = None
    width: int | None = None
    height: int | None = None
    image_path: str | None = None
    image_sha256: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]{64}$",
    )
    screen_hash: str | None = Field(
        default=None,
        pattern=r"^[0-9a-f]+$",
    )
    approval_request: dict[str, Any] | None = None
    error: str | None = None
    raw: dict[str, Any] = Field(default_factory=dict)


class ModelRequest(BaseModel):
    role: ModelRole
    prompt: str
    output_schema: dict[str, Any]
    image_path: str | None = None
    run_id: str
    metadata: dict[str, Any] = Field(default_factory=dict)


class ModelResponse(BaseModel):
    provider: str
    model: str
    data: dict[str, Any]
    usage: dict[str, Any] = Field(default_factory=dict)
    latency_ms: int | None = None


class StrictModelDecision(BaseModel):
    """Provider-authored structure: unknown fields are never action input."""

    model_config = ConfigDict(extra="forbid")


class AssistantToolCall(StrictModelDecision):
    """One host-validated capability request from the conversational model."""

    name: str = Field(pattern=r"^[A-Za-z0-9_.:-]{1,200}$")
    arguments: dict[str, Any] = Field(default_factory=dict)


class AssistantDecision(StrictModelDecision):
    """Normal chat reply, visible tool request, or explicit computer hand-off."""

    outcome: Literal["reply", "tool", "computer"]
    message: str = Field(default="", max_length=40_000)
    tool_call: AssistantToolCall | None = None
    computer_task: str | None = Field(default=None, max_length=20_000)

    @model_validator(mode="after")
    def payload_matches_outcome(self) -> "AssistantDecision":
        if self.outcome == "reply":
            if not self.message.strip():
                raise ValueError("reply outcome requires a message")
            if self.tool_call is not None or self.computer_task is not None:
                raise ValueError("reply outcome cannot include a capability request")
        elif self.outcome == "tool":
            if self.tool_call is None:
                raise ValueError("tool outcome requires tool_call")
            if self.computer_task is not None:
                raise ValueError("tool outcome cannot include computer_task")
        else:
            if not (self.computer_task or "").strip():
                raise ValueError("computer outcome requires computer_task")
            if self.tool_call is not None:
                raise ValueError("computer outcome cannot include tool_call")
        return self


class ConversationMessage(BaseModel):
    """Durable turn boundary in a normal assistant conversation."""

    model_config = ConfigDict(extra="forbid")

    message_id: str = Field(min_length=1, max_length=200)
    role: Literal["user", "assistant"]
    content: str = Field(max_length=40_000)
    created_at: datetime = Field(default_factory=utc_now)
    event_cursor: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def user_turn_requires_text(self) -> ConversationMessage:
        if self.role == "user" and not self.content.strip():
            raise ValueError("user conversation message must not be empty")
        return self


class PlanDecision(StrictModelDecision):
    summary: str
    steps: list[str] = Field(min_length=1, max_length=30)
    success_criteria: list[str] = Field(min_length=1, max_length=20)
    constraints: list[str] = Field(default_factory=list, max_length=20)


class KeyAction(StrictModelDecision):
    type: Literal["key"]
    keys: list[str] = Field(min_length=1, max_length=8)


class TypeTextAction(StrictModelDecision):
    type: Literal["type_text"]
    text: str = Field(max_length=240)
    code: bool = False
    secret: bool = False
    context: Literal["", "editor", "field", "terminal"] = ""
    verification: Literal["auto", "exact"] | None = None

    @field_validator("text")
    @classmethod
    def text_contains_no_hid_control_characters(cls, value: str) -> str:
        if any(ord(character) < 32 or ord(character) == 127 for character in value):
            raise ValueError(
                "type_text cannot contain control characters; use explicit key actions"
            )
        return value


class SpreadsheetGridAction(StrictModelDecision):
    type: Literal["spreadsheet_grid"]
    rows: list[list[str]]

    @model_validator(mode="after")
    def grid_is_bounded_and_rectangular(self) -> "SpreadsheetGridAction":
        try:
            validate_spreadsheet_grid(self.rows)
        except SpreadsheetGridError as exc:
            raise ValueError(f"spreadsheet_grid {exc}") from exc
        return self


class ClickAction(StrictModelDecision):
    type: Literal["click", "double_click"]
    x: int = Field(ge=0)
    y: int = Field(ge=0)
    button: Literal["left", "right", "middle"] = "left"


class MoveAction(StrictModelDecision):
    type: Literal["move"]
    x: int = Field(ge=0)
    y: int = Field(ge=0)


class ScrollAction(StrictModelDecision):
    type: Literal["scroll"]
    direction: Literal["up", "down", "left", "right"] = "down"
    amount: int = Field(default=3, ge=1, le=20)


class WaitAction(StrictModelDecision):
    type: Literal["wait"]
    ms: int = Field(ge=0, le=30_000)


class WaitForStableScreenAction(StrictModelDecision):
    type: Literal["wait_for_stable_screen"]
    stable_ms: int = Field(default=300, ge=50, le=10_000)
    timeout_ms: int = Field(default=1_500, ge=50, le=30_000)


class WaitForChangeAction(StrictModelDecision):
    type: Literal["wait_for_change"]
    timeout_ms: int = Field(default=8_000, ge=50, le=30_000)


ComputerAction = Annotated[
    KeyAction
    | TypeTextAction
    | SpreadsheetGridAction
    | ClickAction
    | MoveAction
    | ScrollAction
    | WaitAction
    | WaitForStableScreenAction
    | WaitForChangeAction,
    Field(discriminator="type"),
]


class ControllerDecision(StrictModelDecision):
    outcome: Literal["act", "done", "replan", "blocked"]
    intent: str
    actions: list[ComputerAction] = Field(default_factory=list)
    expected_evidence: list[str] = Field(default_factory=list, max_length=20)
    expects_task_completion: bool = False
    reason: str = ""

    @model_validator(mode="after")
    def action_shape_matches_outcome(self) -> "ControllerDecision":
        if self.outcome == "act" and not self.actions:
            raise ValueError("outcome=act requires at least one action")
        if self.outcome != "act" and self.actions:
            raise ValueError(f"outcome={self.outcome} cannot include actions")
        typed_text = False
        passive_evidence_types = (
            WaitAction,
            WaitForStableScreenAction,
            WaitForChangeAction,
        )
        spreadsheet_actions = [
            action
            for action in self.actions
            if isinstance(action, SpreadsheetGridAction)
        ]
        if spreadsheet_actions and (
            len(spreadsheet_actions) != 1
            or any(
                not isinstance(
                    action,
                    (*passive_evidence_types, SpreadsheetGridAction),
                )
                for action in self.actions
            )
        ):
            raise ValueError(
                "spreadsheet_grid requires a separate verified focus action"
            )
        for action in self.actions:
            if typed_text and not isinstance(action, passive_evidence_types):
                raise ValueError(
                    "type_text cannot have an active follow-up in the same burst"
                )
            if isinstance(action, TypeTextAction):
                typed_text = True
        for previous, current in zip(self.actions, self.actions[1:]):
            if (
                isinstance(previous, MoveAction)
                and isinstance(current, MoveAction)
                and previous.x == current.x
                and previous.y == current.y
            ):
                raise ValueError("duplicate consecutive pointer move is a no-op")
        pointer_activations: set[tuple[int, int, str]] = set()
        for action in self.actions:
            if not isinstance(action, ClickAction):
                continue
            fingerprint = (action.x, action.y, action.button)
            if fingerprint in pointer_activations:
                raise ValueError(
                    "duplicate pointer activation within one burst is unsafe; "
                    "use one click or the explicit double_click action"
                )
            pointer_activations.add(fingerprint)
        if len(self.actions) > 1 and all(
            isinstance(action, MoveAction) for action in self.actions
        ):
            raise ValueError(
                "multiple pointer-only moves do not provide task evidence"
            )
        return self


class CriterionAssessment(StrictModelDecision):
    criterion_index: int = Field(ge=0, le=19)
    satisfied: bool
    evidence: str


VERIFICATION_SUMMARY_MAX_LENGTH = 1_200


class VerificationDecision(StrictModelDecision):
    verdict: Literal["verified", "complete", "uncertain", "failed"]
    summary: str = Field(
        min_length=1,
        max_length=VERIFICATION_SUMMARY_MAX_LENGTH,
    )
    evidence: list[str] = Field(default_factory=list)
    criteria: list[CriterionAssessment] = Field(default_factory=list, max_length=20)
    action_criteria: list[CriterionAssessment] = Field(
        default_factory=list,
        max_length=20,
    )


class PendingAction(BaseModel):
    index: int
    intent: str
    actions: list[dict[str, Any]]
    expected_evidence: list[str] = Field(default_factory=list, max_length=20)
    expects_task_completion: bool = False
    based_on_world_version: int | None
    based_on_control_epoch: int | None
    idempotency_key: str
    attempts: int = 0


class HarnessEvent(BaseModel):
    sequence: int
    at: datetime = Field(default_factory=utc_now)
    kind: str
    data: dict[str, Any] = Field(default_factory=dict)


class VerificationImageArtifact(BaseModel):
    """Private path plus safe coordinates for one visual action receipt."""

    revision: int = Field(ge=1)
    action_index: int = Field(ge=0)
    kind: Literal["before_after", "pre_action"] = "before_after"
    before_frame_id: int | None = Field(default=None, ge=0)
    after_frame_id: int | None = Field(default=None, ge=0)
    path: str = Field(min_length=1, max_length=4_096)


class CurrentActivity(BaseModel):
    """Durable in-flight work independent of the bounded visible event tail."""

    kind: Literal["model", "tool"]
    started_at: datetime = Field(default_factory=utc_now)
    phase: ModelActivityPhase | None = None
    role: str | None = None
    provider: str | None = None
    model: str | None = None
    attempt: int | None = None
    tool: str | None = None
    call_id: str | None = None
    arguments: dict[str, Any] = Field(default_factory=dict)


_MODEL_ACTIVITY_CLOSED = {
    "model.provider_completed",
    "model.provider_failed",
    "model.provider_skipped",
    "model.provider_budget_blocked",
    "model.failed",
}
_TOOL_ACTIVITY_CLOSED = {
    "action.completed",
    "action.failed",
    "action.refused_stale",
    "action.refused_by_operator",
    "action.stale_world_refreshed",
    "action.stale_world_retry_checkpointed",
    "action.transport_uncertain",
    "action.completed_unverified",
    "action.recoverable_failure",
    "approval.required",
    "target.identity_changed",
    "tool.completed",
    "tool.failed",
    "tool.refused",
    "tool.approval_required",
}
_RUN_ACTIVITY_CLOSED = {
    "run.process_interrupted",
    "run.paused",
    "run.steered",
    "run.autonomy_stopped",
    "run.completed",
    "run.blocked",
    "run.rejected",
    "run.aborted",
    "run.failed",
}


class RunSnapshot(BaseModel):
    run_id: str
    task: str
    status: RunStatus
    mode: RunMode = "computer"
    computer_task: str | None = Field(default=None, max_length=20_000)
    origin: Literal["managed", "direct_mcp"] = "managed"
    model_provider: str | None = Field(
        default=None,
        pattern=r"^[A-Za-z0-9_.:-]{1,128}$",
    )
    model_route: RunModelRoute | None = None
    caller: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    session_id: str | None = None
    plan: PlanDecision | None = None
    operator_guidance: list[str] = Field(default_factory=list, max_length=20)
    conversation: list[ConversationMessage] = Field(
        default_factory=list,
        max_length=200,
    )
    observation: ComputerObservation | None = None
    pending_action: PendingAction | None = None
    pending_approval: dict[str, Any] | None = None
    last_controller: ControllerDecision | None = None
    last_verification: VerificationDecision | None = None
    latest_verification_image_path: str | None = None
    latest_verification_image_revision: int = Field(default=0, ge=0)
    verification_images: list[VerificationImageArtifact] = Field(
        default_factory=list,
        max_length=64,
    )
    artifact_acceptance: ArtifactAcceptance | None = None
    media_transaction: MediaTransaction | None = None
    next_action_index: int = 0
    error: str | None = None
    model_budget: RunModelBudgetState = Field(default_factory=RunModelBudgetState)
    active_activity: CurrentActivity | None = None
    event_cursor: int = Field(default=0, ge=0)
    events: list[HarnessEvent] = Field(default_factory=list)

    @model_validator(mode="after")
    def model_selection_is_unambiguous(self) -> "RunSnapshot":
        if self.model_provider is not None and self.model_route is not None:
            raise ValueError(
                "model_provider and model_route cannot both be selected"
            )
        return self

    def record(self, kind: str, **data: Any) -> None:
        next_sequence = max(
            self.event_cursor,
            self.events[-1].sequence if self.events else 0,
        ) + 1
        event = HarnessEvent(
            sequence=next_sequence,
            kind=kind,
            data=data,
        )
        self.events.append(event)
        self.event_cursor = next_sequence
        if kind == "model.started":
            self.active_activity = CurrentActivity(
                kind="model",
                started_at=event.at,
                phase="queued",
                role=str(data.get("role") or "") or None,
            )
        elif kind == "model.provider_started":
            self.active_activity = CurrentActivity(
                kind="model",
                started_at=event.at,
                phase="provider_selected",
                role=str(data.get("role") or "") or None,
                provider=str(data.get("provider") or "") or None,
                model=str(data.get("model") or "") or None,
                attempt=self._optional_int(data.get("attempt")),
            )
        elif kind in {
            "model.provider_request_sent",
            "model.provider_output_received",
            "model.provider_validating",
            "model.provider_schema_repair",
            "model.provider_failover",
        }:
            phase_by_kind: dict[str, ModelActivityPhase] = {
                "model.provider_request_sent": "request_sent",
                "model.provider_output_received": "output_received",
                "model.provider_validating": "validating",
                "model.provider_schema_repair": "schema_repair",
                "model.provider_failover": "failover",
            }
            previous = self.active_activity
            previous_model = (
                previous
                if previous is not None and previous.kind == "model"
                else None
            )
            event_attempt = self._optional_int(data.get("attempt"))
            self.active_activity = CurrentActivity(
                kind="model",
                started_at=(
                    previous_model.started_at
                    if previous_model is not None
                    else event.at
                ),
                phase=phase_by_kind[kind],
                role=(
                    str(data.get("role") or "")
                    or (previous_model.role if previous_model else None)
                ),
                provider=(
                    str(
                        data.get("provider")
                        or data.get("to_provider")
                        or ""
                    )
                    or (previous_model.provider if previous_model else None)
                ),
                model=(
                    str(data.get("model") or "")
                    or (
                        previous_model.model
                        if previous_model and kind != "model.provider_failover"
                        else None
                    )
                ),
                attempt=(
                    event_attempt
                    if event_attempt is not None
                    else (previous_model.attempt if previous_model else None)
                ),
            )
        elif kind == "action.attempted":
            self.active_activity = CurrentActivity(
                kind="tool",
                started_at=event.at,
                tool=str(data.get("tool") or "MCP tool"),
                call_id=str(data.get("call_id") or "") or None,
                attempt=self._optional_int(data.get("attempt")),
                arguments=(
                    dict(data["arguments"])
                    if isinstance(data.get("arguments"), dict)
                    else {}
                ),
            )
        elif kind == "tool.started":
            self.active_activity = CurrentActivity(
                kind="tool",
                started_at=event.at,
                tool=str(data.get("tool") or "Tool"),
                call_id=str(data.get("call_id") or "") or None,
                arguments=(
                    dict(data["arguments"])
                    if isinstance(data.get("arguments"), dict)
                    else {}
                ),
            )
        elif kind in _MODEL_ACTIVITY_CLOSED:
            activity = self.active_activity
            if (
                activity is not None
                and activity.kind == "model"
                and self._activity_matches(activity, data)
            ):
                self.active_activity = None
        elif kind in _TOOL_ACTIVITY_CLOSED:
            activity = self.active_activity
            if (
                activity is not None
                and activity.kind == "tool"
                and self._activity_matches(activity, data)
            ):
                self.active_activity = None
        elif kind in _RUN_ACTIVITY_CLOSED:
            self.active_activity = None
        self.updated_at = utc_now()

    @model_validator(mode="after")
    def validate_event_window(self) -> "RunSnapshot":
        if not self.events:
            return self
        for previous, current in zip(self.events, self.events[1:]):
            if current.sequence != previous.sequence + 1:
                raise ValueError(
                    "loaded run events must be one contiguous history window"
                )
        self.event_cursor = max(self.event_cursor, self.events[-1].sequence)
        return self

    @staticmethod
    def _activity_matches(
        activity: CurrentActivity,
        data: dict[str, Any],
    ) -> bool:
        for field in ("role", "provider", "attempt", "call_id"):
            expected = getattr(activity, field)
            observed = data.get(field)
            if expected is not None and observed is not None and expected != observed:
                return False
        return True

    @staticmethod
    def _optional_int(value: Any) -> int | None:
        if value is None or isinstance(value, bool):
            return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None


class RunSummary(BaseModel):
    """Event-free run metadata used by rails, inventories, and health views."""

    run_id: str
    task: str
    status: RunStatus
    mode: RunMode = "computer"
    origin: Literal["managed", "direct_mcp"] = "managed"
    model_provider: str | None = None
    model_route: RunModelRoute | None = None
    caller: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime
    session_id: str | None = None
    error: str | None = None
    event_count: int = Field(ge=0)
    event_cursor: int = Field(ge=0)
    frame_id: int | None = None
    active_activity: CurrentActivity | None = None
    artifact_acceptance_state: ArtifactAcceptanceState | None = None
    media_transaction_state: MediaTransactionState | None = None

    @classmethod
    def from_snapshot(cls, run: RunSnapshot) -> RunSummary:
        event_cursor = max(
            run.event_cursor,
            run.events[-1].sequence if run.events else 0,
        )
        return cls(
            run_id=run.run_id,
            task=run.task,
            status=run.status,
            mode=run.mode,
            origin=run.origin,
            model_provider=run.model_provider,
            model_route=run.model_route,
            caller=run.caller,
            created_at=run.created_at,
            updated_at=run.updated_at,
            session_id=run.session_id,
            error=run.error,
            event_count=event_cursor,
            event_cursor=event_cursor,
            frame_id=(
                run.observation.frame_id
                if run.observation is not None
                else None
            ),
            active_activity=run.active_activity,
            artifact_acceptance_state=(
                run.artifact_acceptance.state
                if run.artifact_acceptance is not None
                else None
            ),
            media_transaction_state=(
                run.media_transaction.state
                if run.media_transaction is not None
                else None
            ),
        )


class RunEventPage(BaseModel):
    """A bounded durable event page plus the current history cursor."""

    events: list[HarnessEvent]
    latest_cursor: int = Field(ge=0)
    has_more: bool
