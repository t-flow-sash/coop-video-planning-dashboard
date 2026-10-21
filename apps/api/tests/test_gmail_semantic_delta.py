"""Real PostgreSQL semantic worker transaction/replay boundary; synthetic mail only."""

from __future__ import annotations

from datetime import date
from uuid import UUID, uuid4

import pytest
from sqlalchemy import Engine, create_engine, func, select
from sqlalchemy.orm import Session

from app import database_access, semantic_database_access
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
        mid,
        "thread-1",
        when,
        sender,
        OWN if sender == OTHER else OTHER,
        "Synthetic",
        text,
        ("SENT",) if sender == OWN else (),
    )


class Client:
    def __init__(self, messages: list[ParsedMessage], fail: bool = False):
        self.messages = messages
        self.fail = fail
        self.calls = 0
        self.inventory_calls = 0

    def thread_inventory(self, thread_id: str) -> dict[str, tuple[int, tuple[str, ...]]]:
        assert thread_id == "thread-1"
        self.inventory_calls += 1
        return {m.message_id: (m.internal_date, m.labels) for m in self.messages}

    def fetch(self, message_id: str) -> ParsedMessage:
        self.calls += 1
        if self.fail:
            raise RuntimeError("sensitive provider diagnostic")
        return next(m for m in self.messages if m.message_id == message_id)


def setup(engine: Engine, *, existing: bool = False) -> tuple[UUID, UUID]:
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
                    "label_ids": [] if mid == "offer-1" else ["SENT"],
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
        message("offer-1", "Collaboration offer: fee 500 EUR", 100, OTHER),
        message("accept-1", "We accept fee 500 EUR", 200, OWN),
    ]


def test_constrained_writer_create_update_and_replay(test_engine: Engine) -> None:
    """Exercise the actual least-privilege login, not the fixture's admin role."""
    admin = test_engine.url.render_as_string(hide_password=False)
    restricted = (
        "postgresql+psycopg://coop_semantic_writer:semantic-test-only@127.0.0.1:5432/coop_test"
    )
    collector = (
        "postgresql+psycopg://coop_evidence_collector:collector-test-only@127.0.0.1:5432/coop_test"
    )
    semantic_database_access.provision(admin, restricted, collector)
    database_access.reconcile_collector_access(admin)
    semantic_database_access.reconcile(admin)
    writer = create_engine(restricted)
    try:
        source, revision = setup(test_engine)
        client = Client(sample())
        assert process_revision(writer, source, revision, client, OWN) == "CREATED"
        assert process_revision(writer, source, revision, client, OWN) == "UNCHANGED"
        with Session(test_engine) as session:
            assert session.scalar(select(func.count()).select_from(Coop)) == 1
            assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        # The DB fixture resets the case; the second test invocation below covers
        # adoption/update under the same restricted login in a fresh transaction.
    finally:
        writer.dispose()


def test_constrained_writer_existing_update_and_replay(test_engine: Engine) -> None:
    admin = test_engine.url.render_as_string(hide_password=False)
    restricted = (
        "postgresql+psycopg://coop_semantic_writer:semantic-test-only@127.0.0.1:5432/coop_test"
    )
    collector = (
        "postgresql+psycopg://coop_evidence_collector:collector-test-only@127.0.0.1:5432/coop_test"
    )
    semantic_database_access.provision(admin, restricted, collector)
    database_access.reconcile_collector_access(admin)
    semantic_database_access.reconcile(admin)
    writer = create_engine(restricted)
    try:
        source, revision = setup(test_engine, existing=True)
        client = Client(sample())
        assert process_revision(writer, source, revision, client, OWN) == "UPDATED"
        assert process_revision(writer, source, revision, client, OWN) == "UNCHANGED"
        with Session(test_engine) as session:
            coop = session.scalar(select(Coop))
            assert coop is not None and coop.fee_amount_minor == 50000
            assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
    finally:
        writer.dispose()


