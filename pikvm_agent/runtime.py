"""Runtime composition + session lifecycle.

Libraries are instantiated inside our runtime; they never call each other or
PiKVM directly. The Runtime owns the backend, the shared services (screen
parser, operator, policy, the compiled LangGraph), the session store, and
per-session frame/trace/deps state. ``continue_session`` drives the graph until
the next approval interrupt or completion; ``submit_approval`` resumes it.
"""

from __future__ import annotations

import asyncio
import contextlib
import copy
import hashlib
import json
import logging
import os
import tempfile
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from langgraph.types import Command
from PIL import Image, ImageChops, ImageStat

from pikvm_agent.config import AppConfig, PolicyConfig, load_config
from pikvm_agent.core.errors import SessionNotFoundError
from pikvm_agent.core.models import OCRResult, Region
from pikvm_agent.debuglog import DEBUG
from pikvm_agent.executor import burst as _burst
from pikvm_agent.executor.recovery import Recovery
from pikvm_agent.executor.transactions import GuardedTransactionExecutor
from pikvm_agent.graph.checkpoints import build_checkpointer, close_checkpointer
from pikvm_agent.graph.deps import GraphDeps
from pikvm_agent.operator.routing import OperatorModelRouter
from pikvm_agent.graph.graph import build_graph
from pikvm_agent.operator.fake import FakeOperator
from pikvm_agent.pikvm.client import PiKVMBackend
from pikvm_agent.pikvm.fake import FakeBackend
from pikvm_agent.policy.safety import SafetyPolicyEngine
from pikvm_agent.policy.direct import (
    classify_direct_burst,
    is_confirmed_calculator_surface,
    is_confirmed_file_explorer_surface,
    is_confirmed_open_filename_surface,
    is_confirmed_save_as_filename_surface,
    is_confirmed_windows_run_surface,
    is_safe_local_commit_draft,
    is_safe_local_filename_draft,
    is_safe_local_navigation_target,
    needs_calculator_surface_grounding,
    needs_deferred_exact_editor_surface_grounding,
    needs_local_file_overwrite_surface_grounding,
    needs_local_navigation_surface_grounding,
    needs_safe_windows_error_dismissal_surface_grounding,
)
from pikvm_agent.store.frames import FrameStore
from pikvm_agent.store.sqlite import SessionStore
from pikvm_agent.store.trace import TraceLog
from pikvm_agent.vision.frame_diff import screen_hashes_match_surface
from pikvm_agent.vision.omniparser_manager import OmniParserManager
from pikvm_agent.vision.paddleocr_client import paddleocr_available
from pikvm_agent.vision.providers import build_ocr_provider, build_screen_parser
from pikvm_agent.vision.screen_parser import bbox_from_ocr
from pikvm_agent.vision.tesseract_ocr import tesseract_available

log = logging.getLogger("pikvm_agent.runtime")

DEFAULT_MAX_STEPS = 12
PANIC_QUIESCE_TIMEOUT_S = 5.0
LOCAL_POINTER_FRESHNESS_RADIUS_PX = 48
LOCAL_POINTER_FRESHNESS_MAX_DELTA = 0.035
SAFE_ERROR_DIALOG_LEFT_OF_COMMIT_PX = 260
SAFE_ERROR_DIALOG_RIGHT_OF_COMMIT_PX = 140
SAFE_ERROR_DIALOG_ABOVE_COMMIT_PX = 120
SAFE_ERROR_DIALOG_BELOW_COMMIT_PX = 55


@dataclass(frozen=True)
class RuntimeCapabilities:
    """Construction-time capabilities that ordinary daemon config cannot grant."""

    isolated_benchmark_pointer_freshness: bool = False


def _local_pointer_freshness_enabled(
    policy: PolicyConfig,
    capabilities: RuntimeCapabilities,
) -> bool:
    """Limit relaxed pointer freshness to the resettable benchmark profile."""

    return (
        capabilities.isolated_benchmark_pointer_freshness
        and policy.allow_local_pointer_freshness
        and policy.default_profile == "isolated_benchmark"
    )


def _localized_pointer_freshness(
    actions: list[dict[str, Any]],
    *,
    planned_image_path: str,
    current_image_path: str,
    radius_px: int = LOCAL_POINTER_FRESHNESS_RADIUS_PX,
    max_delta: float = LOCAL_POINTER_FRESHNESS_MAX_DELTA,
) -> tuple[bool, float | None]:
    """Check target-local stability for an explicitly opted-in pointer burst.

    Full-frame world versions remain the production default. The isolated
    benchmark profile may use this narrower check for continuously changing,
    unrelated content such as a playing video. Keyboard, typing, and scrolling
    actions can never receive the exemption.
    """

    pointer_kinds = {"click", "double_click", "move"}
    passive_kinds = {"wait", "wait_for_stable_screen", "wait_for_change"}
    kinds = {str(action.get("type", "")) for action in actions}
    if not kinds or not kinds.issubset(pointer_kinds | passive_kinds):
        return False, None
    pointer_actions = [
        action for action in actions if action.get("type") in pointer_kinds
    ]
    if not pointer_actions:
        return False, None

    try:
        with (
            Image.open(planned_image_path) as planned_source,
            Image.open(current_image_path) as current_source,
        ):
            planned = planned_source.convert("RGB")
            current = current_source.convert("RGB")
            if planned.size != current.size:
                return False, None
            width, height = planned.size
            largest_delta = 0.0
            for action in pointer_actions:
                x, y = int(action["x"]), int(action["y"])
                if not (0 <= x < width and 0 <= y < height):
                    return False, None
                box = (
                    max(0, x - radius_px),
                    max(0, y - radius_px),
                    min(width, x + radius_px + 1),
                    min(height, y + radius_px + 1),
                )
                difference = ImageChops.difference(
                    planned.crop(box),
                    current.crop(box),
                )
                channel_means = ImageStat.Stat(difference).mean
                delta = sum(channel_means) / max(1, len(channel_means)) / 255.0
                largest_delta = max(largest_delta, delta)
                if delta > max_delta:
                    return False, largest_delta
            return True, largest_delta
    except (OSError, TypeError, ValueError, KeyError):
        return False, None


def _safe_error_dialog_region(
    width: int,
    height: int,
    click: dict[str, Any] | None,
) -> Region:
    """Bound OCR to the Windows error dialog around its commit button."""

    if click is None:
        left = round(width * 0.15)
        top = round(height * 0.15)
        right = round(width * 0.85)
        bottom = round(height * 0.85)
    else:
        click_x = int(click["x"])
        click_y = int(click["y"])
        left = max(0, click_x - SAFE_ERROR_DIALOG_LEFT_OF_COMMIT_PX)
        top = max(0, click_y - SAFE_ERROR_DIALOG_ABOVE_COMMIT_PX)
        right = min(width, click_x + SAFE_ERROR_DIALOG_RIGHT_OF_COMMIT_PX)
        bottom = min(height, click_y + SAFE_ERROR_DIALOG_BELOW_COMMIT_PX)
    return Region(
        x=left,
        y=top,
        width=max(1, right - left),
        height=max(1, bottom - top),
    )


def nearest_ocr_target_text(
    observed: OCRResult,
    *,
    click_x: int,
    click_y: int,
    region: Region,
) -> str:
    """Return only the OCR line geometrically nearest the click.

    A broad OCR crop is useful for reading icon-adjacent labels, but feeding all
    text in that crop to the safety classifier lets a dangerous word on a
    neighboring row misclassify a routine target. Boxes may be relative to the
    requested crop (local Tesseract) or full-frame (remote OCR).
    """

    confident_lines = [
        line
        for line in observed.lines
        if line.confidence is None or float(line.confidence) >= 0.10
    ]
    candidates: list[tuple[float, float, str]] = []
    for line in confident_lines:
        box = bbox_from_ocr(line.bbox)
        if not box:
            continue
        x0, y0 = float(box.x), float(box.y)
        x1, y1 = float(box.x + box.w), float(box.y + box.h)
        crop_relative = (
            0 <= x0 <= region.width
            and 0 <= x1 <= region.width + 2
            and 0 <= y0 <= region.height
            and 0 <= y1 <= region.height + 2
        )
        point_x = click_x - region.x if crop_relative else click_x
        point_y = click_y - region.y if crop_relative else click_y
        dx = max(x0 - point_x, 0.0, point_x - x1)
        dy = max(y0 - point_y, 0.0, point_y - y1)
        candidates.append((dx * dx + 4 * dy * dy, dy, line.text.strip()))
    if candidates:
        # Discard other rows before ranking by distance.  Previously a label
        # just beyond the vertical acceptance band could be geometrically
        # closer than the text on the clicked row, win ``min()``, and then be
        # rejected without considering the valid same-row candidate.
        same_row = [
            candidate
            for candidate in candidates
            if candidate[1] <= 16 and candidate[2]
        ]
        if not same_row:
            return ""
        distance, _vertical_gap, text = min(
            same_row, key=lambda item: item[0]
        )
        if distance <= 140**2:
            return text
        return ""
    if len(confident_lines) == 1:
        return confident_lines[0].text.strip()
    return ""


