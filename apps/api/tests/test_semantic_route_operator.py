"""Operator surface: sanitized discovery, explicit map apply, fail-closed arming."""

from __future__ import annotations

import json
from pathlib import Path
from uuid import UUID

import pytest
from sqlalchemy import Engine, select
from sqlalchemy.orm import Session

from app.ingestion import gmail_route_provision
from app.ingestion.gmail_route_provision import (
    ARM_FLAG,
    RouteReview,
    _undecided_reason,
    apply_map,
    discover,
    main,
)
from app.ingestion.gmail_semantic import (
    ParsedMessage,
    SemanticDecision,
    SemanticInputError,
)
from app.manual_coop_import import _snapshot
from app.models import (
    Coop,
    CoopImportState,
    IngestionSource,
    ManualImportBatch,
    SemanticTrackRoute,
    SourceRecord,
)
from tests.test_gmail_semantic_delta import OWN, Client, sample, setup


def _source(engine: Engine) -> UUID:
    with Session(engine) as session:
        source = session.scalar(select(IngestionSource))
        assert source is not None
        return source.id


def _thread_id(engine: Engine) -> str:
    with Session(engine) as session:
        record = session.scalar(
            select(SourceRecord).where(SourceRecord.record_type == "GMAIL_THREAD")
        )
        assert record is not None
        return record.external_id


def _map_entry(thread_id: str, ref: str, **overrides: object) -> dict[str, object]:
    entry: dict[str, object] = {
        "thread_id": thread_id,
        "thread_ref": ref,
        "record_key": "exact-coop",
        "track_key": "exact-track",
        "title": "Synthetic cooperation",
    }
    entry.update(overrides)
    return entry


def test_discover_emits_only_hashed_refs_domains_and_verdicts(test_engine: Engine) -> None:
    setup(test_engine)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    rows = discover(test_engine, Client(sample()), OWN, source_id=_source(test_engine))
    assert len(rows) == 1
    row = rows[0]
    assert set(row) == {"thread_ref", "messages", "window", "domains", "verdicts", "provisionable"}
    assert row["messages"] == 2
    assert row["window"][0] <= row["window"][1]
    assert row["domains"] == ["example.invalid"]
    assert row["provisionable"] is True
    rendered = json.dumps(rows)
    assert "thread-1" not in rendered
    assert "offer-1" not in rendered
    assert "exact-coop" not in rendered
    assert "@" not in rendered.replace("example.invalid", "")


def test_discover_skips_routed_threads_and_bounds_limit(test_engine: Engine) -> None:
    setup(test_engine)
    assert discover(test_engine, Client(sample()), OWN, source_id=_source(test_engine)) == []
    with pytest.raises(RouteReview, match="limit_invalid"):
        discover(test_engine, Client(sample()), OWN, source_id=_source(test_engine), limit=0)
    with pytest.raises(RouteReview, match="limit_invalid"):
        discover(test_engine, Client(sample()), OWN, source_id=_source(test_engine), limit=10_000)


def test_apply_map_provisions_exact_route_then_replays_unchanged(test_engine: Engine) -> None:
    setup(test_engine, existing=True)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
        coop = session.scalar(select(Coop))
        assert coop is not None
        batch = ManualImportBatch(
            manifest_id="synthetic-batch", manifest_hash="b" * 64, schema_version=1, summary={}
        )
        session.add(batch)
        session.flush()
        session.add(
            CoopImportState(
                coop_id=coop.id,
                record_version=coop.version,
                content_hash="a" * 64,
                snapshot_hash=_snapshot(coop),
                batch_id=batch.id,
            )
        )
    thread_id = _thread_id(test_engine)
    rows = discover(test_engine, Client(sample()), OWN, source_id=_source(test_engine))
    assert len(rows) == 1
    entry = _map_entry(thread_id, rows[0]["thread_ref"], adopt_existing=True)
    assert apply_map(
        test_engine, Client(sample()), OWN, [entry], source_id=_source(test_engine)
    ) == {
        "PROVISIONED": 1,
        "UNCHANGED": 0,
        "REVIEW": 0,
    }
    assert apply_map(
        test_engine, Client(sample()), OWN, [entry], source_id=_source(test_engine)
    ) == {
        "PROVISIONED": 0,
        "UNCHANGED": 1,
        "REVIEW": 0,
    }
    with Session(test_engine) as session:
        route = session.scalar(select(SemanticTrackRoute))
        coop = session.scalar(select(Coop))
        assert route is not None and coop is not None
        assert route.record_key == "exact-coop"
        assert route.expected_coop_id == coop.id
        assert UUID(str(route.thread_record_id))


def test_apply_map_fails_closed_on_wrong_ref_or_unverified_adoption(test_engine: Engine) -> None:
    setup(test_engine, existing=True)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    thread_id = _thread_id(test_engine)
    rows = discover(test_engine, Client(sample()), OWN, source_id=_source(test_engine))
    assert len(rows) == 1
    ref = str(rows[0]["thread_ref"])
    bad_ref = "0" * 16
    assert apply_map(
        test_engine,
        Client(sample()),
        OWN,
        [_map_entry(thread_id, bad_ref)],
        source_id=_source(test_engine),
    ) == {
        "PROVISIONED": 0,
        "UNCHANGED": 0,
        "REVIEW": 1,
    }
    # Adoption of an absent or unverified target must never fall back to creation.
    unknown = _map_entry(thread_id, ref, record_key="absent-coop", adopt_existing=True)
    assert apply_map(
        test_engine, Client(sample()), OWN, [unknown], source_id=_source(test_engine)
    ) == {
        "PROVISIONED": 0,
        "UNCHANGED": 0,
        "REVIEW": 1,
    }
    with Session(test_engine) as session:
        assert list(session.scalars(select(SemanticTrackRoute))) == []


