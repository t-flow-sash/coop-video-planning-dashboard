from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, delete, func, select
from sqlalchemy.orm import Session

from app.manual_coop_import import ManualImportError, apply_manifest
from app.models import (
    AuditEvent,
    Contact,
    Coop,
    CoopImportState,
    ManualImportBatch,
    Organization,
)
from app.seed import COOPS as SYNTHETIC_SEED_COOPS
from app.seed import _seed


def _hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def manifest(*records: dict[str, object]) -> dict[str, object]:
    prepared = []
    for record in records:
        item = deepcopy(record)
        item["content_hash"] = _hash(item)
        prepared.append(item)
    result: dict[str, object] = {
        "schema_version": 1,
        "manifest_id": "synthetic-reviewed-2026-10-19",
        "records": prepared,
    }
    result["manifest_hash"] = _hash(result)
    return result


def record(key: str, *, version: int = 1, title: str | None = None) -> dict[str, object]:
    return {
        "record_key": key,
        "record_version": version,
        "title": title or key,
        "priority": "P0",
        "status": "NEGOTIATING",
        "last_contact_date": "2026-10-19",
        "next_action": "Confirm synthetic scope",
        "owner": "Test Owner",
        "deadline": "2026-10-20",
        "fee_amount_minor": 50000,
        "fee_currency": "EUR",
        "fee_status": "AGREED",
        "needs_review": False,
        "uncertainty": None,
        "contact": {
            "record_key": f"contact-{key}",
            "display_name": "Synthetic Contact",
            "email": f"{key}@example.invalid",
        },
        "agency": {"record_key": f"agency-{key}", "name": "Synthetic Agency"},
    }


def test_reviewed_import_is_idempotent_and_audited(test_engine: Engine) -> None:
    payload = manifest(record("synthetic-import"))
    with Session(test_engine) as session, session.begin():
        first = apply_manifest(session, payload)
    with Session(test_engine) as session, session.begin():
        second = apply_manifest(session, payload)

    assert first == {
        "created": 1,
        "updated": 0,
        "unchanged": 0,
        "removed_synthetic_seeds": 0,
        "manifest_hash": payload["manifest_hash"],
    }
    assert second == {
        "created": 0,
        "updated": 0,
        "unchanged": 1,
        "removed_synthetic_seeds": 0,
        "manifest_hash": payload["manifest_hash"],
    }
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(Coop)) == 1
        assert session.scalar(select(func.count()).select_from(ManualImportBatch)) == 1
        events = list(session.scalars(select(AuditEvent)))
        assert [event.action for event in events] == ["MANUAL_IMPORT_CREATED"]
        assert events[0].source == "MANUAL_IMPORT"
        assert events[0].source_reference == payload["manifest_hash"]


def test_import_rejects_provider_fields_hash_conflicts_and_rolls_back(test_engine: Engine) -> None:
    forbidden = record("synthetic-provider")
    forbidden["gmail_thread_id"] = "provider-secret"
    with Session(test_engine) as session, pytest.raises(ManualImportError, match="provider"):
        apply_manifest(session, manifest(forbidden))

    conflicting = manifest(record("synthetic-conflict"))
    conflicting["records"][0]["content_hash"] = "0" * 64  # type: ignore[index]
    conflicting["manifest_hash"] = _hash(
        {k: v for k, v in conflicting.items() if k != "manifest_hash"}
    )
    with Session(test_engine) as session, pytest.raises(ManualImportError, match="content_hash"):
        apply_manifest(session, conflicting)

    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(Coop)) == 0
        assert session.scalar(select(func.count()).select_from(ManualImportBatch)) == 0


def test_import_detects_version_conflict_and_local_divergence_atomically(
    test_engine: Engine,
) -> None:
    initial = manifest(record("synthetic-existing"))
    with Session(test_engine) as session, session.begin():
        apply_manifest(session, initial)
    same_version_changed = manifest(record("synthetic-existing", title="Changed at same version"))
    with (
        Session(test_engine) as session,
        pytest.raises(ManualImportError, match="version conflict"),
        session.begin(),
    ):
        apply_manifest(session, same_version_changed)

    with Session(test_engine) as session, session.begin():
        coop = session.scalar(select(Coop).where(Coop.record_key == "synthetic-existing"))
        assert coop is not None
        coop.owner = "Locally changed"
    next_version = manifest(record("synthetic-existing", version=2), record("synthetic-late"))
    with (
        Session(test_engine) as session,
        pytest.raises(ManualImportError, match="local divergence"),
        session.begin(),
    ):
        apply_manifest(session, next_version)

    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(Coop)) == 1
        existing = session.scalar(select(Coop).where(Coop.record_key == "synthetic-existing"))
        assert existing is not None and existing.owner == "Locally changed"


