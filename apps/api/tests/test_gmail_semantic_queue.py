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
