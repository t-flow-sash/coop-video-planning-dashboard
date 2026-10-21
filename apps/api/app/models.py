from __future__ import annotations

import uuid
from datetime import date, datetime
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.db import Base
from app.domain import CoopStatus


class TimestampVersionMixin:
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
    version: Mapped[int] = mapped_column(Integer, default=1, server_default="1", nullable=False)


class Organization(TimestampVersionMixin, Base):
    __tablename__ = "organization"
    __table_args__ = (
        CheckConstraint("kind IN ('BRAND','AGENCY','OTHER')", name="ck_organization_kind"),
        Index("ix_organization_normalized_name", "normalized_name"),
        Index(
            "ix_organization_name_trgm",
            "name",
            postgresql_using="gin",
            postgresql_ops={"name": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    name: Mapped[str] = mapped_column(String(200))
    normalized_name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(20))
    website_url: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)


class Contact(TimestampVersionMixin, Base):
    __tablename__ = "contact"
    __table_args__ = (
        CheckConstraint(
            "email IS NOT NULL OR first_name IS NOT NULL OR last_name IS NOT NULL",
            name="ck_contact_identity",
        ),
        Index(
            "uq_contact_normalized_email",
            "normalized_email",
            unique=True,
            postgresql_where=text("normalized_email IS NOT NULL"),
        ),
        Index(
            "ix_contact_display_name_trgm",
            "display_name",
            postgresql_using="gin",
            postgresql_ops={"display_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_contact_first_name_trgm",
            "first_name",
            postgresql_using="gin",
            postgresql_ops={"first_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_contact_last_name_trgm",
            "last_name",
            postgresql_using="gin",
            postgresql_ops={"last_name": "gin_trgm_ops"},
        ),
        Index(
            "ix_contact_email_trgm",
            "email",
            postgresql_using="gin",
            postgresql_ops={"email": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organization.id"))
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    display_name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str | None] = mapped_column(String(320))
    normalized_email: Mapped[str | None] = mapped_column(String(320))
    phone: Mapped[str | None] = mapped_column(String(50))
    notes: Mapped[str | None] = mapped_column(Text)
    organization: Mapped[Organization | None] = relationship()


class Coop(TimestampVersionMixin, Base):
    __tablename__ = "coop"
    __table_args__ = (
        CheckConstraint(
            "status IN (" + ",".join(f"'{status.value}'" for status in CoopStatus) + ")",
            name="ck_coop_status",
        ),
        CheckConstraint("priority IN ('P0','P1','P2')", name="ck_coop_priority"),
        CheckConstraint(
            "fee_status IN ('UNKNOWN','PROPOSED','AGREED','INVOICED','PAID','NOT_APPLICABLE')",
            name="ck_coop_fee_status",
        ),
        CheckConstraint(
            "(fee_amount_minor IS NULL) = (fee_currency IS NULL)", name="ck_coop_fee_pair"
        ),
        CheckConstraint("fee_amount_minor IS NULL OR fee_amount_minor >= 0", name="ck_coop_fee"),
        CheckConstraint(
            "fee_currency IS NULL OR fee_currency ~ '^[A-Z]{3}$'", name="ck_coop_currency"
        ),
        CheckConstraint(
            "status <> 'SCHEDULED' OR release_at IS NOT NULL", name="ck_coop_scheduled_date"
        ),
        CheckConstraint(
            "NOT needs_review OR (uncertainty IS NOT NULL AND btrim(uncertainty) <> '')",
            name="ck_coop_review_uncertainty",
        ),
        Index("ix_coop_status_release_at", "status", "release_at"),
        Index("ix_coop_brand", "brand_organization_id"),
        Index("ix_coop_agency", "agency_organization_id"),
        Index("ix_coop_priority_deadline", "priority", "deadline"),
        Index(
            "ix_coop_title_trgm",
            "title",
            postgresql_using="gin",
            postgresql_ops={"title": "gin_trgm_ops"},
        ),
        Index(
            "ix_coop_summary_trgm",
            "summary",
            postgresql_using="gin",
            postgresql_ops={"summary": "gin_trgm_ops"},
        ),
        Index(
            "ix_coop_owner_trgm",
            "owner",
            postgresql_using="gin",
            postgresql_ops={"owner": "gin_trgm_ops"},
        ),
        Index(
            "ix_coop_next_action_trgm",
            "next_action",
            postgresql_using="gin",
            postgresql_ops={"next_action": "gin_trgm_ops"},
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    record_key: Mapped[str] = mapped_column(String(160), unique=True, nullable=False)
    is_synthetic_seed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default=text("false")
    )
    title: Mapped[str] = mapped_column(String(240))
    priority: Mapped[str] = mapped_column(String(2), default="P2", server_default="P2")
    status: Mapped[str] = mapped_column(String(30), default=CoopStatus.NEW.value)
    brand_organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organization.id"))
    agency_organization_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("organization.id"))
    summary: Mapped[str | None] = mapped_column(Text)
    deliverables: Mapped[list[dict[str, Any]] | None] = mapped_column(JSONB)
    next_action: Mapped[str | None] = mapped_column(Text)
    last_contact_date: Mapped[date | None] = mapped_column(Date)
    owner: Mapped[str | None] = mapped_column(String(160))
    deadline: Mapped[date | None] = mapped_column(Date)
    release_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    fee_amount_minor: Mapped[int | None] = mapped_column(BigInteger)
    fee_currency: Mapped[str | None] = mapped_column(String(3))
    fee_status: Mapped[str] = mapped_column(String(20), default="UNKNOWN", server_default="UNKNOWN")
    needs_review: Mapped[bool] = mapped_column(Boolean, default=True, server_default=text("true"))
    uncertainty: Mapped[str | None] = mapped_column(Text)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    brand_organization: Mapped[Organization | None] = relationship(
        foreign_keys=[brand_organization_id]
    )
    agency_organization: Mapped[Organization | None] = relationship(
        foreign_keys=[agency_organization_id]
    )
    contact_links: Mapped[list[CoopContact]] = relationship(
        back_populates="coop", cascade="all, delete-orphan"
    )


class CoopContact(Base):
    __tablename__ = "coop_contact"
    __table_args__ = (
        CheckConstraint(
            "role IN ('PRIMARY','AGENCY','BRAND','OTHER')", name="ck_coop_contact_role"
        ),
        Index(
            "uq_coop_primary_contact",
            "coop_id",
            unique=True,
            postgresql_where=text("role = 'PRIMARY'"),
        ),
        Index("ix_coop_contact_contact", "contact_id"),
    )

    coop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("coop.id"), primary_key=True)
    contact_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("contact.id"), primary_key=True)
    role: Mapped[str] = mapped_column(String(20), default="OTHER")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    coop: Mapped[Coop] = relationship(back_populates="contact_links")
    contact: Mapped[Contact] = relationship()


class AuditEvent(Base):
    __tablename__ = "audit_event"
    __table_args__ = (
        CheckConstraint("actor_type IN ('USER','AGENT','SYSTEM')", name="ck_audit_actor"),
        CheckConstraint(
            "source IN ('DASHBOARD','INFLUENCER_AGENT','GMAIL_INGESTION',"
            "'MANUAL_IMPORT','SEED','API')",
            name="ck_audit_source",
        ),
        Index("ix_audit_coop_created", "coop_id", text("created_at DESC")),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    coop_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("coop.id"), nullable=False)
    action: Mapped[str] = mapped_column(String(50))
    actor_type: Mapped[str] = mapped_column(String(20))
    actor_id: Mapped[str | None] = mapped_column(String(200))
    source: Mapped[str] = mapped_column(String(30))
    changes: Mapped[dict[str, Any]] = mapped_column(JSONB)
    reason: Mapped[str | None] = mapped_column(Text)
    request_id: Mapped[str] = mapped_column(String(100))
    source_reference: Mapped[str | None] = mapped_column(String(300))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionSource(Base):
    __tablename__ = "ingestion_source"
    __table_args__ = (CheckConstraint("kind IN ('GMAIL')", name="ck_ingestion_source_kind"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_key: Mapped[str] = mapped_column(String(160), unique=True)
    kind: Mapped[str] = mapped_column(String(30))
    enabled: Mapped[bool] = mapped_column(default=True, server_default=text("true"))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class IngestionRun(Base):
    __tablename__ = "ingestion_run"
    __table_args__ = (
        CheckConstraint("mode IN ('FULL','DELTA')", name="ck_ingestion_run_mode"),
        CheckConstraint(
            "status IN ('RUNNING','SUCCEEDED','FAILED')", name="ck_ingestion_run_status"
        ),
        Index(
            "uq_ingestion_run_one_running",
            "source_id",
            unique=True,
            postgresql_where=text("status = 'RUNNING'"),
        ),
        UniqueConstraint("source_id", "id", name="uq_ingestion_run_source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_source.id"))
    mode: Mapped[str] = mapped_column(String(30))
    status: Mapped[str] = mapped_column(String(30))
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    error_code: Mapped[str | None] = mapped_column(String(100))
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class IngestionCheckpoint(Base):
    __tablename__ = "ingestion_checkpoint"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "succeeded_run_id"],
            ["ingestion_run.source_id", "ingestion_run.id"],
            name="fk_ingestion_checkpoint_run",
        ),
    )

    source_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("ingestion_source.id"), primary_key=True
    )
    checkpoint_key: Mapped[str] = mapped_column(String(100), primary_key=True)
    checkpoint_value: Mapped[str] = mapped_column(Text)
    query_hash: Mapped[str] = mapped_column(String(64))
    succeeded_run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceRecord(Base):
    __tablename__ = "source_record"
    __table_args__ = (
        CheckConstraint(
            "record_type IN ('GMAIL_THREAD','GMAIL_MESSAGE')", name="ck_source_record_type"
        ),
        UniqueConstraint(
            "source_id", "record_type", "external_id", name="uq_source_record_external"
        ),
        UniqueConstraint("source_id", "id", name="uq_source_record_source_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_source.id"))
    record_type: Mapped[str] = mapped_column(String(20))
    external_id: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceRecordRevision(Base):
    __tablename__ = "source_record_revision"
    __table_args__ = (
        UniqueConstraint("source_record_id", "content_hash", name="uq_source_record_revision_hash"),
        UniqueConstraint("source_id", "id", name="uq_source_record_revision_source_id"),
        ForeignKeyConstraint(
            ["source_id", "source_record_id"],
            ["source_record.source_id", "source_record.id"],
            name="fk_source_record_revision_source_record",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_source.id"))
    source_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    content_hash: Mapped[str] = mapped_column(String(64))
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class IngestionItem(Base):
    __tablename__ = "ingestion_item"
    __table_args__ = (
        CheckConstraint("status = 'PENDING_REVIEW'", name="ck_ingestion_item_status"),
        UniqueConstraint("run_id", "candidate_key", name="uq_ingestion_item_run_candidate"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    run_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_run.id"))
    candidate_key: Mapped[str] = mapped_column(String(160))
    payload_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(30))
    reason_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class SourceRecordObservation(Base):
    __tablename__ = "source_record_observation"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "run_id"],
            ["ingestion_run.source_id", "ingestion_run.id"],
            name="fk_source_observation_run",
        ),
        ForeignKeyConstraint(
            ["source_id", "source_record_revision_id"],
            ["source_record_revision.source_id", "source_record_revision.id"],
            name="fk_source_observation_revision",
        ),
        UniqueConstraint("run_id", "source_record_revision_id", name="uq_source_observation"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_source.id"))
    run_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_record_revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    observed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now()
    )


class SemanticTrackRoute(Base):
    """Operator-provisioned exact route; never inferred from mailbox similarity."""

    __tablename__ = "semantic_track_route"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "thread_record_id"], ["source_record.source_id", "source_record.id"]
        ),
    )
    thread_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_source.id"))
    track_key: Mapped[str] = mapped_column(String(160), unique=True)
    record_key: Mapped[str] = mapped_column(String(160), unique=True)
    title: Mapped[str] = mapped_column(String(240))
    expected_coop_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("coop.id"))


