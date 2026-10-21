"""Explicit, CLI-only reviewed evidence → business-track materialization.

The Gmail collector cannot call this entry point or access these tables. The caller
owns the Session transaction; use SessionLocal.begin() for all-or-nothing apply.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import date, datetime
from pathlib import Path
from typing import Any, Literal
from uuid import UUID, uuid4

from fastapi.encoders import jsonable_encoder
from pydantic import BaseModel, ConfigDict, Field, ValidationError, model_validator
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.domain import CoopStatus
from app.models import (
    AuditEvent,
    Coop,
    CoopImportState,
    ReviewedDomainBatch,
    ReviewedDomainEvidence,
    ReviewedDomainItem,
    ReviewedDomainTrack,
    SourceRecordRevision,
)


class ReviewError(ValueError):
    """Sanitized review rejection; never include evidence payloads in messages."""


KEY = r"^[a-z0-9][a-z0-9._-]+$"
HASH = r"^[a-f0-9]{64}$"
FIELDS = frozenset(
    {
        "title",
        "priority",
        "status",
        "summary",
        "deliverables",
        "next_action",
        "last_contact_date",
        "owner",
        "deadline",
        "release_at",
        "fee_amount_minor",
        "fee_currency",
        "fee_status",
        "needs_review",
        "uncertainty",
        "completed_at",
    }
)


class Evidence(BaseModel):
    model_config = ConfigDict(extra="forbid")
    source_id: UUID
    source_record_id: UUID
    revision_id: UUID


class Decision(BaseModel):
    model_config = ConfigDict(extra="forbid", use_enum_values=True)
    track_key: str = Field(min_length=3, max_length=160, pattern=KEY)
    track_revision: int = Field(ge=1)
    decision_hash: str = Field(pattern=HASH)
    adjudication: Literal["ACCEPTED", "EXCLUDED"]
    action: Literal["CREATE", "UPDATE", "EXCLUDE"]
    record_key: str | None = Field(default=None, min_length=3, max_length=160, pattern=KEY)
    evidence: list[Evidence] = Field(min_length=1, max_length=100)
    fields: dict[str, Any] = Field(default_factory=dict)
    # Required on initial adoption of an existing, previously unlinked row.
    expected_version: int | None = Field(default=None, ge=1)
    expected_fields: dict[str, Any] | None = None

    @model_validator(mode="after")
    def validate_decision(self) -> Decision:
        if len({(e.source_id, e.source_record_id, e.revision_id) for e in self.evidence}) != len(
            self.evidence
        ):
            raise ValueError("duplicate evidence reference")
        if self.adjudication == "EXCLUDED":
            if self.action != "EXCLUDE" or self.record_key or self.fields or self.expected_fields:
                raise ValueError("excluded track cannot change business fields")
            return self
        if self.action == "EXCLUDE" or self.record_key is None or not self.fields:
            raise ValueError("accepted track requires action, record_key and fields")
        try:
            UUID(self.record_key)
        except ValueError:
            pass
        else:
            raise ValueError("UUID-shaped record_key is reserved")
        if set(self.fields) - FIELDS or (
            self.expected_fields is not None and set(self.expected_fields) != set(self.fields)
        ):
            raise ValueError("unknown fields or incomplete expected_fields")
        if self.action == "CREATE" and (
            self.expected_version is not None or self.expected_fields is not None
        ):
            raise ValueError("CREATE cannot adopt a target")
        if self.action == "CREATE" and self.track_revision != 1:
            raise ValueError("CREATE is only valid on first track revision")
        if (
            self.action == "UPDATE"
            and self.track_revision == 1
            and (self.expected_version is None or self.expected_fields is None)
        ):
            raise ValueError("initial UPDATE requires exact target snapshot")
        return self


class Manifest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    schema_version: Literal[1]
    manifest_id: str = Field(min_length=3, max_length=160)
    manifest_hash: str = Field(pattern=HASH)
    decisions: list[Decision] = Field(min_length=1, max_length=1000)

    @model_validator(mode="after")
    def unique_tracks(self) -> Manifest:
        tracks = [d.track_key for d in self.decisions]
        keys = [d.record_key for d in self.decisions if d.record_key is not None]
        if len(set(tracks)) != len(tracks) or len(set(keys)) != len(keys):
            raise ValueError("duplicate track or record_key")
        return self


def canonical_hash(value: object) -> str:
    return hashlib.sha256(
        json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode()
    ).hexdigest()


def parse_manifest(raw: dict[str, Any]) -> Manifest:
    if raw.get("manifest_hash") != canonical_hash(
        {k: v for k, v in raw.items() if k != "manifest_hash"}
    ):
        raise ReviewError("manifest hash mismatch")
    try:
        manifest = Manifest.model_validate(raw)
    except ValidationError as error:
        raise ReviewError("invalid reviewed manifest") from error
    for item, decision in zip(raw["decisions"], manifest.decisions, strict=True):
        if decision.decision_hash != canonical_hash(
            {k: v for k, v in item.items() if k != "decision_hash"}
        ):
            raise ReviewError(f"decision hash mismatch for {decision.track_key}")
        if decision.adjudication == "ACCEPTED":
            _validated_fields(decision.fields)
            if decision.expected_fields is not None:
                _validated_fields(decision.expected_fields)
    return manifest


def _validated_fields(fields: dict[str, Any]) -> dict[str, Any]:
    """Parse only supported business types; no unknown/implicit semantic defaults."""
    result: dict[str, Any] = {}
    for key, value in fields.items():
        if key not in FIELDS:
            raise ReviewError("unsupported field")
        if key in {"last_contact_date", "deadline"} and value is not None:
            if not isinstance(value, str):
                raise ReviewError("invalid date")
            try:
                result[key] = date.fromisoformat(value)
            except ValueError as error:
                raise ReviewError("invalid date") from error
        elif key in {"release_at", "completed_at"} and value is not None:
            if not isinstance(value, str):
                raise ReviewError("invalid datetime")
            try:
                parsed = datetime.fromisoformat(value)
            except ValueError as error:
                raise ReviewError("invalid datetime") from error
            if parsed.tzinfo is None:
                raise ReviewError("datetime must include timezone")
            result[key] = parsed
        elif key == "status":
            if value not in {s.value for s in CoopStatus}:
                raise ReviewError("invalid status")
            result[key] = value
        elif key == "priority":
            if value not in {"P0", "P1", "P2"}:
                raise ReviewError("invalid priority")
            result[key] = value
        elif key == "fee_status":
            if value not in {"UNKNOWN", "PROPOSED", "AGREED", "INVOICED", "PAID", "NOT_APPLICABLE"}:
                raise ReviewError("invalid fee status")
            result[key] = value
        elif key == "fee_amount_minor":
            if value is not None and (type(value) is not int or value < 0):
                raise ReviewError("invalid fee")
            result[key] = value
        elif key == "fee_currency":
            if value is not None and (
                not isinstance(value, str)
                or len(value) != 3
                or not value.isascii()
                or not value.isupper()
                or not value.isalpha()
            ):
                raise ReviewError("invalid currency")
            result[key] = value
        elif key == "needs_review":
            if type(value) is not bool:
                raise ReviewError("invalid review state")
            result[key] = value
        elif key == "deliverables":
            if value is not None and (
                not isinstance(value, list) or any(not isinstance(x, dict) for x in value)
            ):
                raise ReviewError("invalid deliverables")
            result[key] = value
        else:
            limits = {"title": 240, "owner": 160}
            if value is not None and (
                not isinstance(value, str)
                or len(value) > limits.get(key, 10000)
                or (key == "title" and not value.strip())
            ):
                raise ReviewError("invalid text field")
            result[key] = value
    return result


def _state_valid(coop: Coop, changes: dict[str, Any]) -> None:
    candidate = {k: changes.get(k, getattr(coop, k)) for k in FIELDS}
    if not candidate["title"] or not str(candidate["title"]).strip():
        raise ReviewError("title required")
    if (candidate["fee_amount_minor"] is None) != (candidate["fee_currency"] is None):
        raise ReviewError("fee fields must be paired")
    if candidate["needs_review"] and not (
        candidate["uncertainty"] and str(candidate["uncertainty"]).strip()
    ):
        raise ReviewError("review uncertainty required")
    if candidate["status"] == "SCHEDULED" and candidate["release_at"] is None:
        raise ReviewError("scheduled release date required")


def _encoded(value: Any) -> Any:
    return jsonable_encoder(value)


def apply_manifest(session: Session, raw: dict[str, Any]) -> dict[str, Any]:
    """Apply one manifest inside the caller's transaction; exceptions require rollback."""
    manifest = parse_manifest(raw)
    session.execute(text("SELECT pg_advisory_xact_lock(hashtext('reviewed-domain-v1'))"))
    previous = session.scalar(
        select(ReviewedDomainBatch).where(
            ReviewedDomainBatch.manifest_hash == manifest.manifest_hash
        )
    )
    if previous and previous.manifest_id != manifest.manifest_id:
        raise ReviewError("manifest identity conflict")
    if session.scalar(
        select(ReviewedDomainBatch.id).where(
            ReviewedDomainBatch.manifest_id == manifest.manifest_id,
            ReviewedDomainBatch.manifest_hash != manifest.manifest_hash,
        )
    ):
        raise ReviewError("manifest id reused with different content")
    batch = previous or ReviewedDomainBatch(
        id=uuid4(),
        manifest_id=manifest.manifest_id,
        manifest_hash=manifest.manifest_hash,
        summary={},
    )
    if previous is None:
        session.add(batch)
    counts = {"created": 0, "updated": 0, "excluded": 0, "conflicted": 0, "unchanged": 0}
    for decision in manifest.decisions:
        # Validate exact DB linkage and evidence presence even on replay. One thread
        # may support multiple tracks; one track can cite multiple threads.
        for evidence in decision.evidence:
            revision = session.scalar(
                select(SourceRecordRevision).where(
                    SourceRecordRevision.id == evidence.revision_id,
                    SourceRecordRevision.source_id == evidence.source_id,
                    SourceRecordRevision.source_record_id == evidence.source_record_id,
                )
            )
            if revision is None:
                raise ReviewError("evidence revision not found for source record")
        link = session.scalar(
            select(ReviewedDomainTrack)
            .where(ReviewedDomainTrack.track_key == decision.track_key)
            .with_for_update()
        )
        if link is not None:
            if decision.track_revision < link.track_revision or (
                decision.track_revision == link.track_revision
                and decision.decision_hash != link.decision_hash
            ):
                raise ReviewError("track revision conflict")
            if decision.track_revision > link.track_revision + 1:
                raise ReviewError("track revision gap")
            if (link.coop_id is None) != (decision.adjudication == "EXCLUDED"):
                raise ReviewError("track adjudication cannot change")
        elif decision.track_revision != 1:
            raise ReviewError("first track revision must be one")
        coop = None
        if decision.record_key:
            coop = session.scalar(
                select(Coop).where(Coop.record_key == decision.record_key).with_for_update()
            )
            if link and (coop is None or coop.id != link.coop_id):
                raise ReviewError("linked target changed or missing")
        if link is not None and decision.track_revision == link.track_revision:
            item = session.get(ReviewedDomainItem, (link.batch_id, link.track_key))
            if item is None:
                raise ReviewError("stored review item missing")
            stored = set(
                session.execute(
                    select(
                        ReviewedDomainEvidence.source_id,
                        ReviewedDomainEvidence.source_record_id,
                        ReviewedDomainEvidence.revision_id,
                    ).where(
                        ReviewedDomainEvidence.batch_id == link.batch_id,
                        ReviewedDomainEvidence.track_key == link.track_key,
                    )
                ).all()
            )
            supplied = {(e.source_id, e.source_record_id, e.revision_id) for e in decision.evidence}
            if stored != supplied:
                raise ReviewError("evidence linkage changed")
            if coop is not None and any(
                _encoded(getattr(coop, key)) != baseline
                for key, baseline in link.field_baselines.items()
                if key not in item.conflict_fields
            ):
                raise ReviewError("local divergence on replay")
            counts["unchanged"] += 1
            continue
        if decision.adjudication == "EXCLUDED":
            outcome = "EXCLUDED"
            baselines: dict[str, Any] = {}
            counts["excluded"] += 1
        else:
            assert decision.record_key is not None
            values = _validated_fields(decision.fields)
            if link is not None and decision.action != "UPDATE":
                raise ReviewError("linked track requires UPDATE")
            if decision.action == "CREATE":
                if (
                    link
                    or coop is not None
                    or not {"title", "priority", "status", "needs_review", "uncertainty"}
                    <= values.keys()
                ):
                    raise ReviewError("CREATE requires a new exact key and required fields")
                coop = Coop(record_key=decision.record_key, **values)
                _state_valid(coop, {})
                session.add(coop)
                session.flush()
                counts["created"] += 1
                outcome = "CREATED"
                baselines = {k: _encoded(v) for k, v in values.items()}
                changes = {k: {"old": None, "new": _encoded(v)} for k, v in values.items()}
            else:
                if (
                    coop is None
                    or coop.is_synthetic_seed
                    or session.scalar(
                        select(CoopImportState).where(CoopImportState.coop_id == coop.id)
                    )
                ):
                    raise ReviewError(
                        "UPDATE requires an existing non-seed, non-manual-managed exact key"
                    )
                if link is None:
                    if (
                        decision.expected_version != coop.version
                        or decision.expected_fields is None
                    ):
                        raise ReviewError("initial target version mismatch")
                    expected = _validated_fields(decision.expected_fields)
                    if any(_encoded(getattr(coop, k)) != _encoded(v) for k, v in expected.items()):
                        raise ReviewError("initial target snapshot mismatch")
                    baselines = {k: _encoded(v) for k, v in expected.items()}
                else:
                    if (
                        decision.expected_version is not None
                        or decision.expected_fields is not None
                    ):
                        raise ReviewError("linked UPDATE must use stored field baselines")
                    baselines = dict(link.field_baselines)
                changes = {}
                conflicts = []
                for key, value in values.items():
                    current = _encoded(getattr(coop, key))
                    if key not in baselines:
                        conflicts.append(
                            key
                        )  # no authority to overwrite a previously unmanaged field
                    elif current != baselines[key]:
                        conflicts.append(key)
                    else:
                        if current != _encoded(value):
                            changes[key] = {"old": current, "new": _encoded(value)}
                        baselines[key] = _encoded(value)
                _state_valid(coop, {key: values[key] for key in changes})
                for key in changes:
                    setattr(coop, key, values[key])
                if changes:
                    coop.version += 1
                    counts["updated"] += 1
                if conflicts:
                    counts["conflicted"] += 1
                outcome = "CONFLICT" if conflicts else ("UPDATED" if changes else "UNCHANGED")
            if changes:
                session.add(
                    AuditEvent(
                        coop_id=coop.id,
                        action=f"REVIEWED_DOMAIN_{outcome if outcome != 'CONFLICT' else 'UPDATED'}",
                        actor_type="SYSTEM",
                        actor_id="reviewed-domain-v1",
                        source="MANUAL_IMPORT",
                        changes=changes,
                        request_id=f"reviewed-{manifest.manifest_hash[:24]}",
                        source_reference=f"{decision.track_key}:{decision.track_revision}",
                    )
                )
        if link is None:
            link = ReviewedDomainTrack(
                track_key=decision.track_key,
                coop_id=coop.id if coop else None,
                track_revision=decision.track_revision,
                decision_hash=decision.decision_hash,
                field_baselines=baselines,
                batch_id=batch.id,
            )
            session.add(link)
        else:
            link.track_revision = decision.track_revision
            link.decision_hash = decision.decision_hash
            link.field_baselines = baselines
            link.batch_id = batch.id
        session.flush()  # persist batch and track before dependent evidence/item rows
        session.add(
            ReviewedDomainItem(
                batch_id=batch.id,
                track_key=decision.track_key,
                outcome=outcome,
                conflict_fields=conflicts
                if decision.adjudication == "ACCEPTED" and decision.action == "UPDATE"
                else [],
            )
        )
        for evidence in decision.evidence:
            session.add(
                ReviewedDomainEvidence(
                    batch_id=batch.id,
                    track_key=decision.track_key,
                    source_id=evidence.source_id,
                    source_record_id=evidence.source_record_id,
                    revision_id=evidence.revision_id,
                )
            )
        session.flush()
    if previous is None:
        batch.summary = counts
    return {**counts, "manifest_hash": manifest.manifest_hash}


def main() -> int:
    parser = argparse.ArgumentParser(description="Apply explicitly reviewed evidence decisions")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--expected-manifest-hash", required=True)
    args = parser.parse_args()
    try:
        raw = json.loads(args.manifest.read_text(encoding="utf-8"))
        if not isinstance(raw, dict) or raw.get("manifest_hash") != args.expected_manifest_hash:
            raise ReviewError("manifest identity mismatch")
        with SessionLocal.begin() as session:
            result = apply_manifest(session, raw)
    except (OSError, json.JSONDecodeError, ReviewError):
        print(json.dumps({"status": "rejected"}), file=sys.stderr)
        return 2
    print(json.dumps({"status": "ok", **result}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
