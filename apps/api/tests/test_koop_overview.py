from __future__ import annotations

from datetime import date
from typing import Any

from fastapi.testclient import TestClient
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.models import AuditEvent, Coop


def _assert_no_provider_or_import_ids(value: Any) -> None:
    if isinstance(value, dict):
        forbidden = {
            "external_id",
            "gmail_thread_id",
            "gmail_message_id",
            "history_id",
            "provider_id",
            "import_id",
            "batch_id",
            "manifest_id",
        }
        assert forbidden.isdisjoint(value)
        for child in value.values():
            _assert_no_provider_or_import_ids(child)
    elif isinstance(value, list):
        for child in value:
            _assert_no_provider_or_import_ids(child)


def test_coop_overview_keeps_public_uuids_but_hides_provider_and_import_ids(
    client: TestClient,
) -> None:
    response = client.post(
        "/api/v1/coops",
        json={
            "record_key": "synthetic-launch",
            "title": "Synthetic launch",
            "priority": "P0",
            "status": "NEGOTIATING",
            "last_contact_date": "2026-10-19",
            "next_action": "Confirm synthetic scope",
            "owner": "Test Owner",
            "deadline": "2026-10-20",
            "fee_amount_minor": 50000,
            "fee_currency": "EUR",
            "fee_status": "AGREED",
            "needs_review": True,
            "uncertainty": "Synthetic ambiguity",
        },
    )

    assert response.status_code == 201, response.text
    payload = response.json()
    assert payload["record_key"] == "synthetic-launch"
    assert payload["priority"] == "P0"
    assert payload["last_contact_date"] == date(2026, 10, 19).isoformat()
    assert payload["deadline"] == date(2026, 10, 20).isoformat()
    assert payload["urgency"] in {"OVERDUE", "ACUTE", "UPCOMING", "NONE"}
    assert payload["fee_status"] == "AGREED"
    assert payload["needs_review"] is True
    assert payload["uncertainty"] == "Synthetic ambiguity"
    assert payload["id"]
    _assert_no_provider_or_import_ids(payload)

    detail = client.get("/api/v1/coops/synthetic-launch")
    assert detail.status_code == 200
    assert detail.json()["id"] == payload["id"]
    _assert_no_provider_or_import_ids(detail.json())


def test_public_audit_omits_internal_import_references_but_database_retains_them(
    client: TestClient, test_engine: Engine
) -> None:
    created = client.post(
        "/api/v1/coops",
        json={
            "record_key": "audit-redaction",
            "title": "Audit redaction",
            "status": "NEW",
            "uncertainty": "Awaiting verification",
        },
    ).json()
    manifest_hash = "a" * 64
    embedded_uuid = "b97f3b09-1514-4511-a84b-75a1f9832ab4"
    embedded_uuid_v7 = "019a01b0-7abc-7def-8abc-0123456789ab"
    nil_uuid = "00000000-0000-0000-0000-000000000000"
    provider_marker = "provider-ref-SYNTHETIC-LEAK-92471"
    mixed_case_marker = "gmail-provider-message-SYNTHETIC-LEAK-83491"
    spaced_markers = (
        "Z9Q8W7V6U5",
        "A1B2C3D4E5",
        "M9N8B7V6C5",
        "T1R2E3A4D5",
        "E1X2T3N4L5",
    )
    spaced_references = (
        f"providerId {spaced_markers[0]} Import-ID {spaced_markers[1]} "
        f"MessageID {spaced_markers[2]} ThreadID {spaced_markers[3]} "
        f"externalId {spaced_markers[4]}"
    )
    internal_request_id = f"manual-import-{manifest_hash[:24]}"
    with Session(test_engine) as session, session.begin():
        coop = session.scalar(select(Coop).where(Coop.record_key == "audit-redaction"))
        assert coop is not None
        session.add(
            AuditEvent(
                coop_id=coop.id,
                action="MANUAL_IMPORT_UPDATED",
                actor_type="SYSTEM",
                actor_id=f"manual-import:{provider_marker}",
                source="MANUAL_IMPORT",
                changes={
                    "external_id": {"old": None, "new": "provider-19"},
                    "metadata": {
                        "message": (
                            f"correlation={manifest_hash} object={embedded_uuid} "
                            f"new={embedded_uuid_v7} nil={nil_uuid}"
                        ),
                        "Provider_ID": provider_marker,
                        "providerId": provider_marker,
                        "MessageID": mixed_case_marker,
                        "ThreadID": mixed_case_marker,
                        "free_text": spaced_references,
                    },
                },
                reason=(
                    f"retry {manifest_hash} for {embedded_uuid} "
                    f"new={embedded_uuid_v7} nil={nil_uuid} reference={provider_marker} "
                    f"{spaced_references}"
                ),
                request_id=internal_request_id,
                source_reference=manifest_hash,
            )
        )

    response = client.get(f"/api/v1/coops/{created['id']}/audit-events")

    assert response.status_code == 200, response.text
    serialized = response.text
    import_event = next(
        item for item in response.json()["items"] if item["source"] == "MANUAL_IMPORT"
    )
    assert "request_id" not in import_event
    assert "source_reference" not in import_event
    assert manifest_hash not in serialized
    assert embedded_uuid not in serialized
    assert embedded_uuid_v7 not in serialized
    assert nil_uuid not in serialized
    assert provider_marker not in serialized
    assert mixed_case_marker not in serialized
    assert not any(marker in serialized for marker in spaced_markers)
    assert "actor_id" not in import_event
    assert "provider-19" not in serialized
    _assert_no_provider_or_import_ids(import_event)
    assert internal_request_id not in serialized
    with Session(test_engine) as session:
        stored = session.scalar(
            select(AuditEvent).where(AuditEvent.action == "MANUAL_IMPORT_UPDATED")
        )
        assert stored is not None
        assert stored.request_id == internal_request_id
        assert stored.source_reference == manifest_hash
        assert stored.actor_id == f"manual-import:{provider_marker}"


