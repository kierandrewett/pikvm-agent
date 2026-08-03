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
from pikvm_agent.core.windows_launch import (
    is_safe_windows_run_text,
    is_verified_windows_run_launch,
)
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

_CALCULATOR_DIGIT_KEYS = {
    *(f"DIGIT{digit}" for digit in range(10)),
    *(f"NUMPAD{digit}" for digit in range(10)),
}
_CALCULATOR_OPERATOR_KEYS = {
    "NUMPADADD",
    "NUMPADSUBTRACT",
    "NUMPADMULTIPLY",
    "NUMPADDIVIDE",
}
_CALCULATOR_DECIMAL_KEYS = {"NUMPADDECIMAL", "PERIOD"}
_COMMIT_KEYS = {"ENTER", "RETURN", "NUMPADENTER"}
_WINDOWS_LOCAL_PATH = re.compile(
    r"^[A-Za-z]:\\[^<>:\"/|?*\x00-\x1f]{1,256}$"
)
_WINDOWS_LOCAL_FILENAME = re.compile(
    r"^[^<>:\"/\\|?*\x00-\x1f]{1,128}$"
)
_WINDOWS_RESERVED_FILENAMES = re.compile(
    r"^(?:con|prn|aux|nul|com[1-9]|lpt[1-9])(?:\..*)?$",
    re.IGNORECASE,
)


def is_safe_local_navigation_target(text: str) -> bool:
    """Recognise one bounded, absolute Windows location without traversal."""

    if text == "This PC":
        return True
    if _WINDOWS_LOCAL_PATH.fullmatch(text) is None:
        return False
    parts = text[3:].split("\\")
    return bool(
        parts
        and all(
            part
            and part not in {".", ".."}
            and not part.endswith((" ", "."))
            for part in parts
        )
    )


def is_safe_local_filename_draft(text: str) -> bool:
    """Recognise one ordinary Windows basename for a grounded Save As dialog."""

    return bool(
        _WINDOWS_LOCAL_FILENAME.fullmatch(text)
        and text not in {".", ".."}
        and not text.endswith((" ", "."))
        and not is_safe_windows_run_text(text)
        and _WINDOWS_RESERVED_FILENAMES.fullmatch(text) is None
    )


def is_safe_local_commit_draft(text: str) -> bool:
    """Recognise text that one independently grounded Enter may commit."""

    return (
        is_safe_local_navigation_target(text)
        or is_safe_local_filename_draft(text)
        or is_safe_windows_run_text(text)
    )


def needs_calculator_surface_grounding(actions: list[dict]) -> bool:
    """Return whether a key-only burst resembles one calculator expression."""

    active_actions = [
        action
        for action in actions
        if action.get("type")
        not in {"wait", "wait_for_change", "wait_for_stable_screen"}
    ]
    if not active_actions or any(
        action.get("type") != "key" for action in active_actions
    ):
        return False
    keys = [
        str(key).strip().upper()
        for action in active_actions
        for key in (action.get("keys") or [action.get("key")])
        if key
    ]
    if not keys or keys[-1] not in _COMMIT_KEYS:
        return False
    expression_keys = keys[:-1]
    return (
        len(keys) == len(active_actions)
        and sum(key in _CALCULATOR_DIGIT_KEYS for key in expression_keys) >= 2
        and any(key in _CALCULATOR_OPERATOR_KEYS for key in expression_keys)
        and all(
            key
            in (
                _CALCULATOR_DIGIT_KEYS
                | _CALCULATOR_OPERATOR_KEYS
                | _CALCULATOR_DECIMAL_KEYS
            )
            for key in expression_keys
        )
    )


def is_confirmed_calculator_surface(observed_surface_text: str) -> bool:
    """Return whether independent OCR identifies Windows Calculator."""

    text = " ".join(observed_surface_text.casefold().split())
    return (
        "calculator" in text
        and "standard" in text
    ) or (
        "standard" in text
        and "history" in text
        and "memory" in text
    )


