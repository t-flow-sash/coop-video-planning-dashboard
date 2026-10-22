"""Separate reviewed business-track provenance from metadata-only collection.

Revision ID: a83c1e4b9270
Revises: f9b2d4e6a801
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects.postgresql import JSONB, UUID

from alembic import op

revision: str = "a83c1e4b9270"
down_revision: str | None = "f9b2d4e6a801"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_unique_constraint(
        "uq_source_revision_record_pair", "source_record_revision", ["source_record_id", "id"]
    )
    op.create_table(
        "reviewed_domain_batch",
        sa.Column("id", UUID(as_uuid=True), primary_key=True),
        sa.Column("manifest_id", sa.String(160), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False, unique=True),
        sa.Column("summary", JSONB, nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_table(
        "reviewed_domain_track",
        sa.Column("track_key", sa.String(160), primary_key=True),
        sa.Column(
            "coop_id",
            UUID(as_uuid=True),
            sa.ForeignKey("coop.id", ondelete="RESTRICT"),
            nullable=True,
            unique=True,
        ),
        sa.Column("track_revision", sa.Integer, nullable=False),
        sa.Column("decision_hash", sa.String(64), nullable=False),
        sa.Column("field_baselines", JSONB, nullable=False),
        sa.Column(
            "batch_id",
            UUID(as_uuid=True),
            sa.ForeignKey("reviewed_domain_batch.id"),
            nullable=False,
        ),
        sa.CheckConstraint("track_revision >= 1", name="ck_reviewed_track_revision"),
    )
    op.create_table(
        "reviewed_domain_evidence",
        sa.Column(
            "batch_id",
            UUID(as_uuid=True),
            sa.ForeignKey("reviewed_domain_batch.id"),
            primary_key=True,
        ),
        sa.Column(
            "track_key",
            sa.String(160),
            sa.ForeignKey("reviewed_domain_track.track_key"),
            primary_key=True,
        ),
        sa.Column("source_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("source_record_id", UUID(as_uuid=True), primary_key=True),
        sa.Column("revision_id", UUID(as_uuid=True), primary_key=True),
        sa.ForeignKeyConstraint(
            ["source_id", "source_record_id"], ["source_record.source_id", "source_record.id"]
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "revision_id"],
            ["source_record_revision.source_id", "source_record_revision.id"],
        ),
        sa.ForeignKeyConstraint(
            ["source_record_id", "revision_id"],
            ["source_record_revision.source_record_id", "source_record_revision.id"],
            name="fk_reviewed_evidence_exact_revision",
        ),
    )
    op.create_table(
        "reviewed_domain_item",
        sa.Column(
            "batch_id",
            UUID(as_uuid=True),
            sa.ForeignKey("reviewed_domain_batch.id"),
            primary_key=True,
        ),
        sa.Column(
            "track_key",
            sa.String(160),
            sa.ForeignKey("reviewed_domain_track.track_key"),
            primary_key=True,
        ),
        sa.Column("outcome", sa.String(20), nullable=False),
        sa.Column("conflict_fields", JSONB, nullable=False),
    )
    # Explicit denial: collector must never write/read reviewed business materialization tables.
    for table in (
        "reviewed_domain_batch",
        "reviewed_domain_track",
        "reviewed_domain_evidence",
        "reviewed_domain_item",
    ):
        op.execute(f"REVOKE ALL ON {table} FROM PUBLIC, coop_evidence_collector")


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(sa.text("SELECT EXISTS (SELECT 1 FROM reviewed_domain_track)")):
        raise RuntimeError("cannot discard reviewed track linkage")
    for table in (
        "reviewed_domain_item",
        "reviewed_domain_evidence",
        "reviewed_domain_track",
        "reviewed_domain_batch",
    ):
        op.drop_table(table)
    op.drop_constraint("uq_source_revision_record_pair", "source_record_revision", type_="unique")
