"""Real PostgreSQL semantic worker transaction/replay boundary; synthetic mail only."""

from __future__ import annotations

from uuid import uuid4

from sqlalchemy import Engine, func, select
from sqlalchemy.orm import Session

from app.ingestion.gmail_semantic import ParsedMessage
from app.ingestion.gmail_semantic_delta import process_revision
from app.models import (
    AuditEvent,
    Coop,
    IngestionCheckpoint,
    IngestionSource,
    ReviewedDomainTrack,
    SemanticMessageState,
    SemanticTrackRoute,
    SourceRecord,
    SourceRecordRevision,
)

OWN = "own@example.invalid"
OTHER = "other@example.invalid"


def message(mid: str, text: str, when: int, sender: str) -> ParsedMessage:
    return ParsedMessage(
        mid, "thread-1", when, sender, OWN if sender == OTHER else OTHER, "Synthetic", text, ()
    )


class Client:
    def __init__(self, messages: list[ParsedMessage], fail: bool = False):
        self.messages = messages
        self.fail = fail
        self.calls = 0

    def fetch(self, message_id: str) -> ParsedMessage:
        self.calls += 1
        if self.fail:
            raise RuntimeError("sensitive provider diagnostic")
        return next(m for m in self.messages if m.message_id == message_id)


def setup(engine: Engine, *, existing: bool = False) -> tuple[object, object]:
    with Session(engine) as session, session.begin():
        source = IngestionSource(source_key="synthetic-semantic", kind="GMAIL", enabled=True)
        session.add(source)
        session.flush()
        refs = []
        for mid in ("offer-1", "accept-1"):
            rec = SourceRecord(source_id=source.id, record_type="GMAIL_MESSAGE", external_id=mid)
            session.add(rec)
            session.flush()
            rev = SourceRecordRevision(
                source_id=source.id,
                source_record_id=rec.id,
                content_hash=("a" if mid == "offer-1" else "b") * 64,
                metadata_json={
                    "evidence_only": True,
                    "message_id": mid,
                    "thread_id": "thread-1",
                    "internal_date": "100" if mid == "offer-1" else "200",
                    "label_ids": [],
                },
            )
            session.add(rev)
            session.flush()
            refs.append(rev.id)
        thread = SourceRecord(
            source_id=source.id, record_type="GMAIL_THREAD", external_id="thread-1"
        )
        session.add(thread)
        session.flush()
        if existing:
            coop = Coop(
                record_key="exact-coop",
                title="Existing",
                priority="P1",
                status="NEW",
                needs_review=False,
            )
            session.add(coop)
            session.flush()
            route = SemanticTrackRoute(
                source_id=source.id,
                thread_record_id=thread.id,
                track_key="exact-track",
                record_key="exact-coop",
                title="Synthetic cooperation",
                expected_coop_id=coop.id,
            )
        else:
            route = SemanticTrackRoute(
                source_id=source.id,
                thread_record_id=thread.id,
                track_key="exact-track",
                record_key="exact-coop",
                title="Synthetic cooperation",
            )
        session.add(route)
        return source.id, refs[-1]


def sample() -> list[ParsedMessage]:
    return [
        message("offer-1", "Collaboration. Offer fee 500 EUR", 100, OTHER),
        message("accept-1", "We accept fee 500 EUR", 200, OWN),
    ]


def test_create_after_collector_cursor_and_replay(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    with Session(test_engine) as session, session.begin():
        # The collector checkpoint is independent of semantic completion.
        assert session.scalar(select(func.count()).select_from(IngestionCheckpoint)) == 0
    client = Client(sample())
    assert process_revision(test_engine, source, revision, client, OWN) == "CREATED"
    assert process_revision(test_engine, source, revision, client, OWN) == "UNCHANGED"
    assert client.calls == 2
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        assert coop is not None and coop.fee_amount_minor == 50000 and coop.status == "CONFIRMED"
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        state = session.scalar(
            select(SemanticMessageState).where(SemanticMessageState.revision_id == revision)
        )
        assert state is not None and state.outcome == "CREATED"
        assert state.provenance["fee"][0]["assertion_sha256"]
        assert "Offer fee" not in str(state.provenance)


def test_existing_exact_link_update_and_replay(test_engine: Engine) -> None:
    source, revision = setup(test_engine, existing=True)
    client = Client(sample())
    assert process_revision(test_engine, source, revision, client, OWN) == "UPDATED"
    assert process_revision(test_engine, source, revision, client, OWN) == "UNCHANGED"
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        assert coop is not None and coop.title == "Existing" and coop.fee_amount_minor == 50000
        assert session.scalar(select(ReviewedDomainTrack)).coop_id == coop.id
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_retry_after_fetch_failure_and_review_ambiguous(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    assert (
        process_revision(test_engine, source, revision, Client(sample(), fail=True), OWN) == "RETRY"
    )
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None
        state = session.scalar(select(SemanticMessageState))
        assert (
            state is not None
            and state.outcome == "RETRY"
            and "sensitive" not in str(state.provenance)
        )
    ambiguous = Client([sample()[0], message("accept-1", "We propose fee 500 EUR", 200, OWN)])
    assert process_revision(test_engine, source, revision, ambiguous, OWN) == "REVIEW"
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None
        assert session.scalar(select(SemanticMessageState)).outcome == "REVIEW"


def test_mismatch_and_unlinked_target_cannot_auto_write(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    mismatched = Client([sample()[0], message("accept-1", "We accept fee 500 EUR", 201, OWN)])
    assert process_revision(test_engine, source, revision, mismatched, OWN) == "REVIEW"
    with Session(test_engine) as session, session.begin():
        session.add(
            Coop(
                record_key="exact-coop",
                title="Unlinked",
                priority="P1",
                status="NEW",
                needs_review=False,
            )
        )
    assert process_revision(test_engine, source, revision, Client(sample()), OWN) == "REVIEW"
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)).fee_amount_minor is None
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_revision_identity_cannot_cross_source(test_engine: Engine) -> None:
    _source, revision = setup(test_engine)
    assert process_revision(test_engine, uuid4(), revision, Client(sample()), OWN) == "REVIEW"
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None
