from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditEvent,
    Coop,
    IngestionSource,
    ReviewedDomainBatch,
    ReviewedDomainEvidence,
    ReviewedDomainItem,
    ReviewedDomainTrack,
    SourceRecord,
    SourceRecordRevision,
)
from app.reviewed_domain_import import ReviewError, apply_manifest, canonical_hash


def evidence(engine: Engine) -> dict[str, str]:
    with Session(engine) as session, session.begin():
        source = IngestionSource(source_key="synthetic-source", kind="GMAIL", enabled=True)
        session.add(source)
        session.flush()
        record = SourceRecord(
            source_id=source.id, record_type="GMAIL_THREAD", external_id="synthetic-thread"
        )
        session.add(record)
        session.flush()
        revision = SourceRecordRevision(
            source_id=source.id,
            source_record_id=record.id,
            content_hash="a" * 64,
            metadata_json={"evidence_only": True},
        )
        session.add(revision)
        session.flush()
        return {
            "source_id": str(source.id),
            "source_record_id": str(record.id),
            "revision_id": str(revision.id),
        }


def payload(
    ref: dict[str, str],
    *,
    revision: int = 1,
    action: str = "CREATE",
    fields: dict[str, object] | None = None,
    **extra: object,
) -> dict[str, object]:
    decision: dict[str, object] = {
        "track_key": "track-abc",
        "track_revision": revision,
        "adjudication": "ACCEPTED",
        "action": action,
        "record_key": "exact-coop",
        "evidence": [ref],
        "fields": fields
        or {
            "title": "Reviewed coop",
            "priority": "P1",
            "status": "NEGOTIATING",
            "needs_review": False,
            "uncertainty": None,
        },
        **extra,
    }
    decision["decision_hash"] = canonical_hash(decision)
    result: dict[str, object] = {
        "schema_version": 1,
        "manifest_id": f"review-{revision}",
        "decisions": [decision],
    }
    result["manifest_hash"] = canonical_hash(result)
    return result


def test_create_replay_and_exact_provenance(test_engine: Engine) -> None:
    ref = evidence(test_engine)
    manifest = payload(ref)
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, manifest)["created"] == 1
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, manifest)["unchanged"] == 1
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        assert coop is not None and coop.record_key == "exact-coop" and coop.version == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert session.scalar(select(func.count()).select_from(ReviewedDomainBatch)) == 1
        assert session.scalar(select(func.count()).select_from(ReviewedDomainEvidence)) == 1
        assert session.scalar(select(ReviewedDomainTrack)).coop_id == coop.id  # type: ignore[union-attr]


def test_initial_exact_update_and_local_conflict_preserve_unaffected_fields(
    test_engine: Engine,
) -> None:
    ref = evidence(test_engine)
    with Session(test_engine) as session, session.begin():
        session.add(
            Coop(
                record_key="exact-coop",
                title="Before",
                priority="P2",
                status="NEW",
                needs_review=True,
                uncertainty="Open",
            )
        )
    initial = payload(
        ref,
        action="UPDATE",
        fields={"title": "Reviewed", "priority": "P1"},
        expected_version=1,
        expected_fields={"title": "Before", "priority": "P2"},
    )
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, initial)["updated"] == 1
    with Session(test_engine) as session, session.begin():
        coop = session.scalar(select(Coop))
        assert coop is not None
        coop.title = "Local title"
    subsequent = payload(
        ref, revision=2, action="UPDATE", fields={"title": "External title", "priority": "P0"}
    )
    with Session(test_engine) as session, session.begin():
        outcome = apply_manifest(session, subsequent)
        assert outcome["conflicted"] == 1 and outcome["updated"] == 1
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        assert coop is not None and (coop.title, coop.priority, coop.version) == (
            "Local title",
            "P0",
            3,
        )
        track = session.scalar(select(ReviewedDomainTrack))
        assert track is not None and track.field_baselines == {
            "title": "Reviewed",
            "priority": "P0",
        }
        item = session.scalar(
            select(ReviewedDomainItem).where(ReviewedDomainItem.outcome == "CONFLICT")
        )
        assert item is not None and item.conflict_fields == ["title"]
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 2


