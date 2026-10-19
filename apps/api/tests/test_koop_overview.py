from __future__ import annotations

from datetime import date
from typing import Any

from fastapi.testclient import TestClient


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


def test_patch_cannot_enable_review_without_explaining_uncertainty(client: TestClient) -> None:
    created = client.post(
        "/api/v1/coops",
        json={"record_key": "review-guard", "title": "Review guard", "status": "NEW"},
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