def _blank_editor_canvas_is_visible(
    image_path: Path,
    *,
    menu_box: tuple[float, float, float, float],
    character_box: tuple[float, float, float, float],
    line_ending_box: tuple[float, float, float, float],
    frame_width: int,
    frame_height: int,
) -> bool:
    """Prove that the chrome encloses one unobscured blank editor canvas."""

    left = max(0, int(min(menu_box[0], character_box[0])))
    top = max(0, int(menu_box[3] + max(8, frame_height * 0.015)))
    right = min(
        frame_width,
        int(
            max(line_ending_box[2], character_box[2])
            + frame_width * 0.05
        ),
    )
    bottom = min(
        frame_height,
        int(min(character_box[1], line_ending_box[1]) - 10),
    )
    if (
        right - left < frame_width * 0.35
        or bottom - top < frame_height * 0.20
    ):
        return False
    try:
        with Image.open(image_path) as image:
            gray = image.convert("L").crop((left, top, right, bottom))
            histogram = gray.histogram()
            pixel_count = max(1, gray.width * gray.height)
            mode = max(range(len(histogram)), key=histogram.__getitem__)
            near_mode = sum(
                histogram[max(0, mode - 5) : min(256, mode + 6)]
            )
            standard_deviation = float(ImageStat.Stat(gray).stddev[0])
    except (OSError, ValueError):
        return False
    return standard_deviation <= 8.0 and near_mode / pixel_count >= 0.94


def _is_confirmed_blank_titleless_notepad_editor(
    observed: OCRResult,
    image_path: Path,
    *,
    frame_width: int,
    frame_height: int,
) -> bool:
    """Recognize fresh Windows 11 Notepad despite bounded OCR corruption.

    The title, menu and aligned status row must all be independently boxed,
    and the rectangle between them must be a mostly uniform blank canvas. This
    keeps background Notepad chrome or VS Code's similar status tokens from
    authorizing bare Enter on an obscuring foreground surface.
    """

    evidence: list[
        tuple[str, tuple[float, float, float, float]]
    ] = []
    for line in observed.lines:
        box = bbox_from_ocr(line.bbox)
        rectangle = (
            (
                float(box.x),
                float(box.y),
                float(box.x + box.w),
                float(box.y + box.h),
            )
            if box is not None
            else None
        )
        text = " ".join(str(line.text or "").casefold().split())
        if rectangle is not None and text:
            evidence.append((text, rectangle))

    titles = [
        (text, box)
        for text, box in evidence
        if "untit" in text or "unfit" in text
    ]
    menus = [
        (text, box)
        for text, box in evidence
        if all(marker in text for marker in ("file", "edit", "view"))
    ]
    character_rows = [
        (text, box)
        for text, box in evidence
        if "character" in text
    ]
    line_ending_rows = [
        (text, box)
        for text, box in evidence
        if "windows" in text and "cr" in text
    ]

    for _title_text, title_box in titles:
        for _menu_text, menu_box in menus:
            if (
                title_box[1] > frame_height * 0.20
                or menu_box[1] > frame_height * 0.25
                or title_box[1] > menu_box[1]
                or menu_box[1] - title_box[3] > frame_height * 0.12
            ):
                continue
            for _character_text, character_box in character_rows:
                if (
                    character_box[0] >= frame_width * 0.25
                    or character_box[1] - menu_box[3]
                    < frame_height * 0.20
                    or character_box[1] > frame_height * 0.90
                ):
                    continue
                for _line_text, line_ending_box in line_ending_rows:
                    character_center = (
                        character_box[1] + character_box[3]
                    ) / 2
                    line_center = (
                        line_ending_box[1] + line_ending_box[3]
                    ) / 2
                    if (
                        line_ending_box[0] <= frame_width * 0.35
                        or abs(character_center - line_center)
                        > frame_height * 0.04
                    ):
                        continue
                    if _blank_editor_canvas_is_visible(
                        image_path,
                        menu_box=menu_box,
                        character_box=character_box,
                        line_ending_box=line_ending_box,
                        frame_width=frame_width,
                        frame_height=frame_height,
                    ):
                        return True
    return False


def build_backend(config: AppConfig) -> Any:
    """Pick a backend: real PiKVM when credentials are present, else the fake."""
    if os.environ.get("PIKVM_AGENT_FAKE") == "1":
        log.info("PIKVM_AGENT_FAKE=1 — using FakeBackend")
        return FakeBackend()
    pk = config.pikvm
    if pk.username or pk.token:
        log.info("Using PiKVMBackend at %s", pk.base_url)
        return PiKVMBackend(pk)
    log.warning("No PiKVM credentials (%s/%s/%s unset) — using FakeBackend",
                pk.username_env, pk.password_env, pk.token_env)
    return FakeBackend()


def build_operator(config: AppConfig, backend: Any) -> Any:
    """OpenRouter operator when configured + keyed, else the deterministic fake."""
    op = config.operator
    if op.provider == "openrouter" and op.api_key:
        from pikvm_agent.operator.openrouter import OpenRouterOperator

        log.info("Using OpenRouterOperator (lanes: %s)", ", ".join(op.lanes))
        return OpenRouterOperator(op)
    log.info("Using FakeOperator (operator.provider=%s)", op.provider)
    return FakeOperator()


@dataclass
class SessionRuntime:
    session_id: str
    task: str
    machine: dict[str, Any]
    frames: FrameStore
    trace: TraceLog
    deps: GraphDeps
    started: bool = False
    status: str = "running"
    events: list[dict[str, Any]] = field(default_factory=list)
    error: str = ""
    # Bumped on abort / panic / steer; the executor refuses a transaction whose
    # decision was made under a stale epoch (see graph.nodes.execute_transaction).
    control_epoch: int = 0
    # Sticky terminal brake — latched by abort / panic. The epoch invalidates an
    # in-flight decision but a re-planned loop re-stamps the new epoch and would pass;
    # this latch makes the stop survive re-planning AND blocks resume of a paused session.
    stopped: bool = False
    # Caller-stable direct-control operations. A response retry must never type
    # or click twice; a key may only be reused for the identical burst.
    burst_idempotency: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Execution claims are separate from completed response receipts. A client
    # may retry after its request timeout while watched typing is still doing
    # OCR readback; identical callers must join that task, never start HID again.
    burst_inflight: dict[str, dict[str, Any]] = field(default_factory=dict)
    # Idempotency is checked once before screen grounding and again immediately
    # before an execution claim is created. Grounding can block behind the
    # original watched-typing request long enough for it to finish, so the
    # second check and claim must be atomic per caller key.
    burst_idempotency_locks: dict[str, asyncio.Lock] = field(
        default_factory=dict
    )
    # A manual input report or newly detected machine client invalidates model
    # authority established under an earlier control epoch.
    last_human_input_at: float | None = None
    observed_control_epoch: int = 0
    other_client_block_active: bool = False
    # One exact, visible local draft may ground the immediately following bare
    # Enter when the same frame independently shows Explorer, Save As, or the
    # Windows Run dialog around an allowlisted launcher.
    verified_local_navigation_draft: dict[str, Any] | None = None