def test_create_after_collector_cursor_and_replay(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    with Session(test_engine) as session, session.begin():
        # The collector checkpoint is independent of semantic completion.
        assert session.scalar(select(func.count()).select_from(IngestionCheckpoint)) == 0
    client = Client(sample())
    assert process_revision(test_engine, source, revision, client, OWN) == "CREATED"
    assert process_revision(test_engine, source, revision, client, OWN) == "UNCHANGED"
    assert client.calls == 4
    assert client.inventory_calls == 4
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


def test_success_replay_missing_route_requires_review_without_erasing_success(
    test_engine: Engine,
) -> None:
    source, revision = setup(test_engine)
    client = Client(sample())
    assert process_revision(test_engine, source, revision, client, OWN) == "CREATED"
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    assert process_revision(test_engine, source, revision, client, OWN) == "REVIEW"
    with Session(test_engine) as session:
        state = session.get(SemanticMessageState, revision)
        assert state is not None and state.outcome == "CREATED"
        assert state.provenance["followup"]["reason_code"] == "exact_route_missing"
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_success_replay_retargeted_route_requires_review(test_engine: Engine) -> None:
    source, revision = setup(test_engine, existing=True)
    client = Client(sample())
    assert process_revision(test_engine, source, revision, client, OWN) == "UPDATED"
    with Session(test_engine) as session, session.begin():
        other = Coop(
            record_key="other", title="Other", priority="P1", status="NEW", needs_review=False
        )
        session.add(other)
        session.flush()
        route = session.scalar(select(SemanticTrackRoute))
        assert route is not None
        route.expected_coop_id = other.id
    assert process_revision(test_engine, source, revision, client, OWN) == "REVIEW"
    with Session(test_engine) as session:
        state = session.get(SemanticMessageState, revision)
        assert state is not None and state.outcome == "UPDATED"
        assert state.provenance["followup"]["reason_code"] == "route_or_target_changed"
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_success_replay_local_deadline_edit_requires_review(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    messages = [
        message("offer-1", "Collaboration offer: fee 500 EUR, deadline 2026-12-01", 100, OTHER),
        message("accept-1", "We accept fee 500 EUR and deadline 2026-12-01", 200, OWN),
    ]
    client = Client(messages)
    assert process_revision(test_engine, source, revision, client, OWN) == "CREATED"
    with Session(test_engine) as session, session.begin():
        coop = session.scalar(select(Coop))
        assert coop is not None and coop.deadline == date(2026, 12, 1)
        coop.deadline = date(2026, 12, 2)
    assert process_revision(test_engine, source, revision, client, OWN) == "REVIEW"
    with Session(test_engine) as session:
        state = session.get(SemanticMessageState, revision)
        assert state is not None and state.outcome == "CREATED"
        assert state.provenance["followup"]["reason_code"] == "linked_target_changed"
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_success_replay_later_cancellation_requires_review(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    client = Client(sample())
    assert process_revision(test_engine, source, revision, client, OWN) == "CREATED"
    client.messages.append(message("cancel-1", "We cancel the collaboration.", 300, OTHER))
    assert process_revision(test_engine, source, revision, client, OWN) == "REVIEW"
    assert client.inventory_calls == 3
    assert client.calls == 2
    with Session(test_engine) as session:
        state = session.get(SemanticMessageState, revision)
        assert state is not None and state.outcome == "CREATED"
        assert state.provenance["followup"]["reason_code"] == "thread_incomplete_or_changed"
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_success_replay_collected_new_deal_retraction_flags_review(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    client = Client(sample())
    assert process_revision(test_engine, source, revision, client, OWN) == "CREATED"
    with Session(test_engine) as session, session.begin():
        record = SourceRecord(source_id=source, record_type="GMAIL_MESSAGE", external_id="cancel-1")
        session.add(record)
        session.flush()
        session.add(
            SourceRecordRevision(
                source_id=source,
                source_record_id=record.id,
                content_hash="c" * 64,
                metadata_json={
                    "evidence_only": True,
                    "message_id": "cancel-1",
                    "thread_id": "thread-1",
                    "internal_date": "300",
                    "label_ids": [],
                },
            )
        )
    client.messages.append(message("cancel-1", "The deal is off.", 300, OTHER))
    assert process_revision(test_engine, source, revision, client, OWN) == "REVIEW"
    with Session(test_engine) as session:
        state = session.get(SemanticMessageState, revision)
        assert state is not None and state.outcome == "CREATED"
        assert state.provenance["followup"]["reason_code"] == "full_message_changed"
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1


def test_existing_exact_link_update_and_replay(test_engine: Engine) -> None:
    source, revision = setup(test_engine, existing=True)
    client = Client(sample())
    assert process_revision(test_engine, source, revision, client, OWN) == "UPDATED"
    assert process_revision(test_engine, source, revision, client, OWN) == "UNCHANGED"
    with Session(test_engine) as session:
        coop = session.scalar(select(Coop))
        assert coop is not None and coop.title == "Existing" and coop.fee_amount_minor == 50000
        linked = session.scalar(select(ReviewedDomainTrack))
        assert linked is not None and linked.coop_id == coop.id
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
        state = session.scalar(select(SemanticMessageState))
        assert state is not None and state.outcome == "REVIEW"


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
        coop = session.scalar(select(Coop))
        assert coop is not None and coop.fee_amount_minor is None
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


def test_uncollected_later_cancellation_blocks_auto_write(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    client = Client([*sample(), message("cancel-1", "We cancel the collaboration.", 300, OTHER)])
    assert process_revision(test_engine, source, revision, client, OWN) == "REVIEW"
    assert client.inventory_calls == 1
    assert client.calls == 0
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None
        state = session.scalar(select(SemanticMessageState))
        assert state is not None and state.reason_code == "thread_incomplete_or_changed"


def test_stale_collected_metadata_blocks_auto_write(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    current = sample()
    current[1] = message("accept-1", "We accept fee 500 EUR", 201, OWN)
    client = Client(current)
    assert process_revision(test_engine, source, revision, client, OWN) == "REVIEW"
    assert client.calls == 0
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None


def test_missing_full_message_requires_review(test_engine: Engine) -> None:
    source, revision = setup(test_engine)

    class MissingClient(Client):
        def fetch(self, message_id: str) -> ParsedMessage:
            raise LookupError("message not available")

    client = MissingClient(sample())
    assert process_revision(test_engine, source, revision, client, OWN) == "REVIEW"
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None


def test_new_message_arriving_during_full_fetch_blocks_auto_write(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    client = Client(sample())
    original_inventory = client.thread_inventory

    def changing_inventory(thread_id: str) -> dict[str, tuple[int, tuple[str, ...]]]:
        current = original_inventory(thread_id)
        if client.calls:
            current["cancel-1"] = (300, ())
        return current

    client.thread_inventory = changing_inventory  # type: ignore[method-assign]
    assert process_revision(test_engine, source, revision, client, OWN) == "REVIEW"
    assert client.inventory_calls == 2
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None


def test_revision_identity_cannot_cross_source(test_engine: Engine) -> None:
    _source, revision = setup(test_engine)
    assert process_revision(test_engine, uuid4(), revision, Client(sample()), OWN) == "REVIEW"
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None


def test_route_alone_does_not_authorize_creating_an_unknown_relationship(
    test_engine: Engine,
) -> None:
    source, revision = setup(test_engine)
    messages = [
        message("offer-1", "Offer fee 500 EUR", 100, OTHER),
        message("accept-1", "We accept fee 500 EUR", 200, OWN),
    ]
    assert process_revision(test_engine, source, revision, Client(messages), OWN) == "REVIEW"
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


@pytest.mark.parametrize(
    "offer_text",
    [
        "Past collaboration. Offer fee 500 EUR",
        "Our previous collaboration offer was for fee 500 EUR",
        "We won't offer collaboration; fee 500 EUR",
    ],
)
def test_historical_collaboration_mention_is_not_a_new_offer(
    test_engine: Engine, offer_text: str
) -> None:
    source, revision = setup(test_engine)
    messages = [
        message("offer-1", offer_text, 100, OTHER),
        message("accept-1", "We accept fee 500 EUR", 200, OWN),
    ]
    assert process_revision(test_engine, source, revision, Client(messages), OWN) == "REVIEW"
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None


def test_spoofed_own_from_incoming_mail_does_not_confirm_fee(test_engine: Engine) -> None:
    source, revision = setup(test_engine)
    offer = sample()[0]
    spoof = ParsedMessage(
        "accept-1", "thread-1", 200, OWN, OWN, "Synthetic", "We accept fee 500 EUR", ("INBOX",)
    )
    with Session(test_engine) as session, session.begin():
        observed = session.get(SourceRecordRevision, revision)
        assert observed is not None
        observed.metadata_json = {**observed.metadata_json, "label_ids": ["INBOX"]}
    assert process_revision(test_engine, source, revision, Client([offer, spoof]), OWN) == "REVIEW"
    with Session(test_engine) as session:
        assert session.scalar(select(Coop)) is None
