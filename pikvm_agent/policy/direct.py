"""Hard-coded safety classification for controller-authored HID bursts.

The autonomous graph already gates semantic actions. Direct MCP control is a
different execution path, so it must independently inspect typed command text
and the visible/declared target of commit-like clicks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Literal

from pikvm_agent.config import PolicyConfig
from pikvm_agent.policy.risk import classify_command

DirectStatus = Literal["allowed", "blocked", "approval_required"]


@dataclass(frozen=True)
class DirectBurstVerdict:
    status: DirectStatus
    category: str = ""
    level: str = "low"
    reason: str = ""


_COMMUNICATION = re.compile(
    r"\b(send|submit|reply|email|message|post|publish|tweet|comment|broadcast|"
    r"share|invite|rsvp|forward|join\s+(?:meeting|call)|start\s+call|"
    r"(?:accept|decline|cancel)\s+meeting)\b",
    re.IGNORECASE,
)
_DELETE = re.compile(
    r"\b(delete|remove|trash|erase|purge|discard|empty\s+(?:recycle\s+bin|trash))\b",
    re.IGNORECASE,
)
_FINANCIAL = re.compile(
    r"\b(pay|purchase|buy|place\s+order|checkout|subscribe|renew\s+subscription|"
    r"confirm\s+(?:order|payment)|donate|transfer\s+(?:funds|money)|tip)\b",
    re.IGNORECASE,
)
_PERMISSIONS = re.compile(
    r"\b(grant|revoke|permission|admin\s+access|make\s+admin|role\s+change|"
    r"add\s+member|remove\s+member|transfer\s+ownership|change\s+owner|"
    r"allow\s+access|deny\s+access|approve\s+request)\b",
    re.IGNORECASE,
)
_INSTALL = re.compile(r"\b(install|update\s+now|allow\s+extension)\b", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"\b(sign\s*in|log\s*in|password|passcode|api\s+key|access\s+token)\b",
    re.IGNORECASE,
)
_CONSENT = re.compile(
    r"\b(accept\s+terms|agree|consent|authorize|sign\s+document)\b",
    re.IGNORECASE,
)
_EXTERNAL_UPLOAD = re.compile(
    r"\b(upload|attach\s+(?:file|document)|import\s+to\s+cloud)\b",
    re.IGNORECASE,
)
_POWER = re.compile(
    r"\b(restart(?:\s+now)?|reboot|shut\s*down|power\s*off|factory\s+reset|"
    r"turn\s+off\s+(?:computer|device|machine))\b",
    re.IGNORECASE,
)
_LOCAL_EDIT = re.compile(
    r"\b(save(?:\s+as)?|rename|overwrite|replace\s+(?:all|in\s+files|file|existing)|"
    r"move\s+(?:file|folder))\b",
    re.IGNORECASE,
)
_SYSTEM_SETTING = re.compile(
    r"\b(apply\s+changes|enable|disable|turn\s+(?:on|off)|reset\s+settings|"
    r"change\s+settings?)\b",
    re.IGNORECASE,
)
_DISABLE_SECURITY = re.compile(
    r"\b((?:disable|turn\s+off|stop)\s+(?:security|firewall|antivirus|"
    r"defender|protection)|bypass\s+(?:security|protection))\b",
    re.IGNORECASE,
)
_AMBIGUOUS_COMMIT = re.compile(
    r"\b(?:ok|okay|continue|done|finish|yes|confirm)\b",
    re.IGNORECASE,
)

_FUZZY_COMMIT_TERMS: tuple[tuple[str, str, str], ...] = (
    ("delete", "delete", "high"),
    ("remove", "delete", "high"),
    ("purchase", "financial_or_purchase", "high"),
    ("checkout", "financial_or_purchase", "high"),
    ("grant", "account_or_permission_change", "high"),
    ("revoke", "account_or_permission_change", "high"),
    ("permission", "account_or_permission_change", "high"),
    ("install", "software_installation", "medium"),
    ("upload", "file_external_upload", "high"),
    ("submit", "communication_send", "medium"),
    ("publish", "communication_send", "medium"),
    ("message", "communication_send", "medium"),
)
_FUZZY_COMMIT_PHRASES: tuple[tuple[str, str, str], ...] = (
    ("disable security", "disable_security", "high"),
    ("turn off firewall", "disable_security", "high"),
    ("disable antivirus", "disable_security", "high"),
    ("bypass protection", "disable_security", "high"),
    ("grant access", "account_or_permission_change", "high"),
    ("revoke permission", "account_or_permission_change", "high"),
    ("add member", "account_or_permission_change", "high"),
    ("transfer ownership", "account_or_permission_change", "high"),
    ("allow access", "account_or_permission_change", "high"),
    ("approve request", "account_or_permission_change", "high"),
    ("delete record", "delete", "high"),
    ("remove file", "delete", "high"),
    ("empty recycle bin", "delete", "high"),
    ("discard changes", "delete", "high"),
    ("purge data", "delete", "high"),
    ("pay now", "financial_or_purchase", "high"),
    ("purchase item", "financial_or_purchase", "high"),
    ("checkout", "financial_or_purchase", "high"),
    ("donate now", "financial_or_purchase", "high"),
    ("transfer funds", "financial_or_purchase", "high"),
    ("renew subscription", "financial_or_purchase", "high"),
    ("install update", "software_installation", "medium"),
    ("update now", "software_installation", "medium"),
    ("allow extension", "software_installation", "medium"),
    ("sign in", "credential_entry", "high"),
    ("log in", "credential_entry", "high"),
    ("enter password", "credential_entry", "high"),
    ("accept terms", "legal_or_consent", "high"),
    ("authorize access", "legal_or_consent", "high"),
    ("sign document", "legal_or_consent", "high"),
    ("upload file", "file_external_upload", "high"),
    ("attach file", "file_external_upload", "high"),
    ("restart now", "power_or_firmware", "high"),
    ("shut down", "power_or_firmware", "high"),
    ("factory reset", "power_or_firmware", "high"),
    ("power off", "power_or_firmware", "high"),
    ("save as", "local_file_edit", "medium"),
    ("rename file", "local_file_edit", "medium"),
    ("overwrite existing", "local_file_edit", "medium"),
    ("move file", "local_file_edit", "medium"),
    ("replace all", "local_file_edit", "medium"),
    ("replace in files", "local_file_edit", "medium"),
    ("replace", "local_file_edit", "medium"),
    ("save", "local_file_edit", "medium"),
    ("apply changes", "system_setting_change", "medium"),
    ("enable feature", "system_setting_change", "medium"),
    ("turn on setting", "system_setting_change", "medium"),
    ("reset settings", "system_setting_change", "medium"),
    ("send message", "communication_send", "medium"),
    ("submit form", "communication_send", "medium"),
    ("forward email", "communication_send", "medium"),
    ("join meeting", "communication_send", "medium"),
    ("start call", "communication_send", "medium"),
    ("publish post", "communication_send", "medium"),
    ("share document", "communication_send", "medium"),
    ("invite guest", "communication_send", "medium"),
    ("send teams message", "communication_send", "medium"),
    ("send email", "communication_send", "medium"),
    ("reply all", "communication_send", "medium"),
    ("send meeting invite", "communication_send", "medium"),
    ("post channel message", "communication_send", "medium"),
    ("accept meeting", "communication_send", "medium"),
    ("decline meeting", "communication_send", "medium"),
    ("cancel meeting", "communication_send", "medium"),
    ("continue", "unknown", "medium"),
    ("confirm", "unknown", "medium"),
    ("done", "unknown", "medium"),
    ("ok", "unknown", "medium"),
)
_SHELL_LAUNCHER = re.compile(
    r"^\s*(?:"
    r"(?:powershell|pwsh|cmd)(?:\.exe)?\b|"
    r"(?:ba|z|k|fi)?sh\b|"
    r"wsl(?:\.exe)?\b|"
    r"(?:sudo\s+)?(?:apt|apt-get|dnf|yum|pacman|brew|winget|choco)\b"
    r")",
    re.IGNORECASE,
)


def _semantic_text(action: dict) -> str:
    return " ".join(
        str(action.get(key, ""))
        for key in ("observed_target_text", "target_text", "intent", "label")
    ).strip()


def _edit_distance(left: str, right: str) -> int:
    """Small Damerau-Levenshtein distance for OCR-corrupted labels."""

    if left == right:
        return 0
    if not left:
        return len(right)
    if not right:
        return len(left)
    matrix = [
        [0 for _ in range(len(right) + 1)]
        for _ in range(len(left) + 1)
    ]
    for row in range(len(left) + 1):
        matrix[row][0] = row
    for column in range(len(right) + 1):
        matrix[0][column] = column
    for row, left_char in enumerate(left, start=1):
        for column, right_char in enumerate(right, start=1):
            matrix[row][column] = min(
                matrix[row - 1][column] + 1,
                matrix[row][column - 1] + 1,
                matrix[row - 1][column - 1] + (left_char != right_char),
            )
            if (
                row > 1
                and column > 1
                and left[row - 1] == right[column - 2]
                and left[row - 2] == right[column - 1]
            ):
                matrix[row][column] = min(
                    matrix[row][column],
                    matrix[row - 2][column - 2] + 1,
                )
    return matrix[-1][-1]


def _ocr_token_variants(raw: str) -> set[str]:
    base = raw.casefold().replace("0", "o").replace("1", "l")
    variants = {
        base,
        base.replace("rn", "m"),
    }
    capital_i_as_l = "".join("l" if char == "I" else char for char in raw).casefold()
    variants.add(capital_i_as_l)
    if capital_i_as_l.startswith("ln"):
        variants.add("i" + capital_i_as_l[1:])
    return variants


def _token_distance(raw: str, expected: str) -> int:
    distance = min(
        _edit_distance(variant, expected)
        for variant in _ocr_token_variants(raw)
    )
    # OCR commonly renders one `d` glyph as `cl`. Accept that compound
    # substitution only when it produces the exact expected token; otherwise
    # ordinary words such as Close would become fuzzy matches for Done.
    cl_as_d = raw.casefold().replace("cl", "d")
    if cl_as_d == expected:
        return 0
    return distance


def _fuzzy_phrase_category(text: str) -> tuple[str, str] | None:
    token_segments = [
        re.findall(r"[A-Za-z0-9]+", segment)
        for segment in re.split(r"[·|:\n]", text)
    ]
    for phrase, category, level in _FUZZY_COMMIT_PHRASES:
        expected_tokens = phrase.split()
        width = len(expected_tokens)
        for tokens in token_segments:
            # Short single-word commit labels such as OK/Done/Save are too
            # collision-prone to fuzzy-match inside ordinary prose.
            if width == 1 and len(tokens) != 1:
                continue
            for start in range(len(tokens) - width + 1):
                observed_tokens = tokens[start : start + width]
                if sum(
                    _token_distance(observed, expected)
                    for observed, expected in zip(
                        observed_tokens,
                        expected_tokens,
                        strict=True,
                    )
                ) <= 1:
                    return category, level
    return None


def _fuzzy_category_from_semantics(text: str) -> tuple[str, str] | None:
    tokens = re.findall(r"[A-Za-z0-9]+", text)
    for raw_token in tokens:
        for token in _ocr_token_variants(raw_token):
            for expected, category, level in _FUZZY_COMMIT_TERMS:
                maximum = 2 if min(len(token), len(expected)) >= 5 else 1
                if abs(len(token) - len(expected)) <= maximum and _edit_distance(
                    token, expected
                ) <= maximum:
                    return category, level
    return None


def _category_from_semantics(text: str) -> tuple[str, str] | None:
    if _DISABLE_SECURITY.search(text):
        return ("disable_security", "high")
    if _FINANCIAL.search(text):
        return ("financial_or_purchase", "high")
    if _PERMISSIONS.search(text):
        return ("account_or_permission_change", "high")
    if _DELETE.search(text):
        return ("delete", "high")
    if _INSTALL.search(text):
        return ("software_installation", "medium")
    if _CREDENTIAL.search(text):
        return ("credential_entry", "high")
    if _CONSENT.search(text):
        return ("legal_or_consent", "high")
    if _EXTERNAL_UPLOAD.search(text):
        return ("file_external_upload", "high")
    if _POWER.search(text):
        return ("power_or_firmware", "high")
    if _LOCAL_EDIT.search(text):
        return ("local_file_edit", "medium")
    fuzzy_phrase = _fuzzy_phrase_category(text)
    if fuzzy_phrase is not None:
        return fuzzy_phrase
    if _SYSTEM_SETTING.search(text):
        return ("system_setting_change", "medium")
    if _COMMUNICATION.search(text):
        return ("communication_send", "medium")
    if _AMBIGUOUS_COMMIT.search(text):
        return ("unknown", "medium")
    return _fuzzy_category_from_semantics(text)


def _terminal_text_groups(
    actions: list[dict],
) -> tuple[list[str], set[int]]:
    """Reconstruct commands split across adjacent watched-typing actions."""

    groups: list[str] = []
    grouped_indexes: set[int] = set()
    fragments: list[str] = []
    fragment_indexes: list[int] = []
    run_dialog_opened = False

    def flush() -> None:
        nonlocal fragments, fragment_indexes
        if fragments:
            groups.append("".join(fragments))
            grouped_indexes.update(fragment_indexes)
            fragments = []
            fragment_indexes = []

    for index, action in enumerate(actions):
        if action.get("type") == "key":
            keys = {
                str(key).strip().upper()
                for key in (action.get("keys") or [action.get("key")])
                if key
            }
            has_meta = bool(
                keys
                & {
                    "META",
                    "WIN",
                    "WINDOWS",
                    "SUPER",
                    "METALEFT",
                    "METARIGHT",
                }
            )
            if has_meta and keys & {"R", "KEYR"}:
                run_dialog_opened = True

        if action.get("type") != "type_text":
            flush()
            continue
        text = str(action.get("text", ""))
        context = str(action.get("context", "")).lower()
        terminal_hint = (
            context == "terminal"
            or run_dialog_opened
            or _SHELL_LAUNCHER.search(text) is not None
        )
        if terminal_hint or fragments:
            fragments.append(text)
            fragment_indexes.append(index)
        else:
            flush()
    flush()
    return groups, grouped_indexes


def classify_direct_burst(
    actions: list[dict], policy: PolicyConfig
) -> DirectBurstVerdict:
    """Classify a burst independently of the model-provided intent.

    Typed text is treated as a command only when it is dangerous on its face or
    the action says it is going to a terminal. Clicks/keypresses consume target
    text supplied by the caller or observed locally with OCR.
    """
    candidates: list[tuple[str, str, str]] = []
    terminal_groups, grouped_terminal_indexes = _terminal_text_groups(actions)
    for command in terminal_groups:
        command_risk = classify_command(command)
        if command_risk == "dangerous":
            candidates.append(
                (
                    "terminal_mutating",
                    "high",
                    "dangerous command text requires human review",
                )
            )
        elif command_risk == "side_effect":
            candidates.append(
                (
                    "communication_send",
                    "medium",
                    "side-effecting command requires human review",
                )
            )
        elif command_risk == "medium":
            candidates.append(
                (
                    "terminal_mutating",
                    "medium",
                    "mutating terminal command requires human review",
                )
            )

    run_dialog_opened = False
    for index, action in enumerate(actions):
        if action.get("type") == "spreadsheet_grid":
            candidates.append(
                (
                    "local_file_edit",
                    "medium",
                    "structured spreadsheet entry requires human review",
                )
            )

        if action.get("type") == "key":
            keys = {
                str(key).strip().upper()
                for key in (action.get("keys") or [action.get("key")])
                if key
            }
            has_control = bool(
                keys
                & {
                    "CTRL",
                    "CONTROL",
                    "CONTROLLEFT",
                    "CONTROLRIGHT",
                    "LCTRL",
                    "RCTRL",
                }
            )
            has_alt = bool(
                keys
                & {
                    "ALT",
                    "ALTLEFT",
                    "ALTRIGHT",
                    "LALT",
                    "RALT",
                    "OPTION",
                }
            )
            if has_control and keys & {"S", "KEYS"}:
                candidates.append(
                    (
                        "local_file_edit",
                        "medium",
                        "save shortcut requires human review",
                    )
                )
            if has_control and keys & {"X", "KEYX", "V", "KEYV"}:
                candidates.append(
                    (
                        "local_file_edit",
                        "medium",
                        "cut/paste shortcut requires human review",
                    )
                )
            if has_control and keys & {"Z", "KEYZ", "Y", "KEYY"}:
                candidates.append(
                    (
                        "local_file_edit",
                        "medium",
                        "undo/redo shortcut requires human review",
                    )
                )
            if has_control and keys & {"ENTER", "RETURN"}:
                candidates.append(
                    (
                        "communication_send",
                        "medium",
                        "commit shortcut requires human review",
                    )
                )
            if has_alt and keys & {"S", "KEYS"}:
                candidates.append(
                    (
                        "communication_send",
                        "medium",
                        "application send shortcut requires human review",
                    )
                )
            if keys in ({"ENTER"}, {"RETURN"}):
                candidates.append(
                    (
                        "unknown",
                        "medium",
                        "bare Enter/Return may commit the focused surface",
                    )
                )
            has_meta = bool(
                keys
                & {
                    "META",
                    "WIN",
                    "WINDOWS",
                    "SUPER",
                    "METALEFT",
                    "METARIGHT",
                }
            )
            if has_meta and keys & {"R", "KEYR"}:
                run_dialog_opened = True

        semantic = _category_from_semantics(_semantic_text(action))
        if semantic is not None:
            candidates.append((semantic[0], semantic[1], "commit target requires human review"))
        if (
            action.get("type") in {"click", "double_click"}
            and not str(action.get("observed_target_text", "")).strip()
        ):
            candidates.append(
                (
                    "unknown",
                    "medium",
                    "coordinate click target could not be independently read",
                )
            )

        if action.get("type") != "type_text":
            continue
        if index in grouped_terminal_indexes:
            continue
        text = str(action.get("text", ""))
        command_risk = classify_command(text)
        context = str(action.get("context", "")).lower()
        terminal_context = (
            context == "terminal"
            or run_dialog_opened
            or _SHELL_LAUNCHER.search(text) is not None
        )
        if command_risk == "dangerous":
            candidates.append(
                ("terminal_mutating", "high", "dangerous command text requires human review")
            )
        elif command_risk == "side_effect":
            candidates.append(
                ("communication_send", "medium", "side-effecting command requires human review")
            )
        elif terminal_context and command_risk == "medium":
            candidates.append(
                ("terminal_mutating", "medium", "mutating terminal command requires human review")
            )

    if not candidates:
        return DirectBurstVerdict("allowed")

    precedence = {
        "local_file_edit": 1,
        "communication_send": 1,
        "system_setting_change": 2,
        "software_installation": 2,
        "file_external_upload": 3,
        "account_or_permission_change": 3,
        "terminal_mutating": 4,
        "power_or_firmware": 5,
        "disable_security": 6,
        "financial_or_purchase": 5,
        "delete": 6,
        "credential_entry": 5,
        "legal_or_consent": 5,
        "unknown": 1,
    }
    category, level, reason = max(candidates, key=lambda item: precedence.get(item[0], 0))
    if category in set(policy.always_block):
        return DirectBurstVerdict("blocked", category, level, reason)
    if category in set(policy.require_human_for):
        return DirectBurstVerdict("approval_required", category, level, reason)
    return DirectBurstVerdict("allowed", category, level, reason)
