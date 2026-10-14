from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi.encoders import jsonable_encoder
from sqlalchemy import Select, func, or_, select
from sqlalchemy.orm import Session, selectinload

from app.config import get_settings
from app.domain import (
    OPEN_STATUSES,
    REOPEN_TARGETS,
    CoopStatus,
    normalize_email,
    normalize_name,
    validate_transition,
)
from app.errors import ApiProblem
from app.models import AuditEvent, Contact, Coop, CoopContact, Organization
from app.schemas import ContactCreate, CoopCreate, CoopPatch, OrganizationCreate


def _problem_not_found() -> ApiProblem:
    return ApiProblem(404, "coop_not_found", "Coop not found", "The requested coop does not exist.")


def _get_coop(session: Session, coop_id: UUID, *, lock: bool = False) -> Coop:
    statement = select(Coop).where(Coop.id == coop_id)
    if lock:
        statement = statement.with_for_update()
    coop = session.scalar(statement)
    if coop is None:
        raise _problem_not_found()
    return coop


def _audit(
    session: Session,
    coop: Coop,
    action: str,
    changes: dict[str, Any],
    request_id: str,
    *,
    reason: str | None = None,
    actor_type: str = "USER",
    actor_id: str | None = None,
    source: str = "API",
) -> None:
    session.add(
        AuditEvent(
            coop_id=coop.id,
            action=action,
            actor_type=actor_type,
            actor_id=actor_id or get_settings().dev_actor_id,
            source=source,
            changes=jsonable_encoder(changes),
            reason=reason,
            request_id=request_id,
        )
    )


def create_organization(session: Session, data: OrganizationCreate) -> Organization:
    organization = Organization(**data.model_dump(), normalized_name=normalize_name(data.name))
    session.add(organization)
    session.flush()
    return organization


def create_contact(session: Session, data: ContactCreate) -> Contact:
    contact = Contact(
        **data.model_dump(),
        normalized_email=normalize_email(data.email) if data.email else None,
    )
    session.add(contact)
    session.flush()
    return contact


def create_coop(session: Session, data: CoopCreate, request_id: str) -> Coop:
    values = data.model_dump(exclude={"contact_ids"}, mode="json")
    values["status"] = data.status.value
    values["deliverables"] = (
        [item.model_dump(mode="json") for item in data.deliverables]
        if data.deliverables is not None
        else None
    )
    coop = Coop(**values)
    if data.status is CoopStatus.COMPLETED:
        coop.completed_at = datetime.now(UTC)
    session.add(coop)
    session.flush()
    for index, contact_id in enumerate(dict.fromkeys(data.contact_ids)):
        if session.get(Contact, contact_id) is None:
            raise ApiProblem(404, "contact_not_found", "Contact not found", str(contact_id))
        session.add(
            CoopContact(
                coop_id=coop.id,
                contact_id=contact_id,
                role="PRIMARY" if index == 0 else "OTHER",
            )
        )
    _audit(
        session,
        coop,
        "COOP_CREATED",
        {"status": {"old": None, "new": data.status.value}},
        request_id,
    )
    session.flush()
    return coop


def update_coop(session: Session, coop_id: UUID, data: CoopPatch, request_id: str) -> Coop:
    coop = _get_coop(session, coop_id, lock=True)
    if coop.version != data.version:
        raise ApiProblem(409, "stale_version", "Version conflict", "The coop was modified.")
    updates = data.model_dump(exclude={"version"}, exclude_unset=True, mode="json")
    if "deliverables" in updates and data.deliverables is not None:
        updates["deliverables"] = [item.model_dump(mode="json") for item in data.deliverables]
    candidate_amount = updates.get("fee_amount_minor", coop.fee_amount_minor)
    candidate_currency = updates.get("fee_currency", coop.fee_currency)
    if (candidate_amount is None) != (candidate_currency is None):
        raise ApiProblem(422, "validation_error", "Validation error", "Fee fields must be paired.")
    if candidate_currency is not None:
        candidate_currency = str(candidate_currency).upper()
        updates["fee_currency"] = candidate_currency
    if (
        coop.status == CoopStatus.SCHEDULED.value
        and updates.get("release_at", coop.release_at) is None
    ):
        raise ApiProblem(
            409,
            "invalid_status_transition",
            "Invalid coop state",
            "A scheduled coop requires release_at.",
        )
    changes: dict[str, Any] = {}
    for field, new_value in updates.items():
        old_value = getattr(coop, field)
        if jsonable_encoder(old_value) != jsonable_encoder(new_value):
            changes[field] = {"old": old_value, "new": new_value}
            setattr(coop, field, new_value)
    if changes:
        coop.version += 1
        coop.updated_at = datetime.now(UTC)
        _audit(session, coop, "COOP_UPDATED", changes, request_id)
        session.flush()
    return coop


