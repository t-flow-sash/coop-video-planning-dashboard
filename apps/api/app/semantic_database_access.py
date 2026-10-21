"""Separate, fail-closed semantic writer credential and ACL lifecycle.

Run provision/reconcile only after an independently reviewed SOPS credential exists.
The metadata collector role is never granted domain access here.
"""

from __future__ import annotations

import argparse
import os
from typing import Any, cast

import psycopg
from psycopg import sql
from sqlalchemy.engine import make_url

from app.database_access import _psycopg_url, _same_target

ROLE = "coop_semantic_writer"
# Explicit scope: evidence and route read, semantic decision and reviewed-domain
# ledger write, coop/domain audit write. No ownership, DDL, sequences or DELETE.
READ_TABLES = (
    "ingestion_source",
    "source_record",
    "source_record_revision",
    "semantic_track_route",
    "semantic_message_state",
    "semantic_queue_cursor",
    "reviewed_domain_track",
    "reviewed_domain_batch",
    "reviewed_domain_item",
    "reviewed_domain_evidence",
    "coop",
    "coop_import_state",
    "audit_event",
)
INSERT_TABLES = (
    "semantic_message_state",
    "semantic_queue_cursor",
    "reviewed_domain_batch",
    "reviewed_domain_track",
    "reviewed_domain_item",
    "reviewed_domain_evidence",
    "coop",
    "audit_event",
)
UPDATE_TABLES = ("semantic_message_state", "semantic_queue_cursor", "reviewed_domain_track", "coop")
ALLOWED = set(READ_TABLES) | set(INSERT_TABLES) | set(UPDATE_TABLES)
PRIVILEGES = ("SELECT", "INSERT", "UPDATE", "DELETE", "TRUNCATE", "REFERENCES", "TRIGGER")


def writer_password_from_urls(admin_url: str, writer_url: str, collector_url: str) -> str:
    try:
        admin, writer, collector = map(make_url, (admin_url, writer_url, collector_url))
    except Exception as error:
        raise RuntimeError("invalid semantic writer database URL") from error
    if (
        not _same_target(admin, writer)
        or not _same_target(admin, collector)
        or writer.username != ROLE
        or collector.username != "coop_evidence_collector"
        or not writer.password
        or writer.password in {admin.password, collector.password}
        or admin.username == writer.username
    ):
        raise RuntimeError("invalid semantic writer database URL")
    return writer.password


def _required_row(cursor: psycopg.Cursor[Any]) -> tuple[Any, ...]:
    row = cursor.fetchone()
    if row is None:
        raise RuntimeError("semantic writer ACL query returned no row")
    return cast(tuple[Any, ...], row)


def _check_role(cursor: psycopg.Cursor[Any], *, login: bool) -> None:
    cursor.execute(
        "SELECT rolcanlogin, rolsuper, rolcreatedb, rolcreaterole, rolinherit, "
        "rolreplication, rolbypassrls FROM pg_roles WHERE rolname = %s",
        (ROLE,),
    )
    if _required_row(cursor) != (login, False, False, False, False, False, False):
        raise RuntimeError("semantic writer role attributes are unsafe")
    cursor.execute(
        "SELECT count(*) FROM pg_auth_members m JOIN pg_roles r ON r.oid=m.member "
        "WHERE r.rolname=%s",
        (ROLE,),
    )
    if _required_row(cursor) != (0,):
        raise RuntimeError("semantic writer role memberships are unsafe")
    cursor.execute(
        "SELECT 1 FROM pg_database d JOIN pg_roles r ON r.oid=d.datdba WHERE r.rolname=%s "
        "UNION ALL SELECT 1 FROM pg_namespace n JOIN pg_roles r "
        "ON r.oid=n.nspowner WHERE r.rolname=%s "
        "UNION ALL SELECT 1 FROM pg_class c JOIN pg_roles r ON r.oid=c.relowner WHERE r.rolname=%s "
        "UNION ALL SELECT 1 FROM pg_proc p JOIN pg_roles r ON r.oid=p.proowner WHERE r.rolname=%s "
        "UNION ALL SELECT 1 FROM pg_type t JOIN pg_roles r "
        "ON r.oid=t.typowner WHERE r.rolname=%s LIMIT 1",
        (ROLE,) * 5,
    )
    if cursor.fetchone() is not None:
        raise RuntimeError("semantic writer owns database objects")


def provision(admin_url: str, writer_url: str, collector_url: str) -> None:
    password = writer_password_from_urls(admin_url, writer_url, collector_url)
    with psycopg.connect(_psycopg_url(admin_url)) as connection, connection.cursor() as cursor:
        cursor.execute("SELECT rolcanlogin FROM pg_roles WHERE rolname=%s", (ROLE,))
        existing = _required_row(cursor)[0]
        _check_role(cursor, login=existing)
        cursor.execute(
            sql.SQL(
                "ALTER ROLE {} LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE "
                "NOINHERIT NOREPLICATION NOBYPASSRLS PASSWORD {}"
            ).format(sql.Identifier(ROLE), sql.Literal(password))
        )


