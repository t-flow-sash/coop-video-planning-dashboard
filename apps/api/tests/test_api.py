from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from fastapi.testclient import TestClient
from sqlalchemy import text

from app.db import engine
from app.main import app


def create_coop(client: TestClient, **overrides: object) -> dict[str, object]:
    payload: dict[str, object] = {
        "title": "Synthetic launch",
        "status": "NEW",
        "summary": "Synthetic test data",
        "deliverables": [{"version": 1, "kind": "VIDEO", "quantity": 1}],
    }
    payload.update(overrides)
    response = client.post("/api/v1/coops", json=payload)
    assert response.status_code == 201, response.text
    return response.json()


def test_health_live_has_no_database_dependency_and_ready_checks_schema(client: TestClient) -> None:
    assert client.get("/api/v1/health/live").json() == {"status": "ok"}
    assert client.get("/api/v1/health/ready").json() == {"status": "ready"}


def test_create_detail_patch_and_audit_history(client: TestClient) -> None:
    created = create_coop(client)
    coop_id = created["id"]
    assert created["version"] == 1

    detail = client.get(f"/api/v1/coops/{coop_id}")
    assert detail.status_code == 200
    assert detail.json()["contacts"] == []
    assert detail.json()["brand_organization"] is None

    patched = client.patch(
        f"/api/v1/coops/{coop_id}",
        json={
            "version": 1,
            "title": "Updated launch",
            "fee_amount_minor": 250000,
            "fee_currency": "eur",
            "next_action": "Send briefing",
        },
    )
    assert patched.status_code == 200, patched.text
    assert patched.json()["version"] == 2
    assert patched.json()["fee_currency"] == "EUR"

    events = client.get(f"/api/v1/coops/{coop_id}/audit-events").json()
    assert [event["action"] for event in events["items"]] == ["COOP_UPDATED", "COOP_CREATED"]
    assert events["items"][0]["changes"] == {
        "fee_amount_minor": {"old": None, "new": 250000},
        "fee_currency": {"old": None, "new": "EUR"},
        "next_action": {"old": None, "new": "Send briefing"},
        "title": {"old": "Synthetic launch", "new": "Updated launch"},
    }
    assert events["items"][0]["actor_type"] == "USER"
    assert events["items"][0]["request_id"]


def test_patch_rejects_status_and_stale_version_as_problem_json(client: TestClient) -> None:
    created = create_coop(client)
    coop_id = created["id"]

    invalid = client.patch(f"/api/v1/coops/{coop_id}", json={"version": 1, "status": "CONFIRMED"})
    assert invalid.status_code == 422
    assert invalid.headers["content-type"].startswith("application/problem+json")
    assert invalid.json()["code"] == "validation_error"

    ok = client.patch(f"/api/v1/coops/{coop_id}", json={"version": 1, "summary": "new"})
    assert ok.status_code == 200
    stale = client.patch(f"/api/v1/coops/{coop_id}", json={"version": 1, "summary": "lost"})
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_version"
    assert stale.json()["status"] == 409


def test_create_terminal_completed_sets_completion_timestamp(client: TestClient) -> None:
    created = create_coop(client, status="COMPLETED")
    assert created["completed_at"] is not None


def test_transitions_enforce_matrix_version_rules_and_completion_timestamp(
    client: TestClient,
) -> None:
    created = create_coop(client, status="PUBLISHED")
    coop_id = created["id"]

    invalid = client.post(
        f"/api/v1/coops/{coop_id}/transitions",
        json={"target_status": "QUALIFYING", "expected_version": 1},
    )
    assert invalid.status_code == 409
    assert invalid.json()["code"] == "invalid_status_transition"

    completed = client.post(
        f"/api/v1/coops/{coop_id}/transitions",
        json={"target_status": "COMPLETED", "expected_version": 1, "reason": "Delivered"},
    )
    assert completed.status_code == 200, completed.text
    assert completed.json()["status"] == "COMPLETED"
    assert completed.json()["completed_at"] is not None
    assert completed.json()["version"] == 2

    stale = client.post(
        f"/api/v1/coops/{coop_id}/transitions",
        json={"target_status": "REVIEW", "expected_version": 1},
    )
    assert stale.status_code == 409
    assert stale.json()["code"] == "stale_version"

    events = client.get(f"/api/v1/coops/{coop_id}/audit-events").json()["items"]
    assert events[0]["action"] == "STATUS_TRANSITIONED"
    assert events[0]["changes"]["status"] == {"old": "PUBLISHED", "new": "COMPLETED"}


