"""Narrow reviewed identity handoff; discovery alone never invents a business key.

Run only from an independently authorized provisioning context, NOT the collector or
semantic writer. The caller supplies a separately established exact track identity;
subject, sender, name and thread are not acceptable identity attestations. No CLI or
scheduler is wired to this module.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import NoReturn
from uuid import UUID

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.ingestion.gmail_semantic import Verdict, decide
from app.ingestion.gmail_semantic_delta import FullFetcher, _current_pairs, _message_fingerprint
from app.manual_coop_import import _snapshot
from app.models import (
    Coop,
    CoopImportState,
    IngestionSource,
    ReviewedDomainTrack,
    SemanticTrackRoute,
    SourceRecord,
)


class RouteReview(ValueError):
    """Sanitized review outcome; never echo provider or business identifiers."""


@dataclass(frozen=True)
class RouteClaim:
    source_id: UUID
    thread_id: str
    evidence: tuple[tuple[UUID, UUID], ...]
    track_key: str
    record_key: str
    title: str
    identity_attestation: str
    expected_coop_id: UUID | None
    expected_version: int | None
    expected_snapshot: str | None
    message_fingerprints: tuple[tuple[str, str], ...] = ()


def _review(reason: str) -> NoReturn:
    raise RouteReview(reason)


def provision_route(
    engine: Engine, claim: RouteClaim, client: FullFetcher, own_address: str
) -> str:
    """Serialize exact claims and atomically insert a single-thread route.

    This does NOT discover identity from Gmail. A distinct authorized reviewer
    must supply a verified stable business key and evidence identity attestation.
    The writer independently rechecks complete current evidence before CREATE.
    """
    if (
        not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,159}", claim.track_key)
        or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,159}", claim.record_key)
        or not claim.title.strip()
        or len(claim.title) > 240
        or claim.identity_attestation != "reviewed-exact-business-identity"
        or not claim.thread_id
        or not claim.evidence
        or len(claim.evidence) > 100
        or len(set(claim.evidence)) != len(claim.evidence)
        or len(claim.message_fingerprints) != len(claim.evidence)
        or len(set(claim.message_fingerprints)) != len(claim.message_fingerprints)
    ):
        _review("identity_unverified")
    try:
        UUID(claim.record_key)
    except ValueError:
        pass
    else:
        _review("identity_unverified")
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('gmail-semantic-worker-v1'))"))
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('manual-coop-import-v1'))"))
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('reviewed-domain-v1'))"))
        source = session.get(IngestionSource, claim.source_id)
        if source is None or source.kind != "GMAIL" or not source.enabled:
            _review("source_unavailable")
        thread = session.scalar(
            select(SourceRecord).where(
                SourceRecord.source_id == claim.source_id,
                SourceRecord.record_type == "GMAIL_THREAD",
                SourceRecord.external_id == claim.thread_id,
            )
        )
        if thread is None:
            _review("evidence_changed")
        pairs = _current_pairs(session, claim.source_id, claim.thread_id)
        if (
            not pairs
            or len(pairs) > 100
            or {(r.id, v.id) for r, v in pairs} != set(claim.evidence)
            or any(
                v.metadata_json.get("evidence_only") is not True
                or v.metadata_json.get("message_id") != r.external_id
                or v.metadata_json.get("thread_id") != claim.thread_id
                for r, v in pairs
            )
        ):
            _review("evidence_changed")
        try:
            inventory = client.thread_inventory(claim.thread_id)
            if set(inventory) != {r.external_id for r, _ in pairs} or any(
                inventory[r.external_id]
                != (
                    int(v.metadata_json["internal_date"]),
                    tuple(sorted(v.metadata_json["label_ids"])),
                )
                for r, v in pairs
            ):
                _review("evidence_changed")
            messages = [client.fetch(r.external_id) for r, _ in pairs]
            if (
                any(
                    msg.message_id != r.external_id
                    or msg.thread_id != claim.thread_id
                    or str(msg.internal_date) != str(v.metadata_json["internal_date"])
                    or tuple(sorted(msg.labels)) != tuple(sorted(v.metadata_json["label_ids"]))
                    for msg, (r, v) in zip(messages, pairs, strict=True)
                )
                or client.thread_inventory(claim.thread_id) != inventory
                or {(m.message_id, _message_fingerprint(m)) for m in messages}
                != set(claim.message_fingerprints)
            ):
                _review("evidence_changed")
        except RouteReview:
            raise
        except Exception:
            _review("evidence_changed")
        # Positive CREATE rule: explicit accepted lead AND mutually accepted terms.
        # A thread with ambiguous terms, withdrawal or attachments stays REVIEW.
        decision = decide(messages, own_address=own_address, existing_track=False)
        if (
            any(
                getattr(decision, name).verdict == Verdict.REVIEW
                for name in ("lead", "fee", "deadline", "status")
            )
            or decision.lead.verdict != Verdict.ACCEPT
            or not any(
                getattr(decision, name).verdict == Verdict.ACCEPT for name in ("fee", "deadline")
            )
        ):
            _review("terms_unverified")
        occupied = session.scalar(
            select(SemanticTrackRoute).where(SemanticTrackRoute.thread_record_id == thread.id)
        )
        target = session.scalar(
            select(Coop).where(Coop.record_key == claim.record_key).with_for_update()
        )
        if occupied is not None:
            if (
                occupied.source_id == claim.source_id
                and occupied.track_key == claim.track_key
                and occupied.record_key == claim.record_key
                and occupied.title == claim.title
                and occupied.expected_coop_id == claim.expected_coop_id
                and (
                    target is not None and target.id == claim.expected_coop_id
                    if claim.expected_coop_id is not None
                    else target is None
                    or session.scalar(
                        select(ReviewedDomainTrack.track_key).where(
                            ReviewedDomainTrack.coop_id == target.id
                        )
                    )
                    == claim.track_key
                )
            ):
                return "UNCHANGED"
            _review("route_occupied")
        if claim.expected_coop_id is None:
            if (
                target is not None
                or claim.expected_version is not None
                or claim.expected_snapshot is not None
            ):
                _review("target_identity_unverified")
        else:
            if target is None or target.id != claim.expected_coop_id or target.is_synthetic_seed:
                _review("target_identity_unverified")
            state = session.get(CoopImportState, target.id)
            if (
                state is None
                or claim.expected_version != target.version
                or claim.expected_version != state.record_version
                or claim.expected_snapshot != state.snapshot_hash
                or state.snapshot_hash != _snapshot(target)
            ):
                _review("target_identity_unverified")
            if session.scalar(
                select(ReviewedDomainTrack.track_key).where(
                    ReviewedDomainTrack.coop_id == target.id
                )
            ):
                _review("target_identity_unverified")
        if session.scalar(
            select(SemanticTrackRoute).where(
                (SemanticTrackRoute.track_key == claim.track_key)
                | (SemanticTrackRoute.record_key == claim.record_key)
            )
        ):
            _review("route_occupied")
        if session.get(ReviewedDomainTrack, claim.track_key):
            _review("route_occupied")
        session.add(
            SemanticTrackRoute(
                source_id=claim.source_id,
                thread_record_id=thread.id,
                track_key=claim.track_key,
                record_key=claim.record_key,
                title=claim.title,
                expected_coop_id=claim.expected_coop_id,
            )
        )
        session.flush()
        return "PROVISIONED"
