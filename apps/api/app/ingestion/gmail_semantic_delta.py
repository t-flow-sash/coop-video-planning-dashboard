"""Separate authorized semantic worker; never imported by the metadata collector.

A route is explicitly provisioned for an exact source thread and business track.
All message MIME remains transient. The database stores only assertion digests,
exact metadata revision citations and sanitized decision outcomes.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sys
from datetime import date
from typing import Protocol
from uuid import UUID

from sqlalchemy import Engine, select, text
from sqlalchemy.orm import Session

from app.db import engine as default_engine
from app.ingestion.gmail import SOURCE_KEY, GmailConfig
from app.ingestion.gmail_semantic import (
    FullMessageClient,
    ParsedMessage,
    SemanticInputError,
    Verdict,
    decide,
)
from app.models import (
    Coop,
    IngestionSource,
    ReviewedDomainTrack,
    SemanticMessageState,
    SemanticTrackRoute,
    SourceRecord,
    SourceRecordRevision,
)
from app.reviewed_domain_import import ReviewError, apply_manifest, canonical_hash


class FullFetcher(Protocol):
    def thread_inventory(self, thread_id: str) -> dict[str, tuple[int, tuple[str, ...]]]: ...

    def fetch(self, message_id: str) -> ParsedMessage: ...


def _message_fingerprint(message: ParsedMessage) -> str:
    """Transient full-text attestation, never persist MIME or plaintext."""
    payload = [
        message.message_id,
        message.thread_id,
        message.internal_date,
        message.sender,
        message.recipient,
        message.subject,
        message.text,
        message.labels,
        message.has_attachments,
    ]
    return hashlib.sha256(json.dumps(payload, sort_keys=True).encode()).hexdigest()


def _current_pairs(
    session: Session, source_id: UUID, thread_id: str
) -> list[tuple[SourceRecord, SourceRecordRevision]]:
    records = session.scalars(
        select(SourceRecord).where(
            SourceRecord.source_id == source_id, SourceRecord.record_type == "GMAIL_MESSAGE"
        )
    ).all()
    pairs: list[tuple[SourceRecord, SourceRecordRevision]] = []
    for candidate in records:
        observed = session.scalar(
            select(SourceRecordRevision)
            .where(
                SourceRecordRevision.source_record_id == candidate.id,
                SourceRecordRevision.source_id == source_id,
            )
            .order_by(SourceRecordRevision.observed_at.desc(), SourceRecordRevision.id.desc())
            .limit(1)
        )
        if observed and observed.metadata_json.get("thread_id") == thread_id:
            pairs.append((candidate, observed))
    return pairs


def _attest_thread(
    client: FullFetcher,
    thread_id: str,
    pairs: list[tuple[SourceRecord, SourceRecordRevision]],
) -> tuple[str | None, dict[str, str]]:
    if not pairs or len(pairs) > 100:
        return "thread_incomplete_or_changed", {}
    inventory: dict[str, tuple[int, tuple[str, ...]]] = {}
    try:
        inventory = client.thread_inventory(thread_id)
        complete = set(inventory) == {record.external_id for record, _ in pairs} and all(
            inventory[record.external_id]
            == (
                int(observed.metadata_json["internal_date"]),
                tuple(sorted(observed.metadata_json["label_ids"])),
            )
            for record, observed in pairs
        )
    except (KeyError, TypeError, ValueError):
        complete = False
    except Exception:
        return "thread_inventory_unavailable", {}
    if not complete:
        return "thread_incomplete_or_changed", {}
    fingerprints: dict[str, str] = {}
    try:
        for record, observed in pairs:
            msg = client.fetch(record.external_id)
            meta = observed.metadata_json
            if (
                msg.message_id != record.external_id
                or msg.thread_id != thread_id
                or str(msg.internal_date) != str(meta.get("internal_date"))
                or sorted(msg.labels) != sorted(meta.get("label_ids", []))
            ):
                return "full_message_mismatch", {}
            fingerprints[record.external_id] = _message_fingerprint(msg)
    except Exception:
        return "full_message_unavailable", {}
    try:
        if client.thread_inventory(thread_id) != inventory:
            return "thread_incomplete_or_changed", {}
    except Exception:
        return "thread_inventory_unavailable", {}
    return None, fingerprints


def _encoded_value(value: object) -> object:
    return value.isoformat() if isinstance(value, date) else value


def _followup(state: SemanticMessageState, reason: str) -> str:
    state.provenance = {**state.provenance, "followup": {"reason_code": reason}}
    return "REVIEW"


def _save(
    session: Session,
    revision: SourceRecordRevision,
    outcome: str,
    reason: str,
    provenance: dict[str, object] | dict[str, list[dict[str, str]]],
) -> None:
    row = session.get(SemanticMessageState, revision.id)
    if row is None:
        session.add(
            SemanticMessageState(
                revision_id=revision.id,
                source_record_id=revision.source_record_id,
                source_id=revision.source_id,
                outcome=outcome,
                reason_code=reason,
                provenance=provenance,
            )
        )
    else:
        row.outcome, row.reason_code, row.provenance = outcome, reason, provenance


def process_revision(
    engine: Engine, source_id: UUID, revision_id: UUID, client: FullFetcher, own_address: str
) -> str:
    """Process a collected revision, independent of collector cursor.

    Call under an authorized domain DB role. Lock before fetching and commit
    decision state with domain/audit atomically. RETRY remains discoverable.
    """
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('gmail-semantic-worker-v1'))"))
        revision = session.scalar(
            select(SourceRecordRevision).where(
                SourceRecordRevision.id == revision_id, SourceRecordRevision.source_id == source_id
            )
        )
        if revision is None:
            return "REVIEW"
        state = session.get(SemanticMessageState, revision_id)
        if state is not None and state.outcome in {"CREATED", "UPDATED", "UNCHANGED"}:
            # A successful decision is historical; replay concerns go in followup.
            thread_id = revision.metadata_json.get("thread_id")
            thread = (
                session.scalar(
                    select(SourceRecord).where(
                        SourceRecord.source_id == source_id,
                        SourceRecord.record_type == "GMAIL_THREAD",
                        SourceRecord.external_id == thread_id,
                    )
                )
                if isinstance(thread_id, str)
                else None
            )
            route = session.get(SemanticTrackRoute, thread.id) if thread else None
            if route is None or route.source_id != source_id:
                return _followup(state, "exact_route_missing")
            expected_route = state.provenance.get("route")
            actual_route = {
                "thread_record_id": str(route.thread_record_id),
                "track_key": route.track_key,
                "record_key": route.record_key,
                "expected_coop_id": str(route.expected_coop_id) if route.expected_coop_id else None,
            }
            if expected_route != actual_route:
                return _followup(state, "route_or_target_changed")
            link = session.get(ReviewedDomainTrack, route.track_key)
            coop = session.get(Coop, link.coop_id) if link and link.coop_id else None
            if (
                coop is None
                or coop.record_key != route.record_key
                or (route.expected_coop_id is not None and route.expected_coop_id != coop.id)
                or any(
                    _encoded_value(getattr(coop, key, None)) != value
                    for key, value in state.provenance.get("applied_fields", {}).items()
                )
            ):
                return _followup(state, "linked_target_changed")
            if not isinstance(thread_id, str):
                return _followup(state, "revision_identity_invalid")
            replay_pairs = _current_pairs(session, source_id, thread_id)
            if revision not in [observed for _, observed in replay_pairs]:
                return _followup(state, "thread_incomplete_or_changed")
            reason, fingerprints = _attest_thread(client, thread_id, replay_pairs)
            if reason is None and fingerprints != state.provenance.get("message_fingerprints"):
                reason = "full_message_changed"
            if reason:
                return _followup(state, reason)
            if "followup" in state.provenance:
                state.provenance = {k: v for k, v in state.provenance.items() if k != "followup"}
            return "UNCHANGED"
        record = session.get(SourceRecord, revision.source_record_id)
        metadata = revision.metadata_json
        if (
            record is None
            or record.record_type != "GMAIL_MESSAGE"
            or record.source_id != source_id
            or metadata.get("evidence_only") is not True
            or metadata.get("message_id") != record.external_id
            or not isinstance(metadata.get("thread_id"), str)
        ):
            _save(session, revision, "REVIEW", "revision_identity_invalid", {})
            return "REVIEW"
        thread = session.scalar(
            select(SourceRecord).where(
                SourceRecord.source_id == source_id,
                SourceRecord.record_type == "GMAIL_THREAD",
                SourceRecord.external_id == metadata["thread_id"],
            )
        )
        route = session.get(SemanticTrackRoute, thread.id) if thread else None
        if (
            route is None
            or route.source_id != source_id
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,159}", route.track_key)
            or not re.fullmatch(r"[a-z0-9][a-z0-9._-]{2,159}", route.record_key)
        ):
            _save(session, revision, "REVIEW", "exact_route_missing", {})
            return "REVIEW"
        records = session.scalars(
            select(SourceRecord).where(
                SourceRecord.source_id == source_id, SourceRecord.record_type == "GMAIL_MESSAGE"
            )
        ).all()
        pairs: list[tuple[SourceRecord, SourceRecordRevision]] = []
        for candidate in records:
            revisions = session.scalars(
                select(SourceRecordRevision)
                .where(
                    SourceRecordRevision.source_record_id == candidate.id,
                    SourceRecordRevision.source_id == source_id,
                )
                .order_by(SourceRecordRevision.observed_at.desc(), SourceRecordRevision.id.desc())
            ).all()
            if revisions and revisions[0].metadata_json.get("thread_id") == metadata["thread_id"]:
                pairs.append((candidate, revisions[0]))
        if not pairs or len(pairs) > 100 or revision not in [r for _, r in pairs]:
            _save(session, revision, "REVIEW", "thread_incomplete_or_changed", {})
            return "REVIEW"
        inventory: dict[str, tuple[int, tuple[str, ...]]] = {}
        try:
            inventory = client.thread_inventory(metadata["thread_id"])
            complete = set(inventory) == {candidate.external_id for candidate, _ in pairs} and all(
                inventory[candidate.external_id]
                == (
                    int(observed.metadata_json["internal_date"]),
                    tuple(sorted(observed.metadata_json["label_ids"])),
                )
                for candidate, observed in pairs
            )
        except (KeyError, TypeError, ValueError):
            complete = False
        except Exception:
            _save(session, revision, "REVIEW", "thread_inventory_unavailable", {})
            return "REVIEW"
        if not complete:
            _save(session, revision, "REVIEW", "thread_incomplete_or_changed", {})
            return "REVIEW"
        messages = []
        try:
            for candidate, observed in pairs:
                msg = client.fetch(candidate.external_id)
                meta = observed.metadata_json
                if (
                    msg.message_id != candidate.external_id
                    or msg.thread_id != metadata["thread_id"]
                    or str(msg.internal_date) != str(meta.get("internal_date"))
                    or sorted(msg.labels) != sorted(meta.get("label_ids", []))
                ):
                    _save(session, revision, "REVIEW", "full_message_mismatch", {})
                    return "REVIEW"
                messages.append(msg)
        except SemanticInputError:
            _save(session, revision, "REVIEW", "full_message_unavailable", {})
            return "REVIEW"
        except LookupError:
            _save(session, revision, "REVIEW", "full_message_unavailable", {})
            return "REVIEW"
        except Exception:
            _save(session, revision, "RETRY", "full_message_unavailable", {})
            return "RETRY"
        try:
            still_current = client.thread_inventory(metadata["thread_id"])
        except Exception:
            _save(session, revision, "REVIEW", "thread_inventory_unavailable", {})
            return "REVIEW"
        if still_current != inventory:
            _save(session, revision, "REVIEW", "thread_incomplete_or_changed", {})
            return "REVIEW"
        try:
            decision = decide(messages, own_address=own_address, existing_track=True)
        except Exception:
            _save(session, revision, "REVIEW", "semantic_input_invalid", {})
            return "REVIEW"
        fields: dict[str, object] = {}
        citations: dict[str, list[dict[str, str]]] = {}
        by_message = {r.external_id: r for r, _ in pairs}
        revisions_by_record = {r.id: v for r, v in pairs}
        for name in ("fee", "deadline", "status"):
            item = getattr(decision, name)
            if item.verdict == Verdict.REVIEW:
                _save(session, revision, "REVIEW", "ambiguous_terms", {})
                return "REVIEW"
            if item.verdict == Verdict.ACCEPT:
                refs = []
                for ref in item.evidence:
                    cited = by_message.get(ref.message_id)
                    cited_revision = (
                        revisions_by_record.get(cited.id) if cited is not None else None
                    )
                    if (
                        cited is None
                        or cited_revision is None
                        or ref.thread_id != metadata["thread_id"]
                    ):
                        _save(session, revision, "REVIEW", "assertion_unbound", {})
                        return "REVIEW"
                    refs.append(
                        {
                            "source_id": str(source_id),
                            "source_record_id": str(cited.id),
                            "revision_id": str(cited_revision.id),
                            "assertion_sha256": ref.assertion_sha256,
                        }
                    )
                citations[name] = refs
                if name == "fee":
                    fields.update(item.value)
                else:
                    fields[name] = item.value
        if not fields or not citations:
            _save(session, revision, "REVIEW", "no_mutual_terms", {})
            return "REVIEW"
        link = session.get(ReviewedDomainTrack, route.track_key)
        target = session.scalar(
            select(Coop).where(Coop.record_key == route.record_key).with_for_update()
        )
        if link:
            if (
                target is None
                or link.coop_id != target.id
                or target.is_synthetic_seed
                or any(
                    getattr(target, key, None) != value
                    for key, value in link.field_baselines.items()
                    if key in fields
                )
            ):
                _save(session, revision, "REVIEW", "linked_target_changed", citations)
                return "REVIEW"
            action, track_revision = "UPDATE", link.track_revision + 1
        elif route.expected_coop_id:
            if target is None or target.id != route.expected_coop_id or target.is_synthetic_seed:
                _save(session, revision, "REVIEW", "expected_target_missing", citations)
                return "REVIEW"
            action, track_revision = "UPDATE", 1
        elif target is None:
            action, track_revision = "CREATE", 1
            fields = {
                "title": route.title,
                "priority": "P1",
                "status": "NEW",
                "needs_review": False,
                "uncertainty": None,
                **fields,
            }
        else:
            _save(session, revision, "REVIEW", "unlinked_target_exists", citations)
            return "REVIEW"
        evidence = list(
            {
                (r["source_id"], r["source_record_id"], r["revision_id"]): {
                    k: r[k] for k in ("source_id", "source_record_id", "revision_id")
                }
                for refs in citations.values()
                for r in refs
            }.values()
        )
        manifest_item: dict[str, object] = {
            "track_key": route.track_key,
            "track_revision": track_revision,
            "adjudication": "ACCEPTED",
            "action": action,
            "record_key": route.record_key,
            "evidence": evidence,
            "fields": fields,
        }
        if action == "UPDATE" and link is None and target is not None:
            manifest_item["expected_version"] = target.version
            manifest_item["expected_fields"] = {
                k: getattr(target, k)
                if k != "deadline" or getattr(target, k) is None
                else getattr(target, k).isoformat()
                for k in fields
            }
        manifest_item["decision_hash"] = canonical_hash(manifest_item)
        manifest: dict[str, object] = {
            "schema_version": 1,
            "manifest_id": f"semantic-{revision_id}-{track_revision}",
            "decisions": [manifest_item],
        }
        manifest["manifest_hash"] = canonical_hash(manifest)
        try:
            with session.begin_nested():
                result = apply_manifest(session, manifest)
        except ReviewError:
            _save(session, revision, "REVIEW", "domain_conflict", citations)
            return "REVIEW"
        outcome = (
            "CREATED" if result["created"] else "UPDATED" if result["updated"] else "UNCHANGED"
        )
        _save(
            session,
            revision,
            outcome,
            "mutual_evidence",
            {
                **citations,
                "manifest_hash": result["manifest_hash"],
                "message_fingerprints": {
                    msg.message_id: _message_fingerprint(msg) for msg in messages
                },
                "route": {
                    "thread_record_id": str(route.thread_record_id),
                    "track_key": route.track_key,
                    "record_key": route.record_key,
                    "expected_coop_id": (
                        str(route.expected_coop_id) if route.expected_coop_id else None
                    ),
                },
                "applied_fields": {
                    k: v.isoformat() if isinstance(v, date) else v for k, v in fields.items()
                },
            },
        )
        return outcome


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Process one exact collected Gmail message revision"
    )
    parser.add_argument("--revision-id", type=UUID, required=True)
    parser.add_argument("--source-key", default=SOURCE_KEY)
    args = parser.parse_args()
    with Session(default_engine) as session:
        source = session.scalar(
            select(IngestionSource).where(
                IngestionSource.source_key == args.source_key,
                IngestionSource.kind == "GMAIL",
                IngestionSource.enabled.is_(True),
            )
        )
        if source is None:
            print(json.dumps({"outcome": "REVIEW"}))
            return 2
        source_id = source.id
    try:
        config = GmailConfig.from_env()
        outcome = process_revision(
            default_engine, source_id, args.revision_id, FullMessageClient(config), config.account
        )
    except Exception:
        print(json.dumps({"outcome": "RETRY"}), file=sys.stderr)
        return 2
    print(json.dumps({"outcome": outcome}))
    return 0 if outcome in {"CREATED", "UPDATED", "UNCHANGED"} else 2


if __name__ == "__main__":
    raise SystemExit(main())
