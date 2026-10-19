from __future__ import annotations

import os
import uuid
from typing import Any

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.exc import DBAPIError

from app import database_access

COLLECTOR_ROLE = "coop_evidence_collector"
DEFAULT_COLLECTOR_TEST_URL = (
    "postgresql+psycopg://coop_evidence_collector:collector-test-only@127.0.0.1:5432/coop_test"
)
EVIDENCE_TABLES = {
    "ingestion_source",
    "ingestion_run",
    "ingestion_checkpoint",
    "source_record",
    "source_record_revision",
    "ingestion_item",
    "source_record_observation",
}


def _collector_test_url(test_engine: Engine) -> str:
    collector_url = os.environ.get("COLLECTOR_DATABASE_URL", DEFAULT_COLLECTOR_TEST_URL)
    database_access.collector_password_from_urls(
        test_engine.url.render_as_string(hide_password=False), collector_url
    )
    return collector_url


def _privilege(connection: Any, table: str, privilege: str) -> bool:
    return bool(
        connection.scalar(
            text("SELECT has_table_privilege(:role, :table, :privilege)"),
            {"role": COLLECTOR_ROLE, "table": table, "privilege": privilege},
        )
    )


@pytest.mark.integration
def test_collector_role_is_evidence_only(test_engine: Engine) -> None:
    with test_engine.connect() as connection:
        attributes = connection.execute(
            text(
                "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
                "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = :role"
            ),
            {"role": COLLECTOR_ROLE},
        ).one()
        assert tuple(attributes) == (True, False, False, False, False, False, False)
        assert (
            connection.scalar(
                text(
                    "SELECT count(*) FROM pg_auth_members membership "
                    "JOIN pg_roles child ON child.oid = membership.member "
                    "WHERE child.rolname = :role"
                ),
                {"role": COLLECTOR_ROLE},
            )
            == 0
        )
        tables = set(
            connection.execute(
                text(
                    "SELECT table_name FROM information_schema.tables "
                    "WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
                )
            ).scalars()
        )
        for table in tables:
            if table in EVIDENCE_TABLES:
                assert _privilege(connection, table, "SELECT")
                assert _privilege(connection, table, "INSERT") is (table != "ingestion_source")
                assert not _privilege(connection, table, "DELETE")
                assert not _privilege(connection, table, "TRUNCATE")
            else:
                for privilege in ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE"):
                    assert not _privilege(connection, table, privilege), (table, privilege)
        assert connection.scalar(
            text("SELECT has_schema_privilege(:role, current_schema(), 'USAGE')"),
            {"role": COLLECTOR_ROLE},
        )
        assert not connection.scalar(
            text("SELECT has_schema_privilege(:role, current_schema(), 'CREATE')"),
            {"role": COLLECTOR_ROLE},
        )
        assert connection.scalar(
            text("SELECT has_database_privilege(:role, current_database(), 'CONNECT')"),
            {"role": COLLECTOR_ROLE},
        )
        assert not connection.scalar(
            text("SELECT has_database_privilege(:role, current_database(), 'CREATE')"),
            {"role": COLLECTOR_ROLE},
        )
        assert not connection.scalar(
            text("SELECT has_database_privilege(:role, current_database(), 'TEMPORARY')"),
            {"role": COLLECTOR_ROLE},
        )
        update_columns = set(
            connection.execute(
                text(
                    "SELECT table_name, column_name FROM information_schema.column_privileges "
                    "WHERE table_schema = current_schema() AND grantee = :role "
                    "AND privilege_type = 'UPDATE'"
                ),
                {"role": COLLECTOR_ROLE},
            )
        )
        assert update_columns == {
            ("ingestion_run", "status"),
            ("ingestion_run", "summary"),
            ("ingestion_run", "finished_at"),
            ("ingestion_run", "error_code"),
            ("ingestion_checkpoint", "checkpoint_value"),
            ("ingestion_checkpoint", "query_hash"),
            ("ingestion_checkpoint", "succeeded_run_id"),
            ("ingestion_checkpoint", "updated_at"),
        }


