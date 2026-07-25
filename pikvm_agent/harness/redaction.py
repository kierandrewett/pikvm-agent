"""Shared secret redaction for durable and operator-visible harness records."""

from __future__ import annotations

from typing import Any

REDACTED_VALUE = "••••••••"
_ALWAYS_SECRET_KEYS = {
    "access_token",
    "api_key",
    "authorization",
    "password",
    "passcode",
    "secret_value",
}
_SECRET_PAYLOAD_KEYS = {"content", "text", "value"}


def redact_secrets(value: Any) -> Any:
    """Return a deep copy with credentials and secret-marked payloads removed."""
    if isinstance(value, list):
        return [redact_secrets(item) for item in value]
    if not isinstance(value, dict):
        return value

    secret_value = value.get("secret") is True
    redacted: dict[str, Any] = {}
    for key, item in value.items():
        normalized = str(key).casefold()
        if normalized in _ALWAYS_SECRET_KEYS:
            redacted[key] = REDACTED_VALUE
        elif secret_value and normalized in _SECRET_PAYLOAD_KEYS:
            redacted[key] = REDACTED_VALUE
        else:
            redacted[key] = redact_secrets(item)
    if secret_value:
        redacted["redacted"] = True
    return redacted
