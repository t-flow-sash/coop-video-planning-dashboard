from __future__ import annotations

import os
from collections.abc import Iterator

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import text

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://coop:coop@127.0.0.1:5432/coop_test")
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("AUTH_MODE", "disabled")

from app.db import engine
from app.main import app


@pytest.fixture(autouse=True)
def clean_database() -> Iterator[None]:
    with engine.begin() as connection:
        connection.execute(
            text(
                "TRUNCATE audit_event, coop_contact, coop, contact, organization "
                "RESTART IDENTITY CASCADE"
            )
        )
    yield


@pytest.fixture
def client() -> Iterator[TestClient]:
    with TestClient(app) as test_client:
        yield test_client
