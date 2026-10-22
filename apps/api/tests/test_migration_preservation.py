from __future__ import annotations

import os
import subprocess
import sys
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text

from app.test_safety import assert_safe_test_connection

BASE_DATABASE_URL = "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test"
EXPECTED_SERVER_ADDRESS = os.environ.get("TEST_DATABASE_EXPECTED_SERVER_ADDRESS")
API_ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(database_url: str, revision: str) -> None:
    environment = {
        **os.environ,
        "APP_ENV": "development",
        "AUTH_MODE": "disabled",
        "DATABASE_URL": database_url,
    }
    # Invoke alembic through the interpreter already running the suite instead of
    # `uv run`, whose environment lock is held by an outer `uv run pytest` on CI and
    # stalls the migration tests indefinitely.
    subprocess.run(
        [sys.executable, "-m", "alembic", "upgrade", revision],
        cwd=API_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


def _downgrade_alembic(database_url: str, revision: str) -> None:
    environment = {
        **os.environ,
        "APP_ENV": "development",
        "AUTH_MODE": "disabled",
        "DATABASE_URL": database_url,
    }
    subprocess.run(
        [sys.executable, "-m", "alembic", "downgrade", revision],
        cwd=API_ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )


@pytest.mark.isolated_db
def test_m1_to_head_preserves_six_seed_coops_and_audit_history() -> None:
    schema = f"m3_preserve_{uuid.uuid4().hex}"
    admin = create_engine(BASE_DATABASE_URL)
    with admin.begin() as connection:
        assert_safe_test_connection(
            connection,
            BASE_DATABASE_URL,
            "test",
            expected_server_address=EXPECTED_SERVER_ADDRESS,
        )
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated_url = f"{BASE_DATABASE_URL}?options={quote(f'-csearch_path={schema}')}"
    isolated = create_engine(isolated_url)
    try:
        _run_alembic(isolated_url, "0fe02ef55c8a")
        with isolated.begin() as connection:
            organization_id = uuid.uuid4()
            connection.execute(
                text(
                    "INSERT INTO organization (id, name, normalized_name, kind) "
                    "VALUES (:id, 'Migration Seed', 'migration seed', 'BRAND')"
                ),
                {"id": organization_id},
            )
            known_seed_ids = [
                uuid.UUID(f"00000000-0000-4000-8000-{number:012d}") for number in range(301, 307)
            ]
            genuine_user_id = uuid.uuid4()
            for number, coop_id in enumerate([*known_seed_ids, genuine_user_id]):
                connection.execute(
                    text(
                        "INSERT INTO coop (id, title, status, brand_organization_id) "
                        "VALUES (:id, :title, 'NEW', :organization_id)"
                    ),
                    {
                        "id": coop_id,
                        "title": (
                            f"Migration seed {number}"
                            if coop_id in known_seed_ids
                            else "Genuine user cooperation"
                        ),
                        "organization_id": organization_id,
                    },
                )
                connection.execute(
                    text(
                        "INSERT INTO audit_event "
                        "(id, coop_id, action, actor_type, source, changes, request_id) "
                        "VALUES (:id, :coop_id, 'COOP_CREATED', 'SYSTEM', 'SEED', "
                        "CAST(:changes AS jsonb), :request_id)"
                    ),
                    {
                        "id": uuid.uuid4(),
                        "coop_id": coop_id,
                        "changes": '{"status":{"old":null,"new":"NEW"}}',
                        "request_id": f"migration-seed-{number}",
                    },
                )
            before = connection.execute(
                text("SELECT id, title, status, version FROM coop ORDER BY title")
            ).all()
            audit_before = connection.execute(
                text(
                    "SELECT coop_id, action, source, request_id FROM audit_event "
                    "ORDER BY request_id"
                )
            ).all()

        _run_alembic(isolated_url, "9e31c2b8a4f0")
        with isolated.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO ingestion_source (id, source_key, kind, enabled) "
                    "VALUES (:id, 'gmail-open-coops-v1', 'GMAIL', true)"
                ),
                {"id": uuid.uuid4()},
            )
        _run_alembic(isolated_url, "head")

        with isolated.connect() as connection:
            after = connection.execute(
                text("SELECT id, title, status, version FROM coop ORDER BY title")
            ).all()
            audit_after = connection.execute(
                text(
                    "SELECT coop_id, action, source, request_id FROM audit_event "
                    "ORDER BY request_id"
                )
            ).all()
            assert connection.execute(
                text("SELECT version_num FROM alembic_version")
            ).scalar_one() == ("6a8d1e42c907")
            record_keys = connection.execute(
                text(
                    "SELECT id, record_key, priority, fee_status, is_synthetic_seed, "
                    "needs_review, uncertainty "
                    "FROM coop ORDER BY title"
                )
            ).all()
            assert len({row.record_key for row in record_keys}) == 7
            assert all(row.record_key.startswith("coop-") for row in record_keys)
            assert all((row.priority, row.fee_status) == ("P2", "UNKNOWN") for row in record_keys)
            assert all(row.needs_review is True for row in record_keys)
            assert all(
                row.uncertainty == "Migrated record has not been explicitly reviewed."
                for row in record_keys
            )
            assert {row.id for row in record_keys if row.is_synthetic_seed} == set(known_seed_ids)
            assert (
                next(row for row in record_keys if row.id == genuine_user_id).is_synthetic_seed
                is False
            )
            assert {"manual_import_batch", "coop_import_state"} <= set(
                connection.execute(
                    text(
                        "SELECT table_name FROM information_schema.tables "
                        "WHERE table_schema = current_schema()"
                    )
                ).scalars()
            )
            source = connection.execute(
                text(
                    "SELECT source_key, kind, enabled FROM ingestion_source "
                    "WHERE source_key = 'gmail-open-coops-v1'"
                )
            ).one()
            assert source == ("gmail-open-coops-v1", "GMAIL", True)
            constraints = set(
                connection.execute(
                    text(
                        "SELECT constraint_name FROM information_schema.table_constraints "
                        "WHERE table_schema = current_schema() AND table_name IN "
                        "('ingestion_checkpoint', 'source_record_observation', "
                        "'source_record_revision')"
                    )
                ).scalars()
            )
            assert {
                "fk_ingestion_checkpoint_run",
                "fk_source_observation_run",
                "fk_source_observation_revision",
                "fk_source_record_revision_source_record",
            } <= constraints
        assert after == before
        assert audit_after == audit_before
        assert len(after) == 7
    finally:
        isolated.dispose()
        with admin.begin() as connection:
            assert_safe_test_connection(
                connection,
                BASE_DATABASE_URL,
                "test",
                expected_server_address=EXPECTED_SERVER_ADDRESS,
            )
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()


