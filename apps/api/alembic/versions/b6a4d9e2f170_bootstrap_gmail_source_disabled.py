"""bootstrap the Gmail evidence source disabled

Revision ID: b6a4d9e2f170
Revises: 9e31c2b8a4f0
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "b6a4d9e2f170"
down_revision: str | None = "9e31c2b8a4f0"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_ID = "ce6c8014-850c-4aca-8f41-bf1c2da798dd"
SOURCE_KEY = "gmail-open-coops-v1"


def upgrade() -> None:
    op.execute(
        f"""
        INSERT INTO ingestion_source (id, source_key, kind, enabled)
        VALUES ('{SOURCE_ID}'::uuid, '{SOURCE_KEY}', 'GMAIL', false)
        ON CONFLICT (source_key) DO UPDATE
        SET kind = 'GMAIL', enabled = false;
        """
    )
    op.execute("REVOKE INSERT ON ingestion_source FROM coop_evidence_collector")
    op.execute("GRANT SELECT ON ingestion_source TO coop_evidence_collector")


def downgrade() -> None:
    # Preserve operational state and evidence. Removing a source row can cascade
    # into evidence loss and is therefore intentionally not automated.
    pass
