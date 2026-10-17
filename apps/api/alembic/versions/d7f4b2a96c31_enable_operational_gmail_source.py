"""enable the reviewed operational Gmail source

Revision ID: d7f4b2a96c31
Revises: c2d7a91f4e60
Create Date: 2026-10-17
"""

from collections.abc import Sequence

from sqlalchemy import text

from alembic import op

revision: str = "d7f4b2a96c31"
down_revision: str | None = "c2d7a91f4e60"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

SOURCE_KEY = "gmail-open-coops-v1"


def _locked_source() -> tuple[object, bool]:
    rows = (
        op.get_bind()
        .execute(
            text(
                "SELECT id, enabled FROM ingestion_source WHERE source_key = :source_key FOR UPDATE"
            ),
            {"source_key": SOURCE_KEY},
        )
        .all()
    )
    if len(rows) != 1:
        raise RuntimeError("operational Gmail source is missing or ambiguous")
    return rows[0].id, rows[0].enabled


def upgrade() -> None:
    source_id, enabled = _locked_source()
    if enabled:
        raise RuntimeError("operational Gmail source is already enabled")
    result = op.get_bind().execute(
        text(
            "UPDATE ingestion_source SET enabled = true WHERE id = :source_id AND enabled = false"
        ),
        {"source_id": source_id},
    )
    if result.rowcount != 1:
        raise RuntimeError("operational Gmail source activation lost its lock")


def downgrade() -> None:
    source_id, enabled = _locked_source()
    connection = op.get_bind()
    consumed = connection.scalar(
        text(
            "SELECT EXISTS ("
            "SELECT 1 FROM ingestion_checkpoint WHERE source_id = :source_id "
            "UNION ALL "
            "SELECT 1 FROM ingestion_run "
            "WHERE source_id = :source_id AND status = 'SUCCEEDED'"
            ")"
        ),
        {"source_id": source_id},
    )
    if consumed:
        raise RuntimeError("cannot disable an operational Gmail source after a successful run")
    if not enabled:
        raise RuntimeError("operational Gmail source is already disabled")
    result = connection.execute(
        text(
            "UPDATE ingestion_source SET enabled = false WHERE id = :source_id AND enabled = true"
        ),
        {"source_id": source_id},
    )
    if result.rowcount != 1:
        raise RuntimeError("operational Gmail source deactivation lost its lock")
