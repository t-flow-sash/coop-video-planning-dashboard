from __future__ import annotations

import pytest

from app.database_access import collector_password_from_urls

pytestmark = pytest.mark.no_db

ADMIN_URL = "postgresql+psycopg://coop:admin@postgres:5432/coop"


def test_collector_password_requires_dedicated_role_on_same_database() -> None:
    password = collector_password_from_urls(
        ADMIN_URL,
        "postgresql+psycopg://coop_evidence_collector:collector-secret@postgres:5432/coop",
    )
    assert password == "collector-secret"


@pytest.mark.parametrize(
    "collector_url",
    [
        "postgresql+psycopg://coop:secret@postgres:5432/coop",
        "postgresql+psycopg://coop_evidence_collector:secret@other-db:5432/coop",
        "postgresql+psycopg://coop_evidence_collector:secret@postgres:5432/other",
        "postgresql+psycopg://coop_evidence_collector:admin@postgres:5432/coop",
        "postgresql+psycopg://coop_evidence_collector@postgres:5432/coop",
        "postgresql+psycopg://coop_evidence_collector:secret@postgres:5432/coop?host=other-db",
    ],
)
def test_collector_password_rejects_ambiguous_or_shared_credentials(collector_url: str) -> None:
    with pytest.raises(RuntimeError, match="collector database URL"):
        collector_password_from_urls(ADMIN_URL, collector_url)
