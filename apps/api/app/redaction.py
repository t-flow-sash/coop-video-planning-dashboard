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
    r"[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-"
    r"[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}"
    r"(?![0-9A-Fa-f])"
)
_SHA256_PATTERN = re.compile(r"(?<![0-9A-Fa-f])[0-9A-Fa-f]{64}(?![0-9A-Fa-f])")
_LABELED_INTERNAL_REFERENCE_PATTERN = re.compile(
    r"(?i)(?<![A-Za-z0-9])"
    r"(?:gmail[-_.:]*)?"
    r"(?:provider|external|import|message|thread|history|manifest|batch|request|source|reference|ref)"
    r"(?:[-_.:]*(?:id|ref(?:erence)?))?"
    r"(?:[-_.:=]+[A-Za-z0-9][A-Za-z0-9._:-]*|\s+[A-Za-z0-9][A-Za-z0-9._:-]{7,})"
)


def _is_internal_key(key: str) -> bool:
    snake_case = re.sub(r"(?<!^)(?=[A-Z])", "_", key).replace("-", "_").lower()
    compact = re.sub(r"[^a-z0-9]", "", snake_case)
    internal_compact = {re.sub(r"[^a-z0-9]", "", item) for item in INTERNAL_REFERENCE_KEYS}
    return compact in internal_compact or snake_case.endswith("_id") or bool(re.search(r"ID$", key))


def _redact_text(value: str) -> str:
    value = _SHA256_PATTERN.sub(REDACTED, value)
    value = _UUID_PATTERN.sub(REDACTED, value)
    return _LABELED_INTERNAL_REFERENCE_PATTERN.sub(REDACTED, value)


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


def redact_mapping(value: dict[str, Any], *, drop_internal_keys: bool = False) -> dict[str, Any]:
    redacted = redact_internal_references(value, drop_internal_keys=drop_internal_keys)
    if not isinstance(redacted, dict):  # pragma: no cover - type invariant
        raise TypeError("redacted mapping changed type")
    return redacted


def redact_optional_text(value: str | None) -> str | None:
    return None if value is None else _redact_text(value)
