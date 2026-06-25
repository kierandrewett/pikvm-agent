"""Pydantic domain models — the data contracts of the runtime.

These are the *evidence* and *decision* records that flow between the daemon, the
graph nodes, the operator, the policy engine, and the executor. Vision and
operator adapters import the canonical types from here so there is one contract,
not several.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal, Union

from pydantic import BaseModel, ConfigDict, Field

# --------------------------------------------------------------------------- #
# Enumerations (kept as Literals so they serialize as plain strings)
# --------------------------------------------------------------------------- #

RiskLevel = Literal["low", "medium", "high"]

RiskCategory = Literal[
    "navigation",
    "text_entry",
    "read_only_inspection",
    "local_file_edit",
    "terminal_read_only",
    "terminal_mutating",
    "communication_draft",
    "communication_send",
    "credential_entry",
    "sensitive_data_view",
    "sensitive_data_transmit",
    "account_or_permission_change",
    "software_installation",
    "system_setting_change",
    "power_or_firmware",
    "disk_or_partition",
    "financial_or_purchase",
    "legal_or_consent",
    "unknown",
]

# Detected application / interaction mode (governs which actions are allowed).
Mode = Literal[
    "unknown",
    "windows.desktop",
    "windows.start_search",
    "windows.update_modal",
    "system.notification",
    "vscode.editor",
    "vscode.quick_open",
    "vscode.terminal",
    "terminal.readline",
    "terminal.pager",
    "browser.page",
    "browser.address_bar",
    "browser.form",
    "outlook.inbox",
    "outlook.compose",
    "teams.chat",
    "teams.compose",
    "credential_prompt",
    "captcha_or_human_verification",
    "installer.disk_selection",
    "bios.uefi",
]

ElementKind = Literal[
    "button",
    "input",
    "editor",
    "terminal_prompt",
    "menu_item",
    "tab",
    "toast",
    "modal",
    "notification",
    "close_button",
    "send_button",
    "text",
    "unknown",
]

SessionStatus = Literal[
    "running",
    "needs_approval",
    "human_takeover",
    "blocked",
    "failed",
    "done",
]

# Text-entry verification outcomes. The verifier classifies; nothing else may.
VerificationStatus = Literal[
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
]

VERIFIED_STATUSES: frozenset[str] = frozenset(
    {"verified_exact", "verified_safe_normalized", "verified_with_warnings"}
)
"""Only these mean 'typed and verified'. Never claim success without one."""

# --------------------------------------------------------------------------- #
# Geometry
# --------------------------------------------------------------------------- #


class BBox(BaseModel):
    """An axis-aligned box in frame-pixel space (origin top-left)."""

    x: int
    y: int
    w: int
    h: int

    def center(self) -> tuple[int, int]:
        return (self.x + self.w // 2, self.y + self.h // 2)

    def area(self) -> int:
        return max(0, self.w) * max(0, self.h)


# --------------------------------------------------------------------------- #
# Keyboard / frame
# --------------------------------------------------------------------------- #


class KeyboardState(BaseModel):
    """What we know about the target keyboard at capture time."""

    layout: Literal["us", "uk", "unknown"] = "unknown"
    caps_lock: bool | None = None
    num_lock: bool | None = None
    scroll_lock: bool | None = None
    online: bool | None = None


class FrameRecord(BaseModel):
    """A captured video frame plus everything we know about the world at capture.

    `world_version` is the plan-invalidation counter: it is bumped whenever a
    meaningful change is detected (new app, modal, toast over target, keyboard
    state, pager, black screen, focus mode). A decision is only valid against the
    exact `(frame_id, world_version)` it was planned on.
    """

    frame_id: int
    world_version: int
    captured_at: str  # ISO-8601 UTC
    monotonic_ms: int
    image_path: str
    image_sha256: str = ""
    screen_hash: str = ""  # perceptual fingerprint hex
    width: int = 0
    height: int = 0
    active_app_guess: str = "unknown"
    mode_guess: Mode = "unknown"
    keyboard_state: KeyboardState = Field(default_factory=KeyboardState)
    age_ms: int = 0


# --------------------------------------------------------------------------- #
# Vision evidence
# --------------------------------------------------------------------------- #


class OCRLine(BaseModel):
    text: str
    confidence: float | None = None
    bbox: list[int] | list[list[int]] | None = None
    raw: dict[str, Any] | None = None


class OCRResult(BaseModel):
    lines: list[OCRLine] = Field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(line.text for line in self.lines)


class VisualElement(BaseModel):
    """A grounded, addressable UI element. The operator chooses these by `id`;
    the harness only resolves an id to coordinates if it is still valid."""

    id: str
    frame_id: int
    world_version: int
    bbox: BBox
    kind: ElementKind = "unknown"
    text: str | None = None
    caption: str | None = None
    confidence: float = 0.0
    source: list[str] = Field(default_factory=list)
    app_hint: str | None = None


class ElementMap(BaseModel):
    frame_id: int
    world_version: int
    elements: list[VisualElement] = Field(default_factory=list)
    ocr_text: str = ""

    def by_id(self, element_id: str) -> VisualElement | None:
        for el in self.elements:
            if el.id == element_id:
                return el
        return None


class DetectedState(BaseModel):
    """The runtime's read of where we are, fed to the operator and the policy."""

    active_app: str = "unknown"
    mode: Mode = "unknown"
    keyboard: KeyboardState = Field(default_factory=KeyboardState)
    blocking_events: list[str] = Field(default_factory=list)
    confidence: float = 0.0


