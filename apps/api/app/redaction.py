from __future__ import annotations

import re
from typing import Any

REDACTED = "[redacted]"
INTERNAL_REFERENCE_KEYS = {
    "id",
    "request_id",
    "source_reference",
    "manifest_hash",
    "external_id",
    "gmail_thread_id",
    "gmail_message_id",
    "history_id",
    "provider_id",
    "import_id",
    "batch_id",
    "manifest_id",
}
_UUID_PATTERN = re.compile(
    r"(?<![0-9A-Fa-f])"
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[1-5][0-9A-Fa-f]{3}-"
    r"[89ABab][0-9A-Fa-f]{3}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])"
)
_SHA256_PATTERN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")


def _is_internal_key(key: str) -> bool:
    return key in INTERNAL_REFERENCE_KEYS or key.endswith("_id")


def _redact_text(value: str) -> str:
    return _UUID_PATTERN.sub(REDACTED, _SHA256_PATTERN.sub(REDACTED, value))


def redact_internal_references(value: Any, *, drop_internal_keys: bool = False) -> Any:
    """Recursively remove import identifiers and redact UUID/hash substrings."""
    if isinstance(value, dict):
        redacted: dict[str, Any] = {}
        for key, child in value.items():
            if _is_internal_key(key):
                if not drop_internal_keys:
                    redacted[key] = REDACTED
                continue
            redacted[key] = redact_internal_references(child, drop_internal_keys=drop_internal_keys)
        return redacted
    if isinstance(value, list):
        return [
            redact_internal_references(item, drop_internal_keys=drop_internal_keys)
            for item in value
        ]
    if isinstance(value, str):
        return _redact_text(value)
    return value