@pytest.mark.isolated_db
def test_operational_source_activation_downgrade_is_consumption_safe() -> None:
    schema = f"gmail_activation_{uuid.uuid4().hex}"
    admin = create_engine(BASE_DATABASE_URL)
    with admin.begin() as connection:
        assert_safe_test_connection(
            connection,
            BASE_DATABASE_URL,
            "test",
            expected_server_address=EXPECTED_SERVER_ADDRESS,
        )
        connection.execute(text(f'CREATE SCHEMA "{schema}"'))
    isolated_url = f"{BASE_DATABASE_URL}?options={quote(f'-csearch_path={schema}')}"
    isolated = create_engine(isolated_url)
    try:
        _run_alembic(isolated_url, "c2d7a91f4e60")
        with isolated.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT enabled FROM ingestion_source "
                        "WHERE source_key = 'gmail-open-coops-v1'"
                    )
                )
                is False
            )

        _run_alembic(isolated_url, "head")
        with isolated.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT enabled FROM ingestion_source "
                        "WHERE source_key = 'gmail-open-coops-v1'"
                    )
                )
                is True
            )

        _downgrade_alembic(isolated_url, "c2d7a91f4e60")
        with isolated.connect() as connection:
            assert (
                connection.scalar(
                    text(
                        "SELECT enabled FROM ingestion_source "
                        "WHERE source_key = 'gmail-open-coops-v1'"
                    )
                )
                is False
            )

        _run_alembic(isolated_url, "head")
        with isolated.begin() as connection:
            source_id = connection.scalar(
                text("SELECT id FROM ingestion_source WHERE source_key = 'gmail-open-coops-v1'")
            )
            connection.execute(
                text(
                    "INSERT INTO ingestion_run "
                    "(id, source_id, mode, status, summary, finished_at) "
                    "VALUES (:id, :source_id, 'FULL', 'SUCCEEDED', "
                    "'{}'::jsonb, now())"
                ),
                {"id": uuid.uuid4(), "source_id": source_id},
            )

        with pytest.raises(subprocess.CalledProcessError):
            _downgrade_alembic(isolated_url, "c2d7a91f4e60")
        with isolated.connect() as connection:
            assert connection.scalar(text("SELECT version_num FROM alembic_version")) == (
                "6a8d1e42c907"
            )
            assert (
                connection.scalar(
                    text(
                        "SELECT enabled FROM ingestion_source "
                        "WHERE source_key = 'gmail-open-coops-v1'"
                    )
                )
                is True
            )
    finally:
        isolated.dispose()
        with admin.begin() as connection:
            assert_safe_test_connection(
                connection,
                BASE_DATABASE_URL,
                "test",
                expected_server_address=EXPECTED_SERVER_ADDRESS,
            )
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