def needs_deferred_exact_editor_surface_grounding(
    actions: list[dict],
) -> bool:
    """Identify one inert multiline code draft whose Enters need editor proof."""

    active = [
        action
        for action in actions
        if action.get("type")
        not in {"wait", "wait_for_change", "wait_for_stable_screen"}
    ]
    typed = [action for action in active if action.get("type") == "type_text"]
    if (
        len(typed) < 2
        or not active
        or active[0].get("type") != "type_text"
    ):
        return False
    for action in active:
        if action.get("type") == "type_text":
            if not (
                action.get("code") is True
                and str(action.get("context") or "").casefold() == "editor"
                and str(action.get("verification") or "").casefold()
                == "deferred_exact"
            ):
                return False
            continue
        if action.get("type") != "key":
            return False
        keys = {
            str(key).strip().upper()
            for key in (action.get("keys") or [action.get("key")])
            if key
        }
        if keys not in ({"ENTER"}, {"RETURN"}):
            return False
    return all(
        left.get("type") != right.get("type")
        or left.get("type") == "key"
        for left, right in zip(active, active[1:])
    )


def is_confirmed_notepad_editor_surface(observed_surface_text: str) -> bool:
    """Require independent title and editor-chrome evidence for bare newlines."""

    text = " ".join(observed_surface_text.casefold().split())
    title_and_chrome = "notepad" in text and (
        all(marker in text for marker in ("file", "edit", "view"))
        or all(marker in text for marker in ("ln ", "col", "characters"))
    )
    # Windows 11's tabbed Notepad can omit the product name entirely. Its
    # foreground status row still exposes a distinctive, independently OCRed
    # text-mode signature. Requiring all three tokens avoids treating generic
    # File/Edit/View menus or communications surfaces as an editor.
    titleless_text_mode_chrome = all(
        marker in text
        for marker in ("plain text", "windows (crlf)", "utf-8")
    )
    return title_and_chrome or titleless_text_mode_chrome


def needs_local_navigation_surface_grounding(
    actions: list[dict],
) -> bool:
    """Return whether a burst is one bare commit plus passive observation."""

    active_actions = [
        action
        for action in actions
        if action.get("type")
        not in {"wait", "wait_for_change", "wait_for_stable_screen"}
    ]
    if len(active_actions) != 1 or active_actions[0].get("type") != "key":
        return False
    keys = {
        str(key).strip().upper()
        for key in (
            active_actions[0].get("keys")
            or [active_actions[0].get("key")]
        )
        if key
    }
    return keys in ({"ENTER"}, {"RETURN"}, {"NUMPADENTER"})


def needs_local_file_overwrite_surface_grounding(
    actions: list[dict],
) -> bool:
    """Return whether one grounded commit may target a Save As replacement."""

    if needs_local_navigation_surface_grounding(actions):
        return True
    active_actions = [
        action
        for action in actions
        if action.get("type")
        not in {"wait", "wait_for_change", "wait_for_stable_screen"}
    ]
    if (
        len(active_actions) != 1
        or active_actions[0].get("type") not in {"click", "double_click"}
    ):
        return False
    target = re.sub(
        r"[^a-z]+",
        "",
        str(active_actions[0].get("observed_target_text") or "").casefold(),
    )
    return target in {"yes", "replace"}


def is_confirmed_local_file_overwrite_surface(
    actions: list[dict],
    observed_surface_text: str,
) -> bool:
    """Identify the bounded Windows Save As replacement confirmation."""

    if not needs_local_file_overwrite_surface_grounding(actions):
        return False
    text = observed_surface_text.casefold().replace("’", "'")
    normalized = re.sub(r"[^a-z0-9.]+", " ", text).strip()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    dangerous_surface = any(
        pattern.search(text)
        for pattern in (
            _COMMUNICATION,
            _DELETE,
            _FINANCIAL,
            _PERMISSIONS,
            _INSTALL,
            _CREDENTIAL,
            _CONSENT,
            _EXTERNAL_UPLOAD,
            _POWER,
            _DISABLE_SECURITY,
        )
    )
    return bool(
        not dangerous_surface
        and "confirm save as" in normalized
        and "already" in compact
        and any(
            marker in compact
            for marker in ("exists", "easts", "casts")
        )
        and "doyouwanttoreplace" in compact
        and re.search(r"\byes\b", normalized)
        and re.search(r"\bno\b", normalized)
    )


def _is_communication_compose_surface(text: str) -> bool:
    return bool(
        any(
            marker in text
            for marker in (
                "new message",
                "replying to",
                "message compose",
                "new email",
            )
        )
        and "send" in text
    )


