"""Operational status is derived from persisted evidence, not collector success claims."""

from datetime import UTC, datetime, timedelta

from fastapi.testclient import TestClient
from sqlalchemy import Engine
from sqlalchemy.orm import Session

from app.config import Settings
from app.main import create_app
from app.models import (
    Coop,
    IngestionRun,
    IngestionSource,
    SemanticMessageState,
    SourceRecord,
    SourceRecordRevision,
)


def test_authenticated_semantic_status_counts_latest_revisions_and_never_claims_coverage(
    test_engine: Engine,
) -> None:
    now = datetime.now(UTC)
    with Session(test_engine) as session, session.begin():
        source = IngestionSource(source_key="synthetic", kind="GMAIL", enabled=True)
        session.add(source)
        session.flush()
        session.add(
            IngestionRun(
                source_id=source.id,
                mode="DELTA",
                status="SUCCEEDED",
                started_at=now - timedelta(hours=2),
                finished_at=now - timedelta(hours=2),
            )
        )
        session.add(
            Coop(
                record_key="manual", title="Manual", priority="P1", status="NEW", needs_review=False
            )
        )
        for key, revisions in (
            ("first", (("a", "CREATED"), ("b", "REVIEW"))),
            ("second", (("c", "UPDATED"),)),
            ("third", (("d", None),)),
        ):
            record = SourceRecord(source_id=source.id, record_type="GMAIL_MESSAGE", external_id=key)
            session.add(record)
            session.flush()
            for hash_character, outcome in revisions:
                revision = SourceRecordRevision(
                    source_id=source.id,
                    source_record_id=record.id,
                    content_hash=hash_character * 64,
                    metadata_json={},
                    observed_at=now - timedelta(minutes=10 if hash_character != "a" else 60),
                )
                session.add(revision)
                session.flush()
                if outcome:
                    session.add(
                        SemanticMessageState(
                            source_id=source.id,
                            source_record_id=record.id,
                            revision_id=revision.id,
                            outcome=outcome,
                            reason_code="test",
                            provenance={},
                            updated_at=now,
                        )
                    )
    settings = Settings(
        app_env="test",
        auth_mode="basic",
        auth_username="operator",
        auth_password="synthetic-password",
    )
    with TestClient(create_app(settings, test_engine)) as client:
        assert client.get("/api/v1/semantic/status").status_code == 401
        response = client.get("/api/v1/semantic/status", auth=("operator", "synthetic-password"))
    assert response.status_code == 200
    data = response.json()
    assert data["domain_records"] == 1
    assert data["coverage_verified"] is False
    assert data["sources"][0]["discovered"] == 3
    assert data["sources"][0]["processed"] == 2
    assert data["sources"][0]["created"] == 0  # superseded by latest review
    assert data["sources"][0]["updated"] == 1
    assert data["sources"][0]["review"] == 1
    assert data["sources"][0]["backlog"] == 1
    assert data["sources"][0]["last_collector_run"]["status"] == "SUCCEEDED"
    assert data["sources"][0]["last_semantic_activity"] is not None
    assert "synthetic" not in str(data)  # no source keys or external identities


def test_retry_is_unresolved_and_failed_collector_has_no_success_age(test_engine: Engine) -> None:
    now = datetime.now(UTC)
    with Session(test_engine) as session, session.begin():
        source = IngestionSource(source_key="synthetic", kind="GMAIL", enabled=True)
        session.add(source)
        session.flush()
        for status, offset in (("SUCCEEDED", 120), ("FAILED", 10)):
            session.add(
                IngestionRun(
                    source_id=source.id,
                    mode="DELTA",
                    status=status,
                    started_at=now - timedelta(minutes=offset + 1),
                    finished_at=now - timedelta(minutes=offset),
                )
            )
        record = SourceRecord(source_id=source.id, record_type="GMAIL_MESSAGE", external_id="retry")
        session.add(record)
        session.flush()
        revision = SourceRecordRevision(
            source_id=source.id,
            source_record_id=record.id,
            content_hash="e" * 64,
            metadata_json={},
            observed_at=now - timedelta(minutes=30),
        )
        session.add(revision)
        session.flush()
        session.add(
            SemanticMessageState(
                source_id=source.id,
                source_record_id=record.id,
                revision_id=revision.id,
                outcome="RETRY",
                reason_code="test",
                provenance={},
                updated_at=now,
            )
        )
    with TestClient(
        create_app(
            Settings(
                app_env="test",
                auth_mode="basic",
                auth_username="operator",
                auth_password="synthetic-password",
            ),
            test_engine,
        )
    ) as client:
        response = client.get("/api/v1/semantic/status", auth=("operator", "synthetic-password"))
    assert response.status_code == 200
    status = response.json()["sources"][0]
    assert status["discovered"] == 1
    assert status["processed"] == 0
    assert status["retry"] == 1
    assert status["backlog"] == 1
    assert status["last_collector_run"]["status"] == "FAILED"
    assert status["collector_age_seconds"] is None


def test_empty_status_is_unknown_not_complete(test_engine: Engine) -> None:
    settings = Settings(
        app_env="test",
        auth_mode="basic",
        auth_username="operator",
        auth_password="synthetic-password",
    )
    with TestClient(create_app(settings, test_engine)) as client:
        response = client.get("/api/v1/semantic/status", auth=("operator", "synthetic-password"))
    assert response.status_code == 200
    assert response.json()["sources"] == []
    assert response.json()["coverage_verified"] is False
