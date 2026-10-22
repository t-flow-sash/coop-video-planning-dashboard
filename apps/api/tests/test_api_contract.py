"""The authenticated API surface must be a typed, non-leaking contract.

The dashboard renders the operational semantic status from this payload, so the
response shape is part of the release contract: an untyped ``dict[str, Any]``
response silently allows new fields (and therefore leaked source identities) as
well as client drift. These tests pin the documented field sets, the absence of
private evidence identifiers, and the freshness of the checked-in
``packages/api-client/openapi.json`` that clients are generated from.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from app.main import app

CONTRACT_PATH = Path(__file__).resolve().parents[3] / "packages" / "api-client" / "openapi.json"

SEMANTIC_STATUS_FIELDS = {
    "as_of",
    "coverage_verified",
    "coverage_note",
    "domain_records",
    "tracks_total",
    "tracks_routed",
    "tracks_awaiting_route",
    "sources",
}
SEMANTIC_SOURCE_FIELDS = {
    "enabled",
    "discovered",
    "processed",
    "created",
    "updated",
    "unchanged",
    "review",
    "retry",
    "backlog",
    "oldest_backlog_at",
    "latest_evidence_at",
    "last_semantic_activity",
    "collector_age_seconds",
    "last_collector_run",
    "threads_discovered",
    "threads_routed",
    "threads_awaiting_route",
    "oldest_awaiting_route_at",
}
SEMANTIC_COLLECTOR_RUN_FIELDS = {"mode", "status", "started_at", "finished_at"}

# Provider identities, mailbox text and business keys must never reach this surface.
FORBIDDEN_FIELD_NAMES = {
    "external_id",
    "external_key",
    "source_key",
    "source_record_id",
    "revision_id",
    "record_key",
    "track_key",
    "thread_id",
    "message_id",
    "body",
    "body_text",
    "snippet",
    "subject",
    "provenance",
    "metadata_json",
    "assertion_sha256",
    "content_hash",
}


def _semantic_status_schema() -> dict[str, Any]:
    schema: dict[str, Any] = app.openapi()["paths"]["/api/v1/semantic/status"]["get"]["responses"][
        "200"
    ]["content"]["application/json"]["schema"]
    return schema


@pytest.mark.no_db
def test_checked_in_client_contract_matches_the_application() -> None:
    """A stale checked-in contract ships an untyped client and cannot be trusted."""
    expected = json.dumps(app.openapi(), indent=2, sort_keys=True)
    checked_in = json.dumps(
        json.loads(CONTRACT_PATH.read_text(encoding="utf-8")), indent=2, sort_keys=True
    )
    assert checked_in == expected, (
        "packages/api-client/openapi.json is out of date; run `make openapi`."
    )


@pytest.mark.no_db
def test_semantic_status_response_is_a_typed_model_not_a_free_form_object() -> None:
    schema = _semantic_status_schema()
    assert schema == {"$ref": "#/components/schemas/SemanticStatus"}


@pytest.mark.no_db
def test_semantic_status_contract_declares_exactly_the_documented_fields() -> None:
    components = app.openapi()["components"]["schemas"]
    assert set(components["SemanticStatus"]["properties"]) == SEMANTIC_STATUS_FIELDS
    assert set(components["SemanticSourceStatus"]["properties"]) == SEMANTIC_SOURCE_FIELDS
    assert set(components["SemanticCollectorRun"]["properties"]) == SEMANTIC_COLLECTOR_RUN_FIELDS
    assert set(components["SemanticStatus"]["required"]) == SEMANTIC_STATUS_FIELDS
    assert set(components["SemanticSourceStatus"]["required"]) == SEMANTIC_SOURCE_FIELDS
    assert set(components["SemanticCollectorRun"]["required"]) == SEMANTIC_COLLECTOR_RUN_FIELDS
    # Freshness/backlog fields are always present but must stay explicitly nullable so an
    # unknown age is rendered as "unknown" instead of a fabricated number.
    nullable = {
        "oldest_backlog_at",
        "latest_evidence_at",
        "last_semantic_activity",
        "collector_age_seconds",
        "last_collector_run",
        "oldest_awaiting_route_at",
    }
    for field in sorted(nullable):
        declared = components["SemanticSourceStatus"]["properties"][field]
        assert "null" in json.dumps(declared), field


@pytest.mark.no_db
def test_semantic_status_contract_never_exposes_evidence_identifiers() -> None:
    components = app.openapi()["components"]["schemas"]
    for name in ("SemanticStatus", "SemanticSourceStatus", "SemanticCollectorRun"):
        properties = set(components[name]["properties"])
        assert not (properties & FORBIDDEN_FIELD_NAMES), name
        assert components[name].get("additionalProperties") is not True, name