@pytest.mark.integration
def test_acl_reconciler_revokes_preexisting_business_privilege(test_engine: Engine) -> None:
    with test_engine.begin() as connection:
        connection.execute(text(f"GRANT INSERT ON coop TO {COLLECTOR_ROLE}"))
        assert _privilege(connection, "coop", "INSERT")

    database_access.reconcile_collector_access(
        test_engine.url.render_as_string(hide_password=False)
    )

    with test_engine.connect() as connection:
        assert not _privilege(connection, "coop", "INSERT")


@pytest.mark.integration
def test_acl_reconciler_fails_closed_on_public_business_privilege(test_engine: Engine) -> None:
    with test_engine.begin() as connection:
        connection.execute(text("GRANT INSERT ON coop TO PUBLIC"))
    try:
        with pytest.raises(RuntimeError, match="table ACL attestation"):
            database_access.reconcile_collector_access(
                test_engine.url.render_as_string(hide_password=False)
            )
    finally:
        with test_engine.begin() as connection:
            connection.execute(text("REVOKE INSERT ON coop FROM PUBLIC"))
        database_access.reconcile_collector_access(
            test_engine.url.render_as_string(hide_password=False)
        )


@pytest.mark.integration
def test_acl_reconciler_preserves_existing_function_access_for_unrelated_roles(
    test_engine: Engine,
) -> None:
    role = f"unrelated_reader_{uuid.uuid4().hex}"
    with test_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE ROLE "{role}"')
        connection.execute(
            text("GRANT EXECUTE ON FUNCTION public.similarity(text, text) TO PUBLIC")
        )
        assert connection.scalar(
            text("SELECT has_function_privilege(:role, 'public.similarity(text,text)', 'EXECUTE')"),
            {"role": role},
        )
    try:
        database_access.reconcile_collector_access(
            test_engine.url.render_as_string(hide_password=False)
        )
        with test_engine.connect() as connection:
            assert connection.scalar(
                text(
                    "SELECT has_function_privilege(:role, "
                    "'public.similarity(text,text)', 'EXECUTE')"
                ),
                {"role": role},
            )
    finally:
        with test_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP OWNED BY "{role}"')
            connection.exec_driver_sql(f'DROP ROLE IF EXISTS "{role}"')


@pytest.mark.integration
def test_acl_reconciler_fails_closed_on_collector_owned_schema(test_engine: Engine) -> None:
    database_name = test_engine.url.database
    assert database_name is not None
    collector_engine = create_engine(_collector_test_url(test_engine))
    with test_engine.begin() as connection:
        connection.execute(text(f"GRANT CREATE ON DATABASE {database_name} TO {COLLECTOR_ROLE}"))
    try:
        with collector_engine.begin() as connection:
            connection.execute(text("CREATE SCHEMA collector_owned"))
        with test_engine.begin() as connection:
            connection.execute(
                text(f"REVOKE CREATE ON DATABASE {database_name} FROM {COLLECTOR_ROLE}")
            )
        with pytest.raises(RuntimeError, match="owns database objects"):
            database_access.reconcile_collector_access(
                test_engine.url.render_as_string(hide_password=False)
            )
    finally:
        with collector_engine.begin() as connection:
            connection.execute(text("DROP SCHEMA IF EXISTS collector_owned CASCADE"))
        collector_engine.dispose()
        with test_engine.begin() as connection:
            connection.execute(
                text(f"REVOKE CREATE ON DATABASE {database_name} FROM {COLLECTOR_ROLE}")
            )


@pytest.mark.integration
def test_collector_role_cannot_mutate_business_tables(test_engine: Engine) -> None:
    collector_engine = create_engine(_collector_test_url(test_engine))
    try:
        with (
            collector_engine.connect() as connection,
            connection.begin(),
            pytest.raises(DBAPIError),
        ):
            connection.execute(
                text(
                    "INSERT INTO organization (id, name, normalized_name, kind) "
                    "VALUES (:id, 'Forbidden', 'forbidden', 'BRAND')"
                ),
                {"id": uuid.uuid4()},
            )
    finally:
        collector_engine.dispose()


@pytest.mark.integration
def test_collector_role_cannot_create_or_activate_source(test_engine: Engine) -> None:
    collector_engine = create_engine(_collector_test_url(test_engine))
    try:
        with (
            collector_engine.connect() as connection,
            connection.begin(),
            pytest.raises(DBAPIError),
        ):
            connection.execute(
                text(
                    "INSERT INTO ingestion_source (id, source_key, kind, enabled) "
                    "VALUES (:id, 'forbidden-source', 'GMAIL', true)"
                ),
                {"id": uuid.uuid4()},
            )
    finally:
        collector_engine.dispose()