def _save_as_surface_markers(text: str) -> set[str]:
    compact = re.sub(r"[^a-z0-9]+", "", text)
    markers = {
        marker
        for marker, compact_marker in (
            ("file name", "filename"),
            ("save as type", "saveastype"),
            ("new folder", "newfolder"),
            ("encoding", "encoding"),
            ("this pc", "thispc"),
            ("date modified", "datemodified"),
            ("name", "name"),
            ("size", "size"),
        )
        if marker in text or compact_marker in compact
    }
    if (
        re.search(r"\b(?:s[a-z]?ve|ave)\s*as\b", text)
        or "saveas" in compact
    ):
        markers.add("save as")
    return markers


def is_confirmed_file_explorer_surface(
    observed_surface_text: str,
    *,
    draft_text: str = "This PC",
    top_band_text: str = "",
    verified_same_frame_draft: bool = False,
) -> bool:
    """Confirm Explorer or Save As around one exact local-navigation draft."""

    text = " ".join(observed_surface_text.casefold().split())
    if not is_safe_local_navigation_target(draft_text):
        return False
    if draft_text != "This PC":
        top_band = " ".join(top_band_text.casefold().split())
        if _is_communication_compose_surface(text):
            return False
        marker_text = f"{text}\n{top_band}"
        file_picker_markers = _save_as_surface_markers(marker_text)
        return (
            (
                draft_text.casefold() in top_band
                or verified_same_frame_draft
            )
            and "save as" in file_picker_markers
            and len(file_picker_markers) >= 2
        )
    if "this pc" not in text:
        return False
    markers = {
        marker
        for marker in (
            "home",
            "gallery",
            "onedrive",
            "downloads",
            "documents",
            "desktop",
            "pictures",
            "this pc",
            "search home",
        )
        if marker in text
    }
    return (
        "file explorer" in text and len(markers) >= 2
    ) or (
        "quick access" in text and len(markers) >= 3
    )


def is_confirmed_save_as_filename_surface(
    observed_surface_text: str,
    *,
    draft_text: str,
    verified_same_frame_draft: bool = False,
) -> bool:
    """Confirm a native Save As surface around one exact basename read-back."""

    if (
        not verified_same_frame_draft
        or not is_safe_local_filename_draft(draft_text)
    ):
        return False
    text = " ".join(observed_surface_text.casefold().split())
    if _is_communication_compose_surface(text):
        return False
    markers = _save_as_surface_markers(text)
    return "save as" in markers and len(markers) >= 2


def is_confirmed_open_filename_surface(
    observed_surface_text: str,
    *,
    draft_text: str,
    verified_same_frame_draft: bool = False,
) -> bool:
    """Confirm a native Open picker around one exact basename read-back."""

    if (
        not verified_same_frame_draft
        or not is_safe_local_filename_draft(draft_text)
    ):
        return False
    text = " ".join(observed_surface_text.casefold().split())
    if _is_communication_compose_surface(text):
        return False
    markers = _save_as_surface_markers(text)
    open_visible = re.search(r"\bopen\b", text) is not None
    file_list_markers = markers & {
        "new folder",
        "this pc",
        "date modified",
        "name",
        "size",
    }
    return bool(
        open_visible
        and "new folder" in file_list_markers
        and len(file_list_markers) >= 3
    )