def test_exact_adoption_of_manual_managed_target_and_manual_writer_fence(
    test_engine: Engine,
) -> None:
    from test_manual_coop_import import manifest, record

    from app.manual_coop_import import ManualImportError
    from app.manual_coop_import import apply_manifest as apply_manual

    ref = evidence(test_engine)
    original = manifest(record("exact-coop"))
    with Session(test_engine) as session, session.begin():
        apply_manual(session, original)
    reviewed = payload(
        ref,
        action="UPDATE",
        fields={"deadline": "2026-10-22", "fee_amount_minor": 55000},
        expected_version=1,
        expected_fields={"deadline": "2026-10-20", "fee_amount_minor": 50000},
    )
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, reviewed)["updated"] == 1
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        assert coop is not None and coop.deadline.isoformat() == "2026-10-22"
        assert coop.fee_amount_minor == 55000
    with (
        pytest.raises(ManualImportError),
        Session(test_engine) as session,
        session.begin(),
    ):
        apply_manual(session, original)
    with (
        pytest.raises(ManualImportError),
        Session(test_engine) as session,
        session.begin(),
    ):
        apply_manual(session, manifest(record("exact-coop", version=2)))


def test_manual_import_cannot_reclaim_noop_adoption(test_engine: Engine) -> None:
    from test_manual_coop_import import manifest, record

    from app.manual_coop_import import ManualImportError
    from app.manual_coop_import import apply_manifest as apply_manual

    ref = evidence(test_engine)
    original = manifest(record("exact-coop"))
    with Session(test_engine) as session, session.begin():
        apply_manual(session, original)
    reviewed = payload(
        ref,
        action="UPDATE",
        fields={"deadline": "2026-10-20"},
        expected_version=1,
        expected_fields={"deadline": "2026-10-20"},
    )
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, reviewed)["unchanged"] == 0
    with (
        pytest.raises(ManualImportError, match="reviewed"),
        Session(test_engine) as session,
        session.begin(),
    ):
        apply_manual(session, original)


def test_utc_equivalent_datetime_snapshot_and_replay(test_engine: Engine) -> None:
    ref = evidence(test_engine)
    with Session(test_engine) as session, session.begin():
        session.add(
            Coop(
                record_key="exact-coop",
                title="Before",
                priority="P1",
                status="NEW",
                needs_review=False,
                release_at=datetime(2026, 10, 21, 10, tzinfo=UTC),
            )
        )
    initial = payload(
        ref,
        action="UPDATE",
        fields={"release_at": "2026-10-21T13:00:00+03:00"},
        expected_version=1,
        expected_fields={"release_at": "2026-10-21T12:00:00+02:00"},
    )
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, initial)["conflicted"] == 0
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, initial)["unchanged"] == 1


def test_conflict_rebase_requires_exact_version_and_snapshot(test_engine: Engine) -> None:
    ref = evidence(test_engine)
    with Session(test_engine) as session, session.begin():
        session.add(
            Coop(
                record_key="exact-coop",
                title="Before",
                priority="P2",
                status="NEW",
                needs_review=False,
            )
        )
    initial = payload(
        ref,
        action="UPDATE",
        fields={"title": "Reviewed"},
        expected_version=1,
        expected_fields={"title": "Before"},
    )
    with Session(test_engine) as session, session.begin():
        apply_manifest(session, initial)
    with Session(test_engine) as session, session.begin():
        coop = session.scalar(select(Coop))
        assert coop is not None
        coop.title = "Local"
        coop.version += 1
    conflict = payload(ref, revision=2, action="UPDATE", fields={"title": "Proposal"})
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, conflict)["conflicted"] == 1
    stale = payload(
        ref,
        revision=3,
        action="UPDATE",
        fields={"title": "Accepted"},
        expected_version=2,
        expected_fields={"title": "Local"},
    )
    with (
        pytest.raises(ReviewError, match="version"),
        Session(test_engine) as session,
        session.begin(),
    ):
        apply_manifest(session, stale)
    resolved = payload(
        ref,
        revision=3,
        action="UPDATE",
        fields={"title": "Accepted"},
        expected_version=3,
        expected_fields={"title": "Local"},
    )
    with Session(test_engine) as session, session.begin():
        result = apply_manifest(session, resolved)
        assert result["updated"] == 1 and result["conflicted"] == 0
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        assert coop is not None and coop.title == "Accepted"
        track = session.scalar(select(ReviewedDomainTrack))
        assert track is not None and track.field_baselines["title"] == "Accepted"
        item = session.scalar(
            select(ReviewedDomainItem).where(ReviewedDomainItem.outcome == "UPDATED")
        )
        assert item is not None and item.conflict_fields == []


