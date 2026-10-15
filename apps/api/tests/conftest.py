from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import Engine, text

TEST_DATABASE_URL = "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test"
os.environ["DATABASE_URL"] = TEST_DATABASE_URL
os.environ["APP_ENV"] = "test"
os.environ["AUTH_MODE"] = "disabled"

from app.db import engine  # noqa: E402
from app.main import app  # noqa: E402
from app.test_safety import (  # noqa: E402
    assert_safe_test_connection,
    assert_safe_test_engine,
)


@pytest.fixture(scope="session")
def test_engine() -> Iterator[Engine]:
    assert_safe_test_engine(engine, TEST_DATABASE_URL, "test")
    yield engine


@pytest.fixture(autouse=True)
def clean_database(request: pytest.FixtureRequest, test_engine: Engine) -> Iterator[None]:
    if (
        request.node.get_closest_marker("no_db") is not None
        or request.node.get_closest_marker("isolated_db") is not None
    ):
        yield
        return
    with test_engine.begin() as connection:
        assert_safe_test_connection(connection, TEST_DATABASE_URL, "test")
        connection.execute(
            text(
                "TRUNCATE source_record_observation, ingestion_item, "
                "source_record_revision, source_record, ingestion_checkpoint, "
                "audit_event, ingestion_run, ingestion_source, coop_contact, coop, contact, "
                "organization "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
