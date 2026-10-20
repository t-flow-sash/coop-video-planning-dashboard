"""Add operational cooperation overview and reviewed manual import state.

Revision ID: e8a1c4d7b902
Revises: d7f4b2a96c31
"""

from collections.abc import Sequence

import sqlalchemy as sa
from sqlalchemy.dialects import postgresql

from alembic import op

revision: str = "e8a1c4d7b902"
down_revision: str | None = "d7f4b2a96c31"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column("organization", sa.Column("record_key", sa.String(160), nullable=True))
    op.add_column("contact", sa.Column("record_key", sa.String(160), nullable=True))
    op.add_column("coop", sa.Column("record_key", sa.String(160), nullable=True))
    op.add_column(
        "coop",
        sa.Column("is_synthetic_seed", sa.Boolean(), nullable=False, server_default=sa.false()),
    )
    op.add_column("coop", sa.Column("priority", sa.String(2), nullable=False, server_default="P2"))
    op.add_column("coop", sa.Column("last_contact_date", sa.Date(), nullable=True))
    op.add_column("coop", sa.Column("owner", sa.String(160), nullable=True))
    op.add_column("coop", sa.Column("deadline", sa.Date(), nullable=True))
    op.add_column(
        "coop", sa.Column("fee_status", sa.String(20), nullable=False, server_default="UNKNOWN")
    )
    op.add_column(
        "coop", sa.Column("needs_review", sa.Boolean(), nullable=False, server_default=sa.false())
    )
    op.add_column("coop", sa.Column("uncertainty", sa.Text(), nullable=True))

    op.execute("UPDATE organization SET record_key = 'org-' || md5(id::text)")
    op.execute("UPDATE contact SET record_key = 'contact-' || md5(id::text)")
    op.execute("UPDATE coop SET record_key = 'coop-' || md5(id::text)")
    op.execute(
        "UPDATE coop SET is_synthetic_seed = true WHERE id IN ("
        "'00000000-0000-4000-8000-000000000301'::uuid,"
        "'00000000-0000-4000-8000-000000000302'::uuid,"
        "'00000000-0000-4000-8000-000000000303'::uuid,"
        "'00000000-0000-4000-8000-000000000304'::uuid,"
        "'00000000-0000-4000-8000-000000000305'::uuid,"
        "'00000000-0000-4000-8000-000000000306'::uuid)"
    )
    op.alter_column("organization", "record_key", nullable=False)
    op.alter_column("contact", "record_key", nullable=False)
    op.alter_column("coop", "record_key", nullable=False)
    op.create_unique_constraint("uq_organization_record_key", "organization", ["record_key"])
    op.create_unique_constraint("uq_contact_record_key", "contact", ["record_key"])
    op.create_unique_constraint("uq_coop_record_key", "coop", ["record_key"])
    op.create_check_constraint("ck_coop_priority", "coop", "priority IN ('P0','P1','P2')")
    op.create_check_constraint(
        "ck_coop_fee_status",
        "coop",
        "fee_status IN ('UNKNOWN','PROPOSED','AGREED','INVOICED','PAID','NOT_APPLICABLE')",
    )
    op.create_index("ix_coop_priority_deadline", "coop", ["priority", "deadline"])

    op.drop_constraint("ck_audit_source", "audit_event", type_="check")
    op.create_check_constraint(
        "ck_audit_source",
        "audit_event",
        "source IN ('DASHBOARD','INFLUENCER_AGENT','GMAIL_INGESTION','MANUAL_IMPORT','SEED','API')",
    )

    op.create_table(
        "manual_import_batch",
        sa.Column("id", postgresql.UUID(as_uuid=True), primary_key=True),
        sa.Column("manifest_id", sa.String(160), nullable=False),
        sa.Column("manifest_hash", sa.String(64), nullable=False),
        sa.Column("schema_version", sa.Integer(), nullable=False),
        sa.Column(
            "summary", postgresql.JSONB(), nullable=False, server_default=sa.text("'{}'::jsonb")
        ),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.UniqueConstraint("manifest_hash", name="uq_manual_import_batch_hash"),
        sa.CheckConstraint("schema_version = 1", name="ck_manual_import_schema_version"),
    )
    op.create_table(
        "coop_import_state",
        sa.Column("coop_id", postgresql.UUID(as_uuid=True), nullable=False, primary_key=True),
        sa.Column("record_version", sa.Integer(), nullable=False),
        sa.Column("content_hash", sa.String(64), nullable=False),
        sa.Column("snapshot_hash", sa.String(64), nullable=False),
        sa.Column("batch_id", postgresql.UUID(as_uuid=True), nullable=False),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), nullable=False, server_default=sa.func.now()
        ),
        sa.ForeignKeyConstraint(["coop_id"], ["coop.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["batch_id"], ["manual_import_batch.id"]),
        sa.CheckConstraint("record_version >= 1", name="ck_coop_import_record_version"),
    )


def downgrade() -> None:
    op.drop_table("coop_import_state")
    op.drop_table("manual_import_batch")
    op.drop_constraint("ck_audit_source", "audit_event", type_="check")
    op.create_check_constraint(
        "ck_audit_source",
        "audit_event",
        "source IN ('DASHBOARD','INFLUENCER_AGENT','GMAIL_INGESTION','SEED','API')",
    )
    op.drop_index("ix_coop_priority_deadline", table_name="coop")
    op.drop_constraint("ck_coop_fee_status", "coop", type_="check")
    op.drop_constraint("ck_coop_priority", "coop", type_="check")
    op.drop_constraint("uq_coop_record_key", "coop", type_="unique")
    op.drop_constraint("uq_contact_record_key", "contact", type_="unique")
    op.drop_constraint("uq_organization_record_key", "organization", type_="unique")
    for column in (
        "uncertainty",
        "needs_review",
        "fee_status",
        "deadline",
        "owner",
        "last_contact_date",
        "priority",
        "is_synthetic_seed",
        "record_key",
    ):
        op.drop_column("coop", column)
    op.drop_column("contact", "record_key")
    op.drop_column("organization", "record_key")
