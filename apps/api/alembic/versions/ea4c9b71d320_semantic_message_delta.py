"""Independent semantic processing state and exact pre-linked route.

Revision ID: ea4c9b71d320
Revises: a83c1e4b9270
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "ea4c9b71d320"
down_revision: str | None = "a83c1e4b9270"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "semantic_track_route",
        sa.Column("thread_record_id", UUID(as_uuid=True), primary_key=True),
        sa.Column(
            "source_id", UUID(as_uuid=True), sa.ForeignKey("ingestion_source.id"), nullable=False
        ),
        sa.Column("track_key", sa.String(160), nullable=False, unique=True),
        sa.Column("record_key", sa.String(160), nullable=False, unique=True),
        sa.Column("title", sa.String(240), nullable=False),
        sa.Column("expected_coop_id", UUID(as_uuid=True), sa.ForeignKey("coop.id")),
        sa.ForeignKeyConstraint(
            ["source_id", "thread_record_id"], ["source_record.source_id", "source_record.id"]
        ),
    )
    op.create_table(
        "semantic_message_state",
        sa.Column("revision_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source_record_id", UUID(as_uuid=True), nullable=False),
        sa.Column(
            "source_id", UUID(as_uuid=True), sa.ForeignKey("ingestion_source.id"), nullable=False
        ),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("reason_code", sa.String(60), nullable=False),
        sa.Column("provenance", JSONB, nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "source_record_id"], ["source_record.source_id", "source_record.id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id", "revision_id"],
            ["source_record_revision.source_record_id", "source_record_revision.id"],
        ),
        sa.CheckConstraint(
            "outcome IN ('RETRY','REVIEW','CREATED','UPDATED','UNCHANGED')",
            name="ck_semantic_message_outcome",
        ),
    )
    for table in ("semantic_track_route", "semantic_message_state"):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC, coop_evidence_collector")


def downgrade() -> None:
    bind = op.get_bind()
    if bind.scalar(
        sa.text(
            "SELECT EXISTS(SELECT 1 FROM semantic_message_state) OR EXISTS(SELECT 1 FROM semantic_track_route)"
        )
    ):
        raise RuntimeError("cannot discard semantic route or processing history")
    op.drop_table("semantic_message_state")
    op.drop_table("semantic_track_route")
