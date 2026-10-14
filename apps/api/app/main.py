from __future__ import annotations

import uuid
from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from fastapi import Depends, FastAPI, Query, Request, status
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from sqlalchemy import func, select, text
from sqlalchemy.orm import Session
from starlette.exceptions import HTTPException as StarletteHTTPException

from app.config import get_settings
from app.db import engine, get_session
from app.domain import CoopStatus
from app.errors import ApiProblem
from app.models import AuditEvent, Coop
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
app.add_middleware(
    CORSMiddleware,
    allow_origins=[settings.cors_origin],
    allow_credentials=True,
    allow_methods=["GET", "POST", "PATCH", "PUT", "DELETE"],
    allow_headers=["Content-Type", "Authorization", "X-Request-ID"],
)


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


@app.middleware("http")
async def request_id_middleware(request: Request, call_next: Any) -> Any:
    request.state.request_id = request.headers.get("X-Request-ID") or str(uuid.uuid4())
    response = await call_next(request)
    response.headers["X-Request-ID"] = request.state.request_id
    return response


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
def ready() -> dict[str, str]:
    try:
        with engine.connect() as connection:
            connection.execute(text("SELECT 1"))
            current = connection.scalar(text("SELECT version_num FROM alembic_version"))
            tables = connection.scalar(text("SELECT to_regclass('public.coop') IS NOT NULL"))
            if current is None or not tables:
                raise RuntimeError("schema not migrated")
    except Exception as error:
        raise ApiProblem(
            503, "not_ready", "Service not ready", "Database schema unavailable."
        ) from error
    return {"status": "ready"}


@app.post("/api/v1/organizations", response_model=OrganizationRead, status_code=201)
def post_organization(
    payload: OrganizationCreate, session: Session = Depends(get_session)
) -> OrganizationRead:
    return OrganizationRead.model_validate(create_organization(session, payload))


@app.post("/api/v1/contacts", response_model=ContactRead, status_code=201)
def post_contact(payload: ContactCreate, session: Session = Depends(get_session)) -> ContactRead:
    return ContactRead.model_validate(create_contact(session, payload))


@app.post("/api/v1/coops", response_model=CoopRead, status_code=status.HTTP_201_CREATED)
def post_coop(
    payload: CoopCreate, request: Request, session: Session = Depends(get_session)
) -> CoopRead:
    return CoopRead.model_validate(create_coop(session, payload, request.state.request_id))


@app.get("/api/v1/coops", response_model=CoopPage)
def get_coops(
    scope: Literal["open", "all"] = "open",
    coop_status: CoopStatus | None = Query(default=None, alias="status"),
    contact_id: UUID | None = None,
    organization_id: UUID | None = None,
    release_from: datetime | None = None,
    release_to: datetime | None = None,
    query: str | None = Query(default=None, max_length=200),
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
        release_from=release_from,
        release_to=release_to,
        query=query,
    )
    items, total = list_coops(session, limit=limit, offset=offset, filters=filters)
    next_cursor = encode_cursor(offset + len(items)) if offset + len(items) < total else None
    return {
        "items": [CoopRead.model_validate(item).model_dump(mode="json") for item in items],
        "next_cursor": next_cursor,
        "total": total,
    }


@app.get("/api/v1/coops/{coop_id}", response_model=CoopDetail)
def get_coop(coop_id: UUID, session: Session = Depends(get_session)) -> CoopDetail:
    coop = session.scalar(detail_statement(coop_id))
    if coop is None:
        raise ApiProblem(
            404, "coop_not_found", "Coop not found", "The requested coop does not exist."
        )
    return CoopDetail.model_validate(coop)


@app.patch("/api/v1/coops/{coop_id}", response_model=CoopRead)
def patch_coop(
    coop_id: UUID,
    payload: CoopPatch,
    request: Request,
    session: Session = Depends(get_session),
) -> CoopRead:
    return CoopRead.model_validate(update_coop(session, coop_id, payload, request.state.request_id))


@app.post("/api/v1/coops/{coop_id}/transitions", response_model=CoopRead)
def post_transition(
    coop_id: UUID,
    payload: TransitionRequest,
    request: Request,
    session: Session = Depends(get_session),
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
    coop_id: UUID,
    payload: ReopenRequest,
    request: Request,
    session: Session = Depends(get_session),
) -> CoopRead:
    return CoopRead.model_validate(
        reopen_coop(
            session, coop_id, payload.expected_version, request.state.request_id, payload.reason
        )
    )


@app.get("/api/v1/coops/{coop_id}/audit-events", response_model=AuditEventPage)
def get_audit_events(
    coop_id: UUID,
    limit: int = Query(default=50, ge=1, le=100),
    cursor: str | None = None,
    session: Session = Depends(get_session),
) -> dict[str, Any]:
    if session.get(Coop, coop_id) is None:
        raise ApiProblem(
            404, "coop_not_found", "Coop not found", "The requested coop does not exist."
        )
    offset = decode_cursor(cursor)
    total = (
        session.scalar(
            select(func.count()).select_from(AuditEvent).where(AuditEvent.coop_id == coop_id)
        )
        or 0
    )
    events = list(
        session.scalars(
            select(AuditEvent)
            .where(AuditEvent.coop_id == coop_id)
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