# --------------------------------------------------------------------------- #
# Operator decision schema (discriminated-union actions)
# --------------------------------------------------------------------------- #


class RiskAssessment(BaseModel):
    level: RiskLevel
    category: RiskCategory
    requires_human: bool
    reason: str = ""


class KeypressAction(BaseModel):
    type: Literal["keypress"]
    keys: list[str]


class TypeTextAction(BaseModel):
    type: Literal["type_text"]
    text: str
    # A type action never submits; an explicit keypress(Enter) is separate.


class ClickElementAction(BaseModel):
    type: Literal["click_element"]
    element_id: str | None = None
    locator: dict[str, Any] | None = None
    intent: str = ""


class ScrollAction(BaseModel):
    # E10: a scroll carries a real direction+amount and must never become (0,0).
    type: Literal["scroll"]
    direction: Literal["up", "down", "left", "right"]
    amount: int = Field(default=3, ge=1, le=50)


class WaitAction(BaseModel):
    type: Literal["wait"]
    ms: int = Field(ge=50, le=5000)


class WaitForModeAction(BaseModel):
    type: Literal["wait_for_mode"]
    mode: str
    timeout_ms: int = Field(ge=100, le=10000)


Action = Annotated[
    Union[
        KeypressAction,
        TypeTextAction,
        ClickElementAction,
        ScrollAction,
        WaitAction,
        WaitForModeAction,
    ],
    Field(discriminator="type"),
]


class OperatorDecision(BaseModel):
    """The operator's proposal. It proposes; it never executes."""

    based_on_frame_id: int
    based_on_world_version: int
    intent: str
    state_assessment: dict[str, Any] = Field(default_factory=dict)
    risk: RiskAssessment
    preconditions: dict[str, Any] = Field(default_factory=dict)
    actions: list[Action]
    postconditions: dict[str, Any] = Field(default_factory=dict)
    fallback: str | None = None


class OperatorRequest(BaseModel):
    """Everything the multimodal operator is shown for one decision."""

    task: str
    frame: dict[str, Any]
    detected_state: dict[str, Any] = Field(default_factory=dict)
    visual_elements: list[dict[str, Any]] = Field(default_factory=list)
    recent_events: list[dict[str, Any]] = Field(default_factory=list)
    retrieved_playbooks: list[dict[str, Any]] = Field(default_factory=list)
    policy: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Policy / approval
# --------------------------------------------------------------------------- #


class PolicyResult(BaseModel):
    status: Literal["allowed", "blocked", "approval_required"]
    category: RiskCategory | None = None
    level: RiskLevel | None = None
    requires_human: bool = False
    blocked: bool = False
    reason: str = ""


class ApprovalRequest(BaseModel):
    approval_id: str
    session_id: str
    frame_id: int
    world_version: int
    risk: str
    reason: str
    proposed_action: dict[str, Any]
    screenshot_path: str | None = None
    allowed_decisions: list[str] = Field(
        default_factory=lambda: ["approve", "edit", "reject", "respond"]
    )


class ApprovalResponse(BaseModel):
    type: Literal["approve", "edit", "reject", "respond", "abort", "take_over"]
    instruction: str | None = None
    message: str | None = None
    reason: str | None = None


# --------------------------------------------------------------------------- #
# Guarded transaction + result
# --------------------------------------------------------------------------- #


class GuardedTransaction(BaseModel):
    """A validated, freshness-stamped, policy-cleared unit of execution."""

    id: str
    session_id: str
    based_on_frame_id: int
    based_on_world_version: int
    intent: str
    actions: list[Action]
    postconditions: dict[str, Any] = Field(default_factory=dict)
    risk: RiskAssessment
    approval_id: str | None = None


class VerificationResult(BaseModel):
    status: VerificationStatus
    safe_to_continue: bool
    intended: str | None = None
    observed: str | None = None
    detail: str = ""

    @property
    def verified(self) -> bool:
        return self.status in VERIFIED_STATUSES


class TransactionResult(BaseModel):
    status: Literal[
        "executed",
        "verified",
        "failed_stale_frame",
        "blocked_by_policy",
        "needs_approval",
        "failed",
    ]
    executed_actions: list[dict[str, Any]] = Field(default_factory=list)
    verification: VerificationResult | None = None
    screenshot_path: str | None = None
    world_version_after: int | None = None
    error: str = ""


# --------------------------------------------------------------------------- #
# Eval / replay metrics (OSWorld-style — track from day one)
# --------------------------------------------------------------------------- #


class SessionMetrics(BaseModel):
    model_config = ConfigDict(extra="allow")

    task_success: bool = False
    unsafe_action_blocked: int = 0
    human_escalation_count: int = 0
    stale_frame_refusals: int = 0
    typing_mismatch_count: int = 0
    wrong_region_count: int = 0
    operator_calls: int = 0
    screenshots_taken: int = 0
    transactions_executed: int = 0
    wall_clock_ms: int = 0
    operator_cost_estimate: float = 0.0
