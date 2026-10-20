"""Make review metadata fail-closed and index operational search.

Revision ID: f9b2d4e6a801
Revises: e8a1c4d7b902
"""

from collections.abc import Sequence

import sqlalchemy as sa

from alembic import op

revision: str = "f9b2d4e6a801"
down_revision: str | None = "e8a1c4d7b902"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

UNCERTAINTY = "Migrated record has not been explicitly reviewed."


def upgrade() -> None:
    # A false value installed by the former server default does not establish a
    # review. Preserve rows that already carry explicit uncertainty or whose
    # audit trail records an explicit review-state edit.
    op.execute(
        sa.text(
            """
            UPDATE coop AS c
            SET needs_review = true, uncertainty = :uncertainty
            WHERE c.needs_review = false
              AND c.uncertainty IS NULL
              AND NOT EXISTS (
                SELECT 1 FROM audit_event AS a
                WHERE a.coop_id = c.id AND a.changes ? 'needs_review'
              )
            """
        ).bindparams(uncertainty=UNCERTAINTY)
    )
    op.alter_column("coop", "needs_review", server_default=sa.true())
    op.create_check_constraint(
        "ck_coop_review_uncertainty",
        "coop",
        "NOT needs_review OR (uncertainty IS NOT NULL AND btrim(uncertainty) <> '')",
    )

    op.execute("CREATE EXTENSION IF NOT EXISTS pg_trgm")
    for table, column in (
        ("coop", "title"),
        ("coop", "summary"),
        ("coop", "owner"),
        ("coop", "next_action"),
        ("organization", "name"),
        ("contact", "display_name"),
        ("contact", "first_name"),
        ("contact", "last_name"),
        ("contact", "email"),
    ):
        op.execute(
            f'CREATE INDEX "ix_{table}_{column}_trgm" ON "{table}" '
            f'USING gin ("{column}" public.gin_trgm_ops)'
        )


def downgrade() -> None:
    for table, column in reversed(
        (
            ("coop", "title"),
            ("coop", "summary"),
            ("coop", "owner"),
            ("coop", "next_action"),
            ("organization", "name"),
            ("contact", "display_name"),
            ("contact", "first_name"),
            ("contact", "last_name"),
            ("contact", "email"),
        )
    ):
        op.drop_index(f"ix_{table}_{column}_trgm", table_name=table)
    op.drop_constraint("ck_coop_review_uncertainty", "coop", type_="check")
    op.alter_column("coop", "needs_review", server_default=sa.false())