def test_list_filters_priority_review_and_deadline_and_sorts_priority_first(
    client: TestClient,
) -> None:
    for record_key, priority, deadline, needs_review in (
        ("synthetic-p2", "P2", "2026-10-18", False),
        ("synthetic-p0", "P0", "2026-10-21", True),
        ("synthetic-p1", "P1", None, False),
    ):
        response = client.post(
            "/api/v1/coops",
            json={
                "record_key": record_key,
                "title": record_key,
                "priority": priority,
                "status": "NEW",
                "deadline": deadline,
                "needs_review": needs_review,
                "uncertainty": "Synthetic review note" if needs_review else None,
            },
        )
        assert response.status_code == 201, response.text

    all_items = client.get("/api/v1/coops", params={"scope": "all"}).json()["items"]
    assert [item["record_key"] for item in all_items] == [
        "synthetic-p0",
        "synthetic-p1",
        "synthetic-p2",
    ]
    reviewed = client.get("/api/v1/coops", params={"needs_review": True}).json()
    assert [item["record_key"] for item in reviewed["items"]] == ["synthetic-p0"]
    overdue = client.get(
        "/api/v1/coops",
        params={"deadline_to": "2026-10-18", "priority": "P2"},
    ).json()
    assert [item["record_key"] for item in overdue["items"]] == ["synthetic-p2"]


def test_create_defaults_to_review_and_requires_a_meaningful_uncertainty(
    client: TestClient,
) -> None:
    missing_reason = client.post(
        "/api/v1/coops",
        json={"record_key": "default-review-missing", "title": "Default review missing"},
    )
    blank_reason = client.post(
        "/api/v1/coops",
        json={
            "record_key": "default-review-blank",
            "title": "Default review blank",
            "uncertainty": "   ",
        },
    )
    explained = client.post(
        "/api/v1/coops",
        json={
            "record_key": "default-review-explained",
            "title": "Default review explained",
            "uncertainty": "Source details have not been verified",
        },
    )

    assert missing_reason.status_code == 422
    assert blank_reason.status_code == 422
    assert explained.status_code == 201, explained.text
    assert explained.json()["needs_review"] is True