def test_apply_map_rejects_malformed_entries_without_writes(test_engine: Engine) -> None:
    setup(test_engine)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    thread_id = _thread_id(test_engine)
    for entry in (
        {"thread_ref": "a" * 16},
        _map_entry(thread_id, "a" * 16, track_key=""),
        _map_entry(thread_id, "a" * 16, title="   "),
    ):
        assert apply_map(
            test_engine, Client(sample()), OWN, [entry], source_id=_source(test_engine)
        ) == {
            "PROVISIONED": 0,
            "UNCHANGED": 0,
            "REVIEW": 1,
        }
    with Session(test_engine) as session:
        assert list(session.scalars(select(SemanticTrackRoute))) == []


def test_cli_refuses_without_arm_flag(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path: Path
) -> None:
    monkeypatch.delenv(ARM_FLAG, raising=False)
    monkeypatch.setattr("sys.argv", ["gmail_route_provision", "discover"])
    assert main() == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"outcome": "NOT_ARMED"}
    assert captured.out == ""
    monkeypatch.setenv(ARM_FLAG, "true")
    monkeypatch.setattr("sys.argv", ["gmail_route_provision", "apply"])
    assert main() == 2
    captured = capsys.readouterr()
    assert json.loads(captured.err) == {"outcome": "RETRY"}
    assert captured.out == ""
    assert not list(tmp_path.iterdir())


class _FailingClient(Client):
    def fetch(self, message_id: str):  # type: ignore[override]
        raise RuntimeError("provider unavailable")


def test_discover_fails_closed_when_every_thread_is_unfetchable(test_engine: Engine) -> None:
    setup(test_engine)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    with pytest.raises(RouteReview, match="provider_unavailable"):
        discover(test_engine, _FailingClient(sample()), OWN, source_id=_source(test_engine))


def _undecide(monkeypatch: pytest.MonkeyPatch, error: Exception) -> None:
    """Make the semantic gate reject every thread with one fixed sanitized error."""

    def _reject(*_args: object, **_kwargs: object) -> None:
        raise error

    monkeypatch.setattr(gmail_route_provision, "decide", _reject)


def test_undecided_reason_allowlists_only_fixed_semantic_codes() -> None:
    assert _undecided_reason(SemanticInputError("participants_invalid")) == "participants_invalid"
    assert _undecided_reason(SemanticInputError("term_invalid")) == "term_invalid"
    # An unexpected exception never exposes its class name or message.
    assert _undecided_reason(RuntimeError("provider said leak-marker-9f13")) == "unexpected"
    # A non-allowlisted argument must not become a reason either.
    assert _undecided_reason(SemanticInputError("Owner <a@b.example>")) == "unexpected"
    assert _undecided_reason(SemanticInputError()) == "unexpected"


def test_discover_reports_allowlisted_reason_instead_of_a_class_name(
    test_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    setup(test_engine)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    _undecide(monkeypatch, SemanticInputError("participants_invalid"))
    rows = discover(test_engine, Client(sample()), OWN, source_id=_source(test_engine))
    assert len(rows) == 1
    # Documented row shape is unchanged; only the reason becomes actionable.
    assert set(rows[0]) == {
        "thread_ref",
        "messages",
        "window",
        "domains",
        "verdicts",
        "provisionable",
    }
    assert rows[0]["verdicts"] == {"error": "participants_invalid"}
    assert rows[0]["provisionable"] is False
    # The operator sees the distribution without scanning rows.
    assert json.loads(capsys.readouterr().err) == {
        "discovery_undecided": {"participants_invalid": 1}
    }


def test_discover_never_leaks_an_unexpected_exception_message(
    test_engine: Engine, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    setup(test_engine)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    _undecide(monkeypatch, RuntimeError("provider said leak-marker-9f13 partner@example.invalid"))
    rows = discover(test_engine, Client(sample()), OWN, source_id=_source(test_engine))
    assert rows[0]["verdicts"] == {"error": "unexpected"}
    rendered = json.dumps(rows) + capsys.readouterr().err
    assert "provider said" not in rendered
    assert "leak-marker-9f13" not in rendered
    assert "RuntimeError" not in rendered
    assert "@" not in rendered


def test_apply_map_classifies_undecidable_entry_and_still_processes_the_rest(
    test_engine: Engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """One undecidable entry must be REVIEW, not an aborting top-level RETRY."""
    setup(test_engine)
    with Session(test_engine) as session, session.begin():
        session.delete(session.scalar(select(SemanticTrackRoute)))
    thread_id = _thread_id(test_engine)
    rows = discover(test_engine, Client(sample()), OWN, source_id=_source(test_engine))
    assert rows[0]["provisionable"] is True
    entry = _map_entry(thread_id, str(rows[0]["thread_ref"]))

    real = gmail_route_provision.decide
    calls = {"n": 0}

    def _flaky(
        messages: list[ParsedMessage], *, own_address: str, existing_track: bool
    ) -> SemanticDecision:
        calls["n"] += 1
        if calls["n"] == 1:
            raise SemanticInputError("term_invalid")
        return real(messages, own_address=own_address, existing_track=existing_track)

    monkeypatch.setattr(gmail_route_provision, "decide", _flaky)
    counts = apply_map(
        test_engine, Client(sample()), OWN, [entry, entry], source_id=_source(test_engine)
    )
    assert counts == {"PROVISIONED": 1, "UNCHANGED": 0, "REVIEW": 1}
    with Session(test_engine) as session:
        assert session.scalar(select(SemanticTrackRoute)) is not None
