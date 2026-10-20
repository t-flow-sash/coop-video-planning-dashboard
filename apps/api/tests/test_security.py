from __future__ import annotations

import base64
import json
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import cast
from uuid import UUID

import pytest
from fastapi.testclient import TestClient
from pydantic import ValidationError
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

from app.config import Settings
from app.db import engine
from app.main import app, create_app


def test_checked_in_openapi_contract_matches_runtime_schema() -> None:
    contract_path = Path(__file__).parents[3] / "packages/api-client/openapi.json"
    assert json.loads(contract_path.read_text(encoding="utf-8")) == app.openapi()


def basic_auth(username: str = "api-user", password: str = "api-password") -> dict[str, str]:
    token = base64.b64encode(f"{username}:{password}".encode()).decode()
    return {"Authorization": f"Basic {token}"}


def basic_settings(*, enable_docs: bool | None = None) -> Settings:
    return Settings(
        _env_file=None,
        app_env="production",
        auth_mode="basic",
        auth_username="api-user",
        auth_password="api-password",
        cors_origin="https://dashboard.example.com",
        enable_docs=enable_docs,
    )


def test_disabled_auth_outside_development_requires_explicit_opt_in() -> None:
    with pytest.raises(ValidationError, match="ALLOW_UNAUTHENTICATED"):
        Settings(_env_file=None, app_env="production", auth_mode="disabled")


def test_disabled_auth_with_opt_in_serves_business_routes_without_credentials() -> None:
    open_settings = Settings(
        _env_file=None,
        app_env="production",
        auth_mode="disabled",
        allow_unauthenticated=True,
        cors_origin="https://dashboard.example.com",
        enable_docs=False,
    )
    open_app = create_app(open_settings)
    with TestClient(open_app) as client:
        assert client.get("/api/v1/coops").status_code == 200
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/health/ready").status_code == 200
        assert "www-authenticate" not in client.get("/api/v1/coops").headers


def test_disabled_auth_opt_in_does_not_require_auth_secrets() -> None:
    open_settings = Settings(
        _env_file=None,
        app_env="production",
        auth_mode="disabled",
        allow_unauthenticated=True,
        cors_origin="https://dashboard.example.com",
    )
    assert open_settings.auth_username is None
    assert open_settings.auth_password is None


def test_basic_auth_requires_both_secret_environment_values() -> None:
    with pytest.raises(ValidationError, match="AUTH_USERNAME and AUTH_PASSWORD"):
        Settings(_env_file=None, app_env="production", auth_mode="basic")

    with pytest.raises(ValidationError, match="AUTH_USERNAME and AUTH_PASSWORD"):
        Settings(
            _env_file=None,
            app_env="production",
            auth_mode="basic",
            auth_username="api-user",
        )


def test_cors_origin_must_be_a_single_explicit_http_origin() -> None:
    for origin in ("*", "https://*.example.com", "null", "https://example.com/path"):
        with pytest.raises(ValidationError, match="CORS_ORIGIN"):
            Settings(_env_file=None, cors_origin=origin)


def test_basic_auth_protects_every_business_and_documentation_route() -> None:
    secured_app = create_app(basic_settings(enable_docs=True))
    protected_paths = {
        path
        for route in secured_app.routes
        if (path := getattr(route, "path", ""))
        not in {"/api/v1/health/live", "/api/v1/health/ready"}
    }
    assert {"/docs", "/openapi.json", "/api/v1/coops"} <= protected_paths

    with TestClient(secured_app) as client:
        for path in sorted(protected_paths):
            concrete_path = path.replace("{coop_id}", "00000000-0000-4000-8000-000000000000")
            response = client.get(concrete_path)
            assert response.status_code == 401, concrete_path
            assert response.headers["www-authenticate"] == 'Basic realm="coop-api"'

        assert client.get("/docs", headers=basic_auth()).status_code == 200
        assert client.get("/openapi.json", headers=basic_auth()).status_code == 200
        assert client.get("/api/v1/coops", headers=basic_auth()).status_code == 200