class Runtime:
    def __init__(self, config: AppConfig, store: SessionStore, backend: Any, *,
                 screen_parser: Any, operator: Any, policy: SafetyPolicyEngine,
                 graph: Any, checkpointer: Any, executor: Any, recovery: Any,
                 ocr_provider: Any,
                 omniparser: OmniParserManager | None = None,
                 capabilities: RuntimeCapabilities | None = None) -> None:
        self.config = config
        self.capabilities = capabilities or RuntimeCapabilities()
        self.store = store
        self.backend = backend
        self._screen_parser = screen_parser
        self._operator = operator
        self._policy = policy
        self._graph = graph
        self._checkpointer = checkpointer
        self._executor = executor
        self._recovery = recovery
        self._ocr_provider = ocr_provider
        warmup = getattr(ocr_provider, "warmup", None)
        self._ocr_warmup_task = (
            asyncio.create_task(self._warm_ocr_provider(warmup))
            if callable(warmup)
            else None
        )
        self._omniparser = omniparser
        self._omniparser_started = False  # lazy: spawned on first perception/autonomous use
        self._sessions: dict[str, SessionRuntime] = {}
        # Tasks that crossed the last safety gate and may currently emit HID.
        # Panic-stop cannot report success until all of them have exited.
        self._active_hid_tasks: set[asyncio.Task[Any]] = set()
        self._hid_idle = asyncio.Event()
        self._hid_idle.set()
        # /status is polled constantly by the readiness UI; cache it so each poll doesn't
        # re-run the (network) health probes and contend with real work.
        self._status_cache: tuple[float, dict[str, Any]] | None = None

    async def _warm_ocr_provider(self, warmup: Any) -> bool:
        """Warm optional secondary OCR from one read-only captured frame."""

        temporary_path: Path | None = None
        try:
            frame = await self.backend.screenshot()
            if not frame or not frame.data:
                return False
            temporary = tempfile.NamedTemporaryFile(
                suffix=".jpg",
                delete=False,
            )
            temporary_path = Path(temporary.name)
            try:
                temporary.write(frame.data)
            finally:
                temporary.close()
            return bool(await warmup(temporary_path))
        except asyncio.CancelledError:
            raise
        except Exception:
            return False
        finally:
            if temporary_path is not None:
                temporary_path.unlink(missing_ok=True)

    @classmethod
    async def from_config(
        cls,
        config: AppConfig | None = None,
        *,
        capabilities: RuntimeCapabilities | None = None,
    ) -> "Runtime":
        config = config or load_config()
        # Wire the ultimate debug log first so startup itself is captured.
        DEBUG.configure(config.daemon.debug_log_path, session_dir=config.daemon.session_dir,
                        enabled=config.daemon.debug_log, truncate=config.daemon.debug_log_truncate)
        store = SessionStore(config.daemon.sqlite_path)
        await store.connect()
        backend = build_backend(config)
        ocr = build_ocr_provider(config, backend)
        screen_parser = build_screen_parser(
            config,
            backend,
            ocr_provider=ocr,
        )
        operator = build_operator(config, backend)
        policy = SafetyPolicyEngine(config.policy)
        from pikvm_agent.executor.typing import WatchedTyper

        typer = WatchedTyper(backend, ocr)
        executor = GuardedTransactionExecutor(backend, ocr, typer=typer)
        recovery = Recovery(backend)

        # Construct the OmniParser manager but DON'T start it — element grounding is only
        # needed by the opt-in Layer-2 perception + Layer-3 autonomous paths, so the (heavy,
        # GPU) child process spawns lazily on first use, not with the daemon. Burst mode
        # never touches it.
        omniparser: OmniParserManager | None = (
            OmniParserManager(config.omniparser) if config.omniparser.enabled else None
        )

        graph_db = str(Path(config.daemon.sqlite_path).with_name("graph.sqlite3"))
        checkpointer = await build_checkpointer(graph_db)
        graph = build_graph(checkpointer)
        return cls(config, store, backend, screen_parser=screen_parser, operator=operator,
                   policy=policy, graph=graph, checkpointer=checkpointer,
                   executor=executor, recovery=recovery, ocr_provider=ocr,
                   omniparser=omniparser,
                   capabilities=capabilities)

    async def aclose(self) -> None:
        warmup_task = self._ocr_warmup_task
        if warmup_task is not None and not warmup_task.done():
            warmup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await warmup_task
        try:
            await self.backend.aclose()
        finally:
            # Close pooled HTTP clients on the operator / element parser if present.
            for owner in (
                self._operator,
                getattr(self._screen_parser, "elements", None),
                self._ocr_provider,
            ):
                closer = getattr(owner, "aclose", None)
                if closer is not None:
                    try:
                        await closer()
                    except Exception:  # noqa: BLE001 - best-effort cleanup
                        pass
            if self._omniparser is not None:
                await self._omniparser.stop()
            await close_checkpointer(self._checkpointer)
            await self.store.close()

    def _get(self, session_id: str) -> SessionRuntime:
        sr = self._sessions.get(session_id)
        if sr is None:
            raise SessionNotFoundError(session_id)
        return sr

    def _graph_config(self, sr: SessionRuntime) -> dict[str, Any]:
        return {"configurable": {"deps": sr.deps, "thread_id": sr.session_id}}

    def _cursor_state(self) -> dict[str, Any] | None:
        """The backend's tracked cursor (pixel x/y, trusted, other_clients), if it tracks."""
        getter = getattr(self.backend, "cursor", None)
        return getter() if callable(getter) else None

    def _other_client_count(self) -> int:
        getter = getattr(self.backend, "other_clients", None)
        return max(0, int(getter())) if callable(getter) else 0

    @staticmethod
    def _revoke_control_for_human(
        sr: SessionRuntime,
        *,
        event_kind: str,
        source: str,
        **details: Any,
    ) -> None:
        sr.control_epoch += 1
        sr.last_human_input_at = time.time()
        sr.trace.append(
            event_kind,
            source=source,
            control_epoch=sr.control_epoch,
            **details,
        )

    def machine_identity(self) -> dict[str, str]:
        """Return a safe, stable identity for the configured computer target.

        This is configuration continuity, not hardware attestation.  The raw
        endpoint and optional explicit id are never returned.
        """

        cfg = self.config.pikvm
        explicit = os.environ.get(cfg.machine_id_env, "").strip()
        if explicit:
            source = "explicit_machine_id"
            material = f"explicit\0{explicit}"
        else:
            parsed = urlsplit(cfg.base_url)
            host = (parsed.hostname or "").lower()
            port = f":{parsed.port}" if parsed.port is not None else ""
            path = parsed.path.rstrip("/")
            canonical_endpoint = f"{parsed.scheme.lower()}://{host}{port}{path}"
            source = "configured_endpoint"
            material = f"endpoint\0{canonical_endpoint}"
        digest = hashlib.sha256(
            f"pikvm-agent-target-v1\0{material}".encode()
        ).hexdigest()[:16]
        return {
            "alias": cfg.machine_alias.strip() or "Unlabelled target",
            "fingerprint": f"target:{digest}",
            "identity_source": source,
            "desktop_layer": cfg.desktop_layer.strip() or "Physical console",
            "attestation": "configured_target",
        }

    def report_external_cursor(self, nx: float, ny: float) -> dict[str, Any]:
        """The desktop live-view reports where the USER just moved the cursor (norm ±32767),
        so the daemon's tracked position stays current with manual moves kvmd won't report."""
        setter = getattr(self.backend, "set_cursor_from_norm", None)
        if callable(setter):
            setter(nx, ny)
        invalidated: list[str] = []
        for sr in self._sessions.values():
            if sr.stopped:
                continue
            self._revoke_control_for_human(
                sr,
                event_kind="human_input",
                source="external_cursor",
            )
            invalidated.append(sr.session_id)
        return {
            **(self._cursor_state() or {}),
            "invalidated_sessions": invalidated,
        }

    async def _ensure_omniparser(self) -> None:
        """Spawn + warm OmniParser the first time perception/autonomous mode actually needs
        it (it loads GPU models on boot — minutes — so we never pay that with the daemon)."""
        if self._omniparser is None or self._omniparser_started:
            return
        self._omniparser_started = True
        with DEBUG.span("omniparser.lazy_start"):
            await self._omniparser.ensure_running(wait_s=self.config.omniparser.startup_wait_s)

    # ---- lifecycle -------------------------------------------------------- #

    async def start_session(self, task: str, policy: dict | None = None,
                            operator: dict | None = None) -> dict[str, Any]:
        session_id = "s_" + uuid.uuid4().hex[:12]
        row = await self.store.create_session(session_id, task, policy or {}, operator or {})
        frames = FrameStore(session_id, self.config.daemon.session_dir, self.backend,
                            fp_meaningful=self.config.watchers.fp_meaningful)
        trace = TraceLog(session_id, self.config.daemon.session_dir)
        machine = self.machine_identity()
        trace.append(
            "session_start",
            task=task,
            policy=policy or {},
            operator=operator or {},
            machine=machine,
        )
        deps = GraphDeps(
            backend=self.backend, frames=frames, trace=trace,
            screen_parser=self._screen_parser, operator=self._operator, policy=self._policy,
            execute=self._executor.execute, recovery=self._recovery,
            model_router=OperatorModelRouter(self.config.operator.routing),
            max_steps=DEFAULT_MAX_STEPS,
        )
        sr = SessionRuntime(
            session_id=session_id,
            task=task,
            machine=machine,
            frames=frames,
            trace=trace,
            deps=deps,
        )
        # The executor reads the session's LIVE epoch + stop latch through deps; bumping
        # the epoch (steer) invalidates an in-flight decision, and the latch (abort / panic)
        # refuses every subsequent action even after a re-plan.
        deps.control_epoch_getter = lambda: sr.control_epoch
        deps.stop_getter = lambda: sr.stopped
        self._sessions[session_id] = sr
        return {
            "session_id": session_id,
            "status": row["status"],
            "task": task,
            "created_at": row["created_at"],
            "machine": machine,
        }

    async def get_session_summary(self, session_id: str, capture: bool = True) -> dict[str, Any]:
        """Report the session's status + frame metadata.

        ``capture=True`` (pikvm_observe) grabs a FRESH screenshot and records an
        ``observe`` step — an explicit "look now". ``capture=False`` is read-only: it
        returns the LAST captured frame without touching the backend or the trace, so a
        UI can poll it cheaply (polling must never drive captures or flood the trace)."""
        DEBUG.set_session(session_id)
        sr = self._get(session_id)
        if capture:
            try:
                await self.backend.connect()
            except Exception as exc:  # noqa: BLE001
                log.warning("backend.connect failed: %s", exc)
            frame = await sr.frames.capture()
            sr.trace.append("observe", frame_id=frame.frame_id, world_version=frame.world_version,
                            screenshot_path=frame.image_path)
            sr.observed_control_epoch = sr.control_epoch
        else:
            frame = sr.frames.latest()
        row = await self.store.get_session(session_id)
        status = row["status"] if row else sr.status
        base = {
            "session_id": session_id,
            "status": status,
            "task": sr.task,
            "machine": sr.machine,
            "control_epoch": sr.control_epoch,
            "cursor": self._cursor_state(),
            "human_input_since_observation": (
                sr.last_human_input_at is not None
                and sr.control_epoch != sr.observed_control_epoch
            ),
            "last_human_input_at": sr.last_human_input_at,
            "events": sr.events[-20:],
            "error": sr.error,
        }
        if frame is None:  # read-only poll before the first capture
            return {**base, "frame_id": None, "world_version": None, "screenshot_path": None,
                    "image_sha256": None, "screen_hash": None,
                    "width": None, "height": None, "keyboard_state": None}
        return {
            **base,
            "frame_id": frame.frame_id, "world_version": frame.world_version,
            "screenshot_path": frame.image_path,
            "image_sha256": frame.image_sha256,
            "screen_hash": frame.screen_hash,
            "width": frame.width, "height": frame.height,
            "keyboard_state": frame.keyboard_state.model_dump(),
        }

    async def preview_frame(self, session_id: str) -> Any:
        """Capture a UI-only frame without changing controller freshness state.

        Operator visibility must not manufacture ``frame_id`` values, bump
        ``world_version``, mark a model look, or append trace events.  The next
        guarded action still performs its own fresh capture before HID.
        """

        self._get(session_id)
        try:
            await self.backend.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("backend.connect failed: %s", exc)
        return await self.backend.screenshot()

    async def abort_session(self, session_id: str, reason: str = "") -> dict[str, Any]:
        sr = self._get(session_id)
        sr.control_epoch += 1  # invalidate any in-flight transaction
        sr.stopped = True       # latch: refuse re-planned actions + block resume
        sr.status = "failed"
        sr.error = reason or "aborted by human"
        sr.trace.append("abort", reason=reason)
        await self.store.update_session(session_id, status="failed", error=sr.error)
        return {"session_id": session_id, "status": "failed", "reason": reason}

    async def panic_stop(self) -> dict[str, Any]:
        """Emergency brake — independent of any agent/MCP. Bumps every session's
        control epoch (so any in-flight transaction is refused before it executes) and
        marks active sessions failed. It reports ``ok=true`` only after every HID task
        that had already started has observed the brake and exited."""
        stopped: list[str] = []
        for sid, sr in list(self._sessions.items()):
            sr.control_epoch += 1
            sr.stopped = True  # latch ALL sessions so none can be resumed after a panic
            # Any non-terminal session is halted — including a budget-`paused` one, which
            # would otherwise stay resumable and re-plan under the bumped epoch.
            if sr.status in ("running", "needs_approval", "paused"):
                sr.status = "failed"
                sr.error = "panic_stop"
                sr.trace.append("panic_stop")
                try:
                    await self.store.update_session(sid, status="failed", error="panic_stop")
                except Exception as exc:  # noqa: BLE001 - best-effort persistence
                    log.warning("panic_stop persist failed for %s: %s", sid, exc)
                stopped.append(sid)

        # Drop held modifiers/buttons immediately, then wait for any already-started
        # bounded micro-action to return. A timeout is explicitly NOT success.
        try:
            await self.backend.release_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("panic_stop release_all failed: %s", exc)
        try:
            await asyncio.wait_for(
                self._hid_idle.wait(),
                timeout=PANIC_QUIESCE_TIMEOUT_S,
            )
            quiesced = True
        except TimeoutError:
            quiesced = False
        try:
            await self.backend.release_all()
        except Exception as exc:  # noqa: BLE001
            log.warning("panic_stop final release_all failed: %s", exc)

        in_flight = sum(not task.done() for task in self._active_hid_tasks)
        quiesced = quiesced and in_flight == 0
        log.warning(
            "PANIC STOP — halted %d session(s), quiesced=%s, in_flight=%d: %s",
            len(stopped),
            quiesced,
            in_flight,
            stopped,
        )
        return {
            "ok": quiesced,
            "quiesced": quiesced,
            "in_flight_actions": in_flight,
            "stopped": stopped,
            "machine": self.machine_identity(),
        }

    # ---- operator loop (LangGraph) --------------------------------------- #

    async def continue_session(self, session_id: str, max_transactions: int | None = None,
                               max_runtime_ms: int | None = None) -> dict[str, Any]:
        """Run the graph until the next approval, completion, or the per-call budget is
        spent — then it PAUSES (resumable). None/None = unbounded (daemon-direct
        default); the MCP facade passes small bounds so interrupting the agent stops it
        within one transaction instead of letting one call run for minutes."""
        DEBUG.set_session(session_id)
        await self._ensure_omniparser()  # autonomous mode needs element grounding
        sr = self._get(session_id)
        if sr.stopped:
            # Aborted / panicked — never resume the loop (a paused session must stay dead).
            return {"session_id": session_id, "task": sr.task, "status": "failed",
                    "error": sr.error or "stopped"}
        try:
            await self.backend.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("backend.connect failed: %s", exc)
        config = self._graph_config(sr)
        budget = self._budget_fields(max_transactions, max_runtime_ms)
        if not sr.started:
            sr.started = True
            initial = {"session_id": session_id, "task": sr.task, "step": 0,
                       "max_steps": DEFAULT_MAX_STEPS, **budget}
            result = await self._graph.ainvoke(initial, config)
        elif sr.status == "paused":
            # Resume a budget pause: reset the per-call counter + apply the new budget.
            sr.status = "running"
            result = await self._graph.ainvoke(Command(resume=None, update=budget), config)
        else:
            # Already running/paused without a pending approval — let it proceed.
            result = await self._graph.ainvoke(None, config)
        return await self._after_run(sr, result)

    # ---- playbooks (named burst macros) ---------------------------------- #

    async def run_playbook(self, session_id: str, name: str, args: dict[str, Any] | None = None,
                           **burst_kw: Any) -> dict[str, Any]:
        """Expand a named playbook to a burst and run it (same gates as run_burst)."""
        from pikvm_agent.executor import playbooks

        try:
            actions = playbooks.expand(name, args or {})
        except playbooks.UnknownPlaybook:
            return {"session_id": session_id, "status": "failed",
                    "error": f"unknown playbook: {name}", "available": playbooks.names()}
        except playbooks.MissingPlaybookArg as exc:
            return {"session_id": session_id, "status": "failed",
                    "error": f"playbook {name} missing arg: {exc}"}
        return await self.run_burst(session_id, actions, **burst_kw)

    # ---- direct burst control (the fast model-in-the-loop path) ---------- #

    async def run_burst(self, session_id: str, actions: list[dict[str, Any]], *,
                        based_on_world_version: int | None = None,
                        based_on_control_epoch: int | None = None,
                        max_runtime_ms: int | None = None,
                        return_screenshot: bool = True,
                        idempotency_key: str | None = None,
                        _approved_digest: str | None = None) -> dict[str, Any]:
        """Execute a controller-authored HID burst LOCALLY in one shot (no perception
        loop). Gates first on freshness (world_version) + control epoch, runs the burst
        with a live abort/panic/deadline gate, then returns one screenshot so the
        controller can decide the next burst."""
        DEBUG.set_session(session_id)
        sr = self._get(session_id)
        machine = sr.machine
        if sr.stopped:
            return {
                "session_id": session_id,
                "status": "stopped",
                "error": sr.error or "stopped",
                "machine": machine,
            }
        try:
            _burst.validate_actions(actions)
        except _burst.BurstError as exc:
            sr.status = "paused"
            return {
                "session_id": session_id,
                "status": "failed",
                "error": f"bad burst: {exc}",
                "control_epoch": sr.control_epoch,
                "machine": machine,
            }

        runtime_budget_source = "explicit" if max_runtime_ms is not None else "auto"
        effective_runtime_ms = (
            int(max_runtime_ms)
            if max_runtime_ms is not None
            else _burst.recommended_runtime_ms(actions)
        )
        digest = hashlib.sha256(
            json.dumps(
                {
                    "actions": actions,
                    "max_runtime_ms": effective_runtime_ms,
                    "return_screenshot": return_screenshot,
                    "session_id": session_id,
                    "machine_fingerprint": machine["fingerprint"],
                },
                sort_keys=True,
                separators=(",", ":"),
            ).encode()
        ).hexdigest()
        key = (idempotency_key or "").strip()
        if not key:
            return {
                "session_id": session_id,
                "status": "failed",
                "error": "non-blank idempotency_key is required before HID",
                "control_epoch": sr.control_epoch,
                "machine": machine,
            }
        if key and len(key) > 160:
            return {
                "session_id": session_id,
                "status": "failed",
                "error": "idempotency_key exceeds 160 characters",
                "control_epoch": sr.control_epoch,
                "machine": machine,
            }
        prior = sr.burst_idempotency.get(key) if key else None
        if prior is not None:
            if prior["digest"] != digest:
                return {
                    "session_id": session_id,
                    "status": "idempotency_conflict",
                    "error": "idempotency_key was already used for a different burst",
                    "control_epoch": sr.control_epoch,
                    "machine": machine,
                }
            if (
                prior.get("status") != "approval_pending"
                or _approved_digest != digest
            ):
                replay = copy.deepcopy(prior["result"])
                replay["idempotent_replay"] = True
                return replay
        # FRESHNESS: the screen must still be the one the controller planned against.
        # The resettable benchmark profile can explicitly opt into a target-local
        # check for pointer-only bursts across unrelated animation.
        planned_frame = sr.frames.latest()
        if (
            based_on_world_version is None
            or based_on_control_epoch is None
        ):
            sr.trace.append(
                "burst_freshness_required",
                supplied_world_version=based_on_world_version,
                supplied_control_epoch=based_on_control_epoch,
            )
            return {
                "session_id": session_id,
                "status": "freshness_required",
                "error": (
                    "based_on_world_version and based_on_control_epoch are "
                    "required before HID"
                ),
                "frame_id": (
                    planned_frame.frame_id if planned_frame is not None else None
                ),
                "world_version": (
                    planned_frame.world_version
                    if planned_frame is not None
                    else None
                ),
                "control_epoch": sr.control_epoch,
                "screenshot_path": (
                    planned_frame.image_path if planned_frame is not None else None
                ),
                "width": planned_frame.width if planned_frame is not None else None,
                "height": (
                    planned_frame.height if planned_frame is not None else None
                ),
                "machine": machine,
            }
        try:
            await self.backend.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("backend.connect failed: %s", exc)
        frame = await sr.frames.capture()
        localized_freshness = False
        localized_freshness_delta: float | None = None
        local_freshness_enabled = _local_pointer_freshness_enabled(
            self.config.policy,
            self.capabilities,
        )
        if based_on_world_version is not None and frame.world_version != based_on_world_version:
            if (
                local_freshness_enabled
                and planned_frame is not None
                and planned_frame.world_version == based_on_world_version
            ):
                (
                    localized_freshness,
                    localized_freshness_delta,
                ) = await asyncio.to_thread(
                    _localized_pointer_freshness,
                    actions,
                    planned_image_path=planned_frame.image_path,
                    current_image_path=frame.image_path,
                )
            if localized_freshness:
                sr.trace.append(
                    "burst_local_freshness_accepted",
                    planned=based_on_world_version,
                    current=frame.world_version,
                    target_delta=localized_freshness_delta,
                    radius_px=LOCAL_POINTER_FRESHNESS_RADIUS_PX,
                    max_delta=LOCAL_POINTER_FRESHNESS_MAX_DELTA,
                    policy_profile=self.config.policy.default_profile,
                )
            else:
                sr.trace.append(
                    "burst_stale",
                    planned=based_on_world_version,
                    current=frame.world_version,
                    target_delta=localized_freshness_delta,
                    local_check_enabled=local_freshness_enabled,
                )
                return {
                    "session_id": session_id,
                    "status": "stale_world",
                    "frame_id": frame.frame_id,
                    "world_version": frame.world_version,
                    "control_epoch": sr.control_epoch,
                    "screenshot_path": frame.image_path,
                    "width": frame.width,
                    "height": frame.height,
                    "localized_freshness": False,
                    "localized_freshness_delta": localized_freshness_delta,
                    "machine": machine,
                }
        planned_epoch = based_on_control_epoch
        if sr.control_epoch != planned_epoch:
            return {
                "session_id": session_id,
                "status": "control_changed",
                "control_epoch": sr.control_epoch,
                "machine": machine,
            }

        other_clients = self._other_client_count()
        if other_clients:
            if not sr.other_client_block_active:
                self._revoke_control_for_human(
                    sr,
                    event_kind="human_concurrency",
                    source="machine_client",
                    other_clients=other_clients,
                )
                sr.other_client_block_active = True
            return {
                "session_id": session_id,
                "status": "control_changed",
                "reason": "another machine client is connected",
                "control_epoch": sr.control_epoch,
                "human_concurrency": {"other_clients": other_clients},
                "machine": machine,
                "frame_id": frame.frame_id,
                "world_version": frame.world_version,
                "screenshot_path": frame.image_path,
                "width": frame.width,
                "height": frame.height,
            }
        sr.other_client_block_active = False

        grounded_actions = await self._ground_click_targets(actions, frame)
        matching_local_navigation_draft = (
            self._has_matching_local_navigation_draft(
                sr,
                grounded_actions,
                frame,
            )
        )
        draft_state = sr.verified_local_navigation_draft or {}
        local_navigation_draft = str(draft_state.get("text") or "")
        (
            observed_surface_text,
            verified_local_navigation_surface,
            verified_local_file_save_surface,
            verified_deferred_editor_surface,
        ) = await self._ground_keyboard_surface(
            grounded_actions,
            frame,
            local_navigation_draft=(
                local_navigation_draft
                if matching_local_navigation_draft
                else ""
            ),
            verified_same_frame_draft=(
                matching_local_navigation_draft
            ),
        )
        verdict = classify_direct_burst(
            grounded_actions,
            self.config.policy,
            observed_surface_text=observed_surface_text,
            verified_deferred_editor_surface=(
                verified_deferred_editor_surface
            ),
            verified_local_navigation_commit=(
                matching_local_navigation_draft
                and verified_local_navigation_surface
            ),
            verified_local_file_save_commit=(
                matching_local_navigation_draft
                and verified_local_file_save_surface
            ),
        )
        if verdict.status == "blocked":
            return {
                "session_id": session_id,
                "status": "blocked",
                "risk": verdict.category,
                "reason": verdict.reason,
                "control_epoch": sr.control_epoch,
                "frame_id": frame.frame_id,
                "world_version": frame.world_version,
                "screenshot_path": frame.image_path,
                "width": frame.width,
                "height": frame.height,
                "machine": machine,
            }
        if verdict.status == "approval_required" and _approved_digest != digest:
            approval_id = str(uuid.uuid4())
            request = {
                "kind": "direct_burst",
                "approval_id": approval_id,
                "session_id": session_id,
                "frame_id": frame.frame_id,
                "world_version": frame.world_version,
                "control_epoch": planned_epoch,
                "machine": machine,
                "risk": verdict.category,
                "reason": verdict.reason,
                "proposed_action": {
                    "actions": actions,
                    "grounded_actions": grounded_actions,
                    "max_runtime_ms": effective_runtime_ms,
                    "runtime_budget_source": runtime_budget_source,
                    "return_screenshot": return_screenshot,
                    "idempotency_key": key or None,
                    "digest": digest,
                },
                "screenshot_path": frame.image_path,
                "allowed_decisions": ["approve", "reject", "take_over"],
            }
            await self.store.save_approval(approval_id, session_id, request)
            sr.status = "needs_approval"
            await self.store.update_session(
                session_id,
                status="needs_approval",
            )
            result = {
                "session_id": session_id,
                "status": "needs_approval",
                "approval_request": request,
                "control_epoch": sr.control_epoch,
                "frame_id": frame.frame_id,
                "world_version": frame.world_version,
                "screenshot_path": frame.image_path,
                "width": frame.width,
                "height": frame.height,
                "machine": machine,
            }
            if key:
                sr.burst_idempotency[key] = {
                    "digest": digest,
                    "status": "approval_pending",
                    "result": copy.deepcopy(result),
                }
            return result

        def gate() -> bool:
            current_other_clients = self._other_client_count()
            if current_other_clients and not sr.other_client_block_active:
                self._revoke_control_for_human(
                    sr,
                    event_kind="human_concurrency",
                    source="machine_client",
                    other_clients=current_other_clients,
                )
                sr.other_client_block_active = True
            return (
                (not sr.stopped)
                and sr.control_epoch == planned_epoch
                and current_other_clients == 0
            )

        deadline = (
            time.monotonic() * 1000 + effective_runtime_ms
            if effective_runtime_ms
            else None
        )
        idempotency_lock = sr.burst_idempotency_locks.setdefault(
            key,
            asyncio.Lock(),
        )
        async with idempotency_lock:
            # A retry may have entered this method while the original request
            # was still typing, then blocked for minutes in frame capture or
            # keyboard-surface grounding. Recheck the completed receipt after
            # those awaits; otherwise the retry can miss both the original
            # in-flight claim and its newly cached result and emit HID again.
            prior = sr.burst_idempotency.get(key)
            if prior is not None:
                if prior["digest"] != digest:
                    return {
                        "session_id": session_id,
                        "status": "idempotency_conflict",
                        "error": (
                            "idempotency_key was already used for a "
                            "different burst"
                        ),
                        "control_epoch": sr.control_epoch,
                        "machine": machine,
                    }
                if (
                    prior.get("status") != "approval_pending"
                    or _approved_digest != digest
                ):
                    replay = copy.deepcopy(prior["result"])
                    replay["idempotent_replay"] = True
                    return replay
            inflight = sr.burst_inflight.get(key)
            if inflight is not None and inflight["digest"] != digest:
                return {
                    "session_id": session_id,
                    "status": "idempotency_conflict",
                    "error": (
                        "idempotency_key was already used for a "
                        "different burst"
                    ),
                    "control_epoch": sr.control_epoch,
                    "machine": machine,
                }
            joined_inflight = inflight is not None
            # The watched typer can't read back a synthetic FakeBackend screen,
            # so skip it under the fake (the verify path is unit-tested directly
            # in test_burst.py).
            typer = (
                None
                if os.environ.get("PIKVM_AGENT_FAKE")
                else getattr(self._executor, "typer", None)
            )
            if inflight is None:
                async def execute_once() -> _burst.BurstOutcome:
                    current_task = asyncio.current_task()
                    if current_task is not None:
                        self._active_hid_tasks.add(current_task)
                        self._hid_idle.clear()
                    try:
                        with DEBUG.span("burst.run", actions=len(actions)) as result:
                            outcome = await _burst.run_burst(
                                actions,
                                backend=self.backend,
                                should_continue=gate,
                                deadline_ms=deadline,
                                typer=typer,
                            )
                            result(
                                status=outcome.status,
                                completed=outcome.completed,
                                reason=outcome.reason,
                            )
                            return outcome
                    finally:
                        if current_task is not None:
                            self._active_hid_tasks.discard(current_task)
                            if not self._active_hid_tasks:
                                self._hid_idle.set()

                burst_task = asyncio.create_task(execute_once())
                inflight = {
                    "digest": digest,
                    "task": burst_task,
                }
                sr.burst_inflight[key] = inflight
            else:
                burst_task = inflight["task"]
        sr.status = "running"
        await self.store.update_session(session_id, status="running")
        try:
            outcome = await asyncio.shield(burst_task)
        except _burst.BurstError as exc:
            sr.status = "paused"
            failed = {
                "session_id": session_id,
                "status": "failed",
                "error": f"bad burst: {exc}",
                "control_epoch": sr.control_epoch,
                "machine": machine,
            }
            sr.burst_idempotency[key] = {
                "digest": digest,
                "status": "completed",
                "result": copy.deepcopy(failed),
            }
            if sr.burst_inflight.get(key) is inflight:
                sr.burst_inflight.pop(key, None)
            return failed

        post_settle_applied = False
        post_settle_stable: bool | None = None
        if (
            return_screenshot
            and outcome.status == "completed"
            and outcome.completed
            and _burst.needs_post_action_settle(actions)
            and not isinstance(self.backend, FakeBackend)
            and gate()
        ):
            post_settle_applied = True
            remaining_ms = (
                max(0, round(deadline - time.monotonic() * 1000))
                if deadline is not None
                else 1_200
            )
            grace_ms = min(250, remaining_ms)
            if grace_ms:
                await asyncio.sleep(grace_ms / 1000)
            remaining_ms = (
                max(0, round(deadline - time.monotonic() * 1000))
                if deadline is not None
                else 1_200
            )
            if remaining_ms and gate():
                post_settle_stable = await _burst.wait_for_stable_screen(
                    self.backend,
                    stable_ms=min(300, remaining_ms),
                    timeout_ms=min(1_200, remaining_ms),
                    should_continue=gate,
                )

        sr.trace.append("burst", status=outcome.status, completed=outcome.completed,
                        total=outcome.total, reason=outcome.reason, actions=outcome.executed,
                        post_settle_applied=post_settle_applied,
                        post_settle_stable=post_settle_stable)
        evidence_error = ""
        if return_screenshot:
            try:
                final = await sr.frames.capture()
            except Exception as exc:  # noqa: BLE001 - preserve the HID outcome
                final = None
                evidence_error = f"{type(exc).__name__}: {exc}"
                sr.trace.append(
                    "post_action_evidence_failed",
                    action_status=outcome.status,
                    completed=outcome.completed,
                    total=outcome.total,
                    error=evidence_error,
                )
        else:
            final = sr.frames.latest()
        self._update_verified_local_navigation_draft(
            sr,
            actions,
            outcome.action_receipts,
            final,
        )
        # A concurrent abort/panic owns the terminal state. Never let the request that
        # was in flight overwrite that sticky brake with a resumable "paused" status.
        effective_status = outcome.status
        effective_reason = outcome.reason
        if sr.stopped:
            sr.status = "failed"
            effective_status = "stopped"
            effective_reason = sr.error or outcome.reason or "stopped"
        else:
            sr.status = "paused"  # idle, awaiting the controller's next burst
            if evidence_error:
                effective_status = "failed"
                effective_reason = "post_action_evidence_failed"
        await self.store.update_session(
            session_id,
            status=sr.status,
            error=sr.error,
        )
        out: dict[str, Any] = {
            "session_id": session_id, "status": effective_status,
            "machine": machine,
            "completed_actions": outcome.completed, "remaining_actions": outcome.remaining,
            "partial_action": outcome.partial_action,
            "action_receipts": outcome.action_receipts,
            "reason": effective_reason or None,
            "error": (
                sr.error if sr.stopped else evidence_error or outcome.error
            ) or None,
            "action_status": outcome.status,
            "control_epoch": sr.control_epoch, "cursor": self._cursor_state(),
            "post_action_settle": {
                "applied": post_settle_applied,
                "stable": post_settle_stable,
            },
            "runtime_budget_ms": effective_runtime_ms,
            "runtime_budget_source": runtime_budget_source,
            "localized_freshness": localized_freshness,
            "localized_freshness_delta": localized_freshness_delta,
        }
        if joined_inflight:
            out["idempotent_inflight_replay"] = True
        current_other_clients = self._other_client_count()
        if current_other_clients:
            out["human_concurrency"] = {
                "other_clients": current_other_clients
            }
        if final is not None:
            out.update({"frame_id": final.frame_id, "world_version": final.world_version,
                        "screenshot_path": final.image_path,
                        "image_sha256": final.image_sha256,
                        "screen_hash": final.screen_hash,
                        "width": final.width, "height": final.height})
        if key:
            sr.burst_idempotency[key] = {
                "digest": digest,
                "status": "completed",
                "result": copy.deepcopy(out),
            }
            if sr.burst_inflight.get(key) is inflight:
                sr.burst_inflight.pop(key, None)
        return out

    async def _ground_click_targets(
        self, actions: list[dict[str, Any]], frame: Any
    ) -> list[dict[str, Any]]:
        """Read text around coordinate clicks so safety does not trust the caller alone."""
        grounded = copy.deepcopy(actions)
        ocr = getattr(self._screen_parser, "ocr", None)
        if ocr is None:
            return grounded
        for action in grounded:
            if action.get("type") not in ("click", "double_click"):
                continue
            try:
                x, y = int(action["x"]), int(action["y"])
                left, top = max(0, x - 180), max(0, y - 45)
                right, bottom = min(frame.width, x + 180), min(frame.height, y + 45)
                region = Region(
                    x=left,
                    y=top,
                    width=max(1, right - left),
                    height=max(1, bottom - top),
                )
                observed = await ocr.ocr(Path(frame.image_path), region=region)
                target_text = nearest_ocr_target_text(
                    observed,
                    click_x=x,
                    click_y=y,
                    region=region,
                )
                precise_ocr = getattr(ocr, "ocr_precise", None)
                if not target_text and callable(precise_ocr):
                    precise_left = max(0, x - 60)
                    precise_top = max(0, y - 35)
                    precise_right = min(frame.width, x + 60)
                    precise_bottom = min(frame.height, y + 35)
                    precise_region = Region(
                        x=precise_left,
                        y=precise_top,
                        width=max(1, precise_right - precise_left),
                        height=max(1, precise_bottom - precise_top),
                    )
                    observed = await precise_ocr(
                        Path(frame.image_path),
                        region=precise_region,
                    )
                    target_text = nearest_ocr_target_text(
                        observed,
                        click_x=x,
                        click_y=y,
                        region=precise_region,
                    )
                if target_text:
                    action["observed_target_text"] = target_text
            except Exception as exc:  # noqa: BLE001 - missing OCR must not break navigation
                log.debug("click target OCR failed: %s", exc)
        return grounded

    async def _ground_keyboard_surface(
        self,
        actions: list[dict[str, Any]],
        frame: Any,
        *,
        local_navigation_draft: str = "",
        verified_same_frame_draft: bool = False,
    ) -> tuple[str, bool, bool, bool]:
        """Ground one local commit as navigation or a local file save."""

        calculator = needs_calculator_surface_grounding(actions)
        deferred_editor = needs_deferred_exact_editor_surface_grounding(actions)
        safe_error_dismissal = (
            not local_navigation_draft
            and needs_safe_windows_error_dismissal_surface_grounding(actions)
        )
        local_file_overwrite = (
            not local_navigation_draft
            and needs_local_file_overwrite_surface_grounding(actions)
        )
        if (
            not calculator
            and not deferred_editor
            and not local_navigation_draft
            and not safe_error_dismissal
            and not local_file_overwrite
        ):
            return ("", False, False, False)
        ocr = getattr(self._screen_parser, "ocr", None)
        if ocr is None:
            return ("", False, False, False)
        try:
            observed = await ocr.ocr(Path(frame.image_path))
        except Exception:
            return ("", False, False, False)
        observed_text = str(observed.text or "")[:2_000]
        if deferred_editor:
            return (
                observed_text,
                False,
                False,
                _is_confirmed_blank_titleless_notepad_editor(
                    observed,
                    Path(frame.image_path),
                    frame_width=frame.width,
                    frame_height=frame.height,
                ),
            )
        if local_file_overwrite:
            precise_ocr = getattr(ocr, "ocr_precise", None)
            if not callable(precise_ocr):
                return (observed_text, False, False, False)
            try:
                precise = await precise_ocr(
                    Path(frame.image_path),
                    region=Region(
                        x=frame.width * 0.20,
                        y=frame.height * 0.20,
                        width=frame.width * 0.60,
                        height=frame.height * 0.60,
                    ),
                )
            except Exception:
                return (observed_text, False, False, False)
            return (
                f"{observed_text}\n{str(precise.text or '')[:2_000]}",
                False,
                False,
                False,
            )
        if safe_error_dismissal:
            precise_ocr = getattr(ocr, "ocr_precise", None)
            if not callable(precise_ocr):
                return (observed_text, False, False, False)
            click = next(
                (
                    action
                    for action in actions
                    if action.get("type") in {"click", "double_click"}
                ),
                None,
            )
            dialog_region = _safe_error_dialog_region(
                frame.width,
                frame.height,
                click,
            )
            try:
                precise = await precise_ocr(
                    Path(frame.image_path),
                    region=dialog_region,
                )
            except Exception:
                return (observed_text, False, False, False)
            return (
                f"{observed_text}\n{str(precise.text or '')[:2_000]}",
                False,
                False,
                False,
            )
        if calculator and is_confirmed_calculator_surface(observed_text):
            return (observed_text, False, False, False)
        if (
            local_navigation_draft == "This PC"
            and is_confirmed_file_explorer_surface(observed_text)
        ):
            return (observed_text, True, False, False)
        if is_safe_local_filename_draft(local_navigation_draft):
            precise_ocr = getattr(ocr, "ocr_precise", None)
            combined_text = observed_text
            if callable(precise_ocr):
                try:
                    precise = await precise_ocr(
                        Path(frame.image_path),
                        region=Region(
                            x=0,
                            y=0,
                            width=frame.width * 0.70,
                            height=frame.height * 0.65,
                        ),
                    )
                    combined_text = (
                        f"{observed_text}\n"
                        f"{str(precise.text or '')[:2_000]}"
                    )
                except Exception:
                    pass
            save_as_confirmed = is_confirmed_save_as_filename_surface(
                combined_text,
                draft_text=local_navigation_draft,
                verified_same_frame_draft=verified_same_frame_draft,
            )
            open_confirmed = is_confirmed_open_filename_surface(
                combined_text,
                draft_text=local_navigation_draft,
                verified_same_frame_draft=verified_same_frame_draft,
            )
            return (
                combined_text,
                save_as_confirmed or open_confirmed,
                save_as_confirmed,
                False,
            )
        precise_ocr = getattr(ocr, "ocr_precise", None)
        if not callable(precise_ocr):
            return (observed_text, False, False, False)
        if local_navigation_draft:
            if is_safe_local_navigation_target(local_navigation_draft):
                precise_region = Region(
                    x=frame.width * 0.05,
                    y=0,
                    width=frame.width * 0.90,
                    height=frame.height * 0.25,
                )
            else:
                precise_region = Region(
                    x=0,
                    y=frame.height * 0.68,
                    width=frame.width * 0.40,
                    height=frame.height * 0.32,
                )
        else:
            precise_region = Region(
                x=0,
                y=0,
                width=frame.width,
                height=max(1, frame.height * 0.25),
            )
        try:
            precise = await precise_ocr(
                Path(frame.image_path),
                region=precise_region,
            )
        except Exception:
            return (observed_text, False, False, False)
        precise_text = str(precise.text or "")[:2_000]
        if local_navigation_draft:
            combined_text = f"{observed_text}\n{precise_text}"
            if not is_safe_local_navigation_target(
                local_navigation_draft
            ):
                return (
                    combined_text,
                    is_confirmed_windows_run_surface(
                        observed_text,
                        draft_text=local_navigation_draft,
                        dialog_text=precise_text,
                        verified_same_frame_draft=(
                            verified_same_frame_draft
                        ),
                    ),
                    False,
                    False,
                )
            confirmed = is_confirmed_file_explorer_surface(
                (
                    combined_text
                    if local_navigation_draft == "This PC"
                    else observed_text
                ),
                draft_text=local_navigation_draft,
                top_band_text=precise_text,
                verified_same_frame_draft=verified_same_frame_draft,
            )
            return (combined_text, confirmed, False, False)
        if is_confirmed_calculator_surface(precise_text):
            return (precise_text, False, False, False)
        return (observed_text, False, False, False)

    @staticmethod
    def _has_matching_local_navigation_draft(
        sr: SessionRuntime,
        actions: list[dict[str, Any]],
        frame: Any,
    ) -> bool:
        draft = sr.verified_local_navigation_draft
        frame_screen_hash = str(
            getattr(frame, "screen_hash", "") or ""
        ).lower()
        frame_image_sha256 = str(
            getattr(frame, "image_sha256", "") or ""
        ).lower()
        return bool(
            draft
            and needs_local_navigation_surface_grounding(actions)
            and is_safe_local_commit_draft(
                str(draft.get("text") or "")
            )
            and draft.get("control_epoch") == sr.control_epoch
            and draft.get("world_version") == frame.world_version
            and len(str(draft.get("readback_frame_sha256") or "")) == 64
            and len(str(draft.get("post_action_image_sha256") or "")) == 64
            and len(frame_image_sha256) == 64
            and len(frame_screen_hash) == 512
            and screen_hashes_match_surface(
                str(draft.get("frame_screen_hash") or ""),
                frame_screen_hash,
            )
        )

    @staticmethod
    def _update_verified_local_navigation_draft(
        sr: SessionRuntime,
        actions: list[dict[str, Any]],
        receipts: list[dict[str, Any]],
        final_frame: Any | None,
    ) -> None:
        passive = {"wait", "wait_for_change", "wait_for_stable_screen"}
        active = [
            (index, action)
            for index, action in enumerate(actions)
            if action.get("type") not in passive
        ]
        if not active:
            return
        select_all = {"CTRL", "A"}
        exact_field_replacement = bool(
            len(active) == 2
            and active[0][1].get("type") == "key"
            and {
                str(key).strip().upper()
                for key in (
                    active[0][1].get("keys")
                    or [active[0][1].get("key")]
                )
                if key
            }
            == select_all
            and active[1][1].get("type") == "type_text"
        )
        if not (
            (len(active) == 1 and active[0][1].get("type") == "type_text")
            or exact_field_replacement
        ):
            sr.verified_local_navigation_draft = None
            return
        index, action = active[-1]
        receipt = next(
            (
                item
                for item in receipts
                if item.get("index") == index
            ),
            {},
        )
        frame_sha256 = str(
            receipt.get("readback_frame_sha256") or ""
        ).lower()
        text = str(action.get("text") or "")
        final_screen_hash = str(
            getattr(final_frame, "screen_hash", "") or ""
        ).lower()
        final_image_sha256 = str(
            getattr(final_frame, "image_sha256", "") or ""
        ).lower()
        exact = (
            action.get("context") == "field"
            and action.get("secret") is not True
            and is_safe_local_commit_draft(text)
            and receipt.get("status") == "verified_exact"
            and receipt.get("verdict") == "match"
            and receipt.get("focus_evidence") == "read_back_verified"
            and receipt.get("exact_readback_sha256_match") is True
            and receipt.get("emitted_exactly_once") is True
            and receipt.get("observed_text") == text
            and len(frame_sha256) == 64
            and len(final_screen_hash) == 512
            and len(final_image_sha256) == 64
        )
        sr.verified_local_navigation_draft = (
            {
                "text": text,
                "readback_frame_sha256": frame_sha256,
                "post_action_image_sha256": final_image_sha256,
                "frame_screen_hash": final_screen_hash,
                "world_version": final_frame.world_version,
                "control_epoch": sr.control_epoch,
            }
            if exact
            else None
        )

    # ---- on-demand perception (Layer 2 — OFF the hot path, opt-in) ------- #

    async def parse_screen_now(self, session_id: str) -> dict[str, Any]:
        """Run OmniParser + OCR on the CURRENT screen on demand (the controller calls this
        only when it's stuck). Returns grounded elements (id, kind, text, bbox, center) +
        full OCR text — so the controller can pick a click target by coordinate."""
        from pathlib import Path as _Path

        DEBUG.set_session(session_id)
        await self._ensure_omniparser()  # Layer-2 grounding needs OmniParser — spawn on demand
        sr = self._get(session_id)
        try:
            await self.backend.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("backend.connect failed: %s", exc)
        frame = await sr.frames.capture()
        with DEBUG.span("perception.parse"):
            em = await self._screen_parser.parse(_Path(frame.image_path), frame.frame_id,
                                                 frame.world_version)
        elements = [
            {"id": e.id, "kind": e.kind, "text": e.text or e.caption or "",
             "bbox": {"x": e.bbox.x, "y": e.bbox.y, "w": e.bbox.w, "h": e.bbox.h},
             "center": list(e.bbox.center())}
            for e in em.elements
        ]
        return {"session_id": session_id, "frame_id": frame.frame_id,
                "world_version": frame.world_version, "control_epoch": sr.control_epoch,
                "screenshot_path": frame.image_path,
                "image_sha256": frame.image_sha256,
                "screen_hash": frame.screen_hash,
                "elements": elements,
                "ocr_text": em.ocr_text}

    async def ocr_region(self, session_id: str, x: int, y: int, w: int, h: int) -> dict[str, Any]:
        """OCR a native-resolution crop and return confidence-bearing evidence."""
        from pikvm_agent.core.models import Region
        from pikvm_agent.vision.pikvm_ocr import PiKVMOcrProvider

        DEBUG.set_session(session_id)
        sr = self._get(session_id)
        try:
            await self.backend.connect()
        except Exception as exc:  # noqa: BLE001
            log.warning("backend.connect failed: %s", exc)
        frame = await sr.frames.capture()
        region = Region(x=x, y=y, width=w, height=h)
        with DEBUG.span("perception.ocr_region"):
            provider = self._screen_parser.ocr
            if isinstance(provider, PiKVMOcrProvider):
                res = await provider.ocr(None, region=region)
            else:
                # Use the exact frame already captured for this response. The
                # provider owns the crop, adds OCR context padding, and returns
                # evidence from the same frame ID instead of taking a second
                # potentially different screenshot.
                res = await provider.ocr(
                    Path(frame.image_path),
                    region=region,
                )
        confidences = [
            float(line.confidence)
            for line in res.lines
            if line.confidence is not None
        ]
        return {"session_id": session_id, "frame_id": frame.frame_id,
                "world_version": frame.world_version, "control_epoch": sr.control_epoch,
                "region": {"x": x, "y": y, "w": w, "h": h}, "text": res.text,
                "confidence": (
                    sum(confidences) / len(confidences) if confidences else None
                ),
                "lines": [line.model_dump() for line in res.lines]}

    async def find_text(self, session_id: str, text: str) -> dict[str, Any]:
        """Locate on-screen text: parse the screen and return the elements whose label
        contains ``text`` (with their click centers)."""
        parsed = await self.parse_screen_now(session_id)
        needle = (text or "").strip().lower()
        matches = [e for e in parsed["elements"] if needle and needle in (e["text"] or "").lower()]
        return {"session_id": session_id, "frame_id": parsed["frame_id"],
                "world_version": parsed["world_version"], "control_epoch": parsed["control_epoch"],
                "query": text, "matches": matches}

    @staticmethod
    def _budget_fields(max_transactions: int | None, max_runtime_ms: int | None) -> dict[str, Any]:
        deadline = (time.monotonic() * 1000 + max_runtime_ms) if max_runtime_ms else 0
        return {"tx_this_call": 0,
                "max_transactions": max_transactions if max_transactions is not None else 0,
                "deadline_ms": deadline}

    async def submit_approval(self, session_id: str, approval_id: str,
                             decision: dict) -> dict[str, Any]:
        """Resume a paused graph with the human's approval decision."""
        DEBUG.set_session(session_id)
        sr = self._get(session_id)
        # Validate the id matches THIS session's pending approval before resuming —
        # a stale/mistyped id must never approve the current pending action.
        appr = await self.store.get_approval(approval_id)
        if appr is None or appr.get("session_id") != session_id or appr.get("status") != "pending":
            return {"session_id": session_id, "approval_id": approval_id, "status": "error",
                    "error": "unknown or already-resolved approval_id for this session"}
        request = appr.get("request") or {}
        if request.get("kind") == "direct_burst":
            response_type = str(decision.get("type", ""))
            proposed = request.get("proposed_action") or {}
            key = str(proposed.get("idempotency_key") or "")
            if response_type != "approve":
                status_word = "rejected" if response_type == "reject" else "resolved"
                await self.store.resolve_approval(approval_id, decision, status_word)
                result = {
                    "session_id": session_id,
                    "approval_id": approval_id,
                    "status": "blocked",
                    "reason": decision.get("reason") or f"human {response_type or 'resolved'}",
                    "control_epoch": sr.control_epoch,
                    "machine": sr.machine,
                }
                if key and key in sr.burst_idempotency:
                    sr.burst_idempotency[key]["status"] = status_word
                    sr.burst_idempotency[key]["result"] = copy.deepcopy(result)
                sr.status = "paused"
                await self.store.update_session(session_id, status="paused")
                return result

            result = await self.run_burst(
                session_id,
                proposed.get("actions") or [],
                based_on_world_version=request.get("world_version"),
                based_on_control_epoch=request.get("control_epoch"),
                max_runtime_ms=(
                    None
                    if proposed.get("runtime_budget_source") == "auto"
                    else int(proposed.get("max_runtime_ms", 4000))
                ),
                return_screenshot=bool(proposed.get("return_screenshot", True)),
                idempotency_key=key or None,
                _approved_digest=str(proposed.get("digest") or ""),
            )
            resolved_status = (
                "approved" if result.get("status") == "completed" else "approved_not_executed"
            )
            await self.store.resolve_approval(approval_id, decision, resolved_status)
            return result
        result = await self._graph.ainvoke(Command(resume=decision), self._graph_config(sr))
        status_word = "approved" if decision.get("type") == "approve" else decision.get("type", "resolved")
        try:
            await self.store.resolve_approval(approval_id, decision, status_word)
        except Exception as exc:  # noqa: BLE001
            log.warning("resolve_approval failed: %s", exc)
        return await self._after_run(sr, result)

    async def _after_run(self, sr: SessionRuntime, result: dict[str, Any]) -> dict[str, Any]:
        base = {
            "session_id": sr.session_id, "task": sr.task,
            "machine": sr.machine,
            "frame_id": result.get("frame_id"), "world_version": result.get("world_version"),
            "screenshot_path": result.get("frame_path"), "step": result.get("step", 0),
        }
        # If a panic / abort landed WHILE this graph run was in flight, the run may return
        # a stale "paused"/"done"/"needs_approval" — the latch wins, force it terminal so
        # the emergency stop can't be overwritten by an already-running invocation.
        if sr.stopped:
            sr.status = "failed"
            sr.error = sr.error or "stopped"
            await self.store.update_session(sr.session_id, status="failed", error=sr.error)
            return {**base, "status": "failed", "error": sr.error}
        if "__interrupt__" in result:
            itr = result["__interrupt__"]
            val = getattr(itr[0], "value", None) if itr else None
            # A budget pause is a RESUMABLE checkpoint, not an approval — report it as
            # "paused" so the next continue resumes the loop (rather than awaiting input).
            if isinstance(val, dict) and val.get("reason") == "budget_paused":
                sr.status = "paused"
                await self.store.update_session(sr.session_id, status="paused")
                return {**base, "status": "paused"}
            appr = result.get("approval_request") or {}
            sr.status = "needs_approval"
            if appr.get("approval_id"):
                await self.store.save_approval(appr["approval_id"], sr.session_id, appr)
            await self.store.update_session(sr.session_id, status="needs_approval")
            return {**base, "status": "needs_approval", "approval_request": appr}
        status = result.get("status", "done")
        sr.status = status
        sr.error = result.get("error", "")
        await self.store.update_session(sr.session_id, status=status, error=sr.error)
        return {**base, "status": status, "error": sr.error}

    # ---- console support -------------------------------------------------- #

    async def list_sessions(self) -> list[dict[str, Any]]:
        rows = await self.store.list_sessions()
        live = {sid: sr.status for sid, sr in self._sessions.items()}
        for row in rows:
            row["live_status"] = live.get(row["id"], row["status"])
        return rows

    async def status(self) -> dict[str, Any]:
        """Readiness snapshot for UIs. The daemon is up if this responds; we report
        each dependency the daemon needs to actually drive a session:

          * pikvm       — the target host (reachable?)
          * omniparser  — element grounding (enabled/required/reachable; lags the
                          daemon by minutes on the first GPU boot)
          * operator    — the planner LLM (provider + whether its API key is set)
          * ocr         — the read-back engine (provider + whether it's installed)
          * store       — the local session/checkpoint sqlite (connected at boot)

        ``ok`` is True only when every REQUIRED dependency is satisfied (the target
        reachable, the operator configured, and OmniParser reachable when required).
        """
        cfg = self.config

        # Serve a recent snapshot — the readiness pill polls this every few seconds and
        # the health probes are network calls; recomputing each time piled up slow /status
        # requests that contended with everything else (panel polls, the loop).
        cached = self._status_cache
        if cached is not None and (time.monotonic() - cached[0]) < 3.0:
            return cached[1]

        async def _probe(coro: Any) -> bool:
            # Hard-bound each probe: a busy OmniParser (mid GPU-parse) or a slow PiKVM
            # must not make /status take many seconds.
            try:
                return bool(await asyncio.wait_for(coro, timeout=2.0))
            except Exception:  # noqa: BLE001 - a probe failure/timeout is just "not ready"
                return False

        probes = [_probe(self.backend.health())]
        if self._omniparser is not None:
            probes.append(_probe(self._omniparser.healthy()))
        results = await asyncio.gather(*probes)
        pikvm_ok = results[0]
        omni_ok = results[1] if self._omniparser is not None else False

        op = cfg.operator
        operator = {
            "provider": op.provider,
            "configured": op.provider == "fake" or op.api_key is not None,
            "routing": op.routing.model_dump(),
        }

        ocr_provider = cfg.ocr.provider
        if ocr_provider == "paddleocr":
            ocr_available = paddleocr_available()
        elif ocr_provider == "hybrid":
            ocr_available = (
                paddleocr_available()
                and tesseract_available()
            )
        elif ocr_provider == "tesseract":
            ocr_available = tesseract_available()
        else:  # "pikvm" — uses the target's built-in OCR, so it tracks pikvm reachability
            ocr_available = True

        warmup_task = self._ocr_warmup_task
        if warmup_task is None:
            ocr_warmup = "not_supported"
        elif not warmup_task.done():
            ocr_warmup = "warming"
        elif warmup_task.cancelled():
            ocr_warmup = "cancelled"
        else:
            try:
                ocr_warmup = "ready" if warmup_task.result() else "degraded"
            except Exception:
                ocr_warmup = "degraded"
        ocr_dependency: dict[str, Any] = {
            "provider": ocr_provider,
            "available": ocr_available,
            "warmup": ocr_warmup,
        }
        diagnostics = getattr(self._ocr_provider, "diagnostics", None)
        if callable(diagnostics):
            try:
                ocr_dependency["diagnostics"] = diagnostics()
            except Exception:
                pass

        deps: dict[str, Any] = {
            "pikvm": {"reachable": pikvm_ok},
            "omniparser": {
                "enabled": cfg.omniparser.enabled,
                "required": cfg.omniparser.required,
                "reachable": omni_ok,
            },
            "operator": operator,
            "ocr": ocr_dependency,
            "store": {"connected": True},
        }
        # Burst-first: the daemon can drive the machine the moment the TARGET is reachable.
        # OmniParser + the operator LLM are only for the opt-in Layer-2 (perception) and
        # Layer-3 (autonomous) paths, so they're REPORTED as dependencies but don't gate
        # overall readiness — the default direct-control path needs neither.
        deps["operator"]["needed_for"] = "autonomous mode only"
        deps["omniparser"]["needed_for"] = "on-demand perception / autonomous mode only"
        ready = pikvm_ok
        result = {
            "ok": ready,
            "machine": self.machine_identity(),
            "dependencies": deps,
        }
        self._status_cache = (time.monotonic(), result)
        return result

    def latest_frame_path(self, session_id: str) -> str | None:
        sr = self._sessions.get(session_id)
        if sr is None:
            return None
        frame = sr.frames.latest()
        return frame.image_path if frame else None

    async def pending_approvals(self, session_id: str) -> list[dict[str, Any]]:
        self._get(session_id)
        return await self.store.pending_approvals(session_id)

    def recent_trace(self, session_id: str, limit: int = 40) -> list[dict[str, Any]]:
        sr = self._get(session_id)
        return sr.trace.read()[-limit:]

    async def export_memory_update(self, session_id: str) -> dict[str, Any]:
        """Produce a safe Atlas memory-update proposal from the session trace.

        Returns a redacted markdown page + structured incident (no screenshots,
        secrets, credentials, or verbatim typed/message bodies) for Claude/Codex
        to write to Atlas via the atlas MCP tools."""
        from pikvm_agent.memory.atlas_export import build_memory_update

        sr = self._get(session_id)
        mu = build_memory_update(session_id, sr.task, sr.trace.read(), status=sr.status)
        return mu.model_dump()