def is_confirmed_windows_run_surface(
    observed_surface_text: str,
    *,
    draft_text: str,
    dialog_text: str = "",
    verified_same_frame_draft: bool = False,
) -> bool:
    """Confirm the Windows Run dialog around one exact allowlisted draft."""

    if not is_safe_windows_run_text(draft_text):
        return False
    text = " ".join(
        f"{observed_surface_text}\n{dialog_text}".casefold().split()
    )
    normalized = re.sub(r"[^a-z0-9:-]+", " ", text).strip()
    communication_surface = (
        any(
            marker in normalized
            for marker in (
                "new message",
                "replying to",
                "message compose",
                "new email",
            )
        )
        and "send" in normalized
    )
    controls = {
        marker
        for marker in ("open", "ok", "cancel", "browse")
        if re.search(rf"\b{marker}\b", normalized)
    }
    instruction_markers = {
        marker
        for marker in (
            "program",
            "folder",
            "document",
            "internet",
            "resource",
            "windows",
        )
        if re.search(rf"\b{marker}\b", normalized)
    }
    draft_visible = bool(
        re.search(
            rf"\b{re.escape(draft_text.casefold())}\b",
            normalized,
        )
    )
    return bool(
        not communication_surface
        and (draft_visible or verified_same_frame_draft)
        and re.search(r"\brun\b", normalized)
        and "type the name of" in normalized
        and len(instruction_markers) >= 4
        and len(controls) >= 3
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
_TERMINAL_SYSTEM_SETTING = re.compile(
    r"^\s*gsettings\s+(?:set|reset|reset-recursively)\b",
    re.IGNORECASE,
)
_MARKUP_TOKEN = re.compile(
    r"(?:"
    r"<!--(?:(?!-->).)*-->|"
    r"<!doctype\s+[A-Za-z][^<>]*>|"
    r"<\?[A-Za-z][^<>]*\?>|"
    r"</?[A-Za-z][A-Za-z0-9:._-]*"
    r"(?:\s+(?:[^<>\"']+|\"[^\"]*\"|'[^']*')*)?\s*/?>"
    r")",
    re.IGNORECASE,
)


def _semantic_text(action: dict) -> str:
    return " ".join(
        str(action.get(key, ""))
        for key in ("observed_target_text", "target_text", "intent", "label")
    ).strip()


def _is_structured_markup_editor_literal(action: dict, text: str) -> bool:
    """Recognise bounded markup source without trusting editor metadata alone."""

    if (
        action.get("type") != "type_text"
        or str(action.get("context", "")).casefold() != "editor"
        or action.get("code") is not True
        or "\n" in text
        or "\r" in text
    ):
        return False
    stripped = text.strip()
    if not stripped.startswith("<") or not stripped.endswith(">"):
        return False

    cursor = 0
    matched = False
    for token in _MARKUP_TOKEN.finditer(stripped):
        between = stripped[cursor : token.start()]
        if "<" in between or ">" in between:
            return False
        matched = True
        cursor = token.end()
    tail = stripped[cursor:]
    return matched and "<" not in tail and ">" not in tail


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


def needs_safe_windows_error_dismissal_surface_grounding(
    actions: list[dict],
) -> bool:
    """Identify one dismissal input that still needs independent dialog OCR."""

    active_actions = [
        action
        for action in actions
        if action.get("type")
        not in {"wait", "wait_for_change", "wait_for_stable_screen"}
    ]
    if len(active_actions) != 1:
        return False
    active = active_actions[0]
    if active.get("type") == "key":
        keys = {
            str(key).strip().upper()
            for key in (active.get("keys") or [active.get("key")])
            if key
        }
        return keys in ({"ENTER"}, {"RETURN"}, {"NUMPADENTER"})
    if active.get("type") not in {"click", "double_click"}:
        return False
    tokens = re.findall(r"[A-Za-z0-9]+", _semantic_text(active))
    return bool(
        len(tokens) == 1
        and len(tokens[0]) <= 3
        and _token_distance(tokens[0], "ok") <= 1
    )


def is_confirmed_safe_windows_error_dismissal(
    actions: list[dict],
    observed_surface_text: str,
) -> bool:
    """Allow only a harmless dismissal on a fully identified missing-file error."""

    if not needs_safe_windows_error_dismissal_surface_grounding(actions):
        return False
    text = observed_surface_text.casefold().replace("’", "'")
    normalized = re.sub(r"[^a-z0-9]+", " ", text).strip()
    compact = re.sub(r"[^a-z0-9]+", "", text)
    active = next(
        action
        for action in actions
        if action.get("type")
        not in {"wait", "wait_for_change", "wait_for_stable_screen"}
    )
    grounded_ok_click = active.get("type") in {"click", "double_click"}
    local_path_visible = re.search(r"\b[a-z]:\\", text) is not None
    dangerous_surface = any(
        pattern.search(text)
        for pattern in (
            _COMMUNICATION,
            _DELETE,
            _FINANCIAL,
            _PERMISSIONS,
            _INSTALL,
            _CREDENTIAL,
            _CONSENT,
            _EXTERNAL_UPLOAD,
            _POWER,
            _DISABLE_SECURITY,
        )
    )
    explorer_error = bool(
        "file explorer" in normalized
        and re.search(
            r"\b[vw]i{1,2}ndows can(?:not| t) find\b",
            normalized,
        )
        and "checkthespellingandtryagain" in compact
    )
    notepad_missing_file = bool(
        "notepad" in normalized
        and re.search(r"\bcannot find the\b", normalized)
        and (re.search(r"\bfile\b", normalized) or local_path_visible)
        and (grounded_ok_click or re.search(r"\bok\b", normalized))
    )
    return not dangerous_surface and (
        explorer_error or notepad_missing_file
    )


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


def _medium_terminal_candidate(command: str) -> tuple[str, str, str]:
    if _TERMINAL_SYSTEM_SETTING.search(command):
        return (
            "system_setting_change",
            "medium",
            "terminal system-setting change requires human review",
        )
    return (
        "terminal_mutating",
        "medium",
        "mutating terminal command requires human review",
    )


def classify_direct_burst(
    actions: list[dict],
    policy: PolicyConfig,
    *,
    observed_surface_text: str = "",
    verified_local_navigation_commit: bool = False,
    verified_local_file_save_commit: bool = False,
) -> DirectBurstVerdict:
    """Classify a burst independently of the model-provided intent.

    Typed text is treated as a command only when it is dangerous on its face or
    the action says it is going to a terminal. Clicks/keypresses consume target
    text supplied by the caller or observed locally with OCR.
    """
    candidates: list[tuple[str, str, str]] = []
    terminal_groups, grouped_terminal_indexes = _terminal_text_groups(actions)
    safe_windows_run_launch = is_verified_windows_run_launch(actions)
    terminal_commands = (
        [] if safe_windows_run_launch else terminal_groups
    )
    verified_calculator_expression = (
        needs_calculator_surface_grounding(actions)
        and is_confirmed_calculator_surface(observed_surface_text)
    )
    verified_deferred_editor_newlines = (
        needs_deferred_exact_editor_surface_grounding(actions)
        and is_confirmed_notepad_editor_surface(observed_surface_text)
    )
    verified_safe_error_dismissal = (
        is_confirmed_safe_windows_error_dismissal(
            actions,
            observed_surface_text,
        )
    )
    verified_local_file_overwrite = (
        is_confirmed_local_file_overwrite_surface(
            actions,
            observed_surface_text,
        )
    )
    for command in terminal_commands:
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
            candidates.append(_medium_terminal_candidate(command))

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
            if (
                keys in ({"ENTER"}, {"RETURN"}, {"NUMPADENTER"})
                and not safe_windows_run_launch
                and not verified_calculator_expression
                and not verified_deferred_editor_newlines
                and not verified_safe_error_dismissal
            ):
                if verified_local_file_save_commit:
                    candidates.append(
                        (
                            "local_file_edit",
                            "medium",
                            "Save As commit requires human review",
                        )
                    )
                elif verified_local_navigation_commit:
                    pass
                elif verified_local_file_overwrite:
                    candidates.append(
                        (
                            "local_file_edit",
                            "medium",
                            "file replacement requires human review",
                        )
                    )
                else:
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
            safe_error_dismissal = (
                verified_safe_error_dismissal
                and action.get("type") in {"click", "double_click"}
                and semantic == ("unknown", "medium")
            )
            confirmed_overwrite_click = (
                verified_local_file_overwrite
                and action.get("type") in {"click", "double_click"}
                and semantic == ("unknown", "medium")
            )
            if confirmed_overwrite_click:
                candidates.append(
                    (
                        "local_file_edit",
                        "medium",
                        "file replacement requires human review",
                    )
                )
            elif not safe_error_dismissal:
                candidates.append(
                    (
                        semantic[0],
                        semantic[1],
                        "commit target requires human review",
                    )
                )
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
        elif command_risk == "side_effect" and not _is_structured_markup_editor_literal(
            action,
            text,
        ):
            candidates.append(
                ("communication_send", "medium", "side-effecting command requires human review")
            )
        elif terminal_context and command_risk == "medium":
            candidates.append(_medium_terminal_candidate(text))

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
