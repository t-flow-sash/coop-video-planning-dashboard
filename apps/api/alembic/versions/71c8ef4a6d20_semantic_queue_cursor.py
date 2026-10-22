"""Independent bounded semantic queue replay cursor.

Revision ID: 71c8ef4a6d20
Revises: ea4c9b71d320
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import UUID

from alembic import op

revision: str = "71c8ef4a6d20"
down_revision: str | None = "ea4c9b71d320"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_queue_cursor",
        sa.Column(
            "source_id", UUID(as_uuid=True), sa.ForeignKey("ingestion_source.id"), primary_key=True
        ),
        sa.Column("revision_id", UUID(as_uuid=True), nullable=True),
        sa.Column("observed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "positions",
            sa.JSON().with_variant(sa.dialects.postgresql.JSONB(), "postgresql"),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
    )
    op.execute("REVOKE ALL ON semantic_queue_cursor FROM PUBLIC, coop_evidence_collector")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(sa.text("SELECT EXISTS(SELECT 1 FROM semantic_queue_cursor)")):
        raise RuntimeError("cannot discard semantic queue progress")
    op.drop_table("semantic_queue_cursor")
