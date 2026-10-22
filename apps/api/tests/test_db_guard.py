from __future__ import annotations

from unittest.mock import MagicMock

import pytest
from sqlalchemy import create_engine

from app.test_safety import (
    assert_safe_test_connection,
    assert_safe_test_database,
    assert_safe_test_engine,
)

pytestmark = pytest.mark.no_db


@pytest.mark.parametrize(
    ("url", "environment"),
    [
        ("postgresql+psycopg://coop:coop@db:5432/coop_test", "test"),
        ("postgresql+psycopg://coop:coop@127.0.0.1:5432/coop", "test"),
        ("postgresql+psycopg://coop:coop@localhost:5432/coop_test", "development"),
        ("sqlite:///coop_test.db", "test"),
    ],
)
def test_guard_rejects_unsafe_database(url: str, environment: str) -> None:
    with pytest.raises(RuntimeError, match="Refusing destructive test cleanup"):
        assert_safe_test_database(url, environment)


@pytest.mark.parametrize(
    "url",
    [
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?host=prod.example.com",
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?dbname=production",
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?database=production",
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?host=%2Fvar%2Frun%2Fpostgresql",
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?host=localhost&host=prod.example.com",
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?port=5433,5432",
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?service=production",
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?servicefile=%2Ftmp%2Fpg_service.conf",
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?user=postgres",
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?password=secret",
        "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test?application_name=pytest",
    ],
)
def test_guard_rejects_all_query_connection_parameters(url: str) -> None:
    with pytest.raises(RuntimeError, match="Refusing destructive test cleanup"):
        assert_safe_test_database(url, "test")


@pytest.mark.parametrize(
    "url",
    [
        "postgresql://coop:coop@127.0.0.1:5432/coop_test",
        "postgresql+psycopg://postgres:coop@127.0.0.1:5432/coop_test",
        "postgresql+psycopg://coop:coop@127.0.0.1/coop_test",
        "postgresql+psycopg://coop:coop@127.0.0.1:5433/coop_test",
        "postgresql+psycopg://coop:coop@/coop_test",
    ],
)
def test_guard_rejects_ambiguous_or_unexpected_connection_identity(url: str) -> None:
    with pytest.raises(RuntimeError, match="Refusing destructive test cleanup"):
        assert_safe_test_database(url, "test")


@pytest.mark.parametrize("host", ["localhost", "127.0.0.1", "[::1]"])
def test_guard_allows_explicit_local_test_database(host: str) -> None:
    url = f"postgresql+psycopg://coop:coop@{host}:5432/coop_test"
    assert_safe_test_database(url, "test")


def test_guard_allows_dedicated_self_hosted_runner_port() -> None:
    url = "postgresql+psycopg://coop:coop@127.0.0.1:55435/coop_test"
    assert_safe_test_database(url, "test")


def test_guard_rejects_engine_bound_before_environment_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    unsafe_engine = create_engine("postgresql+psycopg://coop:coop@127.0.0.1:5432/coop")
    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test")
    try:
        with pytest.raises(RuntimeError, match="Refusing destructive test cleanup"):
            assert_safe_test_engine(
                unsafe_engine,
                "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test",
                "test",
            )
    finally:
        unsafe_engine.dispose()


def test_container_nat_requires_exact_attested_server_address() -> None:
    url = "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test"
    engine = create_engine(url)
    connection = MagicMock()
    connection.engine = engine
    connection.execute.return_value.mappings.return_value.one.return_value = {
        "database_name": "coop_test",
        "session_user": "coop",
        "current_user": "coop",
        "server_address": "172.17.0.2",
        "server_port": 5432,
    }
    try:
        with pytest.raises(RuntimeError, match="connected database identity differs"):
            assert_safe_test_connection(connection, url, "test")
        assert_safe_test_connection(
            connection,
            url,
            "test",
            expected_server_address="172.17.0.2",
        )
        for unexpected_address in ("172.17.0.3", "10.200.0.15", "203.0.113.77", "8.8.8.8"):
            with pytest.raises(RuntimeError, match="connected database identity differs"):
                assert_safe_test_connection(
                    connection,
                    url,
                    "test",
                    expected_server_address=unexpected_address,
                )
    finally:
        engine.dispose()
