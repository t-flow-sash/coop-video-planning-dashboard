from __future__ import annotations

import argparse
import os
from typing import Any, cast

import psycopg
from psycopg import sql
from sqlalchemy.engine import URL, make_url

COLLECTOR_ROLE = "coop_evidence_collector"
EVIDENCE_TABLES = (
    "ingestion_source",
    "ingestion_run",
    "ingestion_checkpoint",
    "source_record",
    "source_record_revision",
    "ingestion_item",
    "source_record_observation",
)
INSERT_TABLES = tuple(table for table in EVIDENCE_TABLES if table != "ingestion_source")
UPDATE_COLUMNS = {
    "ingestion_run": ("status", "summary", "finished_at", "error_code"),
    "ingestion_checkpoint": (
        "checkpoint_value",
        "query_hash",
        "succeeded_run_id",
        "updated_at",
    ),
}
TABLE_PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def _required_row(cursor: psycopg.Cursor[Any]) -> tuple[Any, ...]:
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("collector ACL query returned no row")
    return cast(tuple[Any, ...], row)


def _same_target(left: URL, right: URL) -> bool:
    return (
        left.drivername == right.drivername == "postgresql+psycopg"
        and left.host == right.host
        and left.port == right.port
        and left.database == right.database
        and not left.query
        and not right.query
    )


def collector_password_from_urls(admin_url: str, collector_url: str) -> str:
    """Fail closed unless the collector URL targets the admin database with its own role."""
    try:
        admin = make_url(admin_url)
        collector = make_url(collector_url)
    except Exception as error:
        raise RuntimeError("invalid collector database URL") from error
    if (
        not _same_target(admin, collector)
        or collector.username != COLLECTOR_ROLE
        or not collector.password
        or admin.username == collector.username
        or admin.password == collector.password
    ):
        raise RuntimeError("invalid collector database URL")
    return collector.password


def _psycopg_url(database_url: str) -> str:
    return make_url(database_url).set(drivername="postgresql").render_as_string(hide_password=False)


def provision_collector_password(admin_url: str, collector_url: str) -> None:
    password = collector_password_from_urls(admin_url, collector_url)
    with psycopg.connect(_psycopg_url(admin_url)) as connection, connection.cursor() as cursor:
        cursor.execute(
            sql.SQL(
                "DO $role$ BEGIN IF NOT EXISTS "
                "(SELECT 1 FROM pg_roles WHERE rolname = {role_name}) THEN "
                "CREATE ROLE {role_identifier} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS; END IF; END $role$;"
            ).format(
                role_name=sql.Literal(COLLECTOR_ROLE),
                role_identifier=sql.Identifier(COLLECTOR_ROLE),
            )
        )
        cursor.execute(
            "SELECT parent.rolname FROM pg_auth_members membership "
            "JOIN pg_roles parent ON parent.oid = membership.roleid "
            "JOIN pg_roles child ON child.oid = membership.member "
            "WHERE child.rolname = %s",
            (COLLECTOR_ROLE,),
        )
        for (parent_role,) in cursor.fetchall():
            cursor.execute(
                sql.SQL("REVOKE {} FROM {}").format(
                    sql.Identifier(parent_role), sql.Identifier(COLLECTOR_ROLE)
                )
            )
        cursor.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(
                sql.Identifier(COLLECTOR_ROLE),
                sql.Literal(password),
            )
        )


