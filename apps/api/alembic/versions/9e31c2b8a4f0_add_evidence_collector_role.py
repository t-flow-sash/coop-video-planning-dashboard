"""add technically constrained evidence collector role

Revision ID: 9e31c2b8a4f0
Revises: 4c7e2f1a9d30
Create Date: 2026-10-15
"""

from collections.abc import Sequence

from alembic import op

revision: str = "9e31c2b8a4f0"
down_revision: str | None = "4c7e2f1a9d30"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

ROLE = "coop_evidence_collector"
EVIDENCE_TABLES = (
    "ingestion_source",
    "ingestion_run",
    "ingestion_checkpoint",
    "source_record",
    "source_record_revision",
    "ingestion_item",
    "source_record_observation",
)


def upgrade() -> None:
    op.execute(
        f"""
        DO $role$
        BEGIN
          IF NOT EXISTS (
            SELECT 1 FROM pg_roles
            WHERE rolname = '{ROLE}'
              AND rolcanlogin
              AND NOT rolsuper
              AND NOT rolcreatedb
              AND NOT rolcreaterole
              AND NOT rolinherit
              AND NOT rolreplication
              AND NOT rolbypassrls
          ) THEN
            RAISE EXCEPTION 'required constrained role {ROLE} is missing or unsafe';
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_auth_members membership
            JOIN pg_roles child ON child.oid = membership.member
            WHERE child.rolname = '{ROLE}'
          ) THEN
            RAISE EXCEPTION 'required constrained role {ROLE} has role memberships';
          END IF;
          IF EXISTS (
            SELECT 1 FROM pg_database database
            JOIN pg_roles owner ON owner.oid = database.datdba
            WHERE owner.rolname = '{ROLE}'
          ) OR EXISTS (
            SELECT 1 FROM pg_class object
            JOIN pg_roles owner ON owner.oid = object.relowner
            WHERE owner.rolname = '{ROLE}'
          ) OR EXISTS (
            SELECT 1 FROM pg_namespace schema
            JOIN pg_roles owner ON owner.oid = schema.nspowner
            WHERE owner.rolname = '{ROLE}'
          ) OR EXISTS (
            SELECT 1 FROM pg_proc function
            JOIN pg_roles owner ON owner.oid = function.proowner
            WHERE owner.rolname = '{ROLE}'
          ) OR EXISTS (
            SELECT 1 FROM pg_type type
            JOIN pg_roles owner ON owner.oid = type.typowner
            WHERE owner.rolname = '{ROLE}'
          ) THEN
            RAISE EXCEPTION 'required constrained role {ROLE} owns database objects';
          END IF;
        END
        $role$;
        """
    )
    tables = ", ".join(EVIDENCE_TABLES)
    op.execute(f"REVOKE ALL PRIVILEGES ON {tables} FROM {ROLE}")
    op.execute(f"GRANT SELECT, INSERT ON {tables} TO {ROLE}")
    op.execute(
        f"GRANT UPDATE (status, summary, finished_at, error_code) ON ingestion_run TO {ROLE}"
    )
    op.execute(
        f"GRANT UPDATE (checkpoint_value, query_hash, succeeded_run_id, updated_at) "
        f"ON ingestion_checkpoint TO {ROLE}"
    )
    op.execute(
        f"DO $acl$ BEGIN "
        f"EXECUTE format('REVOKE CREATE ON SCHEMA %I FROM {ROLE}', current_schema()); "
        f"EXECUTE format('GRANT USAGE ON SCHEMA %I TO {ROLE}', current_schema()); "
        f"EXECUTE format('GRANT CONNECT ON DATABASE %I TO {ROLE}', current_database()); "
        "END $acl$;"
    )


def downgrade() -> None:
    tables = ", ".join(EVIDENCE_TABLES)
    op.execute(f"REVOKE ALL PRIVILEGES ON {tables} FROM {ROLE}")
    op.execute(
        f"DO $acl$ BEGIN "
        f"EXECUTE format('REVOKE ALL PRIVILEGES ON SCHEMA %I FROM {ROLE}', current_schema()); "
        f"EXECUTE format('REVOKE ALL PRIVILEGES ON DATABASE %I FROM {ROLE}', current_database()); "
        "END $acl$;"
    )