def test_scheduled_requires_release_at_and_terminal_reopen_requires_reason(
    client: TestClient,
) -> None:
    review = create_coop(client, title="Review", status="REVIEW")
    missing_date = client.post(
        f"/api/v1/coops/{review['id']}/transitions",
        json={"target_status": "SCHEDULED", "expected_version": 1},
    )
    assert missing_date.status_code == 409
    assert missing_date.json()["code"] == "invalid_status_transition"

    declined = create_coop(client, title="Declined", status="DECLINED")
    no_reason = client.post(
        f"/api/v1/coops/{declined['id']}/reopen",
        json={"expected_version": 1, "reason": " "},
    )
    assert no_reason.status_code == 422
    reopened = client.post(
        f"/api/v1/coops/{declined['id']}/reopen",
        json={"expected_version": 1, "reason": "New budget"},
    )
    assert reopened.status_code == 200
    assert reopened.json()["status"] == "QUALIFYING"


def test_list_defaults_open_filters_and_uses_stable_cursor_pagination(client: TestClient) -> None:
    org = client.post(
        "/api/v1/organizations", json={"name": "Synthetic Brand", "kind": "BRAND"}
    ).json()
    contact = client.post(
        "/api/v1/contacts",
        json={
            "display_name": "Casey Contact",
            "email": "Casey@Example.invalid",
            "organization_id": org["id"],
        },
    ).json()
    dated = create_coop(
        client,
        title="Dated campaign",
        status="SCHEDULED",
        release_at=datetime(2027, 1, 1, tzinfo=UTC).isoformat(),
        brand_organization_id=org["id"],
        contact_ids=[contact["id"]],
    )
    create_coop(client, title="Undated campaign", status="NEW")
    create_coop(client, title="Done campaign", status="COMPLETED")

    first = client.get("/api/v1/coops", params={"limit": 1}).json()
    assert [item["id"] for item in first["items"]] == [dated["id"]]
    assert first["next_cursor"]
    second = client.get("/api/v1/coops", params={"limit": 1, "cursor": first["next_cursor"]}).json()
    assert [item["title"] for item in second["items"]] == ["Undated campaign"]

    assert client.get("/api/v1/coops", params={"scope": "all"}).json()["total"] == 3
    assert client.get("/api/v1/coops", params={"status": "COMPLETED"}).json()["total"] == 1
    assert client.get("/api/v1/coops", params={"query": "dated"}).json()["total"] == 2
    assert client.get("/api/v1/coops", params={"organization_id": org["id"]}).json()["total"] == 1
    assert client.get("/api/v1/coops", params={"contact_id": contact["id"]}).json()["total"] == 1


def test_not_found_and_bad_cursor_are_problem_details(client: TestClient) -> None:
    missing = client.get(f"/api/v1/coops/{uuid4()}")
    assert missing.status_code == 404
    assert missing.headers["content-type"].startswith("application/problem+json")
    assert missing.json()["code"] == "coop_not_found"

    cursor = client.get("/api/v1/coops", params={"cursor": "not-base64"})
    assert cursor.status_code == 422
    assert cursor.json()["code"] == "invalid_cursor"


def test_coop_update_rolls_back_when_audit_insert_fails(client: TestClient) -> None:
    created = create_coop(client)
    coop_id = created["id"]
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE OR REPLACE FUNCTION reject_audit() RETURNS trigger LANGUAGE plpgsql AS $$ "
                "BEGIN RAISE EXCEPTION 'audit rejected'; END $$"
            )
        )
        connection.execute(
            text(
                "CREATE TRIGGER reject_audit_trigger BEFORE INSERT ON audit_event "
                "FOR EACH ROW EXECUTE FUNCTION reject_audit()"
            )
        )
    try:
        with TestClient(app, raise_server_exceptions=False) as failing_client:
            response = failing_client.patch(
                f"/api/v1/coops/{coop_id}",
                json={"version": 1, "title": "Must roll back"},
            )
        assert response.status_code == 500
    finally:
        with engine.begin() as connection:
            connection.execute(text("DROP TRIGGER IF EXISTS reject_audit_trigger ON audit_event"))
            connection.execute(text("DROP FUNCTION IF EXISTS reject_audit()"))

    unchanged = client.get(f"/api/v1/coops/{coop_id}").json()
    assert unchanged["title"] == "Synthetic launch"
    assert unchanged["version"] == 1