def test_search_covers_operational_coop_and_linked_identity_fields(client: TestClient) -> None:
    brand = client.post(
        "/api/v1/organizations",
        json={"record_key": "brand-aurora", "name": "Aurora Organisation", "kind": "BRAND"},
    ).json()
    agency = client.post(
        "/api/v1/organizations",
        json={"record_key": "agency-nimbus", "name": "Nimbus Agency", "kind": "AGENCY"},
    ).json()
    contact = client.post(
        "/api/v1/contacts",
        json={
            "record_key": "contact-quasar",
            "display_name": "Quasar Contact",
            "email": "quasar@example.invalid",
            "organization_id": agency["id"],
        },
    ).json()
    target = client.post(
        "/api/v1/coops",
        json={
            "record_key": "search-target",
            "title": "Tangerine title",
            "summary": "Violet summary",
            "owner": "Cobalt Owner",
            "next_action": "Prepare Magenta briefing",
            "brand_organization_id": brand["id"],
            "agency_organization_id": agency["id"],
            "contact_ids": [contact["id"]],
            "needs_review": False,
        },
    ).json()
    distractor = client.post(
        "/api/v1/coops",
        json={"record_key": "search-distractor", "title": "Unrelated", "needs_review": False},
    ).json()

    for query in (
        "tangerine",
        "violet",
        "cobalt",
        "magenta",
        "aurora",
        "quasar",
        "nimbus",
    ):
        response = client.get("/api/v1/coops", params={"scope": "all", "query": query})
        assert response.status_code == 200, response.text
        assert [item["id"] for item in response.json()["items"]] == [target["id"]]
        assert distractor["id"] not in {item["id"] for item in response.json()["items"]}


def test_coop_list_embeds_relationships_without_detail_requests(client: TestClient) -> None:
    brand = client.post(
        "/api/v1/organizations",
        json={"record_key": "brand-list", "name": "List Brand", "kind": "BRAND"},
    ).json()
    agency = client.post(
        "/api/v1/organizations",
        json={"record_key": "agency-list", "name": "List Agency", "kind": "AGENCY"},
    ).json()
    contact = client.post(
        "/api/v1/contacts",
        json={
            "record_key": "contact-list",
            "display_name": "List Contact",
            "email": "list-contact@example.invalid",
        },
    ).json()
    created = client.post(
        "/api/v1/coops",
        json={
            "record_key": "relationship-list",
            "title": "Relationship list",
            "brand_organization_id": brand["id"],
            "agency_organization_id": agency["id"],
            "contact_ids": [contact["id"]],
            "needs_review": False,
        },
    ).json()

    response = client.get("/api/v1/coops", params={"scope": "all", "query": "Relationship"})

    assert response.status_code == 200, response.text
    [item] = response.json()["items"]
    assert item["id"] == created["id"]
    assert item["brand_organization"]["name"] == "List Brand"
    assert item["agency_organization"]["name"] == "List Agency"
    assert item["contacts"][0]["contact"]["display_name"] == "List Contact"


def test_patch_cannot_enable_review_without_explaining_uncertainty(client: TestClient) -> None:
    created = client.post(
        "/api/v1/coops",
        json={
            "record_key": "review-guard",
            "title": "Review guard",
            "status": "NEW",
            "needs_review": False,
        },
    ).json()

    response = client.patch(
        "/api/v1/coops/review-guard",
        json={"version": created["version"], "needs_review": True},
    )

    assert response.status_code == 422


def test_patch_cannot_clear_uncertainty_while_review_remains_enabled(client: TestClient) -> None:
    created = client.post(
        "/api/v1/coops",
        json={
            "record_key": "review-clear-guard",
            "title": "Review clear guard",
            "status": "NEW",
            "needs_review": True,
            "uncertainty": "Needs clarification",
        },
    ).json()

    response = client.patch(
        "/api/v1/coops/review-clear-guard",
        json={"version": created["version"], "uncertainty": None},
    )

    assert response.status_code == 422


def test_coop_record_key_cannot_be_uuid_shaped(client: TestClient) -> None:
    response = client.post(
        "/api/v1/coops",
        json={
            "record_key": "b97f3b09-1514-4511-a84b-75a1f9832ab4",  # gitleaks:allow
            "title": "Ambiguous route key",
            "status": "NEW",
        },
    )

    assert response.status_code == 422