def test_import_replay_rejects_local_divergence_instead_of_claiming_unchanged(
    test_engine: Engine,
) -> None:
    payload = manifest(record("synthetic-replay"))
    with Session(test_engine) as session, session.begin():
        apply_manifest(session, payload)
    with Session(test_engine) as session, session.begin():
        coop = session.scalar(select(Coop).where(Coop.record_key == "synthetic-replay"))
        assert coop is not None
        coop.owner = "Manual owner"

    with (
        Session(test_engine) as session,
        pytest.raises(ManualImportError, match="local divergence"),
        session.begin(),
    ):
        apply_manifest(session, payload)


def test_import_replay_rejects_a_missing_managed_record(test_engine: Engine) -> None:
    payload = manifest(record("synthetic-deleted"))
    with Session(test_engine) as session, session.begin():
        apply_manifest(session, payload)
    with Session(test_engine) as session, session.begin():
        coop = session.scalar(select(Coop).where(Coop.record_key == "synthetic-deleted"))
        assert coop is not None
        session.execute(delete(AuditEvent).where(AuditEvent.coop_id == coop.id))
        session.execute(delete(CoopImportState).where(CoopImportState.coop_id == coop.id))
        session.delete(coop)

    with (
        Session(test_engine) as session,
        pytest.raises(ManualImportError, match="missing managed record"),
        session.begin(),
    ):
        apply_manifest(session, payload)


@pytest.mark.parametrize(
    "invalid_record, expected",
    [
        (
            {
                **record("contact-without-email"),
                "contact": {
                    "record_key": "contact-without-email",
                    "display_name": "Named only",
                    "email": None,
                },
            },
            "valid string",
        ),
        ({**record("scheduled-without-release"), "status": "SCHEDULED"}, "SCHEDULED"),
    ],
)
def test_manifest_rejects_unrepresentable_contact_and_scheduled_states(
    test_engine: Engine,
    invalid_record: dict[str, object],
    expected: str,
) -> None:
    with (
        Session(test_engine) as session,
        pytest.raises(ManualImportError, match=expected),
        session.begin(),
    ):
        apply_manifest(session, manifest(invalid_record))


def test_import_update_advances_version_audits_and_deduplicates_relations(
    test_engine: Engine,
) -> None:
    initial = manifest(record("synthetic-update"))
    updated = manifest(record("synthetic-update", version=2, title="Updated synthetic title"))
    with Session(test_engine) as session, session.begin():
        apply_manifest(session, initial)
    with Session(test_engine) as session, session.begin():
        result = apply_manifest(session, updated)

    assert result["updated"] == 1
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop).where(Coop.record_key == "synthetic-update"))
        assert coop is not None and coop.title == "Updated synthetic title" and coop.version == 2
        state = session.get(CoopImportState, coop.id)
        assert state is not None
        assert state.record_version == 2
        assert state.content_hash == updated["records"][0]["content_hash"]  # type: ignore[index]
        assert session.scalar(select(func.count()).select_from(Contact)) == 1
        assert session.scalar(select(func.count()).select_from(Organization)) == 1
        assert list(session.scalars(select(AuditEvent.action).order_by(AuditEvent.created_at))) == [
            "MANUAL_IMPORT_CREATED",
            "MANUAL_IMPORT_UPDATED",
        ]


def test_import_rolls_back_earlier_records_when_later_identity_conflicts(
    test_engine: Engine,
) -> None:
    with Session(test_engine) as session, session.begin():
        apply_manifest(session, manifest(record("synthetic-anchor")))
    before = manifest(
        record("synthetic-first"),
        {
            **record("synthetic-conflicting-agency"),
            "agency": {
                "record_key": "agency-synthetic-anchor",
                "name": "Conflicting agency name",
            },
        },
    )

    with (
        Session(test_engine) as session,
        pytest.raises(ManualImportError, match="agency identity conflict"),
        session.begin(),
    ):
        apply_manifest(session, before)

    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(Coop)) == 1
        assert session.scalar(select(func.count()).select_from(ManualImportBatch)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_import_rejects_contact_reuse_across_agencies_atomically(test_engine: Engine) -> None:
    first = record("synthetic-first-agency")
    second = record("synthetic-second-agency")
    second["contact"] = first["contact"]

    with (
        Session(test_engine) as session,
        pytest.raises(ManualImportError, match="contact identity conflict"),
        session.begin(),
    ):
        apply_manifest(session, manifest(first, second))

    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(Coop)) == 0
        assert session.scalar(select(func.count()).select_from(Contact)) == 0
        assert session.scalar(select(func.count()).select_from(Organization)) == 0


