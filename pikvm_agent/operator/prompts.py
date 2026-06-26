"""Operator prompt assembly.

Pure text/JSON construction: the mandatory safety rules (the system message) and
the request payload the multimodal model is shown. No network calls — vision
content blocks and the HTTP transport are wired up with the OpenRouter client in
a later phase; for now the request rides as text-JSON in a single user message.
"""

from __future__ import annotations

import json
from typing import Any

from pikvm_agent.core.models import OperatorRequest

__all__ = [
    "OPERATOR_SYSTEM_RULES",
    "build_request_payload",
    "build_messages",
]

# The mandatory rules every operator prompt must carry (docs/PLAN.md →
# "Operator prompt rules"). Hard-coded here; never prompt-only safety relies on
# this, but it states the contract the model is held to.
OPERATOR_SYSTEM_RULES: str = (
    "You are controlling a physical computer through PiKVM raw video, raw "
    "keyboard, and raw mouse.\n"
    "You do not have DOM, accessibility APIs, browser DevTools, OS APIs, or "
    "application APIs.\n"
    "Prefer keyboard shortcuts and visual element IDs over raw coordinates.\n"
    "Return only valid JSON matching the schema.\n"
    "Every decision must reference based_on_frame_id and based_on_world_version.\n"
    "Never send, submit, delete, purchase, authenticate, change security "
    "settings, enter credentials, or perform destructive actions without human "
    "approval.\n"
    "Escalate when uncertain.\n\n"
    "Return ONLY a JSON object (no prose, no markdown, no code fences) of this shape:\n"
    '{\n'
    '  "based_on_frame_id": <int>,          // the frame id you were shown\n'
    '  "based_on_world_version": <int>,     // the world_version you were shown\n'
    '  "intent": "<short goal for this step; start with \'DONE\' when the task is complete>",\n'
    '  "risk": {"level": "low|medium|high", "category": "<short>", "requires_human": <bool>, "reason": "<why>"},\n'
    '  "actions": [<zero or more actions, see below>],\n'
    '  "state_assessment": {}, "preconditions": {}, "postconditions": {}, "fallback": null\n'
    '}\n'
    "Each action is exactly ONE of:\n"
    '  {"type": "keypress", "keys": ["ControlLeft", "KeyS"]}\n'
    '  {"type": "type_text", "text": "..."}              // never submits; add a separate keypress Enter\n'
    '  {"type": "click_element", "element_id": "e3"}     // use a visual element id (or "locator": {...})\n'
    '  {"type": "scroll", "direction": "up|down|left|right", "amount": <1-50>}\n'
    '  {"type": "wait", "ms": <50-5000>}\n'
    '  {"type": "wait_for_mode", "mode": "<mode>", "timeout_ms": <100-10000>}\n'
    "An empty actions array (or an intent beginning with 'DONE') means the task is complete."
)


def build_request_payload(request: OperatorRequest) -> dict[str, Any]:
    """The JSON object the model is shown for one decision.

    Mirrors the shape in docs/PLAN.md → "Operator prompt rules": task, frame,
    detected_state, visual_elements, recent_events, retrieved_playbooks, policy.
    """
    frame = request.frame
    # NOTE: the screenshot is attached as a multimodal image_url block by the client —
    # it must NOT be duplicated here as a giant base64 string in the text JSON (that
    # bloated every request and sent the image twice).
    return {
        "task": request.task,
        "frame": {
            "id": frame.get("id"),
            "world_version": frame.get("world_version"),
            "age_ms": frame.get("age_ms", 0),
        },
        "detected_state": request.detected_state,
        "visual_elements": request.visual_elements,
        "recent_events": request.recent_events,
        "retrieved_playbooks": request.retrieved_playbooks,
        "policy": request.policy,
    }


def build_messages(request: OperatorRequest) -> list[dict[str, Any]]:
    """Chat messages for the operator call.

    System message carries the mandatory rules; the user message carries the
    request payload as text-JSON. Vision content blocks are added with the HTTP
    client later; keeping it text-JSON here makes the assembly pure + testable.
    """
    return [
        {"role": "system", "content": OPERATOR_SYSTEM_RULES},
        {"role": "user", "content": json.dumps(build_request_payload(request))},
    ]
