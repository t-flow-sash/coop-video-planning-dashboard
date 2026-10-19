from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import delete, select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.domain import CoopStatus, normalize_email, normalize_name
from app.models import (
    AuditEvent,
    Contact,
    Coop,
    CoopContact,
    CoopImportState,
    ManualImportBatch,
    Organization,
)
from app.synthetic_seeds import SYNTHETIC_SEED_COOP_IDS

PROVIDER_KEYS = {
    "provider",
    "external_id",
    "gmail_thread_id",
    "gmail_message_id",
    "thread_id",
    "message_id",
    "history_id",
    "source_reference",
}


class ManualImportError(ValueError):
    pass


class AgencyInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_key: str = Field(min_length=3, max_length=160, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    name: str = Field(min_length=1, max_length=200)


class ContactInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_key: str = Field(min_length=3, max_length=160, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    display_name: str = Field(min_length=1, max_length=200)
    email: str = Field(min_length=3, max_length=320)


class RecordInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_key: str = Field(min_length=3, max_length=160, pattern=r"^[a-z0-9][a-z0-9._-]+$")
    record_version: int = Field(ge=1)
    content_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    title: str = Field(min_length=1, max_length=240)
    priority: Literal["P0", "P1", "P2"]
    status: CoopStatus
    last_contact_date: date | None = None
    next_action: str | None = None
    owner: str | None = Field(default=None, max_length=160)
    deadline: date | None = None
    fee_amount_minor: int | None = Field(default=None, ge=0)
    fee_currency: str | None = Field(default=None, pattern=r"^[A-Z]{3}$")
    fee_status: Literal["UNKNOWN", "PROPOSED", "AGREED", "INVOICED", "PAID", "NOT_APPLICABLE"] = (
        "UNKNOWN"
    )
    needs_review: bool = False
    uncertainty: str | None = None
    contact: ContactInput | None = None
    agency: AgencyInput | None = None

    @model_validator(mode="after")
    def validate_semantics(self) -> RecordInput:
        try:
            UUID(self.record_key)
        except ValueError:
            pass
        else:
            raise ValueError("UUID-shaped record_key is reserved for public UUID routes")
        if (self.fee_amount_minor is None) != (self.fee_currency is None):
            raise ValueError("fee fields must be paired")
        if self.needs_review and not (self.uncertainty and self.uncertainty.strip()):
            raise ValueError("uncertainty is required when needs_review is true")
        if self.status is CoopStatus.SCHEDULED:
            raise ValueError("SCHEDULED is not representable by manual import")
        return self


class ManifestInput(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    manifest_id: str = Field(min_length=3, max_length=160)
    manifest_hash: str = Field(pattern=r"^[a-f0-9]{64}$")
    records: list[RecordInput] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_record_keys(self) -> ManifestInput:
        keys = [record.record_key for record in self.records]
        if len(keys) != len(set(keys)):
            raise ValueError("duplicate record_key in manifest")
        return self


def _canonical_hash(value: object) -> str:
    encoded = json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    return hashlib.sha256(encoded.encode()).hexdigest()


def _reject_provider_fields(value: object) -> None:
    if isinstance(value, dict):
        for key, child in value.items():
            if key.casefold() in PROVIDER_KEYS or key.casefold().startswith(
                ("gmail_", "provider_")
            ):
                raise ManualImportError(f"provider field is forbidden: {key}")
            _reject_provider_fields(child)
    elif isinstance(value, list):
        for child in value:
            _reject_provider_fields(child)


def _parse(raw: dict[str, Any]) -> ManifestInput:
    _reject_provider_fields(raw)
    expected_manifest_hash = _canonical_hash(
        {key: value for key, value in raw.items() if key != "manifest_hash"}
    )
    if raw.get("manifest_hash") != expected_manifest_hash:
        raise ManualImportError("manifest_hash does not match canonical content")
    try:
        manifest = ManifestInput.model_validate(raw)
    except ValidationError as error:
        raise ManualImportError(f"invalid versioned manifest: {error}") from error
    for raw_record, record in zip(raw["records"], manifest.records, strict=True):
        expected = _canonical_hash(
            {key: value for key, value in raw_record.items() if key != "content_hash"}
        )
        if record.content_hash != expected:
            raise ManualImportError(f"content_hash mismatch for {record.record_key}")
    return manifest


MANAGED_FIELDS = (
    "title",
    "priority",
    "status",
    "last_contact_date",
    "next_action",
    "owner",
    "deadline",
    "fee_amount_minor",
    "fee_currency",
    "fee_status",
    "needs_review",
    "uncertainty",
)


def _snapshot(coop: Coop) -> str:
    values = {
        field: (value.isoformat() if isinstance(value, date) else value)
        for field in MANAGED_FIELDS
        if (value := getattr(coop, field)) is not None
    }
    values.update(
        {
            "record_key": coop.record_key,
            "agency": (
                {
                    "record_key": coop.agency_organization.record_key,
                    "name": coop.agency_organization.name,
                    "kind": coop.agency_organization.kind,
                }
                if coop.agency_organization
                else None
            ),
            "contacts": sorted(
                (
                    {
                        "record_key": link.contact.record_key,
                        "display_name": link.contact.display_name,
                        "normalized_email": link.contact.normalized_email,
                        "organization_record_key": (
                            link.contact.organization.record_key
                            if link.contact.organization
                            else None
                        ),
                    }
                    for link in coop.contact_links
                ),
                key=lambda contact: str(contact["record_key"]),
            ),
        }
    )
    return _canonical_hash(values)


def _record_values(record: RecordInput) -> dict[str, Any]:
    return {field: getattr(record, field) for field in MANAGED_FIELDS}


def _ensure_agency(session: Session, value: AgencyInput | None) -> Organization | None:
    if value is None:
        return None
    agency = session.scalar(select(Organization).where(Organization.record_key == value.record_key))
    if agency is None:
        agency = Organization(
            record_key=value.record_key,
            name=value.name,
            normalized_name=normalize_name(value.name),
            kind="AGENCY",
        )
        session.add(agency)
        session.flush()
    elif agency.name != value.name or agency.kind != "AGENCY":
        raise ManualImportError(f"agency identity conflict for {value.record_key}")
    return agency


def _ensure_contact(
    session: Session, value: ContactInput | None, agency: Organization | None
) -> Contact | None:
    if value is None:
        return None
    contact = session.scalar(select(Contact).where(Contact.record_key == value.record_key))
    normalized = normalize_email(value.email) if value.email else None
    if contact is None:
        contact = Contact(
            record_key=value.record_key,
            display_name=value.display_name,
            email=value.email,
            normalized_email=normalized,
            organization_id=agency.id if agency else None,
        )
        session.add(contact)
        session.flush()
    elif (
        contact.display_name != value.display_name
        or contact.normalized_email != normalized
        or contact.organization_id != (agency.id if agency else None)
    ):
        raise ManualImportError(f"contact identity conflict for {value.record_key}")
    return contact


def apply_manifest(session: Session, raw: dict[str, Any]) -> dict[str, Any]:
    manifest = _parse(raw)
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('manual-coop-import-v1'))"))
    existing_batch = session.scalar(
        select(ManualImportBatch).where(ManualImportBatch.manifest_hash == manifest.manifest_hash)
    )
    plans: list[tuple[RecordInput, Coop | None, CoopImportState | None, str]] = []
    for record in manifest.records:
        coop = session.scalar(
            select(Coop).where(Coop.record_key == record.record_key).with_for_update()
        )
        if existing_batch is not None and coop is None:
            raise ManualImportError(f"missing managed record for {record.record_key}")
        state = session.get(CoopImportState, coop.id) if coop else None
        action = "create"
        if coop is not None:
            if state is None:
                raise ManualImportError(f"unmanaged record conflict for {record.record_key}")
            if record.record_version < state.record_version:
                raise ManualImportError(f"version conflict for {record.record_key}")
            if record.record_version == state.record_version:
                if record.content_hash != state.content_hash:
                    raise ManualImportError(f"version conflict for {record.record_key}")
                if _snapshot(coop) != state.snapshot_hash:
                    raise ManualImportError(f"local divergence for {record.record_key}")
                action = "unchanged"
            else:
                if _snapshot(coop) != state.snapshot_hash:
                    raise ManualImportError(f"local divergence for {record.record_key}")
                action = "update"
        plans.append((record, coop, state, action))

    synthetic_seeds = list(
        session.scalars(
            select(Coop)
            .where(
                Coop.id.in_(SYNTHETIC_SEED_COOP_IDS),
                Coop.is_synthetic_seed.is_(True),
            )
            .with_for_update()
        )
    )

    if existing_batch is not None:
        return {
            "created": 0,
            "updated": 0,
            "unchanged": len(manifest.records),
            "removed_synthetic_seeds": 0,
            "manifest_hash": manifest.manifest_hash,
        }

    batch = ManualImportBatch(
        id=uuid4(),
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        schema_version=manifest.schema_version,
        summary={},
    )
    session.add(batch)
    seed_ids = [coop.id for coop in synthetic_seeds]
    if seed_ids:
        session.execute(delete(AuditEvent).where(AuditEvent.coop_id.in_(seed_ids)))
        session.execute(delete(CoopContact).where(CoopContact.coop_id.in_(seed_ids)))
        for synthetic_seed in synthetic_seeds:
            session.delete(synthetic_seed)
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    for record, coop, state, action in plans:
        counts[f"{action}d" if action in {"create", "update"} else "unchanged"] += 1
        if action == "unchanged":
            continue
        agency = _ensure_agency(session, record.agency)
        contact = _ensure_contact(session, record.contact, agency)
        old = {} if coop is None else {field: getattr(coop, field) for field in MANAGED_FIELDS}
        created_state = False
        if coop is None:
            coop = Coop(record_key=record.record_key, **_record_values(record))
            coop.agency_organization = agency
            session.add(coop)
            session.flush()
            if contact:
                coop.contact_links.append(CoopContact(contact=contact, role="PRIMARY"))
            created_state = True
        else:
            for field, value in _record_values(record).items():
                setattr(coop, field, value)
            coop.agency_organization = agency
            coop.contact_links.clear()
            if contact:
                coop.contact_links.append(CoopContact(contact=contact, role="PRIMARY"))
            coop.version += 1
        session.flush()
        snapshot_hash = _snapshot(coop)
        if created_state:
            state = CoopImportState(
                coop_id=coop.id,
                record_version=record.record_version,
                content_hash=record.content_hash,
                snapshot_hash=snapshot_hash,
                batch_id=batch.id,
            )
            session.add(state)
        else:
            assert state is not None
            state.record_version = record.record_version
            state.content_hash = record.content_hash
            state.snapshot_hash = snapshot_hash
            state.batch_id = batch.id
        changes = {
            field: {"old": old.get(field), "new": getattr(coop, field)}
            for field in MANAGED_FIELDS
            if old.get(field) != getattr(coop, field)
        }
        session.add(
            AuditEvent(
                coop_id=coop.id,
                action=f"MANUAL_IMPORT_{action.upper()}D",
                actor_type="SYSTEM",
                actor_id="manual-coop-import-v1",
                source="MANUAL_IMPORT",
                changes=jsonable_encoder(changes),
                request_id=f"manual-import-{manifest.manifest_hash[:24]}",
                source_reference=manifest.manifest_hash,
            )
        )
    batch.summary = counts
    session.flush()
    return {
        **counts,
        "removed_synthetic_seeds": len(synthetic_seeds),
        "manifest_hash": manifest.manifest_hash,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Strict reviewed cooperation manifest import")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--expected-manifest-hash", required=True)
    args = parser.parse_args()
    try:
        raw = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, dict):
            raise ManualImportError("manifest root must be an object")
        if raw.get("manifest_hash") != args.expected_manifest_hash:
            raise ManualImportError("expected manifest hash does not match")
        with SessionLocal() as session, session.begin():
            result = apply_manifest(session, raw)
    except (OSError, json.JSONDecodeError, ManualImportError) as error:
        print(json.dumps({"status": "rejected", "error": str(error)}), file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
