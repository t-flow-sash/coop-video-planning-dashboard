from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain import CoopStatus, Deliverable, MoneyFields


class OrganizationCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str = Field(min_length=1, max_length=200)
    kind: Literal["BRAND", "AGENCY", "OTHER"] = "OTHER"
    website_url: str | None = Field(default=None, max_length=500)
    notes: str | None = None


class OrganizationRead(OrganizationCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    version: int
    created_at: datetime
    updated_at: datetime


class ContactCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    organization_id: UUID | None = None
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


class ContactRead(ContactCreate):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    normalized_email: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class ContactLinkRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    role: str
    contact: ContactRead


class CoopFields(MoneyFields):
    title: str = Field(min_length=1, max_length=240)
    summary: str | None = None
    deliverables: list[Deliverable] | None = None
    next_action: str | None = None
    release_at: datetime | None = None
    brand_organization_id: UUID | None = None
    agency_organization_id: UUID | None = None


class CoopCreate(CoopFields):
    model_config = ConfigDict(extra="forbid")
    status: CoopStatus = CoopStatus.NEW
    contact_ids: list[UUID] = Field(default_factory=list)

    @model_validator(mode="after")
    def scheduled_has_date(self) -> CoopCreate:
        if self.status is CoopStatus.SCHEDULED and self.release_at is None:
            raise ValueError("release_at is required for SCHEDULED")
        return self


class CoopPatch(MoneyFields):
    model_config = ConfigDict(extra="forbid")
    version: int = Field(ge=1)
    title: str | None = Field(default=None, min_length=1, max_length=240)
    summary: str | None = None
    deliverables: list[Deliverable] | None = None
    next_action: str | None = None
    release_at: datetime | None = None
    brand_organization_id: UUID | None = None
    agency_organization_id: UUID | None = None


class CoopRead(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    title: str
    status: CoopStatus
    summary: str | None
    deliverables: list[dict[str, Any]] | None
    next_action: str | None
    release_at: datetime | None
    fee_amount_minor: int | None
    fee_currency: str | None
    completed_at: datetime | None
    brand_organization_id: UUID | None
    agency_organization_id: UUID | None
    version: int
    created_at: datetime
    updated_at: datetime


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
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    coop_id: UUID
    action: str
    actor_type: str
    actor_id: str | None
    source: str
    changes: dict[str, Any]
    reason: str | None
    request_id: str
    source_reference: str | None
    created_at: datetime


class CoopPage(BaseModel):
    items: list[CoopRead]
    next_cursor: str | None
    total: int


class AuditEventPage(BaseModel):
    items: list[AuditEventRead]
    next_cursor: str | None
    total: int
