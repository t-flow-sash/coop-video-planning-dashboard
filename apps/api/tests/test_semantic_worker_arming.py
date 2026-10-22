"""Runtime arming must precede any database or provider access."""

from __future__ import annotations

import json

import pytest

from app.ingestion import gmail_semantic_delta as delta
from app.ingestion import gmail_semantic_queue as queue


@pytest.mark.no_db
@pytest.mark.parametrize(
    ("worker", "argv"),
    [
        (queue, ["semantic-queue", "--max-batch", "1"]),
        (delta, ["semantic-delta", "--revision-id", "00000000-0000-0000-0000-000000000001"]),
    ],
)
@pytest.mark.parametrize("flag", [None, "false", "TRUE", "1", "true "])
def test_unarmed_cli_never_touches_db_or_provider(worker, argv, flag, monkeypatch, capsys):
    if flag is None:
        monkeypatch.delenv("SEMANTIC_WORKER_ARMED", raising=False)
    else:
        monkeypatch.setenv("SEMANTIC_WORKER_ARMED", flag)

    def forbidden(*args, **kwargs):
        raise AssertionError("unarmed worker touched DB or provider")

    monkeypatch.setattr(worker, "Session", forbidden)
    monkeypatch.setattr(worker.GmailConfig, "from_env", forbidden)
    monkeypatch.setattr("sys.argv", argv)
    assert worker.main() == 2
    output = capsys.readouterr()
    assert output.out == ""
    assert json.loads(output.err) == {"outcome": "NOT_ARMED"}
