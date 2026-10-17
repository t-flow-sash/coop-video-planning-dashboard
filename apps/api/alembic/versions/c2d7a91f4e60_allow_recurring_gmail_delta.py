"""allow recurring Gmail delta ingestion runs

Revision ID: c2d7a91f4e60
Revises: b6a4d9e2f170
Create Date: 2026-10-17
"""

from collections.abc import Sequence

from alembic import op
from sqlalchemy import text

revision: str = "c2d7a91f4e60"
down_revision: str | None = "b6a4d9e2f170"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.drop_constraint("ck_ingestion_run_mode", "ingestion_run", type_="check")
    op.create_check_constraint(
        "ck_ingestion_run_mode",
        "ingestion_run",
        "mode IN ('FULL','DELTA')",
    )


def downgrade() -> None:
    connection = op.get_bind()
    if connection.scalar(text("SELECT EXISTS (SELECT 1 FROM ingestion_run WHERE mode = 'DELTA')")):
        raise RuntimeError("cannot remove DELTA mode while recurring ingestion runs exist")
    op.drop_constraint("ck_ingestion_run_mode", "ingestion_run", type_="check")
    op.create_check_constraint("ck_ingestion_run_mode", "ingestion_run", "mode = 'FULL'")