@pytest.mark.integration
def test_collector_role_can_persist_evidence_snapshot(test_engine: Engine) -> None:
    from sqlalchemy.orm import sessionmaker

    from app.ingestion.gmail import SOURCE_KEY, FullSnapshot, persist_full_snapshot

    with test_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ingestion_source (id, source_key, kind, enabled) "
                "VALUES (:id, :source_key, 'GMAIL', true)"
            ),
            {"id": uuid.uuid4(), "source_key": SOURCE_KEY},
        )

    collector_engine = create_engine(_collector_test_url(test_engine))
    collector_sessions = sessionmaker(bind=collector_engine, expire_on_commit=False)
    try:
        with collector_sessions.begin() as session:
            report = persist_full_snapshot(
                session,
                FullSnapshot("100", "100", ()),
                query="in:anywhere evidence-probe",
            )
            assert report.status == "SUCCEEDED"
            session.rollback()
    finally:
        collector_engine.dispose()


@pytest.mark.integration
def test_collector_role_can_commit_delta_and_advance_checkpoint(test_engine: Engine) -> None:
    from sqlalchemy.orm import sessionmaker

    from app.ingestion.gmail import (
        SOURCE_KEY,
        DeltaSnapshot,
        FullSnapshot,
        persist_delta_snapshot,
        persist_full_snapshot,
    )

    with test_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ingestion_source (id, source_key, kind, enabled) "
                "VALUES (:id, :source_key, 'GMAIL', true)"
            ),
            {"id": uuid.uuid4(), "source_key": SOURCE_KEY},
        )

    collector_engine = create_engine(_collector_test_url(test_engine))
    collector_sessions = sessionmaker(bind=collector_engine, expire_on_commit=False)
    try:
        with collector_sessions.begin() as session:
            persist_full_snapshot(session, FullSnapshot("100", "100", ()), query="query")
        with collector_sessions.begin() as session:
            report = persist_delta_snapshot(
                session,
                DeltaSnapshot("100", "110", ()),
                query="query",
            )
            assert report.status == "SUCCEEDED"
    finally:
        collector_engine.dispose()

    with test_engine.connect() as connection:
        assert connection.scalar(text("SELECT checkpoint_value FROM ingestion_checkpoint")) == "110"


@pytest.mark.integration
def test_collector_role_can_terminalize_committed_pilot_failure(test_engine: Engine) -> None:
    from sqlalchemy.orm import sessionmaker

    from app.ingestion.gmail import (
        PILOT_RUN_ID,
        SOURCE_KEY,
        claim_pilot_slot,
        mark_claimed_pilot_failed,
    )
    from app.models import IngestionSource

    source_id = uuid.uuid4()
    with test_engine.begin() as connection:
        connection.execute(
            text(
                "INSERT INTO ingestion_source (id, source_key, kind, enabled) "
                "VALUES (:id, :source_key, 'GMAIL', true)"
            ),
            {"id": source_id, "source_key": SOURCE_KEY},
        )

    collector_engine = create_engine(_collector_test_url(test_engine))
    collector_sessions = sessionmaker(bind=collector_engine, expire_on_commit=False)
    try:
        with collector_sessions.begin() as session:
            source = session.get(IngestionSource, source_id)
            assert source is not None
            run_id = claim_pilot_slot(session, source)

        with test_engine.begin() as connection:
            connection.execute(
                text("UPDATE ingestion_source SET enabled = false WHERE id = :id"),
                {"id": source_id},
            )

        with collector_sessions.begin() as session:
            mark_claimed_pilot_failed(session, run_id, "transport_unavailable")
    finally:
        collector_engine.dispose()

    with test_engine.connect() as connection:
        run = connection.execute(
            text(
                "SELECT status, error_code, finished_at, summary FROM ingestion_run WHERE id = :id"
            ),
            {"id": PILOT_RUN_ID},
        ).one()
        assert run.status == "FAILED"
        assert run.error_code == "transport_unavailable"
        assert run.finished_at is not None
        assert run.summary == {"pilot_slot": "gmail-full-pilot-v1"}
