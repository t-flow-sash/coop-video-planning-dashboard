from __future__ import annotations

from datetime import UTC, date, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, computed_field, field_validator, model_validator

from app.domain import CoopStatus, Deliverable, MoneyFields
from app.redaction import redact_internal_references

RecordKey = str
Priority = Literal["P0", "P1", "P2"]
FeeStatus = Literal["UNKNOWN", "PROPOSED", "AGREED", "INVOICED", "PAID", "NOT_APPLICABLE"]
Urgency = Literal["OVERDUE", "ACUTE", "UPCOMING", "NONE"]


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_key: RecordKey | None = Field(
        default=None, min_length=3, max_length=160, pattern=r"^[a-z0-9][a-z0-9._-]+$"
    )
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["BRAND", "AGENCY", "OTHER"] = "OTHER"
    website_url: str | None = Field(default=None, max_length=500)
    notes: str | None = None


class OrganizationRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    record_key: RecordKey
    name: str
    kind: Literal["BRAND", "AGENCY", "OTHER"]
    website_url: str | None
    notes: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    record_key: RecordKey | None = Field(
        default=None, min_length=3, max_length=160, pattern=r"^[a-z0-9][a-z0-9._-]+$"
    )
    organization_id: UUID | None = None
    organization_key: RecordKey | None = Field(default=None, max_length=160)
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    display_name: str = Field(min_length=1, max_length=200)
    email: str | None = Field(default=None, max_length=320)
    phone: str | None = Field(default=None, max_length=50)
    notes: str | None = None

    @model_validator(mode="after")
    def identity_present(self) -> ContactCreate:
        if not (self.email or self.first_name or self.last_name):
            raise ValueError("at least email or a name is required")
        return self


class ContactRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    record_key: RecordKey
    organization_id: UUID | None
    first_name: str | None
    last_name: str | None
    display_name: str
    email: str | None
    normalized_email: str | None
    phone: str | None
    notes: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ContactLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    role: str
    contact: ContactRead


class CoopFields(MoneyFields):
    record_key: RecordKey | None = Field(
        default=None, min_length=3, max_length=160, pattern=r"^[a-z0-9][a-z0-9._-]+$"
    )
    title: str = Field(min_length=1, max_length=240)
    priority: Priority = "P2"
    summary: str | None = None
    deliverables: list[Deliverable] | None = None
    last_contact_date: date | None = None
    next_action: str | None = None
    owner: str | None = Field(default=None, max_length=160)
    deadline: date | None = None
    release_at: datetime | None = None
    fee_status: FeeStatus = "UNKNOWN"
    needs_review: bool = True
    uncertainty: str | None = None
    brand_organization_id: UUID | None = None
    agency_organization_id: UUID | None = None
    brand_key: RecordKey | None = Field(default=None, max_length=160)
    agency_key: RecordKey | None = Field(default=None, max_length=160)

    @field_validator("record_key")
    @classmethod
    def reject_uuid_shaped_record_key(cls, value: RecordKey | None) -> RecordKey | None:
        if value is None:
            return value
        try:
            UUID(value)
        except ValueError:
            return value
        raise ValueError("UUID-shaped record_key is reserved for public UUID routes")


class CoopCreate(CoopFields):
    model_config = ConfigDict(extra="forbid")
    status: CoopStatus = CoopStatus.NEW
    contact_ids: list[UUID] = Field(default_factory=list)
    contact_keys: list[RecordKey] = Field(default_factory=list)

    @model_validator(mode="after")
    def validate_state(self) -> CoopCreate:
        if self.status is CoopStatus.SCHEDULED and self.release_at is None:
            raise ValueError("release_at is required for SCHEDULED")
        if self.needs_review and not (self.uncertainty and self.uncertainty.strip()):
            raise ValueError("uncertainty is required when needs_review is true")
        return self


class CoopPatch(MoneyFields):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    priority: Priority | None = None
    summary: str | None = None
    deliverables: list[Deliverable] | None = None
    last_contact_date: date | None = None
    next_action: str | None = None
    owner: str | None = Field(default=None, max_length=160)
    deadline: date | None = None
    release_at: datetime | None = None
    fee_status: FeeStatus | None = None
    needs_review: bool | None = None
    uncertainty: str | None = None
    brand_organization_id: UUID | None = None
    agency_organization_id: UUID | None = None
    brand_key: RecordKey | None = Field(default=None, max_length=160)
    agency_key: RecordKey | None = Field(default=None, max_length=160)

    @model_validator(mode="after")
    def validate_review_state(self) -> CoopPatch:
        if self.needs_review is True and not (self.uncertainty and self.uncertainty.strip()):
            raise ValueError("uncertainty is required when needs_review is true")
        return self


class CoopRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    record_key: RecordKey
    title: str
    priority: Priority
    status: CoopStatus
    summary: str | None
    deliverables: list[dict[str, Any]] | None
    last_contact_date: date | None
    next_action: str | None
    owner: str | None
    deadline: date | None
    release_at: datetime | None
    fee_amount_minor: int | None
    fee_currency: str | None
    fee_status: FeeStatus
    needs_review: bool
    uncertainty: str | None
    completed_at: datetime | None
    brand_organization_id: UUID | None
    agency_organization_id: UUID | None
    version: int
    created_at: datetime
    updated_at: datetime

    @computed_field  # type: ignore[prop-decorator]
    @property
    def urgency(self) -> Urgency:
        if self.deadline is None:
            return "NONE"
        today = datetime.now(UTC).date()
        if self.deadline < today:
            return "OVERDUE"
        if self.deadline <= today + timedelta(days=2):
            return "ACUTE"
        return "UPCOMING"


class CoopDetail(CoopRead):
    brand_organization: OrganizationRead | None
    agency_organization: OrganizationRead | None
    contacts: list[ContactLinkRead] = Field(validation_alias="contact_links")


class TransitionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    target_status: CoopStatus
    expected_version: int = Field(ge=1)
    reason: str | None = None


class ReopenRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    expected_version: int = Field(ge=1)
    reason: str = Field(min_length=1)

    @model_validator(mode="after")
    def non_blank_reason(self) -> ReopenRequest:
        if not self.reason.strip():
            raise ValueError("reason must not be blank")
        return self


class AuditEventRead(BaseModel):
    """Public audit projection; internal correlation and source references stay private."""

    model_config = ConfigDict(from_attributes=True)
    id: UUID
    coop_id: UUID
    action: str
    actor_type: str
    actor_id: str | None
    source: str
    changes: dict[str, Any]
    reason: str | None
    created_at: datetime

    @field_validator("changes", mode="before")
    @classmethod
    def redact_changes(cls, value: Any) -> dict[str, Any]:
        return redact_internal_references(value, drop_internal_keys=True)

    @field_validator("reason", mode="before")
    @classmethod
    def redact_reason(cls, value: Any) -> str | None:
        return redact_internal_references(value, drop_internal_keys=True)


class CoopPage(BaseModel):
    # The operational overview needs linked display data. Returning the enriched
    # projection per page avoids one detail request per cooperation in the web UI.
    items: list[CoopDetail]
    next_cursor: str | None
    total: int


class AuditEventPage(BaseModel):
    items: list[AuditEventRead]
    next_cursor: str | None
    total: int