class SemanticMessageState(Base):
    """Semantic completion independent of ingestion_checkpoint; no MIME stored."""

    __tablename__ = "semantic_message_state"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "source_record_id"], ["source_record.source_id", "source_record.id"]
        ),
        ForeignKeyConstraint(
            ["source_record_id", "revision_id"],
            ["source_record_revision.source_record_id", "source_record_revision.id"],
        ),
        CheckConstraint(
            "outcome IN ('RETRY','REVIEW','CREATED','UPDATED','UNCHANGED')",
            name="ck_semantic_message_outcome",
        ),
    )
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True))
    source_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("ingestion_source.id"))
    outcome: Mapped[str] = mapped_column(String(20))
    reason_code: Mapped[str] = mapped_column(String(60))
    provenance: Mapped[dict[str, Any]] = mapped_column(JSONB, default=dict)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewedDomainBatch(Base):
    __tablename__ = "reviewed_domain_batch"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manifest_id: Mapped[str] = mapped_column(String(160))
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)
    summary: Mapped[dict[str, Any]] = mapped_column(JSONB)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class ReviewedDomainTrack(Base):
    __tablename__ = "reviewed_domain_track"
    __table_args__ = (CheckConstraint("track_revision >= 1", name="ck_reviewed_track_revision"),)

    track_key: Mapped[str] = mapped_column(String(160), primary_key=True)
    coop_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("coop.id", ondelete="RESTRICT"), unique=True
    )
    track_revision: Mapped[int] = mapped_column(Integer)
    decision_hash: Mapped[str] = mapped_column(String(64))
    field_baselines: Mapped[dict[str, Any]] = mapped_column(JSONB)
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("reviewed_domain_batch.id"))


