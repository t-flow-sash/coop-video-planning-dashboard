"""Narrow reviewed identity handoff; discovery alone never invents a business key.

Run only from an independently authorized provisioning context, NOT the collector or
semantic writer. The caller supplies a separately established exact track identity;
subject, sender, name and thread are not acceptable identity attestations alone.

The operator surface `discover` prints only hashed thread references, participant
domains, message counts and window dates (never bodies, subjects or raw provider
IDs); `apply` consumes an explicit operator map and re-verifies complete evidence,
message fingerprints, target version/snapshot and route occupancy before one
atomic insert. Every entry point requires SEMANTIC_PROVISION_ARMED=true.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, NoReturn
from uuid import UUID

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.db import engine as default_engine
from app.ingestion.gmail import SOURCE_KEY, GmailConfig
from app.ingestion.gmail_semantic import FullMessageClient, SemanticInputError, Verdict, decide
from app.ingestion.gmail_semantic_delta import (
    FullFetcher,
    _current_pairs,
    _message_fingerprint,
)
from app.manual_coop_import import _snapshot
from app.models import (
    Coop,
    CoopImportState,
    IngestionSource,
    ReviewedDomainTrack,
    SemanticTrackRoute,
    SourceRecord,
)

ARM_FLAG = "SEMANTIC_PROVISION_ARMED"
MAX_DISCOVERY = 200
# Single non-identifying literal for anything that is not an allowlisted input code, so
# an unexpected exception's message (which may embed provider or business text) can
# never reach the discovery inventory.
UNEXPECTED_REASON = "unexpected"
_REASON_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _undecided_reason(error: Exception) -> str:
    """Sanitized, allowlisted reason for a thread the semantic gate cannot decide.

    `SemanticInputError` carries a fixed machine code as its first argument. A bare
    exception class name cannot separate an identity/spoofing rejection from a
    malformed number, an oversized body or an unexpected programming error, so an
    operator sees one indistinguishable reason for every undecidable thread.
    """
    if isinstance(error, SemanticInputError) and error.args:
        code = error.args[0]
        if isinstance(code, str) and _REASON_CODE.fullmatch(code):
            return code
    return UNEXPECTED_REASON


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
        try:
            decision = decide(messages, own_address=own_address, existing_track=False)
        except SemanticInputError:
            # Evidence the semantic gate cannot decide is a permanent, per-entry
            # review condition, not a transient transport failure. Classify it as
            # REVIEW so one unusable entry cannot escape `apply_map` and abort the
            # whole operator map with a top-level RETRY, discarding every later entry.
            _review("semantic_input_invalid")
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


def _thread_ref(thread_id: str) -> str:
    return hashlib.sha256(thread_id.encode()).hexdigest()[:16]


def _domains(values: Iterable[str]) -> list[str]:
    found = set()
    for value in values:
        for domain in re.findall(r"[A-Za-z0-9._%+-]+@([A-Za-z0-9.-]+\.[A-Za-z]{2,})", value or ""):
            found.add(domain.lower())
    return sorted(found)


def _decision_summary(decision: Any) -> dict[str, str]:
    return {
        name: getattr(decision, name).verdict.name for name in ("lead", "fee", "deadline", "status")
    }


def _is_provisionable(decision: Any) -> bool:
    if any(
        getattr(decision, name).verdict == Verdict.REVIEW
        for name in ("lead", "fee", "deadline", "status")
    ):
        return False
    return decision.lead.verdict == Verdict.ACCEPT and any(
        getattr(decision, name).verdict == Verdict.ACCEPT for name in ("fee", "deadline")
    )


def _source_id(session: Session, source_key: str) -> UUID:
    source_id = session.scalar(
        select(IngestionSource.id).where(
            IngestionSource.source_key == source_key,
            IngestionSource.kind == "GMAIL",
            IngestionSource.enabled.is_(True),
        )
    )
    if source_id is None:
        _review("source_unavailable")
    return source_id


def discover(
    engine: Engine,
    client: FullFetcher,
    own_address: str,
    *,
    source_id: UUID,
    limit: int = MAX_DISCOVERY,
) -> list[dict[str, Any]]:
    """Read-only candidate inventory: hashed refs, domains, window, verdicts.

    Emits no message bodies, subjects, raw provider IDs or business keys.
    """
    if not 1 <= limit <= MAX_DISCOVERY:
        _review("limit_invalid")
    rows: list[dict[str, Any]] = []
    failures = 0
    undecided: dict[str, int] = {}
    with Session(engine) as session:
        routed = set(session.scalars(select(SemanticTrackRoute.thread_record_id)))
        threads = session.scalars(
            select(SourceRecord)
            .where(
                SourceRecord.source_id == source_id,
                SourceRecord.record_type == "GMAIL_THREAD",
            )
            .order_by(SourceRecord.created_at.desc())
        ).all()
        for thread in threads:
            if len(rows) >= limit:
                break
            if thread.id in routed:
                continue
            pairs = _current_pairs(session, source_id, thread.external_id)
            if not pairs or len(pairs) > 100:
                continue
            # A single unfetchable thread must not abort the whole inventory; a
            # systemic provider failure still surfaces as an empty RETRY result.
            try:
                messages = [client.fetch(record.external_id) for record, _ in pairs]
            except Exception:
                failures += 1
                continue
            if any(message.thread_id != thread.external_id for message in messages):
                continue
            dates = sorted(str(message.internal_date) for message in messages)
            try:
                decision = decide(messages, own_address=own_address, existing_track=False)
                verdicts = _decision_summary(decision)
                provisionable = _is_provisionable(decision)
            except Exception as error:
                # Sanitized allowlisted code only: never provider text, addresses,
                # business keys or an unexpected exception's message.
                code = _undecided_reason(error)
                verdicts = {"error": code}
                provisionable = False
                undecided[code] = undecided.get(code, 0) + 1
            rows.append(
                {
                    "thread_ref": _thread_ref(thread.external_id),
                    "messages": len(messages),
                    "window": [dates[0], dates[-1]],
                    "domains": _domains(
                        [message.sender for message in messages]
                        + [message.recipient for message in messages]
                    ),
                    "verdicts": verdicts,
                    "provisionable": provisionable,
                }
            )
    if not rows and failures:
        _review("provider_unavailable")
    if failures:
        print(json.dumps({"discovery_skipped": failures}), file=sys.stderr)
    if undecided:
        # Aggregate so an operator sees *why* nothing is provisionable without
        # scanning every row; keys are allowlisted codes only.
        print(json.dumps({"discovery_undecided": undecided}), file=sys.stderr)
    return sorted(rows, key=lambda row: row["window"][-1], reverse=True)


def _claim_from_map(
    engine: Engine, entry: dict[str, Any], client: FullFetcher, source_id: UUID
) -> RouteClaim:
    required = ("thread_ref", "thread_id", "record_key", "track_key", "title")
    if any(not isinstance(entry.get(name), str) or not entry[name] for name in required):
        _review("map_entry_invalid")
    with Session(engine) as session:
        pairs = _current_pairs(session, source_id, entry["thread_id"])
        if not pairs:
            _review("evidence_changed")
        messages = [client.fetch(record.external_id) for record, _ in pairs]
        if {_thread_ref(entry["thread_id"])} != {entry["thread_ref"]}:
            _review("map_entry_invalid")
        target = session.scalar(select(Coop).where(Coop.record_key == entry["record_key"]))
        expected_coop_id = None
        expected_version = None
        expected_snapshot = None
        if entry.get("adopt_existing") is True:
            if target is None or target.is_synthetic_seed:
                _review("target_identity_unverified")
            state = session.get(CoopImportState, target.id)
            if state is None or state.snapshot_hash != _snapshot(target):
                _review("target_identity_unverified")
            expected_coop_id = target.id
            expected_version = target.version
            expected_snapshot = state.snapshot_hash
        return RouteClaim(
            source_id=source_id,
            thread_id=entry["thread_id"],
            evidence=tuple((record.id, revision.id) for record, revision in pairs),
            track_key=entry["track_key"],
            record_key=entry["record_key"],
            title=entry["title"],
            identity_attestation="reviewed-exact-business-identity",
            expected_coop_id=expected_coop_id,
            expected_version=expected_version,
            expected_snapshot=expected_snapshot,
            message_fingerprints=tuple(
                (message.message_id, _message_fingerprint(message)) for message in messages
            ),
        )


def apply_map(
    engine: Engine,
    client: FullFetcher,
    own_address: str,
    entries: Sequence[dict[str, Any]],
    *,
    source_id: UUID,
) -> dict[str, int]:
    """Apply an explicit operator map; every entry is independently re-verified."""
    if not isinstance(entries, list) or len(entries) > MAX_DISCOVERY:
        _review("map_invalid")
    counts = {"PROVISIONED": 0, "UNCHANGED": 0, "REVIEW": 0}
    for entry in entries:
        try:
            claim = _claim_from_map(engine, entry, client, source_id)
            outcome = provision_route(engine, claim, client, own_address)
        except RouteReview:
            counts["REVIEW"] += 1
        else:
            counts[outcome] += 1
    return counts


def main() -> int:
    if os.environ.get(ARM_FLAG) != "true":
        print(json.dumps({"outcome": "NOT_ARMED"}), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(
        description="Operator-only exact route provisioning (discover or apply)"
    )
    parser.add_argument("action", choices=("discover", "apply"))
    parser.add_argument("--map", default="")
    parser.add_argument("--limit", type=int, default=MAX_DISCOVERY)
    parser.add_argument("--source-key", default=SOURCE_KEY)
    args = parser.parse_args()
    try:
        config = GmailConfig.from_env()
        client = FullMessageClient(config)
        with Session(default_engine) as session:
            source_id = _source_id(session, args.source_key)
        if args.action == "discover":
            rows = discover(
                default_engine,
                client,
                config.account,
                source_id=source_id,
                limit=args.limit,
            )
            print(json.dumps({"candidates": rows}))
        else:
            if not args.map:
                _review("map_missing")
            with open(args.map, encoding="utf-8") as handle:
                entries = json.load(handle)
            counts = apply_map(default_engine, client, config.account, entries, source_id=source_id)
            print(json.dumps(counts))
    except Exception:
        print(json.dumps({"outcome": "RETRY"}), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