def test_import_replay_detects_contact_identity_divergence(test_engine: Engine) -> None:
    payload = manifest(record("synthetic-contact-divergence"))
    with Session(test_engine) as session, session.begin():
        apply_manifest(session, payload)
    with Session(test_engine) as session, session.begin():
        contact = session.scalar(select(Contact))
        assert contact is not None
        contact.display_name = "Locally changed contact"

    with (
        Session(test_engine) as session,
        pytest.raises(ManualImportError, match="local divergence"),
        session.begin(),
    ):
        apply_manifest(session, payload)


def test_manifest_rejects_uuid_shaped_record_key(test_engine: Engine) -> None:
    with (
        Session(test_engine) as session,
        pytest.raises(ManualImportError, match="UUID-shaped"),
        session.begin(),
    ):
        apply_manifest(session, manifest(record("b97f3b09-1514-4511-a84b-75a1f9832ab4")))


def test_reviewed_manifest_atomically_replaces_only_the_six_known_synthetic_seeds(
    test_engine: Engine,
) -> None:
    synthetic_ids = {UUID(row[0]) for row in SYNTHETIC_SEED_COOPS}
    genuine_id = uuid4()
    internal_organization_id = uuid4()
    embedded_hash = "a" * 64
    embedded_uuid_v7 = "019a01b0-7abc-7def-8abc-0123456789ab"
    nil_uuid = "00000000-0000-0000-0000-000000000000"
    provider_marker = "provider-ref-SYNTHETIC-LEAK-92471"
    with Session(test_engine) as session, session.begin():
        _seed(session)
        session.add(
            AuditEvent(
                coop_id=next(iter(synthetic_ids)),
                action="COOP_UPDATED",
                actor_type="USER",
                source="API",
                changes={
                    "brand_organization_id": {
                        "old": None,
                        "new": str(internal_organization_id),
                    },
                    "note": f"correlation={embedded_hash}",
                    "message": (
                        f"org {internal_organization_id} new={embedded_uuid_v7} nil={nil_uuid}"
                    ),
                    "Provider_ID": provider_marker,
                    "providerId": provider_marker,
                },
                reason=(
                    f"retry {embedded_hash} for {internal_organization_id} "
                    f"new={embedded_uuid_v7} nil={nil_uuid}"
                ),
                request_id="internal-request-id",
            )
        )
        session.add(
            Coop(
                id=genuine_id,
                record_key=f"user-{genuine_id.hex}",
                title="Genuine user cooperation",
                status="NEGOTIATING",
                needs_review=False,
                is_synthetic_seed=False,
            )
        )

    payload = manifest(*(record(f"reviewed-track-{index:02d}") for index in range(21)))
    with Session(test_engine) as session, session.begin():
        result = apply_manifest(session, payload)

    assert result["created"] == 21
    assert result["removed_synthetic_seeds"] == 6
    with Session(test_engine) as session:
        remaining_ids = set(session.scalars(select(Coop.id)))
        assert not (remaining_ids & synthetic_ids)
        assert genuine_id in remaining_ids
        assert session.scalar(select(func.count()).select_from(Coop)) == 22
        batch = session.scalar(select(ManualImportBatch))
        assert batch is not None
        replacement = batch.summary["synthetic_seed_replacement"]
        assert replacement["removed_count"] == 6
        assert {item["seed_key"] for item in replacement["seeds"]} == {
            f"milestone-one-demo-{number}" for number in range(1, 7)
        }
        assert all(
            item["audit_history"][0]["action"] == "COOP_CREATED" for item in replacement["seeds"]
        )
        assert (
            sum(
                event["action"] == "COOP_UPDATED"
                for item in replacement["seeds"]
                for event in item["audit_history"]
            )
            == 1
        )
        serialized_summary = json.dumps(batch.summary, sort_keys=True)
        assert not any(str(seed_id) in serialized_summary for seed_id in synthetic_ids)
        assert str(internal_organization_id) not in serialized_summary
        assert embedded_hash not in serialized_summary
        assert embedded_uuid_v7 not in serialized_summary
        assert nil_uuid not in serialized_summary
        assert provider_marker not in serialized_summary
        assert "internal-request-id" not in serialized_summary
        assert "seed-00000000" not in serialized_summary

    with Session(test_engine) as session, session.begin():
        replay = apply_manifest(session, payload)
    assert replay["removed_synthetic_seeds"] == 0
    assert replay["unchanged"] == 21
