from __future__ import annotations

import contextlib
import ipaddress
from urllib.parse import urlparse

from sqlalchemy import Engine, text
from sqlalchemy.engine import Connection, make_url

SAFE_TEST_PORTS = {5432, 55435}


def assert_safe_test_database(database_url: str, app_env: str) -> None:
    """Fail closed before any destructive test fixture touches a database."""
    try:
        parsed = urlparse(database_url)
        host = parsed.hostname
        port = parsed.port
    except ValueError:
        parsed = urlparse("")
        host = None
        port = None
    database = parsed.path.removeprefix("/")
    local = host == "localhost"
    if host is not None:
        with contextlib.suppress(ValueError):
            local = local or ipaddress.ip_address(host).is_loopback
    if (
        app_env != "test"
        or parsed.scheme != "postgresql+psycopg"
        or parsed.query
        or parsed.fragment
        or parsed.username != "coop"
        or not parsed.password
        or not local
        or port not in SAFE_TEST_PORTS
        or "/" in database
        or not database.endswith("_test")
    ):
        raise RuntimeError(
            "Refusing destructive test cleanup: APP_ENV=test and an unambiguous "
            "postgresql+psycopg URL for coop on an approved loopback test port with an explicit "
            "*_test database and no query parameters are required"
        )


def assert_safe_test_engine(engine: Engine, expected_url: str, app_env: str) -> None:
    """Validate the immutable engine binding, never a mutable environment variable."""
    assert_safe_test_database(expected_url, app_env)
    bound_url = engine.url.render_as_string(hide_password=False)
    assert_safe_test_database(bound_url, app_env)
    if make_url(bound_url) != make_url(expected_url):
        raise RuntimeError("Refusing destructive test cleanup: bound test engine differs")


def assert_safe_test_connection(
    connection: Connection,
    expected_url: str,
    app_env: str,
    *,
    expected_server_address: str | None = None,
) -> None:
    """Attest the connected PostgreSQL target immediately before destructive SQL."""
    assert_safe_test_engine(connection.engine, expected_url, app_env)
    expected = make_url(expected_url)
    try:
        identity = (
            connection.execute(
                text(
                    "SELECT current_database() AS database_name, session_user, current_user, "
                    "host(inet_server_addr()) AS server_address, inet_server_port() AS server_port"
                )
            )
            .mappings()
            .one()
        )
        server_address = ipaddress.ip_address(identity["server_address"])
        required_server_address = (
            ipaddress.ip_address(expected_server_address)
            if expected_server_address is not None
            else None
        )
    except Exception as error:
        raise RuntimeError(
            "Refusing destructive test cleanup: connected database identity unavailable"
        ) from error
    if (
        identity["database_name"] != expected.database
        or identity["session_user"] != expected.username
        or identity["current_user"] != expected.username
        or (
            server_address != required_server_address
            if required_server_address is not None
            else not server_address.is_loopback
        )
        or identity["server_port"] != expected.port
    ):
        raise RuntimeError("Refusing destructive test cleanup: connected database identity differs")
