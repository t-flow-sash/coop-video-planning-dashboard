from __future__ import annotations

import base64
import json
from datetime import UTC, date, datetime
from typing import Any
from uuid import UUID, uuid4

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


def _get_coop(session: Session, identifier: str | UUID, *, lock: bool = False) -> Coop:
    try:
        coop_id = UUID(str(identifier))
    except ValueError:
        coop_id = None
    criterion = (
        or_(Coop.id == coop_id, Coop.record_key == str(identifier))
        if coop_id is not None
        else Coop.record_key == str(identifier)
    )
    statement = select(Coop).where(criterion)
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
    values = data.model_dump()
    values["record_key"] = values["record_key"] or f"org-{uuid4().hex}"
    organization = Organization(**values, normalized_name=normalize_name(data.name))
    session.add(organization)
    session.flush()
    return organization


def create_contact(session: Session, data: ContactCreate) -> Contact:
    values = data.model_dump(exclude={"organization_id", "organization_key"})
    values["record_key"] = values["record_key"] or f"contact-{uuid4().hex}"
    organization_id = data.organization_id
    if data.organization_key:
        organization = session.scalar(
            select(Organization).where(Organization.record_key == data.organization_key)
        )
        if organization is None:
            raise ApiProblem(
                404, "organization_not_found", "Organization not found", data.organization_key
            )
        organization_id = organization.id
    contact = Contact(
        **values,
        organization_id=organization_id,
        normalized_email=normalize_email(data.email) if data.email else None,
    )
    session.add(contact)
    session.flush()
    return contact