class ReviewedDomainEvidence(Base):
    __tablename__ = "reviewed_domain_evidence"
    __table_args__ = (
        ForeignKeyConstraint(
            ["source_id", "source_record_id"], ["source_record.source_id", "source_record.id"]
        ),
        ForeignKeyConstraint(
            ["source_id", "revision_id"],
            ["source_record_revision.source_id", "source_record_revision.id"],
        ),
        ForeignKeyConstraint(
            ["source_record_id", "revision_id"],
            ["source_record_revision.source_record_id", "source_record_revision.id"],
            name="fk_reviewed_evidence_exact_revision",
        ),
    )

    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reviewed_domain_batch.id"), primary_key=True
    )
    track_key: Mapped[str] = mapped_column(
        ForeignKey("reviewed_domain_track.track_key"), primary_key=True
    )
    source_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    source_record_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    revision_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)


class ReviewedDomainItem(Base):
    __tablename__ = "reviewed_domain_item"

    batch_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("reviewed_domain_batch.id"), primary_key=True
    )
    track_key: Mapped[str] = mapped_column(
        ForeignKey("reviewed_domain_track.track_key"), primary_key=True
    )
    outcome: Mapped[str] = mapped_column(String(20))
    conflict_fields: Mapped[list[str]] = mapped_column(JSONB)


class ManualImportBatch(Base):
    __tablename__ = "manual_import_batch"
    __table_args__ = (
        CheckConstraint("schema_version = 1", name="ck_manual_import_schema_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    manifest_id: Mapped[str] = mapped_column(String(160))
    manifest_hash: Mapped[str] = mapped_column(String(64), unique=True)
    schema_version: Mapped[int] = mapped_column(Integer)
    summary: Mapped[dict[str, Any]] = mapped_column(
        JSONB, default=dict, server_default=text("'{}'::jsonb")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class CoopImportState(Base):
    __tablename__ = "coop_import_state"
    __table_args__ = (CheckConstraint("record_version >= 1", name="ck_coop_import_record_version"),)

    coop_id: Mapped[uuid.UUID] = mapped_column(
        ForeignKey("coop.id", ondelete="CASCADE"), primary_key=True
    )
    record_version: Mapped[int] = mapped_column(Integer)
    content_hash: Mapped[str] = mapped_column(String(64))
    snapshot_hash: Mapped[str] = mapped_column(String(64))
    batch_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("manual_import_batch.id"))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )
