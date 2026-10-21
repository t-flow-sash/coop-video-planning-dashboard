from __future__ import annotations

import base64
import binascii
import re
import secrets
import uuid
from datetime import date, datetime
from functools import lru_cache
from pathlib import Path
from typing import Annotated, Any, Literal, TypeAlias
from uuid import UUID

from alembic.script import ScriptDirectory
from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import Settings, get_settings
from app.db import engine, get_session
from app.domain import CoopStatus
from app.errors import ApiProblem
from app.models import AuditEvent
from app.schemas import (
    AuditEventPage,
    AuditEventRead,
    ContactCreate,
    ContactRead,
    CoopCreate,
    CoopDetail,
    CoopPage,
    CoopPatch,
    CoopRead,
    OrganizationCreate,
    OrganizationRead,
    ReopenRequest,
    TransitionRequest,
)
from app.semantic_status import snapshot as semantic_snapshot
from app.services.coops import (
    coop_filters,
    create_contact,
    create_coop,
    create_organization,
    decode_cursor,
    detail_statement,
    encode_cursor,
    list_coops,
    reopen_coop,
    transition_coop,
    update_coop,
)

settings = get_settings()
app = FastAPI(title="Coop Video Planning API", version="1.0.0")
WriteSession: TypeAlias = Annotated[Session, Depends(get_session, scope="function")]

REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,99}$")
PUBLIC_PATHS = {"/api/v1/health/live", "/api/v1/health/ready"}
REQUIRED_TABLES = {"organization", "contact", "coop", "coop_contact", "audit_event"}


@lru_cache
def alembic_heads() -> frozenset[str]:
    script_path = Path(__file__).resolve().parents[1] / "alembic"
    return frozenset(ScriptDirectory(str(script_path)).get_heads())


def problem_response(
    request: Request,
    *,
    status_code: int,
    code: str,
    title: str,
    detail: str,
    errors: list[dict[str, Any]] | None = None,
) -> JSONResponse:
    body: dict[str, Any] = {
        "type": f"https://coop.local/problems/{code}",
        "title": title,
        "status": status_code,
        "detail": detail,
        "instance": str(request.url.path),
        "code": code,
        "request_id": getattr(request.state, "request_id", None),
    }
    if errors is not None:
        body["errors"] = errors
    return JSONResponse(body, status_code=status_code, media_type="application/problem+json")


@app.exception_handler(ApiProblem)
async def api_problem_handler(request: Request, exc: ApiProblem) -> JSONResponse:
    return problem_response(
        request,
        status_code=exc.status,
        code=exc.code,
        title=exc.title,
        detail=exc.detail,
        errors=exc.errors,
    )


@app.exception_handler(RequestValidationError)
async def validation_handler(request: Request, exc: RequestValidationError) -> JSONResponse:
    return problem_response(
        request,
        status_code=422,
        code="validation_error",
        title="Validation error",
        detail="The request does not satisfy the API contract.",
        errors=[
            {"location": list(error["loc"]), "message": error["msg"], "type": error["type"]}
            for error in exc.errors()
        ],
    )


@app.exception_handler(StarletteHTTPException)
async def http_exception_handler(request: Request, exc: StarletteHTTPException) -> JSONResponse:
    code = "not_found" if exc.status_code == 404 else "http_error"
    title = "Not found" if exc.status_code == 404 else "HTTP error"
    return problem_response(
        request,
        status_code=exc.status_code,
        code=code,
        title=title,
        detail=str(exc.detail),
    )


@app.exception_handler(Exception)
async def unexpected_exception_handler(request: Request, exc: Exception) -> JSONResponse:
    return problem_response(
        request,
        status_code=500,
        code="internal_error",
        title="Internal server error",
        detail="An unexpected error occurred.",
    )


@app.get("/api/v1/health/live")
def live() -> dict[str, str]:
    return {"status": "ok"}


