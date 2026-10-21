from __future__ import annotations

from copy import deepcopy
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