def reconcile(admin_url: str) -> None:
    """Revoke drift before narrowly granting, then attest effective permissions.

    Failure rolls the entire transaction back; caller must block the writer until
    reconciliation succeeds. PUBLIC/membership/ownership bypasses fail closed.
    """
    with psycopg.connect(_psycopg_url(admin_url)) as connection, connection.cursor() as cursor:
        _check_role(cursor, login=True)
        cursor.execute("SELECT current_schema(), current_database()")
        schema, database = _required_row(cursor)
        cursor.execute(
            "SELECT nspname FROM pg_namespace WHERE nspname !~ '^pg_' "
            "AND nspname <> 'information_schema'"
        )
        schemas = [row[0] for row in cursor.fetchall()]
        for item in schemas:
            cursor.execute(
                sql.SQL("REVOKE ALL ON ALL TABLES IN SCHEMA {} FROM {}").format(
                    sql.Identifier(item), sql.Identifier(ROLE)
                )
            )
            cursor.execute(
                sql.SQL("REVOKE ALL ON ALL SEQUENCES IN SCHEMA {} FROM {}").format(
                    sql.Identifier(item), sql.Identifier(ROLE)
                )
            )
            cursor.execute(
                sql.SQL("REVOKE ALL ON ALL FUNCTIONS IN SCHEMA {} FROM {}").format(
                    sql.Identifier(item), sql.Identifier(ROLE)
                )
            )
            cursor.execute(
                "SELECT c.relname, a.attname FROM pg_class c "
                "JOIN pg_namespace n ON n.oid=c.relnamespace "
                "JOIN pg_attribute a ON a.attrelid=c.oid "
                "WHERE n.nspname=%s AND c.relkind IN ('r','p') "
                "AND a.attnum>0 AND NOT a.attisdropped",
                (item,),
            )
            for relation, column in cursor.fetchall():
                cursor.execute(
                    sql.SQL("REVOKE ALL ({}) ON {} FROM {}").format(
                        sql.Identifier(column),
                        sql.Identifier(item, relation),
                        sql.Identifier(ROLE),
                    )
                )
            cursor.execute(
                "SELECT t.typname FROM pg_type t JOIN pg_namespace n ON n.oid=t.typnamespace "
                "WHERE n.nspname=%s AND t.typrelid=0 AND t.typelem=0",
                (item,),
            )
            for (type_name,) in cursor.fetchall():
                cursor.execute(
                    sql.SQL("REVOKE ALL ON TYPE {} FROM {}").format(
                        sql.Identifier(item, type_name), sql.Identifier(ROLE)
                    )
                )
                cursor.execute(
                    "SELECT r.rolname FROM pg_roles r JOIN pg_type t ON t.typname=%s "
                    "JOIN pg_namespace n ON n.oid=t.typnamespace "
                    "WHERE n.nspname=%s AND r.rolname<>%s "
                    "AND has_type_privilege(r.oid,t.oid,'USAGE')",
                    (type_name, item, ROLE),
                )
                entitled = [row[0] for row in cursor.fetchall()]
                cursor.execute(
                    sql.SQL("REVOKE USAGE ON TYPE {} FROM PUBLIC").format(
                        sql.Identifier(item, type_name)
                    )
                )
                for role_name in entitled:
                    cursor.execute(
                        sql.SQL("GRANT USAGE ON TYPE {} TO {}").format(
                            sql.Identifier(item, type_name), sql.Identifier(role_name)
                        )
                    )
            cursor.execute(
                sql.SQL("REVOKE ALL ON SCHEMA {} FROM {}").format(
                    sql.Identifier(item), sql.Identifier(ROLE)
                )
            )
        cursor.execute(
            sql.SQL("REVOKE ALL ON DATABASE {} FROM {}").format(
                sql.Identifier(database), sql.Identifier(ROLE)
            )
        )
        cursor.execute(
            sql.SQL("GRANT CONNECT ON DATABASE {} TO {}").format(
                sql.Identifier(database), sql.Identifier(ROLE)
            )
        )
        cursor.execute(
            sql.SQL("GRANT USAGE ON SCHEMA {} TO {}").format(
                sql.Identifier(schema), sql.Identifier(ROLE)
            )
        )
        for privilege, tables in (
            ("SELECT", READ_TABLES),
            ("INSERT", INSERT_TABLES),
            ("UPDATE", UPDATE_TABLES),
        ):
            cursor.execute(
                sql.SQL("GRANT {} ON {} TO {}").format(
                    sql.SQL(privilege),
                    sql.SQL(", ").join(sql.Identifier(schema, t) for t in tables),
                    sql.Identifier(ROLE),
                )
            )
        cursor.execute(
            "SELECT c.oid, n.nspname, c.relname FROM pg_class c "
            "JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relkind IN ('r','p') AND n.nspname !~ '^pg_' "
            "AND n.nspname <> 'information_schema'"
        )
        table_rows = cursor.fetchall()
        if {t for _, s, t in table_rows if s == schema} < ALLOWED:
            raise RuntimeError("semantic writer table inventory incomplete")
        for oid, item_schema, table in table_rows:
            for privilege in PRIVILEGES:
                cursor.execute("SELECT has_table_privilege(%s,%s,%s)", (ROLE, oid, privilege))
                expected = item_schema == schema and (
                    (privilege == "SELECT" and table in READ_TABLES)
                    or (privilege == "INSERT" and table in INSERT_TABLES)
                    or (privilege == "UPDATE" and table in UPDATE_TABLES)
                )
                if _required_row(cursor)[0] != expected:
                    raise RuntimeError("semantic writer table ACL attestation failed")
            cursor.execute(
                "SELECT attnum FROM pg_attribute "
                "WHERE attrelid=%s AND attnum>0 AND NOT attisdropped",
                (oid,),
            )
            for (column_number,) in cursor.fetchall():
                for privilege in ("SELECT", "INSERT", "UPDATE", "REFERENCES"):
                    cursor.execute(
                        "SELECT has_column_privilege(%s,%s,%s,%s)",
                        (ROLE, oid, column_number, privilege),
                    )
                    expected = item_schema == schema and (
                        (privilege == "SELECT" and table in READ_TABLES)
                        or (privilege == "INSERT" and table in INSERT_TABLES)
                        or (privilege == "UPDATE" and table in UPDATE_TABLES)
                    )
                    if _required_row(cursor)[0] != expected:
                        raise RuntimeError("semantic writer column ACL attestation failed")
        for item in schemas:
            for privilege in ("USAGE", "CREATE"):
                cursor.execute("SELECT has_schema_privilege(%s,%s,%s)", (ROLE, item, privilege))
                if _required_row(cursor)[0] != (item == schema and privilege == "USAGE"):
                    raise RuntimeError("semantic writer schema ACL attestation failed")
        for privilege, expected in (("CONNECT", True), ("CREATE", False), ("TEMPORARY", False)):
            cursor.execute("SELECT has_database_privilege(%s,%s,%s)", (ROLE, database, privilege))
            if _required_row(cursor)[0] != expected:
                raise RuntimeError("semantic writer database ACL attestation failed")
        cursor.execute(
            "SELECT c.oid FROM pg_class c JOIN pg_namespace n ON n.oid=c.relnamespace "
            "WHERE c.relkind='S' AND n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
        )
        for (oid,) in cursor.fetchall():
            for privilege in ("USAGE", "SELECT", "UPDATE"):
                cursor.execute("SELECT has_sequence_privilege(%s,%s,%s)", (ROLE, oid, privilege))
                if _required_row(cursor)[0]:
                    raise RuntimeError("semantic writer sequence ACL attestation failed")
        cursor.execute(
            "SELECT p.oid FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema'"
        )
        for (oid,) in cursor.fetchall():
            cursor.execute("SELECT has_function_privilege(%s,%s,'EXECUTE')", (ROLE, oid))
            if _required_row(cursor)[0]:
                raise RuntimeError("semantic writer function ACL attestation failed")
        cursor.execute(
            "SELECT t.oid, t.typname, n.nspname FROM pg_type t "
            "JOIN pg_namespace n ON n.oid=t.typnamespace "
            "WHERE n.nspname !~ '^pg_' AND n.nspname <> 'information_schema' "
            "AND t.typrelid=0 AND t.typelem=0"
        )
        for oid, type_name, type_schema in cursor.fetchall():
            cursor.execute("SELECT has_type_privilege(%s,%s,'USAGE')", (ROLE, oid))
            if _required_row(cursor)[0]:
                raise RuntimeError(
                    f"semantic writer type ACL attestation failed: {type_schema}.{type_name}"
                )
        # Re-attest the collector's complete evidence-only ACL after writer changes.
        # Caller must run app.database_access reconcile in the same release gate.


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("action", choices=("provision", "reconcile"))
    args = parser.parse_args()
    admin_url = os.environ.get("DATABASE_URL", "")
    if args.action == "provision":
        provision(
            admin_url,
            os.environ.get("SEMANTIC_DATABASE_URL", ""),
            os.environ.get("COLLECTOR_DATABASE_URL", ""),
        )
    else:
        reconcile(admin_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