@app.get("/api/v1/health/ready")
def ready(request: Request) -> dict[str, str]:
    try:
        with request.app.state.database_engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current_heads = frozenset(
                connection.scalars(text("SELECT version_num FROM alembic_version"))
            )
            existing_tables = {
                table
                for table in REQUIRED_TABLES
                if connection.scalar(text("SELECT to_regclass(:table_name)"), {"table_name": table})
                is not None
            }
            if current_heads != alembic_heads() or existing_tables != REQUIRED_TABLES:
                raise RuntimeError("schema does not match the required Alembic head")
    except Exception as error:
        raise ApiProblem(
            503, "not_ready", "Service not ready", "Database schema unavailable."
        ) from error
    return {"status": "ready"}


@app.post("/api/v1/organizations", response_model=OrganizationRead, status_code=201)
def post_organization(payload: OrganizationCreate, session: WriteSession) -> OrganizationRead:
    return OrganizationRead.model_validate(create_organization(session, payload))


@app.post("/api/v1/contacts", response_model=ContactRead, status_code=201)
def post_contact(payload: ContactCreate, session: WriteSession) -> ContactRead:
    return ContactRead.model_validate(create_contact(session, payload))


@app.post("/api/v1/coops", response_model=CoopRead, status_code=status.HTTP_201_CREATED)
def post_coop(payload: CoopCreate, request: Request, session: WriteSession) -> CoopRead:
    return CoopRead.model_validate(create_coop(session, payload, request.state.request_id))


@app.get("/api/v1/coops", response_model=CoopPage)
def get_coops(
    scope: Literal["open", "all"] = "open",
    coop_status: CoopStatus | None = Query(default=None, alias="status"),
    contact_id: UUID | None = None,
    organization_id: UUID | None = None,
    contact_key: str | None = None,
    organization_key: str | None = None,
    release_from: datetime | None = None,
    release_to: datetime | None = None,
    query: str | None = Query(default=None, max_length=200),
    priority: Literal["P0", "P1", "P2"] | None = None,
    needs_review: bool | None = None,
    deadline_to: date | None = None,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    offset = decode_cursor(cursor)
    filters = coop_filters(
        scope=scope,
        status=coop_status,
        contact_id=contact_id,
        organization_id=organization_id,
        contact_key=contact_key,
        organization_key=organization_key,
        release_from=release_from,
        release_to=release_to,
        query=query,
        priority=priority,
        needs_review=needs_review,
        deadline_to=deadline_to,
    )
    items, total = list_coops(session, limit=limit, offset=offset, filters=filters)
    next_cursor = encode_cursor(offset + len(items)) if offset + len(items) < total else None
    return {
        "items": [CoopDetail.model_validate(item) for item in items],
        "next_cursor": next_cursor,
        "total": total,
    }


@app.get("/api/v1/semantic/status")
def get_semantic_status(session: Session = Depends(get_session)) -> dict[str, Any]:
    return semantic_snapshot(session)


@app.get("/api/v1/coops/{coop_id}", response_model=CoopDetail)
def get_coop(coop_id: str, session: Session = Depends(get_session)) -> CoopDetail:
    coop = session.scalar(detail_statement(coop_id))
    if coop is None:
        raise ApiProblem(
            404, "coop_not_found", "Coop not found", "The requested coop does not exist."
        )
    return CoopDetail.model_validate(coop)


@app.patch("/api/v1/coops/{coop_id}", response_model=CoopRead)
def patch_coop(
    coop_id: str,
    payload: CoopPatch,
    request: Request,
    session: WriteSession,
) -> CoopRead:
    return CoopRead.model_validate(update_coop(session, coop_id, payload, request.state.request_id))


@app.post("/api/v1/coops/{coop_id}/transitions", response_model=CoopRead)
def post_transition(
    coop_id: str,
    payload: TransitionRequest,
    request: Request,
    session: WriteSession,
) -> CoopRead:
    return CoopRead.model_validate(
        transition_coop(
            session,
            coop_id,
            payload.target_status,
            payload.expected_version,
            request.state.request_id,
            payload.reason,
        )
    )


@app.post("/api/v1/coops/{coop_id}/reopen", response_model=CoopRead)
def post_reopen(
    coop_id: str,
    payload: ReopenRequest,
    request: Request,
    session: WriteSession,
) -> CoopRead:
    return CoopRead.model_validate(
        reopen_coop(
            session, coop_id, payload.expected_version, request.state.request_id, payload.reason
        )
    )


@app.get("/api/v1/coops/{coop_id}/audit-events", response_model=AuditEventPage)
def get_audit_events(
    coop_id: str,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    coop = session.scalar(detail_statement(coop_id))
    if coop is None:
        raise ApiProblem(
            404, "coop_not_found", "Coop not found", "The requested coop does not exist."
        )
    offset = decode_cursor(cursor)
    total = (
        session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.coop_id == coop.id)
        )
        or 0
    )
    events = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.coop_id == coop.id)
            .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
            .offset(offset)
            .limit(limit)
        )
    )
    return {
        "items": [AuditEventRead.model_validate(event).model_dump(mode="json") for event in events],
        "next_cursor": encode_cursor(offset + len(events))
        if offset + len(events) < total
        else None,
        "total": total,
    }


