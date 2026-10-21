"""Real disposable coop_test ACL probes; never use a production database."""

from __future__ import annotations

import os
import uuid

import pytest
from sqlalchemy import Engine, create_engine, text
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


def test_semantic_url_validation_is_fail_closed(test_engine: Engine) -> None:
    admin, restricted, collector = _urls(test_engine)
    for bad in (
        admin,
        collector,
        restricted.replace("coop_test", "coop"),
        restricted.replace("127.0.0.1", "localhost"),
        restricted.replace("semantic-test-only", "collector-test-only"),
    ):
        with pytest.raises(RuntimeError, match="invalid semantic writer database URL"):
            writer.writer_password_from_urls(admin, bad, collector)
