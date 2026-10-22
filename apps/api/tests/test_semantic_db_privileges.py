"""Real disposable coop_test ACL probes; never use a production database."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import Engine, create_engine, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import DBAPIError

from app import database_access
from app import semantic_database_access as writer

TEST_WRITER_URL = (
    "postgresql+psycopg://coop_semantic_writer:semantic-test-only@127.0.0.1:5432/coop_test"
)
TEST_COLLECTOR_URL = (
    "postgresql+psycopg://coop_evidence_collector:collector-test-only@127.0.0.1:5432/coop_test"
)


def _urls(test_engine: Engine) -> tuple[str, str, str]:
    admin = test_engine.url.render_as_string(hide_password=False)
    restricted = os.environ.get("SEMANTIC_DATABASE_URL", TEST_WRITER_URL)
    collector = os.environ.get("COLLECTOR_DATABASE_URL", TEST_COLLECTOR_URL)
    writer.writer_password_from_urls(admin, restricted, collector)
    return admin, restricted, collector


@pytest.mark.integration
def test_semantic_role_login_effective_acl_and_collector_isolation(test_engine: Engine) -> None:
    admin, restricted, collector = _urls(test_engine)
    writer.provision(admin, restricted, collector)
    database_access.reconcile_collector_access(admin)
    writer.reconcile(admin)
    with test_engine.connect() as connection:
        assert connection.scalar(
            text("SELECT has_table_privilege(:r,'coop','INSERT')"),
            {"r": writer.ROLE},
        )
        assert connection.scalar(
            text("SELECT has_table_privilege(:r,'semantic_queue_cursor','SELECT')"),
            {"r": writer.ROLE},
        )
        assert connection.scalar(
            text("SELECT has_table_privilege(:r,'semantic_queue_cursor','INSERT')"),
            {"r": writer.ROLE},
        )
        assert connection.scalar(
            text("SELECT has_table_privilege(:r,'semantic_queue_cursor','UPDATE')"),
            {"r": writer.ROLE},
        )
        assert not connection.scalar(
            text(
                "SELECT has_table_privilege("
                "'coop_evidence_collector','semantic_queue_cursor','SELECT')"
            )
        )
        assert connection.scalar(
            text("SELECT has_table_privilege(:r,'audit_event','SELECT')"),
            {"r": writer.ROLE},
        )
        assert connection.scalar(
            text("SELECT has_table_privilege(:r,'audit_event','INSERT')"),
            {"r": writer.ROLE},
        )
        assert connection.scalar(
            text("SELECT has_table_privilege(:r,'reviewed_domain_batch','UPDATE')"),
            {"r": writer.ROLE},
        )
        assert not connection.scalar(
            text("SELECT has_table_privilege(:r,'audit_event','UPDATE')"),
            {"r": writer.ROLE},
        )
        assert not connection.scalar(
            text("SELECT has_table_privilege(:r,'ingestion_checkpoint','UPDATE')"),
            {"r": writer.ROLE},
        )
        assert not connection.scalar(
            text("SELECT has_table_privilege('coop_evidence_collector','coop','INSERT')")
        )
        assert not connection.scalar(
            text("SELECT has_function_privilege(:r,'public.similarity(text,text)','EXECUTE')"),
            {"r": writer.ROLE},
        )
    engine = create_engine(restricted)
    try:
        with engine.connect() as connection:
            assert connection.scalar(text("SELECT current_user")) == writer.ROLE
            assert connection.scalar(text("SELECT count(*) FROM coop")) == 0
            with pytest.raises(DBAPIError), connection.begin_nested() as transaction:
                connection.execute(text("DELETE FROM coop"))
                transaction.commit()
            with pytest.raises(DBAPIError), connection.begin_nested() as transaction:
                connection.execute(text("SELECT count(*) FROM ingestion_checkpoint"))
                transaction.commit()
            with pytest.raises(DBAPIError), connection.begin_nested() as transaction:
                connection.execute(text("CREATE TABLE semantic_acl_escape (id integer)"))
                transaction.commit()
    finally:
        engine.dispose()


@pytest.mark.integration
def test_semantic_reconcile_revokes_column_and_table_drift(test_engine: Engine) -> None:
    admin, restricted, collector = _urls(test_engine)
    writer.provision(admin, restricted, collector)
    with test_engine.begin() as connection:
        connection.execute(text(f"GRANT UPDATE(title) ON coop TO {writer.ROLE}"))
        connection.execute(text(f"GRANT INSERT ON ingestion_checkpoint TO {writer.ROLE}"))
    writer.reconcile(admin)
    with test_engine.connect() as connection:
        assert not connection.scalar(
            text("SELECT has_table_privilege(:r,'ingestion_checkpoint','INSERT')"),
            {"r": writer.ROLE},
        )
        assert not connection.scalar(
            text("SELECT has_column_privilege(:r,'organization','name','UPDATE')"),
            {"r": writer.ROLE},
        )


@pytest.mark.integration
def test_semantic_reconcile_fails_closed_on_public_business_write(test_engine: Engine) -> None:
    admin, restricted, collector = _urls(test_engine)
    writer.provision(admin, restricted, collector)
    with test_engine.begin() as connection:
        connection.execute(text("GRANT DELETE ON coop TO PUBLIC"))
    try:
        with pytest.raises(RuntimeError, match="table ACL"):
            writer.reconcile(admin)
    finally:
        with test_engine.begin() as connection:
            connection.execute(text("REVOKE DELETE ON coop FROM PUBLIC"))
        writer.reconcile(admin)


@pytest.mark.integration
def test_semantic_reconcile_fails_closed_on_owned_object(test_engine: Engine) -> None:
    admin, restricted, collector = _urls(test_engine)
    writer.provision(admin, restricted, collector)
    name = f"writer_owned_{uuid.uuid4().hex}"
    with test_engine.begin() as connection:
        connection.exec_driver_sql(f'CREATE SCHEMA "{name}" AUTHORIZATION {writer.ROLE}')
    try:
        with pytest.raises(RuntimeError, match="owns database objects"):
            writer.reconcile(admin)
    finally:
        with test_engine.begin() as connection:
            connection.exec_driver_sql(f'DROP SCHEMA "{name}" CASCADE')
        writer.reconcile(admin)


@pytest.mark.integration
def test_reconcile_dormant_matches_production_migration_job_first_run(
    test_engine: Engine,
) -> None:
    """The production migration Job reconciles the ACL of a NOLOGIN, passwordless role.

    The role is cluster-global, so a prior ``provision`` that set ``LOGIN`` must be
    reset to reproduce the real first-run state; otherwise the dormant path is never
    actually exercised. The ordered chain mirrors the Job args and CI steps:
    ``database_access reconcile`` (database-wide invariants) must run before the
    semantic writer's reconcile-dormant attestation.
    """
    admin, restricted, collector = _urls(test_engine)
    writer.provision(admin, restricted, collector)
    with test_engine.begin() as connection:
        connection.exec_driver_sql(f"ALTER ROLE {writer.ROLE} NOLOGIN PASSWORD NULL")
    engine = create_engine(restricted)
    try:
        with pytest.raises(DBAPIError), engine.connect():
            pass
        database_access.reconcile_collector_access(admin)
        writer.reconcile(admin, login=False)
        with test_engine.connect() as connection:
            assert (
                connection.scalar(
                    text("SELECT rolcanlogin FROM pg_roles WHERE rolname=:r"),
                    {"r": writer.ROLE},
                )
                is False
            )
            assert connection.scalar(
                text("SELECT has_table_privilege(:r,'coop','INSERT')"), {"r": writer.ROLE}
            )
            assert connection.scalar(
                text("SELECT has_table_privilege(:r,'semantic_queue_cursor','UPDATE')"),
                {"r": writer.ROLE},
            )
            assert connection.scalar(
                text("SELECT has_database_privilege(:r,current_database(),'CONNECT')"),
                {"r": writer.ROLE},
            )
            assert not connection.scalar(
                text("SELECT has_database_privilege(:r,current_database(),'TEMPORARY')"),
                {"r": writer.ROLE},
            )
            assert not connection.scalar(
                text("SELECT has_table_privilege('coop_evidence_collector','coop','INSERT')")
            )
        # A live reconcile must fail closed while the role still cannot log in.
        with pytest.raises(RuntimeError, match="unsafe"):
            writer.reconcile(admin)
    finally:
        engine.dispose()
        writer.provision(admin, restricted, collector)
        writer.reconcile(admin)


def test_semantic_url_validation_is_fail_closed(test_engine: Engine) -> None:
    admin, restricted, collector = _urls(test_engine)
    assert (
        writer.writer_password_from_urls(admin, restricted, collector)
        == make_url(restricted).password
    )
    restricted_url = make_url(restricted)
    for bad in (
        admin,
        collector,
        restricted_url.set(database="coop").render_as_string(hide_password=False),
        restricted_url.set(host="localhost").render_as_string(hide_password=False),
        restricted_url.set(password=make_url(collector).password).render_as_string(
            hide_password=False
        ),
    ):
        with pytest.raises(RuntimeError, match="invalid semantic writer database URL"):
            writer.writer_password_from_urls(admin, bad, collector)
