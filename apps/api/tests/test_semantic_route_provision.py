"""Exact identity provisioning: synthetic identifiers and MIME, disposable PostgreSQL."""

from __future__ import annotations

from dataclasses import replace
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.ingestion.gmail_route_provision import RouteClaim, RouteReview, provision_route
from app.ingestion.gmail_semantic_delta import _message_fingerprint, process_revision
from app.manual_coop_import import _snapshot
from app.models import (
    Coop,
    CoopImportState,
    ManualImportBatch,
    ReviewedDomainTrack,
    SemanticTrackRoute,
    SourceRecord,
    SourceRecordRevision,
)
from tests.test_gmail_semantic_delta import OWN, Client, sample, setup


def claim(
    engine: Engine,
    source: UUID,
    revision: UUID,
    *,
    track: str = "exact-track",
    key: str = "exact-coop",
) -> RouteClaim:
    with Session(engine) as session:
        rev = session.get(SourceRecordRevision, revision)
        assert rev is not None
        records = session.scalars(
            select(SourceRecord).where(
                SourceRecord.source_id == source, SourceRecord.record_type == "GMAIL_MESSAGE"
            )
        ).all()
        refs = []
        for record in records:
            latest = session.scalar(
                select(SourceRecordRevision).where(
                    SourceRecordRevision.source_record_id == record.id
                )
            )
            assert latest is not None
            refs.append((record.id, latest.id))
        return RouteClaim(
            source_id=source,
            thread_id="thread-1",
            evidence=tuple(refs),
            track_key=track,
            record_key=key,
            title="Synthetic cooperation",
            identity_attestation="reviewed-exact-business-identity",
            expected_coop_id=None,
            expected_version=None,
            expected_snapshot=None,
            message_fingerprints=tuple((m.message_id, _message_fingerprint(m)) for m in sample()),
        )


def test_discovery_to_provision_to_create_replay(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    client = Client(sample())
    assert process_revision(test_engine, source, revision, client, OWN) == "REVIEW"
    assert (
        provision_route(test_engine, claim(test_engine, source, revision), client, OWN)
        == "PROVISIONED"
    )
    assert process_revision(test_engine, source, revision, client, OWN) == "CREATED"
    assert process_revision(test_engine, source, revision, client, OWN) == "UNCHANGED"
    assert (
        provision_route(test_engine, claim(test_engine, source, revision), client, OWN)
        == "UNCHANGED"
    )


def test_provision_refuses_thread_only_identity_and_competing_track(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    client = Client(sample())
    with pytest.raises(RouteReview, match="identity_unverified"):
        provision_route(
            test_engine,
            replace(claim(test_engine, source, revision), identity_attestation="thread-1"),
            client,
            OWN,
        )
    assert (
        provision_route(test_engine, claim(test_engine, source, revision), client, OWN)
        == "PROVISIONED"
    )
    with pytest.raises(RouteReview, match="route_occupied"):
        provision_route(
            test_engine,
            claim(test_engine, source, revision, track="another-track", key="another-coop"),
            client,
            OWN,
        )
    assert (
        provision_route(test_engine, claim(test_engine, source, revision), client, OWN)
        == "UNCHANGED"
    )


def test_stale_revision_new_message_or_changed_body_blocks_provision(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    client = Client(sample())
    original = claim(test_engine, source, revision)
    with Session(test_engine) as session, session.begin():
        rev = session.get(SourceRecordRevision, revision)
        assert rev is not None
        rev.metadata_json = {**rev.metadata_json, "internal_date": "201"}
    with pytest.raises(RouteReview, match="evidence_changed"):
        provision_route(test_engine, original, client, OWN)
    with Session(test_engine) as session, session.begin():
        rev = session.get(SourceRecordRevision, revision)
        assert rev is not None
        rev.metadata_json = {**rev.metadata_json, "internal_date": "200"}
    client.messages[1] = sample()[1].__class__(
        **{**sample()[1].__dict__, "text": "We retract our agreement"}
    )
    with pytest.raises(RouteReview, match="evidence_changed"):
        provision_route(test_engine, original, client, OWN)


def test_manual_adoption_exact_snapshot_then_legacy_fenced(test_engine: Engine) -> None:
    source, revision = setup(test_engine, existing=True)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
        coop = session.scalar(select(Coop))
        assert coop is not None
        batch = ManualImportBatch(
            manifest_id="synthetic-batch", manifest_hash="b" * 64, schema_version=1, summary={}
        )
        session.add(batch)
        session.flush()
        session.add(
            CoopImportState(
                coop_id=coop.id,
                record_version=coop.version,
                content_hash="a" * 64,
                snapshot_hash=_snapshot(coop),
                batch_id=batch.id,
            )
        )
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        assert coop is not None
        state = session.get(CoopImportState, coop.id)
        assert state is not None
        exact = replace(
            claim(test_engine, source, revision),
            expected_coop_id=coop.id,
            expected_version=coop.version,
            expected_snapshot=state.snapshot_hash,
        )
    assert provision_route(test_engine, exact, Client(sample()), OWN) == "PROVISIONED"
    assert process_revision(test_engine, source, revision, Client(sample()), OWN) == "UPDATED"
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        assert coop is not None and coop.fee_amount_minor == 50000
        assert session.scalar(select(ReviewedDomainTrack.track_key)) == "exact-track"


def test_extra_uncollected_message_blocks_provision(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    client = Client(sample())
    original = claim(test_engine, source, revision)
    client.messages.append(sample()[1].__class__(**{**sample()[1].__dict__, "message_id": "extra"}))
    with pytest.raises(RouteReview, match="evidence_changed"):
        provision_route(test_engine, original, client, OWN)


def test_existing_target_requires_exact_manual_snapshot_version(test_engine: Engine) -> None:
    source, revision = setup(test_engine, existing=True)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        assert coop is not None
        identity = coop.id
    base = claim(test_engine, source, revision)
    with pytest.raises(RouteReview, match="target_identity_unverified"):
        provision_route(test_engine, base, Client(sample()), OWN)
    with pytest.raises(RouteReview, match="target_identity_unverified"):
        provision_route(
            test_engine,
            replace(
                base,
                expected_coop_id=identity,
                expected_version=1,
                expected_snapshot="0" * 64,
            ),
            Client(sample()),
            OWN,
        )
    with Session(test_engine) as session:
        assert len(list(session.scalars(select(SemanticTrackRoute)))) == 0