def reconcile_collector_access(admin_url: str) -> None:
    """Replace collector ACL drift with the evidence-only allowlist and attest it."""
    with psycopg.connect(_psycopg_url(admin_url)) as connection, connection.cursor() as cursor:
        cursor.execute(
            "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
            "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = %s",
            (COLLECTOR_ROLE,),
        )
        attributes = _required_row(cursor)
        if attributes != (True, False, False, False, False, False, False):
            raise RuntimeError("collector role attributes are unsafe")
        cursor.execute(
            "SELECT count(*) FROM pg_auth_members membership "
            "JOIN pg_roles child ON child.oid = membership.member "
            "WHERE child.rolname = %s",
            (COLLECTOR_ROLE,),
        )
        if _required_row(cursor)[0] != 0:
            raise RuntimeError("collector role has role memberships")
        cursor.execute(
            "SELECT object_kind, object_name FROM ("
            "SELECT 'database' AS object_kind, database.datname AS object_name "
            "FROM pg_database database JOIN pg_roles owner ON owner.oid = database.datdba "
            "WHERE owner.rolname = %s "
            "UNION ALL SELECT 'schema', schema.nspname FROM pg_namespace schema "
            "JOIN pg_roles owner ON owner.oid = schema.nspowner WHERE owner.rolname = %s "
            "UNION ALL SELECT 'relation', schema.nspname || '.' || object.relname "
            "FROM pg_class object JOIN pg_namespace schema ON schema.oid = object.relnamespace "
            "JOIN pg_roles owner ON owner.oid = object.relowner WHERE owner.rolname = %s "
            "UNION ALL SELECT 'function', schema.nspname || '.' || function.proname "
            "FROM pg_proc function JOIN pg_namespace schema ON schema.oid = function.pronamespace "
            "JOIN pg_roles owner ON owner.oid = function.proowner WHERE owner.rolname = %s "
            "UNION ALL SELECT 'type', schema.nspname || '.' || type.typname "
            "FROM pg_type type JOIN pg_namespace schema ON schema.oid = type.typnamespace "
            "JOIN pg_roles owner ON owner.oid = type.typowner WHERE owner.rolname = %s"
            ") owned LIMIT 1",
            (COLLECTOR_ROLE,) * 5,
        )
        if cursor.fetchone() is not None:
            raise RuntimeError("collector role owns database objects")

        cursor.execute(
            "SELECT nspname FROM pg_namespace "
            "WHERE nspname !~ '^pg_' AND nspname <> 'information_schema' ORDER BY nspname"
        )
        schemas = [row[0] for row in cursor.fetchall()]
        cursor.execute("SELECT current_user")
        function_owner = _required_row(cursor)[0]
        cursor.execute(
            sql.SQL(
                "ALTER DEFAULT PRIVILEGES FOR ROLE {} REVOKE EXECUTE ON FUNCTIONS FROM PUBLIC"
            ).format(sql.Identifier(function_owner))
        )
        for schema in schemas:
            schema_id = sql.Identifier(schema)
            role_id = sql.Identifier(COLLECTOR_ROLE)
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON ALL TABLES IN SCHEMA {} FROM {}").format(
                    schema_id, role_id
                )
            )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(
                    schema_id, role_id
                )
            )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA {} FROM {}").format(
                    schema_id, role_id
                )
            )
            # EXECUTE is granted to PUBLIC by default. Snapshot each existing
            # non-collector role/function entitlement before removing that
            # ambient grant, then restore those exact effective entitlements as
            # explicit grants. This denies the collector without changing the
            # capabilities of unrelated runtime or reporting roles.
            cursor.execute(
                "SELECT role.rolname, function.proname, "
                "pg_get_function_identity_arguments(function.oid) "
                "FROM pg_roles role CROSS JOIN pg_proc function "
                "JOIN pg_namespace namespace ON namespace.oid = function.pronamespace "
                "WHERE namespace.nspname = %s AND role.rolname <> %s "
                "AND has_function_privilege(role.oid, function.oid, 'EXECUTE') "
                "ORDER BY role.rolname, function.oid",
                (schema, COLLECTOR_ROLE),
            )
            preserved_function_grants = cursor.fetchall()
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON ALL FUNCTIONS IN SCHEMA {} FROM PUBLIC").format(
                    schema_id
                )
            )
            for role, function_name, identity_arguments in preserved_function_grants:
                cursor.execute(
                    sql.SQL("GRANT EXECUTE ON FUNCTION {}.{}({}) TO {}").format(
                        schema_id,
                        sql.Identifier(function_name),
                        sql.SQL(identity_arguments),
                        sql.Identifier(role),
                    )
                )
            cursor.execute(
                "SELECT type.typname FROM pg_type type "
                "JOIN pg_namespace namespace ON namespace.oid = type.typnamespace "
                "WHERE namespace.nspname = %s AND type.typrelid = 0 AND type.typelem = 0",
                (schema,),
            )
            for (type_name,) in cursor.fetchall():
                cursor.execute(
                    sql.SQL("REVOKE ALL PRIVILEGES ON TYPE {} FROM {}").format(
                        sql.Identifier(schema, type_name), role_id
                    )
                )
            cursor.execute(
                sql.SQL("REVOKE ALL PRIVILEGES ON SCHEMA {} FROM {}").format(schema_id, role_id)
            )

        cursor.execute("SELECT current_database(), current_schema()")
        database, evidence_schema = _required_row(cursor)
        cursor.execute(
            sql.SQL("REVOKE ALL PRIVILEGES ON DATABASE {} FROM {}").format(
                sql.Identifier(database), sql.Identifier(COLLECTOR_ROLE)
            )
        )
        # PostgreSQL grants TEMPORARY to PUBLIC by default. There is no role-level
        # DENY, so remove that ambient privilege before attesting this login.
        cursor.execute(
            sql.SQL("REVOKE TEMPORARY ON DATABASE {} FROM PUBLIC").format(sql.Identifier(database))
        )
        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(COLLECTOR_ROLE)
            )
        )
        cursor.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(evidence_schema), sql.Identifier(COLLECTOR_ROLE)
            )
        )
        evidence_relations = sql.SQL(", ").join(
            sql.Identifier(evidence_schema, table) for table in EVIDENCE_TABLES
        )
        cursor.execute(
            sql.SQL("GRANT SELECT ON {} TO {}").format(
                evidence_relations, sql.Identifier(COLLECTOR_ROLE)
            )
        )
        insert_relations = sql.SQL(", ").join(
            sql.Identifier(evidence_schema, table) for table in INSERT_TABLES
        )
        cursor.execute(
            sql.SQL("GRANT INSERT ON {} TO {}").format(
                insert_relations, sql.Identifier(COLLECTOR_ROLE)
            )
        )
        for table, columns in UPDATE_COLUMNS.items():
            cursor.execute(
                sql.SQL("GRANT UPDATE ({}) ON {} TO {}").format(
                    sql.SQL(", ").join(map(sql.Identifier, columns)),
                    sql.Identifier(evidence_schema, table),
                    sql.Identifier(COLLECTOR_ROLE),
                )
            )

        cursor.execute(
            "SELECT c.oid, n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind IN ('r', 'p') AND n.nspname !~ '^pg_' "
            "AND n.nspname <> 'information_schema' ORDER BY n.nspname, c.relname"
        )
        tables = cursor.fetchall()
        present_evidence_tables = {
            table
            for _, schema, table in tables
            if schema == evidence_schema and table in EVIDENCE_TABLES
        }
        if present_evidence_tables != set(EVIDENCE_TABLES):
            raise RuntimeError("collector evidence table inventory is incomplete")
        for oid, schema, table in tables:
            for privilege in TABLE_PRIVILEGES:
                cursor.execute(
                    "SELECT has_table_privilege(%s, %s, %s)",
                    (COLLECTOR_ROLE, oid, privilege),
                )
                actual = _required_row(cursor)[0]
                expected = (
                    schema == evidence_schema
                    and table in EVIDENCE_TABLES
                    and (
                        privilege == "SELECT" or (privilege == "INSERT" and table in INSERT_TABLES)
                    )
                )
                if actual != expected:
                    raise RuntimeError("collector table ACL attestation failed")
            cursor.execute(
                "SELECT attnum, attname FROM pg_attribute "
                "WHERE attrelid = %s AND attnum > 0 AND NOT attisdropped ORDER BY attnum",
                (oid,),
            )
            for column_number, column_name in cursor.fetchall():
                cursor.execute(
                    "SELECT has_column_privilege(%s, %s, %s, 'UPDATE')",
                    (COLLECTOR_ROLE, oid, column_number),
                )
                expected_update = schema == evidence_schema and column_name in UPDATE_COLUMNS.get(
                    table, ()
                )
                if _required_row(cursor)[0] != expected_update:
                    raise RuntimeError("collector column ACL attestation failed")

        cursor.execute(
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid = c.relnamespace "
            "WHERE c.relkind = 'S' AND n.nspname !~ '^pg_' "
            "AND n.nspname <> 'information_schema'"
        )
        for (oid,) in cursor.fetchall():
            for privilege in ("USAGE", "SELECT", "UPDATE"):
                cursor.execute(
                    "SELECT has_sequence_privilege(%s, %s, %s)",
                    (COLLECTOR_ROLE, oid, privilege),
                )
                if _required_row(cursor)[0]:
                    raise RuntimeError("collector sequence ACL attestation failed")

        cursor.execute(
            "SELECT function.oid FROM pg_proc function "
            "JOIN pg_namespace schema ON schema.oid = function.pronamespace "
            "WHERE schema.nspname !~ '^pg_' AND schema.nspname <> 'information_schema'"
        )
        for (oid,) in cursor.fetchall():
            cursor.execute(
                "SELECT has_function_privilege(%s, %s, 'EXECUTE')",
                (COLLECTOR_ROLE, oid),
            )
            if _required_row(cursor)[0]:
                raise RuntimeError("collector function ACL attestation failed")

        for schema in schemas:
            for privilege in ("USAGE", "CREATE"):
                cursor.execute(
                    "SELECT has_schema_privilege(%s, %s, %s)",
                    (COLLECTOR_ROLE, schema, privilege),
                )
                expected = schema == evidence_schema and privilege == "USAGE"
                if _required_row(cursor)[0] != expected:
                    raise RuntimeError("collector schema ACL attestation failed")

        for privilege, expected in (("CONNECT", True), ("CREATE", False), ("TEMPORARY", False)):
            cursor.execute(
                "SELECT has_database_privilege(%s, %s, %s)",
                (COLLECTOR_ROLE, database, privilege),
            )
            if _required_row(cursor)[0] != expected:
                raise RuntimeError("collector database ACL attestation failed")


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("provision", "reconcile"))
    action = parser.parse_args().action
    admin_url = os.environ.get("DATABASE_URL", "")
    if action == "provision":
        provision_collector_password(admin_url, os.environ.get("COLLECTOR_DATABASE_URL", ""))
    else:
        reconcile_collector_access(admin_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
