"""Synthetic, isolated PostgreSQL queue integration; no live provider or production DB."""

from __future__ import annotations

import json

import pytest
from sqlalchemy import create_engine, func, select, text
from sqlalchemy.orm import Session
from test_gmail_semantic_delta import OWN, Client, sample, setup

from app.ingestion import gmail_semantic_queue as queue
from app.models import (
    AuditEvent,
    Coop,
    IngestionCheckpoint,
    IngestionRun,
    SemanticMessageState,
    SemanticQueueCursor,
    SourceRecordRevision,
)
from app.test_safety import assert_safe_test_connection, assert_safe_test_engine

pytestmark = pytest.mark.isolated_db
URL = "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_semqueue_test"


@pytest.fixture
def queue_engine():
    engine = create_engine(URL)
    assert_safe_test_engine(engine, URL, "test")
    with engine.begin() as conn:
        assert_safe_test_connection(conn, URL, "test")
        conn.execute(
            text(
                "TRUNCATE semantic_queue_cursor, semantic_message_state, semantic_track_route, "
                "source_record_observation, ingestion_item, source_record_revision, source_record, "
                "ingestion_checkpoint, reviewed_domain_item, reviewed_domain_evidence, "
                "reviewed_domain_track, reviewed_domain_batch, coop_import_state, "
                "manual_import_batch, audit_event, ingestion_run, ingestion_source, "
                "coop_contact, coop, contact, organization RESTART IDENTITY CASCADE"
            )
        )
    yield engine
    engine.dispose()


def test_checkpoint_independent_bounded_and_replay(queue_engine):
    source, _revision = setup(queue_engine)
    with Session(queue_engine) as session, session.begin():
        run = IngestionRun(source_id=source, mode="FULL", status="SUCCEEDED", summary={})
        session.add(run)
        session.flush()
        session.add(
            IngestionCheckpoint(
                source_id=source,
                checkpoint_key="history_id",
                checkpoint_value="999",
                query_hash="a" * 64,
                succeeded_run_id=run.id,
            )
        )
    client = Client(sample())
    first = queue.run_queue(queue_engine, source, client, OWN, max_batch=1)
    assert first["counts"]["CREATED"] == 1
    assert first["limit_reached"] is True
    assert "has_more" not in first
    with Session(queue_engine) as session:
        checkpoint = session.get(IngestionCheckpoint, (source, "history_id"))
        assert checkpoint.checkpoint_value == "999"
        assert session.scalar(select(func.count()).select_from(SemanticMessageState)) == 1
    second = queue.run_queue(queue_engine, source, client, OWN, max_batch=1)
    assert second["counts"]["UNCHANGED"] == 1
    assert second["limit_reached"] is True
    assert queue.run_queue(queue_engine, source, client, OWN, max_batch=1)["processed"] == 1
    with Session(queue_engine) as session:
        assert session.get(SemanticQueueCursor, source) is not None
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert session.scalar(select(func.count()).select_from(Coop)) == 1
        assert session.get(IngestionCheckpoint, (source, "history_id")).checkpoint_value == "999"
    assert "offer-1" not in json.dumps(first)
    assert "accept-1" not in json.dumps(first)


def test_review_retry_and_followup_priority(queue_engine):
    source, revision = setup(queue_engine)
    client = Client(sample())
    assert queue.process_revision(queue_engine, source, revision, client, OWN) == "CREATED"
    with Session(queue_engine) as session, session.begin():
        state = session.get(SemanticMessageState, revision)
        state.provenance = {**state.provenance, "followup": {"reason_code": "needs_review"}}
    # Query through the real runner; no synthetic queue shortcuts.
    # Pending offer must run first; then the successful accept follow-up is replayed.
    pending = queue.run_queue(queue_engine, source, client, OWN, max_batch=1)
    assert pending["counts"]["UNCHANGED"] == 1
    result = queue.run_queue(queue_engine, source, client, OWN, max_batch=1)
    assert result["counts"]["UNCHANGED"] == 1
    with Session(queue_engine) as session:
        assert "followup" not in session.get(SemanticMessageState, revision).provenance
    with Session(queue_engine) as session, session.begin():
        state = session.get(SemanticMessageState, revision)
        state.outcome = "RETRY"
    result = queue.run_queue(queue_engine, source, client, OWN, max_batch=1)
    assert result["processed"] == 1