def test_conflict_survives_priority_only_revision_until_exact_rebase(test_engine: Engine) -> None:
    ref = evidence(test_engine)
    with Session(test_engine) as session, session.begin():
        session.add(
            Coop(
                record_key="exact-coop",
                title="Before",
                priority="P2",
                status="NEW",
                needs_review=False,
            )
        )
    first = payload(
        ref,
        action="UPDATE",
        fields={"title": "Reviewed", "priority": "P2"},
        expected_version=1,
        expected_fields={"title": "Before", "priority": "P2"},
    )
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, first)["updated"] == 1
    with Session(test_engine) as session, session.begin():
        coop = session.scalar(select(Coop))
        assert coop is not None
        coop.title = "Local"
        coop.version += 1
    conflict = payload(ref, revision=2, action="UPDATE", fields={"title": "Proposal"})
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, conflict)["conflicted"] == 1
    priority_only = payload(ref, revision=3, action="UPDATE", fields={"priority": "P0"})
    with Session(test_engine) as session, session.begin():
        result = apply_manifest(session, priority_only)
        assert result["updated"] == 1 and result["conflicted"] == 1
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        track = session.scalar(select(ReviewedDomainTrack))
        item = session.get(ReviewedDomainItem, (track.batch_id, track.track_key)) if track else None
        assert coop is not None and (coop.title, coop.priority, coop.version) == ("Local", "P0", 4)
        assert track is not None and track.field_baselines == {
            "title": "Reviewed",
            "priority": "P0",
        }
        assert item is not None and item.outcome == "CONFLICT" and item.conflict_fields == ["title"]
        events = list(
            session.scalars(select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id))
        )
        assert len(events) == 3
        assert events[-1].action == "REVIEWED_DOMAIN_CONFLICT"
        assert events[-1].changes == {"priority": {"old": "P2", "new": "P0"}}
        assert events[-1].reason == "Conflict fields: title"
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, priority_only)["unchanged"] == 1
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 3
        assert session.scalar(select(func.count()).select_from(ReviewedDomainItem)) == 3
    stale_rebase = payload(
        ref,
        revision=4,
        action="UPDATE",
        fields={"title": "Accepted"},
        expected_version=3,
        expected_fields={"title": "Local"},
    )
    with (
        pytest.raises(ReviewError, match="version"),
        Session(test_engine) as session,
        session.begin(),
    ):
        apply_manifest(session, stale_rebase)
    wrong_snapshot = payload(
        ref,
        revision=4,
        action="UPDATE",
        fields={"title": "Accepted"},
        expected_version=4,
        expected_fields={"title": "Wrong"},
    )
    with (
        pytest.raises(ReviewError, match="snapshot"),
        Session(test_engine) as session,
        session.begin(),
    ):
        apply_manifest(session, wrong_snapshot)
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(ReviewedDomainBatch)) == 3
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 3
    resolved = payload(
        ref,
        revision=4,
        action="UPDATE",
        fields={"title": "Accepted"},
        expected_version=4,
        expected_fields={"title": "Local"},
    )
    with Session(test_engine) as session, session.begin():
        result = apply_manifest(session, resolved)
        assert result["updated"] == 1 and result["conflicted"] == 0
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        track = session.scalar(select(ReviewedDomainTrack))
        item = session.get(ReviewedDomainItem, (track.batch_id, track.track_key)) if track else None
        assert coop is not None and coop.title == "Accepted"
        assert track is not None and track.field_baselines["title"] == "Accepted"
        assert item is not None and item.outcome == "UPDATED" and item.conflict_fields == []
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 4
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, resolved)["unchanged"] == 1
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 4