_route_template = app


def _credentials_are_valid(authorization: str | None, configured: Settings) -> bool:
    supplied_username = b""
    supplied_password = b""
    if authorization:
        scheme, _, encoded = authorization.partition(" ")
        if scheme.lower() == "basic" and encoded:
            try:
                decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
                username, separator, password = decoded.partition(":")
                if separator:
                    supplied_username = username.encode("utf-8")
                    supplied_password = password.encode("utf-8")
            except (binascii.Error, UnicodeDecodeError):
                pass

    expected_username = configured.auth_username
    expected_password = configured.auth_password
    username_matches = secrets.compare_digest(
        supplied_username,
        expected_username.get_secret_value().encode("utf-8") if expected_username else b"",
    )
    password_matches = secrets.compare_digest(
        supplied_password,
        expected_password.get_secret_value().encode("utf-8") if expected_password else b"",
    )
    return username_matches and password_matches


def create_app(configured: Settings, database_engine: Engine = engine) -> FastAPI:
    docs_url = "/docs" if configured.docs_enabled else None
    redoc_url = "/redoc" if configured.docs_enabled else None
    openapi_url = "/openapi.json" if configured.docs_enabled else None
    configured_app = FastAPI(
        title="Coop Video Planning API",
        version="1.0.0",
        docs_url=docs_url,
        redoc_url=redoc_url,
        openapi_url=openapi_url,
    )
    configured_app.state.database_engine = database_engine

    for exception_type, handler in _route_template.exception_handlers.items():
        configured_app.add_exception_handler(exception_type, handler)
    for route in _route_template.routes:
        if getattr(route, "path", "").startswith("/api/"):
            configured_app.router.routes.append(route)

    if configured.auth_mode == "basic":

        @configured_app.middleware("http")
        async def basic_auth_middleware(request: Request, call_next: Any) -> Any:
            if request.url.path not in PUBLIC_PATHS and not _credentials_are_valid(
                request.headers.get("Authorization"), configured
            ):
                response = problem_response(
                    request,
                    status_code=401,
                    code="unauthorized",
                    title="Unauthorized",
                    detail="Valid HTTP Basic credentials are required.",
                )
                response.headers["WWW-Authenticate"] = 'Basic realm="coop-api"'
                return response
            return await call_next(request)

    configured_app.add_middleware(
        CORSMiddleware,
        allow_origins=[configured.cors_origin],
        allow_credentials=True,
        allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
        allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
    )

    @configured_app.middleware("http")
    async def request_id_middleware(request: Request, call_next: Any) -> Any:
        candidate = request.headers.get("X-Request-ID", "")
        request.state.request_id = (
            candidate if REQUEST_ID_PATTERN.fullmatch(candidate) else str(uuid.uuid4())
        )
        response = await call_next(request)
        response.headers["X-Request-ID"] = request.state.request_id
        return response

    return configured_app


app = create_app(settings)