def test_persistent_review_cannot_starve_completed_replay_at_batch_one(queue_engine, monkeypatch):
    source, created_revision = setup(queue_engine)
    client = Client(sample())
    assert queue.process_revision(queue_engine, source, created_revision, client, OWN) == "CREATED"
    with Session(queue_engine) as session:
        review_revision = session.scalar(
            select(SourceRecordRevision.id).where(SourceRecordRevision.id != created_revision)
        )
    assert review_revision is not None
    with Session(queue_engine) as session, session.begin():
        review_record = session.get(SourceRecordRevision, review_revision)
        assert review_record is not None
        session.add(
            SemanticMessageState(
                revision_id=review_revision,
                source_id=source,
                source_record_id=review_record.source_record_id,
                outcome="REVIEW",
                reason_code="needs_review",
                provenance={},
            )
        )
    real_processor = queue.process_revision
    processed_ids = []

    def persistent_review(engine, source_id, revision_id, fetcher, own_address):
        processed_ids.append(revision_id)
        if revision_id == review_revision:
            return "REVIEW"
        return real_processor(engine, source_id, revision_id, fetcher, own_address)

    monkeypatch.setattr(queue, "process_revision", persistent_review)
    assert queue.run_queue(queue_engine, source, client, OWN, max_batch=1)["counts"]["REVIEW"] == 1
    # A repeated review must not permanently monopolize the one available slot.
    second = queue.run_queue(queue_engine, source, client, OWN, max_batch=1)
    assert second["counts"]["UNCHANGED"] == 1, (second, processed_ids)
    assert queue.run_queue(queue_engine, source, client, OWN, max_batch=1)["counts"]["REVIEW"] == 1
    assert processed_ids == [review_revision, created_revision, review_revision]
    with Session(queue_engine) as session:
        cursor = session.get(SemanticQueueCursor, source)
        assert cursor is not None and cursor.revision_id == created_revision
        assert session.get(SemanticMessageState, created_revision).outcome == "CREATED"
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 1
        assert session.scalar(select(func.count()).select_from(Coop)) == 1


def test_replay_resumes_from_persisted_cursor_without_class_position(queue_engine, monkeypatch):
    source, first_revision = setup(queue_engine)
    client = Client(sample())
    assert queue.process_revision(queue_engine, source, first_revision, client, OWN) == "CREATED"
    with Session(queue_engine) as session:
        second_revision = session.scalar(
            select(SourceRecordRevision.id).where(SourceRecordRevision.id != first_revision)
        )
    assert second_revision is not None
    assert queue.process_revision(queue_engine, source, second_revision, client, OWN) == "UNCHANGED"
    with Session(queue_engine) as session, session.begin():
        first = session.get(SourceRecordRevision, first_revision)
        second = session.get(SourceRecordRevision, second_revision)
        assert first is not None and second is not None
        anchor = min((first, second), key=lambda row: (row.observed_at, row.id))
        expected = second_revision if anchor.id == first_revision else first_revision
        session.add(
            SemanticQueueCursor(
                source_id=source,
                revision_id=anchor.id,
                observed_at=anchor.observed_at,
                positions={},
            )
        )
    visited = []
    real_processor = queue.process_revision

    def record_replay(engine, source_id, revision_id, fetcher, own_address):
        visited.append(revision_id)
        return real_processor(engine, source_id, revision_id, fetcher, own_address)

    monkeypatch.setattr(queue, "process_revision", record_replay)
    assert (
        queue.run_queue(queue_engine, source, client, OWN, max_batch=1)["counts"]["UNCHANGED"] == 1
    )
    assert visited == [expected]


def test_fail_closed_sanitized_and_bounds(queue_engine, monkeypatch, capsys):
    source, _revision = setup(queue_engine)
    client = Client(sample())
    with pytest.raises(ValueError):
        queue.run_queue(queue_engine, source, client, OWN, max_batch=101)
    assert client.calls == 0
    monkeypatch.setattr(queue, "default_engine", queue_engine)
    monkeypatch.setattr(
        queue,
        "process_revision",
        lambda *args: (_ for _ in ()).throw(RuntimeError("private mail body")),
    )
    monkeypatch.setattr(queue, "FullMessageClient", lambda _: client)
    monkeypatch.setattr(
        queue.GmailConfig, "from_env", lambda: type("Config", (), {"account": OWN})()
    )
    monkeypatch.setattr(
        "sys.argv", ["queue", "--source-key", "synthetic-semantic", "--max-batch", "1"]
    )
    assert queue.main() == 2
    output = capsys.readouterr()
    assert "private mail body" not in str(output)
    assert output.err.strip() == '{"outcome": "RETRY"}'
    assert output.out == ""