def test_conflict_only_has_explicit_audit_and_no_false_update(test_engine: Engine) -> None:
    ref = evidence(test_engine)
    with Session(test_engine) as session, session.begin():
        session.add(
            Coop(
                record_key="exact-coop",
                title="Before",
                status="NEW",
                priority="P2",
                needs_review=False,
            )
        )
    initial = payload(
        ref,
        action="UPDATE",
        fields={"title": "Reviewed"},
        expected_version=1,
        expected_fields={"title": "Before"},
    )
    with Session(test_engine) as session, session.begin():
        apply_manifest(session, initial)
    with Session(test_engine) as session, session.begin():
        coop = session.scalar(select(Coop))
        assert coop is not None
        coop.title = "Local"
    conflict = payload(ref, revision=2, action="UPDATE", fields={"title": "Proposal"})
    with Session(test_engine) as session, session.begin():
        result = apply_manifest(session, conflict)
        assert result["conflicted"] == 1 and result["updated"] == 0
    with Session(test_engine) as session:
        events = list(
            session.scalars(select(AuditEvent).order_by(AuditEvent.created_at, AuditEvent.id))
        )
        assert len(events) == 2
        assert events[-1].action == "REVIEWED_DOMAIN_CONFLICT"
        assert events[-1].changes == {}
        assert events[-1].reason is not None and "title" in events[-1].reason


def test_invalid_revision_and_no_fuzzy_adoption_rollback(test_engine: Engine) -> None:
    ref = evidence(test_engine)
    with Session(test_engine) as session, session.begin():
        session.add(
            Coop(
                record_key="different-key",
                title="Reviewed coop",
                priority="P2",
                status="NEW",
                needs_review=True,
                uncertainty="Open",
            )
        )
    wrong = deepcopy(ref)
    wrong["source_record_id"] = str(uuid4())
    with (
        pytest.raises(ReviewError, match="evidence revision"),
        Session(test_engine) as session,
        session.begin(),
    ):
        apply_manifest(session, payload(wrong))
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, payload(ref))["created"] == 1
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(Coop)) == 2
        assert session.scalar(select(func.count()).select_from(ReviewedDomainBatch)) == 1


def test_replay_rejects_local_edits_and_atomic_batch_failure(test_engine: Engine) -> None:
    ref = evidence(test_engine)
    first = payload(ref)
    with Session(test_engine) as session, session.begin():
        apply_manifest(session, first)
    with Session(test_engine) as session, session.begin():
        coop = session.scalar(select(Coop).where(Coop.record_key == "exact-coop"))
        assert coop is not None
        coop.title = "Manual edit"
    with (
        pytest.raises(ReviewError, match="local divergence"),
        Session(test_engine) as session,
        session.begin(),
    ):
        apply_manifest(session, first)
    second = payload(ref, revision=2, action="UPDATE", fields={"priority": "P0"})
    invalid = deepcopy(second)
    invalid["decisions"][0]["evidence"][0]["revision_id"] = str(uuid4())  # type: ignore[index]
    invalid["decisions"][0]["decision_hash"] = canonical_hash(
        {  # type: ignore[index]
            k: v
            for k, v in invalid["decisions"][0].items()
            if k != "decision_hash"  # type: ignore[index]
        }
    )
    invalid["manifest_hash"] = canonical_hash(
        {k: v for k, v in invalid.items() if k != "manifest_hash"}
    )
    with (
        pytest.raises(ReviewError, match="evidence revision"),
        Session(test_engine) as session,
        session.begin(),
    ):
        apply_manifest(session, invalid)
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(ReviewedDomainBatch)) == 1
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_exclusion_is_audited_as_decision_without_business_mutation(test_engine: Engine) -> None:
    ref = evidence(test_engine)
    decision: dict[str, object] = {
        "track_key": "track-excluded",
        "track_revision": 1,
        "adjudication": "EXCLUDED",
        "action": "EXCLUDE",
        "evidence": [ref],
    }
    decision["decision_hash"] = canonical_hash(decision)
    manifest: dict[str, object] = {
        "schema_version": 1,
        "manifest_id": "review-excluded",
        "decisions": [decision],
    }
    manifest["manifest_hash"] = canonical_hash(manifest)
    with Session(test_engine) as session, session.begin():
        assert apply_manifest(session, manifest)["excluded"] == 1
    with Session(test_engine) as session:
        assert session.scalar(select(func.count()).select_from(Coop)) == 0
        assert session.scalar(select(ReviewedDomainTrack)).coop_id is None  # type: ignore[union-attr]
