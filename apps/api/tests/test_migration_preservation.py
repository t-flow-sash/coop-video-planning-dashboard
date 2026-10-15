from __future__ import annotations

import os
import subprocess
import uuid
from pathlib import Path
from urllib.parse import quote

import pytest
from sqlalchemy import create_engine, text

from app.test_safety import assert_safe_test_connection

BASE_DATABASE_URL = "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test"
API_ROOT = Path(__file__).resolve().parents[1]


def _run_alembic(database_url: str, revision: str) -> None:
    environment = {
        **os.environ,
        "APP_ENV": "development",
        "AUTH_MODE": "disabled",
        "DATABASE_URL": database_url,
    }
    subprocess.run(
        ["uv", "run", "alembic", "upgrade", revision],
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
        assert_safe_test_connection(connection, BASE_DATABASE_URL, "test")
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
            for number in range(6):
                coop_id = uuid.uuid4()
                connection.execute(
                    text(
                        "INSERT INTO coop (id, title, status, brand_organization_id) "
                        "VALUES (:id, :title, 'NEW', :organization_id)"
                    ),
                    {
                        "id": coop_id,
                        "title": f"Migration seed {number}",
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
            ).scalar_one() == ("9e31c2b8a4f0")
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
        assert len(after) == 6
    finally:
        isolated.dispose()
        with admin.begin() as connection:
            assert_safe_test_connection(connection, BASE_DATABASE_URL, "test")
            connection.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
        admin.dispose()
