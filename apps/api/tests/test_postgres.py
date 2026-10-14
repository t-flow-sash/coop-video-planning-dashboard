from __future__ import annotations

from sqlalchemy import inspect, text

from app.db import engine
from app.seed import seed_database


def test_migration_created_postgresql_schema() -> None:
    inspector = inspect(engine)
    assert {"organization", "contact", "coop", "coop_contact", "audit_event"} <= set(
        inspector.get_table_names()
    )
    assert engine.dialect.name == "postgresql"


def test_seed_is_idempotent_and_preserves_user_changes() -> None:
    seed_database()
    with engine.begin() as connection:
        first = connection.execute(text("SELECT count(*) FROM coop")).scalar_one()
        assert first >= 6
        repeated = connection.execute(
            text(
                "SELECT count(*) FROM ("
                "SELECT contact_id FROM coop_contact GROUP BY contact_id HAVING count(*) > 1"
                ") recurring"
            )
        ).scalar_one()
        assert repeated >= 1
        connection.execute(
            text("UPDATE coop SET title = 'User changed' WHERE id = :id"),
            {"id": "00000000-0000-4000-8000-000000000301"},
        )

    seed_database()
    with engine.connect() as connection:
        assert connection.execute(text("SELECT count(*) FROM coop")).scalar_one() == first
        title = connection.execute(
            text("SELECT title FROM coop WHERE id = :id"),
            {"id": "00000000-0000-4000-8000-000000000301"},
        ).scalar_one()
        assert title == "User changed"
        assert (
            connection.execute(
                text("SELECT count(*) FROM audit_event WHERE source = 'SEED'")
            ).scalar_one()
            >= 6
        )
