"""Bounded, explicitly invoked semantic catch-up; never run by the collector.

New pending revisions take precedence. Retry, review, follow-up and completed
replay classes rotate fairly; completed states replay round-robin without
resetting historical decisions. Only
latest collected message revisions are candidates. No message content is logged.
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import sys
from typing import TypedDict
from uuid import UUID

from sqlalchemy import Engine, and_, exists, or_, select, text
from sqlalchemy.orm import Session, aliased

from app.db import engine as default_engine
from app.ingestion.gmail import SOURCE_KEY, GmailConfig
from app.ingestion.gmail_semantic import FullMessageClient
from app.ingestion.gmail_semantic_delta import FullFetcher, process_revision
from app.models import (
    IngestionSource,
    SemanticMessageState,
    SemanticQueueCursor,
    SourceRecord,
    SourceRecordRevision,
)

MAX_BATCH = 100
FAIR_CLASSES = ("retry", "review", "followup", "replay")
OUTCOMES = ("CREATED", "UPDATED", "UNCHANGED", "RETRY", "REVIEW")


class QueueSummary(TypedDict):
    processed: int
    counts: dict[str, int]
    limit_reached: bool


def _candidates(session: Session, source_id: UUID, limit: int) -> list[tuple[UUID, bool, str]]:
    """Bound every SQL page and exclude superseded revisions deterministically."""
    rev = SourceRecordRevision
    newer = aliased(SourceRecordRevision)
    state = SemanticMessageState
    latest = ~exists(
        select(newer.id).where(
            newer.source_record_id == rev.source_record_id,
            newer.source_id == source_id,
            or_(
                newer.observed_at > rev.observed_at,
                and_(newer.observed_at == rev.observed_at, newer.id > rev.id),
            ),
        )
    )
    base = (
        select(rev.id)
        .join(SourceRecord, SourceRecord.id == rev.source_record_id)
        .outerjoin(state, state.revision_id == rev.id)
        .where(
            rev.source_id == source_id,
            SourceRecord.source_id == source_id,
            SourceRecord.record_type == "GMAIL_MESSAGE",
            latest,
        )
    )
    # Pending stays first. Rotate the remaining classes across invocations;
    # independent intra-class positions alone cannot prevent REVIEW starving replay.
    chosen: list[tuple[UUID, bool, str]] = []
    cursor = session.get(SemanticQueueCursor, source_id)
    positions = cursor.positions if cursor is not None else {}
    next_class = positions.get("_next_class")
    start = FAIR_CLASSES.index(next_class) if next_class in FAIR_CLASSES else 0
    priorities = (
        ("pending", state.revision_id.is_(None)),
        *(
            (
                group,
                {
                    "retry": state.outcome == "RETRY",
                    "review": state.outcome == "REVIEW",
                    "followup": state.provenance.has_key("followup"),
                    "replay": state.outcome.in_(("CREATED", "UPDATED", "UNCHANGED")),
                }[group],
            )
            for group in (*FAIR_CLASSES[start:], *FAIR_CLASSES[:start])
        ),
    )
    ordering = (rev.observed_at, rev.id)
    for group, predicate in priorities:
        if len(chosen) == limit:
            break
        if group == "replay":
            predicate = and_(predicate, ~state.provenance.has_key("followup"))
        group_base = base.where(predicate, rev.id.not_in([id for id, _, _ in chosen]))
        raw = positions.get(group)
        if group == "replay" and raw is None and cursor is not None and cursor.revision_id:
            raw = str(cursor.revision_id)
        anchor = None
        if isinstance(raw, str):
            with contextlib.suppress(ValueError):
                anchor = UUID(raw)
        position = None
        if anchor is not None:
            # Resolve the stored revision's stable ordering key before paging.
            position = session.get(SourceRecordRevision, anchor)
            if position is not None and position.source_id == source_id:
                after = or_(
                    rev.observed_at > position.observed_at,
                    and_(rev.observed_at == position.observed_at, rev.id > anchor),
                )
                rows = session.scalars(
                    group_base.where(after).order_by(*ordering).limit(limit - len(chosen))
                ).all()
            else:
                rows = []
        else:
            rows = []
        chosen.extend((id, group == "replay", group) for id in rows)
        if len(chosen) < limit:
            remaining = group_base.where(rev.id.not_in([id for id, _, _ in chosen]))
            if anchor is not None and position is not None and position.source_id == source_id:
                remaining = remaining.where(
                    or_(
                        rev.observed_at < position.observed_at,
                        and_(rev.observed_at == position.observed_at, rev.id <= anchor),
                    )
                )
            wrapped = session.scalars(
                remaining.order_by(*ordering).limit(limit - len(chosen))
            ).all()
            chosen.extend((id, group == "replay", group) for id in wrapped)
    return chosen


def run_queue(
    engine: Engine, source_id: UUID, client: FullFetcher, own_address: str, *, max_batch: int
) -> QueueSummary:
    if not 1 <= max_batch <= MAX_BATCH:
        raise ValueError("max_batch outside allowed range")
    counts = {outcome: 0 for outcome in OUTCOMES}
    processed = 0
    # A dedicated transaction lock serializes queue selection/cursor advancement.
    # process_revision uses a separate session, holding its own domain lock.
    with Session(engine) as session, session.begin():
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext('gmail-semantic-queue-v1'))"))
        source = session.get(IngestionSource, source_id)
        if source is None or source.kind != "GMAIL" or not source.enabled:
            raise ValueError("source unavailable")
        chosen = _candidates(session, source_id, max_batch)
        for revision_id, replay, group in chosen:
            # Errors abort this invocation; never advance cursor on failure.
            outcome = process_revision(engine, source_id, revision_id, client, own_address)
            if outcome not in counts:
                raise RuntimeError("invalid semantic outcome")
            counts[outcome] += 1
            processed += 1
            position = session.get(SourceRecordRevision, revision_id)
            if position is None:
                raise RuntimeError("revision vanished during queue run")
            cursor = session.get(SemanticQueueCursor, source_id)
            if cursor is None:
                cursor = SemanticQueueCursor(source_id=source_id)
                session.add(cursor)
                session.flush()
            if replay:
                cursor.observed_at = position.observed_at
                cursor.revision_id = revision_id
            cursor.positions = {**cursor.positions, group: str(revision_id)}
            if group in FAIR_CLASSES:
                next_index = (FAIR_CLASSES.index(group) + 1) % len(FAIR_CLASSES)
                cursor.positions = {**cursor.positions, "_next_class": FAIR_CLASSES[next_index]}
        # No revision IDs, provider text, routes, or failure details escape.
    return {"processed": processed, "counts": counts, "limit_reached": processed == max_batch}


def exit_code(summary: QueueSummary) -> int:
    """Scheduler exit policy for a *completed* queue run.

    A completed run whose remaining work is human review is a successful run:
    the ``REVIEW`` count is published in the printed summary and by the
    authenticated semantic status endpoint, so the truth is never hidden. The
    armed hourly CronJob ran with ``review > 0`` in its steady state, so
    returning nonzero for ``REVIEW`` painted a red ``Failed`` job on every
    healthy interval and eroded the badge that must signal a real outage.

    Only unresolved transient ``RETRY`` work is nonzero here; hard failures
    (arming, configuration, provider, database) are handled separately in
    ``main`` and remain nonzero.
    """
    return 0 if summary["counts"]["RETRY"] == 0 else 2


def main() -> int:
    if os.environ.get("SEMANTIC_WORKER_ARMED") != "true":
        print(json.dumps({"outcome": "NOT_ARMED"}), file=sys.stderr)
        return 2
    parser = argparse.ArgumentParser(
        description="Bounded semantic message catch-up (manual invocation)"
    )
    parser.add_argument("--source-key", default=SOURCE_KEY)
    parser.add_argument("--max-batch", type=int, default=25)
    args = parser.parse_args()
    if not 1 <= args.max_batch <= MAX_BATCH:
        print(json.dumps({"outcome": "RETRY"}), file=sys.stderr)
        return 2
    try:
        with Session(default_engine) as session:
            source_id = session.scalar(
                select(IngestionSource.id).where(
                    IngestionSource.source_key == args.source_key,
                    IngestionSource.kind == "GMAIL",
                    IngestionSource.enabled.is_(True),
                )
            )
        if source_id is None:
            raise ValueError("source unavailable")
        config = GmailConfig.from_env()
        summary = run_queue(
            default_engine,
            source_id,
            FullMessageClient(config),
            config.account,
            max_batch=args.max_batch,
        )
    except Exception:
        print(json.dumps({"outcome": "RETRY"}), file=sys.stderr)
        return 2
    print(json.dumps(summary))
    return exit_code(summary)


if __name__ == "__main__":
    raise SystemExit(main())