def transition_coop(
    session: Session,
    coop_id: UUID,
    target: CoopStatus,
    expected_version: int,
    request_id: str,
    reason: str | None,
) -> Coop:
    coop = _get_coop(session, coop_id, lock=True)
    if coop.version != expected_version:
        raise ApiProblem(409, "stale_version", "Version conflict", "The coop was modified.")
    current = CoopStatus(coop.status)
    try:
        validate_transition(current, target, release_at=coop.release_at)
    except ValueError as error:
        raise ApiProblem(
            409, "invalid_status_transition", "Invalid status transition", str(error)
        ) from error
    changes: dict[str, Any] = {"status": {"old": current.value, "new": target.value}}
    coop.status = target.value
    if target is CoopStatus.COMPLETED:
        coop.completed_at = datetime.now(UTC)
        changes["completed_at"] = {"old": None, "new": coop.completed_at}
    coop.version += 1
    coop.updated_at = datetime.now(UTC)
    _audit(session, coop, "STATUS_TRANSITIONED", changes, request_id, reason=reason)
    session.flush()
    return coop


def reopen_coop(
    session: Session, coop_id: UUID, expected_version: int, request_id: str, reason: str
) -> Coop:
    coop = _get_coop(session, coop_id, lock=True)
    if coop.version != expected_version:
        raise ApiProblem(409, "stale_version", "Version conflict", "The coop was modified.")
    current = CoopStatus(coop.status)
    target = REOPEN_TARGETS.get(current)
    if target is None:
        raise ApiProblem(
            409,
            "invalid_status_transition",
            "Invalid status transition",
            "Only terminal coops can be reopened.",
        )
    changes: dict[str, Any] = {"status": {"old": current.value, "new": target.value}}
    coop.status = target.value
    if current is CoopStatus.COMPLETED:
        changes["completed_at"] = {"old": coop.completed_at, "new": None}
        coop.completed_at = None
    coop.version += 1
    coop.updated_at = datetime.now(UTC)
    _audit(session, coop, "STATUS_REOPENED", changes, request_id, reason=reason.strip())
    session.flush()
    return coop


def detail_statement(coop_id: UUID) -> Select[Any]:
    return (
        select(Coop)
        .where(Coop.id == coop_id)
        .options(
            selectinload(Coop.brand_organization),
            selectinload(Coop.agency_organization),
            selectinload(Coop.contact_links).selectinload(CoopContact.contact),
        )
    )


def encode_cursor(offset: int) -> str:
    return base64.urlsafe_b64encode(json.dumps({"offset": offset}).encode()).decode().rstrip("=")


def decode_cursor(cursor: str | None) -> int:
    if cursor is None:
        return 0
    try:
        padded = cursor + "=" * (-len(cursor) % 4)
        value = json.loads(base64.urlsafe_b64decode(padded).decode())
        offset = int(value["offset"])
        if offset < 0:
            raise ValueError
        return offset
    except (ValueError, TypeError, KeyError, json.JSONDecodeError) as error:
        raise ApiProblem(
            422, "invalid_cursor", "Invalid cursor", "The cursor is malformed."
        ) from error


def coop_filters(
    *,
    scope: str,
    status: CoopStatus | None,
    contact_id: UUID | None,
    organization_id: UUID | None,
    release_from: datetime | None,
    release_to: datetime | None,
    query: str | None,
) -> list[Any]:
    filters: list[Any] = []
    if status is not None:
        filters.append(Coop.status == status.value)
    elif scope == "open":
        filters.append(Coop.status.in_([item.value for item in OPEN_STATUSES]))
    if contact_id is not None:
        filters.append(Coop.contact_links.any(CoopContact.contact_id == contact_id))
    if organization_id is not None:
        filters.append(
            or_(
                Coop.brand_organization_id == organization_id,
                Coop.agency_organization_id == organization_id,
            )
        )
    if release_from is not None:
        filters.append(Coop.release_at >= release_from)
    if release_to is not None:
        filters.append(Coop.release_at <= release_to)
    if query:
        pattern = f"%{query.strip()}%"
        filters.append(or_(Coop.title.ilike(pattern), Coop.summary.ilike(pattern)))
    return filters


def list_coops(
    session: Session, *, limit: int, offset: int, filters: list[Any]
) -> tuple[list[Coop], int]:
    total = session.scalar(select(func.count()).select_from(Coop).where(*filters)) or 0
    statement = (
        select(Coop)
        .where(*filters)
        .order_by(Coop.release_at.asc().nulls_last(), Coop.updated_at.desc(), Coop.id.asc())
        .offset(offset)
        .limit(limit)
    )
    return list(session.scalars(statement)), total