def test_invalid_basic_credentials_are_rejected() -> None:
    secured_app = create_app(basic_settings(enable_docs=True))
    with TestClient(secured_app) as client:
        for headers in (
            {"Authorization": "Bearer token"},
            {"Authorization": "Basic not-base64"},
            basic_auth(password="wrong"),
            basic_auth(username="wrong"),
        ):
            assert client.get("/api/v1/coops", headers=headers).status_code == 401


def test_health_routes_are_public_when_basic_auth_is_enabled() -> None:
    secured_app = create_app(basic_settings())
    with TestClient(secured_app) as client:
        assert client.get("/api/v1/health/live").status_code == 200
        assert client.get("/api/v1/health/ready").status_code == 200


def test_production_disables_documentation_and_openapi_by_default() -> None:
    secured_app = create_app(basic_settings())
    with TestClient(secured_app) as client:
        headers = basic_auth()
        assert client.get("/docs", headers=headers).status_code == 404
        assert client.get("/redoc", headers=headers).status_code == 404
        assert client.get("/openapi.json", headers=headers).status_code == 404


def test_cors_only_allows_the_configured_origin_without_wildcards() -> None:
    secured_app = create_app(basic_settings())
    with TestClient(secured_app) as client:
        allowed = client.options(
            "/api/v1/coops",
            headers={
                "Origin": "https://dashboard.example.com",
                "Access-Control-Request-Method": "GET",
                "Access-Control-Request-Headers": "authorization,x-request-id",
            },
        )
        assert allowed.status_code == 200
        assert allowed.headers["access-control-allow-origin"] == "https://dashboard.example.com"
        assert allowed.headers["access-control-allow-methods"] != "*"
        assert allowed.headers["access-control-allow-headers"] != "*"

        denied = client.options(
            "/api/v1/coops",
            headers={
                "Origin": "https://evil.example.com",
                "Access-Control-Request-Method": "GET",
            },
        )
        assert "access-control-allow-origin" not in denied.headers


def test_request_id_accepts_safe_values_and_replaces_invalid_values() -> None:
    valid = "trace_ID-123.abc"
    assert app.url_path_for("live") == "/api/v1/health/live"
    with TestClient(app) as client:
        assert (
            client.get("/api/v1/health/live", headers={"X-Request-ID": valid}).headers[
                "X-Request-ID"
            ]
            == valid
        )

        for invalid in ("has spaces", "contains/slash", "comma,value", "x" * 101):
            replacement = client.get(
                "/api/v1/health/live", headers={"X-Request-ID": invalid}
            ).headers["X-Request-ID"]
            UUID(replacement)
            assert replacement != invalid


@contextmanager
def temporarily_set_alembic_version(version: str) -> Iterator[None]:
    with engine.begin() as connection:
        original = connection.scalar(text("SELECT version_num FROM alembic_version"))
        connection.execute(
            text("UPDATE alembic_version SET version_num = :version"), {"version": version}
        )
    try:
        yield
    finally:
        with engine.begin() as connection:
            connection.execute(
                text("UPDATE alembic_version SET version_num = :version"), {"version": original}
            )


@contextmanager
def temporarily_rename_table(table: str) -> Iterator[None]:
    renamed = f"{table}_readiness_test"
    with engine.begin() as connection:
        connection.execute(text(f'ALTER TABLE "{table}" RENAME TO "{renamed}"'))
    try:
        yield
    finally:
        with engine.begin() as connection:
            connection.execute(text(f'ALTER TABLE "{renamed}" RENAME TO "{table}"'))


def test_readiness_requires_exact_alembic_head(client: TestClient) -> None:
    with temporarily_set_alembic_version("000000000000"):
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["code"] == "not_ready"


def test_readiness_requires_every_application_table(client: TestClient) -> None:
    with temporarily_rename_table("contact"):
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503


class BrokenEngine:
    @contextmanager
    def connect(self) -> Iterator[Connection]:
        raise RuntimeError("database unavailable")
        yield  # pragma: no cover


def test_readiness_reports_database_failure_without_leaking_details() -> None:
    failed_app = create_app(basic_settings(), database_engine=cast(Engine, BrokenEngine()))
    with TestClient(failed_app) as client:
        response = client.get("/api/v1/health/ready")
    assert response.status_code == 503
    assert response.json()["detail"] == "Database schema unavailable."
