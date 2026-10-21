"""Create a passwordless, non-login semantic role; activation is a separate gate.

Revision ID: 6a8d1e42c907
Revises: 71c8ef4a6d20
"""

from collections.abc import Sequence

from alembic import op

revision: str = "6a8d1e42c907"
down_revision: str | None = "71c8ef4a6d20"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.execute("""
    DO $role$ BEGIN
      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'coop_semantic_writer') THEN
        CREATE ROLE coop_semantic_writer NOLOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE
          NOINHERIT NOREPLICATION NOBYPASSRLS;
      END IF;
    END $role$;
    """)
    # Reconciliation performs the complete ACL replacement/attestation after migration.
    # In particular never grant semantic rights to coop_evidence_collector.


def downgrade() -> None:
    # PostgreSQL roles are cluster-wide: never drop an operator-managed login or
    # credential while downgrading a disposable schema (or application tables).
    # An operator must separately review role deprovisioning.
    pass
