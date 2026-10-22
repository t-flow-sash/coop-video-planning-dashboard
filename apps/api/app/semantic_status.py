"""Read-only, privacy-preserving operational snapshot of persisted semantic evidence."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import cast

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session

from app.models import (
    Coop,
    IngestionRun,
    IngestionSource,
    SemanticMessageState,
    SemanticTrackRoute,
    SourceRecord,
    SourceRecordRevision,
)
from app.schemas import SemanticCollectorRun, SemanticSourceStatus, SemanticStatus


def snapshot(session: Session) -> SemanticStatus:
    """Count only latest revisions per message; never equate collection with coverage."""
    now = datetime.now(UTC)
    latest = (
        select(
            SourceRecordRevision.source_id.label("source_id"),
            SourceRecordRevision.source_record_id.label("record_id"),
            SourceRecordRevision.id.label("revision_id"),
            SourceRecordRevision.observed_at.label("observed_at"),
            func.row_number()
            .over(
                partition_by=SourceRecordRevision.source_record_id,
                order_by=(SourceRecordRevision.observed_at.desc(), SourceRecordRevision.id.desc()),
            )
            .label("rank"),
        )
        .join(SourceRecord, SourceRecord.id == SourceRecordRevision.source_record_id)
        .where(SourceRecord.record_type == "GMAIL_MESSAGE")
        .subquery()
    )
    sources: list[SemanticSourceStatus] = []
    for source in session.scalars(
        select(IngestionSource).order_by(IngestionSource.created_at, IngestionSource.id)
    ):
        rows = session.execute(
            select(SemanticMessageState.outcome, latest.c.observed_at)
            .select_from(latest)
            .outerjoin(
                SemanticMessageState, SemanticMessageState.revision_id == latest.c.revision_id
            )
            .where(latest.c.source_id == source.id, latest.c.rank == 1)
        ).all()
        counts = {
            key: sum(outcome == key for outcome, _ in rows)
            for key in ("CREATED", "UPDATED", "UNCHANGED", "REVIEW", "RETRY")
        }
        # RETRY is visible work, not a completed semantic decision.
        processed = sum(counts[key] for key in ("CREATED", "UPDATED", "UNCHANGED", "REVIEW"))
        run = session.scalar(
            select(IngestionRun)
            .where(IngestionRun.source_id == source.id)
            .order_by(IngestionRun.started_at.desc(), IngestionRun.id.desc())
            .limit(1)
        )
        activity = session.scalar(
            select(func.max(SemanticMessageState.updated_at)).where(
                SemanticMessageState.source_id == source.id
            )
        )
        oldest: datetime | None = None
        latest_observed: datetime | None = None
        for outcome, raw_observed in rows:
            observed = cast(datetime | None, raw_observed)
            if observed is not None:
                if latest_observed is None or observed > latest_observed:
                    latest_observed = observed
                if (outcome is None or outcome == "RETRY") and (
                    oldest is None or observed < oldest
                ):
                    oldest = observed
        # Routing coverage: collected threads versus exact operator-provisioned routes.
        # Both sides are evidence-derived; they cannot prove the mailbox is complete.
        awaiting_rows = session.execute(
            select(SourceRecord.created_at).where(
                SourceRecord.source_id == source.id,
                SourceRecord.record_type == "GMAIL_THREAD",
                ~exists(
                    select(SemanticTrackRoute.thread_record_id).where(
                        SemanticTrackRoute.thread_record_id == SourceRecord.id
                    )
                ),
            )
        ).all()
        threads_awaiting = len(awaiting_rows)
        threads_discovered = (
            session.scalar(
                select(func.count())
                .select_from(SourceRecord)
                .where(
                    SourceRecord.source_id == source.id,
                    SourceRecord.record_type == "GMAIL_THREAD",
                )
            )
            or 0
        )
        oldest_awaiting = min(
            (row[0] for row in awaiting_rows if row[0] is not None),
            default=None,
        )
        sources.append(
            SemanticSourceStatus(
                enabled=source.enabled,
                discovered=len(rows),
                processed=processed,
                created=counts["CREATED"],
                updated=counts["UPDATED"],
                unchanged=counts["UNCHANGED"],
                review=counts["REVIEW"],
                retry=counts["RETRY"],
                backlog=len(rows) - processed,
                oldest_backlog_at=oldest,
                latest_evidence_at=latest_observed,
                last_semantic_activity=activity,
                last_collector_run=(
                    SemanticCollectorRun(
                        mode=run.mode,
                        status=run.status,
                        started_at=run.started_at,
                        finished_at=run.finished_at,
                    )
                    if run
                    else None
                ),
                collector_age_seconds=(
                    max(0, int((now - run.finished_at).total_seconds()))
                    if run and run.finished_at and run.status == "SUCCEEDED"
                    else None
                ),
                threads_discovered=threads_discovered,
                threads_routed=threads_discovered - threads_awaiting,
                threads_awaiting_route=threads_awaiting,
                oldest_awaiting_route_at=oldest_awaiting,
            )
        )
    # Portfolio coverage: live business tracks versus exact operator-provisioned routes.
    # A route links a track either by business ``record_key`` or by the explicit
    # ``expected_coop_id`` adoption FK. Synthetic seed rows are never portfolio.
    live_tracks = (
        select(Coop.id, Coop.record_key).where(Coop.is_synthetic_seed.is_(False)).subquery()
    )
    tracks_total = session.scalar(select(func.count()).select_from(live_tracks)) or 0
    tracks_routed = (
        session.scalar(
            select(func.count())
            .select_from(live_tracks)
            .where(
                exists(
                    select(SemanticTrackRoute.thread_record_id).where(
                        (SemanticTrackRoute.record_key == live_tracks.c.record_key)
                        | (SemanticTrackRoute.expected_coop_id == live_tracks.c.id)
                    )
                )
            )
        )
        or 0
    )
    return SemanticStatus(
        as_of=now,
        coverage_verified=False,
        coverage_note=(
            "Message revisions are not business tracks; collection and processing counts "
            "cannot verify portfolio completeness or scheduler state. Routed and awaiting "
            "thread counts only compare already-collected threads with exact provisioned "
            "routes and cannot prove the mailbox was read in full. Routed track counts only "
            "compare live business tracks with exact operator-provisioned routes and cannot "
            "prove that every real cooperation was discovered, mapped or up to date."
        ),
        domain_records=session.scalar(select(func.count()).select_from(Coop)) or 0,
        tracks_total=tracks_total,
        tracks_routed=tracks_routed,
        tracks_awaiting_route=tracks_total - tracks_routed,
        sources=sources,
    )