def create_coop(session: Session, data: CoopCreate, request_id: str) -> Coop:
    values = data.model_dump(
        exclude={"contact_ids", "contact_keys", "brand_key", "agency_key"}, mode="json"
    )
    values["record_key"] = values["record_key"] or f"coop-{uuid4().hex}"
    values["status"] = data.status.value
    values["deliverables"] = (
        [item.model_dump(mode="json") for item in data.deliverables]
        if data.deliverables is not None
        else None
    )
    for key_field, id_field in (
        ("brand_key", "brand_organization_id"),
        ("agency_key", "agency_organization_id"),
    ):
        record_key = getattr(data, key_field)
        if record_key:
            organization = session.scalar(
                select(Organization).where(Organization.record_key == record_key)
            )
            if organization is None:
                raise ApiProblem(
                    404, "organization_not_found", "Organization not found", record_key
                )
            values[id_field] = organization.id
    coop = Coop(**values)
    if data.status is CoopStatus.COMPLETED:
        coop.completed_at = datetime.now(UTC)
    session.add(coop)
    session.flush()
    contact_identifiers: list[str | UUID] = [*data.contact_ids, *data.contact_keys]
    for index, contact_identifier in enumerate(dict.fromkeys(contact_identifiers)):
        contact = (
            session.get(Contact, contact_identifier)
            if isinstance(contact_identifier, UUID)
            else session.scalar(select(Contact).where(Contact.record_key == contact_identifier))
        )
        if contact is None:
            raise ApiProblem(404, "contact_not_found", "Contact not found", str(contact_identifier))
        session.add(
            CoopContact(
                coop_id=coop.id,
                contact_id=contact.id,
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


def update_coop(session: Session, record_key: str, data: CoopPatch, request_id: str) -> Coop:
    coop = _get_coop(session, record_key, lock=True)
    if coop.version != data.version:
        raise ApiProblem(409, "stale_version", "Version conflict", "The coop was modified.")
    updates = data.model_dump(
        exclude={"version", "brand_key", "agency_key"}, exclude_unset=True, mode="json"
    )
    for key_field, id_field in (
        ("brand_key", "brand_organization_id"),
        ("agency_key", "agency_organization_id"),
    ):
        if key_field not in data.model_fields_set:
            continue
        value = getattr(data, key_field)
        if value is None:
            updates[id_field] = None
            continue
        organization = session.scalar(select(Organization).where(Organization.record_key == value))
        if organization is None:
            raise ApiProblem(404, "organization_not_found", "Organization not found", value)
        updates[id_field] = organization.id
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
    candidate_needs_review = updates.get("needs_review", coop.needs_review)
    candidate_uncertainty = updates.get("uncertainty", coop.uncertainty)
    if candidate_needs_review and not (
        isinstance(candidate_uncertainty, str) and candidate_uncertainty.strip()
    ):
        raise ApiProblem(
            422,
            "validation_error",
            "Validation error",
            "Uncertainty is required when needs_review is true.",
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
    record_key: str,
    target: CoopStatus,
    expected_version: int,
    request_id: str,
    reason: str | None,
) -> Coop:
    coop = _get_coop(session, record_key, lock=True)
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
    session: Session, record_key: str, expected_version: int, request_id: str, reason: str
) -> Coop:
    coop = _get_coop(session, record_key, lock=True)
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


def detail_statement(identifier: str | UUID) -> Select[Any]:
    try:
        coop_id = UUID(str(identifier))
    except ValueError:
        coop_id = None
    criterion = (
        or_(Coop.id == coop_id, Coop.record_key == str(identifier))
        if coop_id is not None
        else Coop.record_key == str(identifier)
    )
    return (
        select(Coop)
        .where(criterion)
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
    contact_key: str | None,
    organization_key: str | None,
    release_from: datetime | None,
    release_to: datetime | None,
    query: str | None,
    priority: str | None = None,
    needs_review: bool | None = None,
    deadline_to: date | None = None,
) -> list[Any]:
    filters: list[Any] = []
    if status is not None:
        filters.append(Coop.status == status.value)
    elif scope == "open":
        filters.append(Coop.status.in_([item.value for item in OPEN_STATUSES]))
    if contact_id is not None:
        filters.append(Coop.contact_links.any(CoopContact.contact_id == contact_id))
    elif contact_key is not None:
        filters.append(
            Coop.contact_links.any(CoopContact.contact.has(Contact.record_key == contact_key))
        )
    if organization_id is not None:
        filters.append(
            or_(
                Coop.brand_organization_id == organization_id,
                Coop.agency_organization_id == organization_id,
            )
        )
    elif organization_key is not None:
        organization_ids = select(Organization.id).where(
            Organization.record_key == organization_key
        )
        filters.append(
            or_(
                Coop.brand_organization_id.in_(organization_ids),
                Coop.agency_organization_id.in_(organization_ids),
            )
        )
    if release_from is not None:
        filters.append(Coop.release_at >= release_from)
    if release_to is not None:
        filters.append(Coop.release_at <= release_to)
    if query and (search_term := query.strip()):
        pattern = f"%{search_term}%"
        organization_name_matches = select(Organization.id).where(Organization.name.ilike(pattern))
        contact_matches = Coop.contact_links.any(
            CoopContact.contact.has(
                or_(
                    Contact.display_name.ilike(pattern),
                    Contact.first_name.ilike(pattern),
                    Contact.last_name.ilike(pattern),
                    Contact.email.ilike(pattern),
                    Contact.organization.has(Organization.name.ilike(pattern)),
                )
            )
        )
        filters.append(
            or_(
                Coop.title.ilike(pattern),
                Coop.summary.ilike(pattern),
                Coop.owner.ilike(pattern),
                Coop.next_action.ilike(pattern),
                Coop.brand_organization_id.in_(organization_name_matches),
                Coop.agency_organization_id.in_(organization_name_matches),
                contact_matches,
            )
        )
    if priority is not None:
        filters.append(Coop.priority == priority)
    if needs_review is not None:
        filters.append(Coop.needs_review.is_(needs_review))
    if deadline_to is not None:
        filters.append(Coop.deadline <= deadline_to)
    return filters


def list_coops(
    session: Session, *, limit: int, offset: int, filters: list[Any]
) -> tuple[list[Coop], int]:
    total = session.scalar(select(func.count()).select_from(Coop).where(*filters)) or 0
    statement = (
        select(Coop)
        .where(*filters)
        .options(
            selectinload(Coop.brand_organization),
            selectinload(Coop.agency_organization),
            selectinload(Coop.contact_links).selectinload(CoopContact.contact),
        )
        .order_by(
            Coop.priority.asc(),
            Coop.deadline.asc().nulls_last(),
            Coop.updated_at.desc(),
            Coop.id.asc(),
        )
        .offset(offset)
        .limit(limit)
    )
    return list(session.scalars(statement)), total
