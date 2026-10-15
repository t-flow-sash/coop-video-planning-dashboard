"""add read-only M3 Gmail evidence foundation

Revision ID: 4c7e2f1a9d30
Revises: 0fe02ef55c8a
Create Date: 2026-10-15
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "4c7e2f1a9d30"
down_revision: str | None = "0fe02ef55c8a"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "ingestion_source",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_key", sa.String(160), nullable=False),
        sa.Column("kind", sa.String(30), nullable=False),
        sa.Column("enabled", sa.Boolean(), server_default=sa.true(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("kind IN ('GMAIL')", name="ck_ingestion_source_kind"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_key", name="uq_ingestion_source_key"),
    )
    op.create_table(
        "ingestion_run",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("mode", sa.String(30), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column(
            "summary", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column("error_code", sa.String(100), nullable=True),
        sa.Column(
            "started_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint("mode = 'FULL'", name="ck_ingestion_run_mode"),
        sa.CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','FAILED')", name="ck_ingestion_run_status"
        ),
        sa.ForeignKeyConstraint(["source_id"], ["ingestion_source.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("source_id", "id", name="uq_ingestion_run_source_id"),
    )
    op.create_index(
        "uq_ingestion_run_one_running",
        "ingestion_run",
        ["source_id"],
        unique=True,
        postgresql_where=sa.text("status = 'RUNNING'"),
    )
    op.create_table(
        "ingestion_checkpoint",
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("checkpoint_key", sa.String(100), nullable=False),
        sa.Column("checkpoint_value", sa.Text(), nullable=False),
        sa.Column("query_hash", sa.String(64), nullable=False),
        sa.Column("succeeded_run_id", sa.UUID(), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["source_id"], ["ingestion_source.id"]),
        sa.ForeignKeyConstraint(
            ["source_id", "succeeded_run_id"],
            ["ingestion_run.source_id", "ingestion_run.id"],
            name="fk_ingestion_checkpoint_run",
        ),
        sa.PrimaryKeyConstraint("source_id", "checkpoint_key"),
    )
    op.create_table(
        "source_record",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("record_type", sa.String(20), nullable=False),
        sa.Column("external_id", sa.Text(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint(
            "record_type IN ('GMAIL_THREAD','GMAIL_MESSAGE')", name="ck_source_record_type"
        ),
        sa.ForeignKeyConstraint(["source_id"], ["ingestion_source.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_id", "record_type", "external_id", name="uq_source_record_external"
        ),
        sa.UniqueConstraint("source_id", "id", name="uq_source_record_source_id"),
    )
    op.create_table(
        "source_record_revision",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("source_record_id", sa.UUID(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column(
            "metadata_json",
            postgresql.JSONB(),
            nullable=False,
            server_default=sa.text("'{}'::jsonb"),
        ),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["source_id"], ["ingestion_source.id"]),
        sa.ForeignKeyConstraint(
            ["source_id", "source_record_id"],
            ["source_record.source_id", "source_record.id"],
            name="fk_source_record_revision_source_record",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "source_record_id", "content_hash", name="uq_source_record_revision_hash"
        ),
        sa.UniqueConstraint("source_id", "id", name="uq_source_record_revision_source_id"),
    )
    op.create_table(
        "ingestion_item",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("candidate_key", sa.String(160), nullable=False),
        sa.Column("payload_hash", sa.String(64), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("reason_code", sa.String(100), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.CheckConstraint("status = 'PENDING_REVIEW'", name="ck_ingestion_item_status"),
        sa.ForeignKeyConstraint(["run_id"], ["ingestion_run.id"]),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "candidate_key", name="uq_ingestion_item_run_candidate"),
    )
    op.create_table(
        "source_record_observation",
        sa.Column("id", sa.UUID(), nullable=False),
        sa.Column("source_id", sa.UUID(), nullable=False),
        sa.Column("run_id", sa.UUID(), nullable=False),
        sa.Column("source_record_revision_id", sa.UUID(), nullable=False),
        sa.Column(
            "observed_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.ForeignKeyConstraint(["source_id"], ["ingestion_source.id"]),
        sa.ForeignKeyConstraint(
            ["source_id", "run_id"],
            ["ingestion_run.source_id", "ingestion_run.id"],
            name="fk_source_observation_run",
        ),
        sa.ForeignKeyConstraint(
            ["source_id", "source_record_revision_id"],
            ["source_record_revision.source_id", "source_record_revision.id"],
            name="fk_source_observation_revision",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", "source_record_revision_id", name="uq_source_observation"),
    )


def downgrade() -> None:
    for table in (
        "source_record_observation",
        "ingestion_item",
        "source_record_revision",
        "source_record",
        "ingestion_checkpoint",
    ):
        op.drop_table(table)
    op.drop_index("uq_ingestion_run_one_running", table_name="ingestion_run")
    op.drop_table("ingestion_run")
    op.drop_table("ingestion_source")
