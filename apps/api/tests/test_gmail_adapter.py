from __future__ import annotations

import json
import threading
import urllib.parse
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any

import pytest
from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from app.db import SessionLocal
from app.ingestion import gmail as gmail_module
from app.ingestion.gmail import (
    GMAIL_READONLY_SCOPE,
    FullSnapshot,
    GmailAuthError,
    GmailClient,
    GmailConfig,
    GmailHttpError,
    GmailRunReport,
    ImportBusyError,
    ImportDisabledError,
    MessageEvidence,
    UrllibTransport,
    canonical_hash,
    persist_full_snapshot,
)
from app.models import (
    AuditEvent,
    Coop,
    IngestionCheckpoint,
    IngestionItem,
    IngestionRun,
    IngestionSource,
    SourceRecord,
    SourceRecordObservation,
    SourceRecordRevision,
)


@dataclass
class FakeTransport:
    responses: dict[str, list[tuple[int, dict[str, Any]]]]
    calls: list[tuple[str, str, dict[str, str], bytes | None]] = field(default_factory=list)

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        safe_headers = dict(headers or {})
        self.calls.append((method, url, safe_headers, body))
        for marker, queue in self.responses.items():
            if marker in url and queue:
                return queue.pop(0)
        raise AssertionError(f"unexpected request: {method} {url}")


def config() -> GmailConfig:
    return GmailConfig("client-id", "client-secret", "refresh-token", "legal@example.com")


def authorized_responses(
    *, scope: str = GMAIL_READONLY_SCOPE, email: str = "legal@example.com"
) -> dict[str, list[tuple[int, dict[str, Any]]]]:
    return {
        "oauth2.googleapis.com/token": [
            (200, {"access_token": "access-token", "token_type": "Bearer"})
        ],
        "oauth2.googleapis.com/tokeninfo": [(200, {"scope": scope, "email": email})],
        "gmail.googleapis.com/gmail/v1/users/me/profile": [
            (200, {"emailAddress": email, "historyId": "100"})
        ],
    }


@pytest.mark.no_db
@pytest.mark.parametrize("cross_authority", [False, True], ids=["same-host", "gmail-to-oauth-host"])
def test_urllib_transport_rejects_redirects_without_forwarding_authorization(
    cross_authority: bool,
) -> None:
    received_authorization: list[str | None] = []

    class TargetHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            received_authorization.append(self.headers.get("Authorization"))
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(b"{}")

        def log_message(self, format: str, *args: object) -> None:
            pass

    target_server = ThreadingHTTPServer(("127.0.0.1", 0), TargetHandler)
    target_thread = threading.Thread(target=target_server.serve_forever)
    target_thread.start()
    target_url = f"http://127.0.0.1:{target_server.server_port}/oauth-target"

    class RedirectHandler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            location = target_url if cross_authority else "/same-host-target"
            if not cross_authority and self.path == "/same-host-target":
                received_authorization.append(self.headers.get("Authorization"))
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(b"{}")
                return
            self.send_response(302)
            self.send_header("Location", location)
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            pass

    redirect_server = ThreadingHTTPServer(("127.0.0.1", 0), RedirectHandler)
    redirect_thread = threading.Thread(target=redirect_server.serve_forever)
    redirect_thread.start()
    try:
        with pytest.raises(GmailHttpError, match="redirect_rejected"):
            UrllibTransport().request(
                "GET",
                f"http://127.0.0.1:{redirect_server.server_port}/gmail-source",
                headers={"Authorization": "Bearer secret-token"},
            )
        assert received_authorization == []
    finally:
        redirect_server.shutdown()
        target_server.shutdown()
        redirect_thread.join()
        target_thread.join()


@pytest.mark.no_db
def test_scope_and_account_are_validated_exactly_without_credentials_in_errors() -> None:
    transport = FakeTransport(
        authorized_responses(
            scope=f"openid email {GMAIL_READONLY_SCOPE}", email="other@example.com"
        )
    )
    client = GmailClient(config(), transport)

    with pytest.raises(GmailAuthError) as caught:
        client.authorize()

    rendered = str(caught.value)
    assert "other@example.com" not in rendered
    assert "client-secret" not in rendered
    assert "refresh-token" not in rendered


@pytest.mark.no_db
@pytest.mark.parametrize(
    "malformed_email",
    [
        12345,
        ["legal@example.com"],
        {"value": "legal@example.com"},
        True,
        None,
        "",
        "   ",
        " legal@example.com",
        "legal@example.com ",
        "legal @example.com",
    ],
)
def test_authorize_rejects_present_malformed_tokeninfo_email_before_gmail_calls(
    malformed_email: Any,
) -> None:
    responses = authorized_responses()
    responses["oauth2.googleapis.com/tokeninfo"] = [
        (200, {"scope": GMAIL_READONLY_SCOPE, "email": malformed_email})
    ]
    transport = FakeTransport(responses)

    with pytest.raises(GmailAuthError, match="account_invalid") as caught:
        GmailClient(config(), transport).authorize()

    assert len(transport.calls) == 2
    assert all("gmail.googleapis.com" not in url for _, url, _, _ in transport.calls)
    assert "legal@example.com" not in str(caught.value)


@pytest.mark.no_db
def test_authorize_allows_missing_tokeninfo_email_and_binds_via_gmail_profile() -> None:
    responses = authorized_responses()
    responses["oauth2.googleapis.com/tokeninfo"] = [(200, {"scope": GMAIL_READONLY_SCOPE})]

    authorized = GmailClient(config(), FakeTransport(responses)).authorize()

    assert authorized.history_id == "100"


@pytest.mark.no_db
def test_authorize_accepts_google_tokeninfo_identity_scope_urls() -> None:
    scope = f"openid https://www.googleapis.com/auth/userinfo.email {GMAIL_READONLY_SCOPE}"
    client = GmailClient(config(), FakeTransport(authorized_responses(scope=scope)))

    authorized = client.authorize()

    assert authorized.history_id == "100"


@pytest.mark.no_db
@pytest.mark.parametrize(
    "scope",
    [
        "https://www.googleapis.com/auth/gmail.modify",
        f"{GMAIL_READONLY_SCOPE} https://www.googleapis.com/auth/gmail.send",
        "https://mail.google.com/",
        f"{GMAIL_READONLY_SCOPE} https://www.googleapis.com/auth/drive.readonly",
    ],
)
def test_authorize_rejects_broad_or_unrelated_scopes(scope: str) -> None:
    client = GmailClient(config(), FakeTransport(authorized_responses(scope=scope)))

    with pytest.raises(GmailAuthError, match="scope_invalid"):
        client.authorize()


@pytest.mark.no_db
def test_full_scan_paginates_deduplicates_and_uses_metadata_fields_only() -> None:
    responses = authorized_responses()
    responses["gmail.googleapis.com/gmail/v1/users/me/profile"].append(
        (200, {"emailAddress": "legal@example.com", "historyId": "105"})
    )
    responses["users/me/messages?"] = [
        (200, {"messages": [{"id": "m1"}, {"id": "m2"}], "nextPageToken": "next"}),
        (200, {"messages": [{"id": "m2"}, {"id": "m3"}]}),
    ]
    responses["users/me/messages/m1?"] = [
        (
            200,
            {
                "id": "m1",
                "threadId": "t1",
                "historyId": "101",
                "internalDate": "1",
                "labelIds": ["INBOX"],
            },
        )
    ]
    responses["users/me/messages/m2?"] = [
        (
            200,
            {
                "id": "m2",
                "threadId": "t1",
                "historyId": "102",
                "internalDate": "2",
                "labelIds": ["SENT"],
            },
        )
    ]
    responses["users/me/messages/m3?"] = [
        (
            200,
            {"id": "m3", "threadId": "t2", "historyId": "103", "internalDate": "3", "labelIds": []},
        )
    ]
    transport = FakeTransport(responses)
    client = GmailClient(config(), transport)

    exact_query = "in:anywhere  -in:chats after:2026/01/01 {label:Legal}"
    snapshot = client.full_snapshot(exact_query)

    assert snapshot.start_history_id == "100"
    assert snapshot.end_history_id == "105"
    assert [message.message_id for message in snapshot.messages] == ["m1", "m2", "m3"]
    list_urls = [url for _, url, _, _ in transport.calls if "users/me/messages?" in url]
    assert len(list_urls) == 2
    assert "pageToken=next" in list_urls[1]
    assert all(
        urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)["q"] == [exact_query]
        for url in list_urls
    )
    detail_urls = [url for _, url, _, _ in transport.calls if "/messages/m" in url]
    assert all("format=metadata" in url for url in detail_urls)
    assert all(
        "fields=id%2CthreadId%2ChistoryId%2CinternalDate%2ClabelIds" in url for url in detail_urls
    )
    assert all("metadataHeaders" not in url for url in detail_urls)


@pytest.mark.no_db
@pytest.mark.parametrize(
    "page",
    [
        {"messages": "invalid"},
        {"messages": ["invalid"]},
        {"messages": [{"id": ""}]},
        {"messages": [{"id": "   "}]},
        {"messages": [], "nextPageToken": ""},
    ],
)
def test_full_scan_rejects_malformed_list_pages(page: dict[str, Any]) -> None:
    responses = authorized_responses()
    responses["users/me/messages?"] = [(200, page)]

    with pytest.raises(GmailHttpError, match="messages_response_invalid"):
        GmailClient(config(), FakeTransport(responses)).full_snapshot("exact query")


@pytest.mark.no_db
def test_full_scan_rejects_repeated_page_cursor() -> None:
    responses = authorized_responses()
    responses["users/me/messages?"] = [
        (200, {"messages": [], "nextPageToken": "same"}),
        (200, {"messages": [], "nextPageToken": "same"}),
    ]

    with pytest.raises(GmailHttpError, match="messages_response_invalid"):
        GmailClient(config(), FakeTransport(responses)).full_snapshot("exact query")


@pytest.mark.no_db
def test_full_scan_rejects_message_response_for_a_different_id() -> None:
    responses = authorized_responses()
    responses["users/me/messages?"] = [(200, {"messages": [{"id": "expected"}]})]
    responses["users/me/messages/expected?"] = [
        (
            200,
            {
                "id": "other",
                "threadId": "thread",
                "historyId": "101",
                "internalDate": "1",
                "labelIds": [],
            },
        )
    ]

    with pytest.raises(GmailHttpError, match="message_response_invalid"):
        GmailClient(config(), FakeTransport(responses)).full_snapshot("exact query")


@pytest.mark.no_db
@pytest.mark.parametrize("field", ["historyId", "internalDate"])
@pytest.mark.parametrize("invalid", ["", "-1", "+1", "1.0", " 1", "1 ", True, 1])
def test_full_scan_rejects_noncanonical_numeric_message_metadata(field: str, invalid: Any) -> None:
    responses = authorized_responses()
    responses["gmail.googleapis.com/gmail/v1/users/me/profile"].append(
        (200, {"emailAddress": "legal@example.com", "historyId": "105"})
    )
    responses["users/me/messages?"] = [(200, {"messages": [{"id": "m1"}]})]
    message: dict[str, Any] = {
        "id": "m1",
        "threadId": "t1",
        "historyId": "0",
        "internalDate": "9999999999999999999",
        "labelIds": [],
    }
    message[field] = invalid
    responses["users/me/messages/m1?"] = [(200, message)]

    with pytest.raises(GmailHttpError, match="message_response_invalid"):
        GmailClient(config(), FakeTransport(responses)).full_snapshot("exact query")


@pytest.mark.no_db
def test_full_scan_accepts_canonical_zero_and_large_numeric_metadata() -> None:
    responses = authorized_responses()
    responses["gmail.googleapis.com/gmail/v1/users/me/profile"] = [
        (200, {"emailAddress": "legal@example.com", "historyId": "0"}),
        (200, {"emailAddress": "legal@example.com", "historyId": "9999999999999999999"}),
    ]
    responses["users/me/messages?"] = [(200, {"messages": [{"id": "m1"}]})]
    responses["users/me/messages/m1?"] = [
        (
            200,
            {
                "id": "m1",
                "threadId": "t1",
                "historyId": "0",
                "internalDate": "9999999999999999999",
                "labelIds": [],
            },
        )
    ]

    snapshot = GmailClient(config(), FakeTransport(responses)).full_snapshot("exact query")

    assert snapshot.start_history_id == "0"
    assert snapshot.end_history_id == "9999999999999999999"


@pytest.mark.no_db
@pytest.mark.parametrize("invalid", ["", "-1", "+1", "1.0", " 1", "1 ", True, 1])
def test_authorize_rejects_noncanonical_profile_history_id(invalid: Any) -> None:
    responses = authorized_responses()
    responses["gmail.googleapis.com/gmail/v1/users/me/profile"] = [
        (200, {"emailAddress": "legal@example.com", "historyId": invalid})
    ]

    with pytest.raises(GmailHttpError, match="profile_invalid"):
        GmailClient(config(), FakeTransport(responses)).authorize()


def test_full_persistence_rejects_invalid_snapshot_without_partial_writes() -> None:
    snapshot = FullSnapshot(
        start_history_id="100",
        end_history_id="105",
        messages=(MessageEvidence("", "thread", "101", "1", ("INBOX",)),),
    )

    with pytest.raises(GmailHttpError, match="snapshot_invalid"), SessionLocal.begin() as session:
        persist_full_snapshot(session, snapshot, query="exact query")

    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(IngestionRun)) == 0
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 0
        assert session.scalar(select(func.count()).select_from(IngestionCheckpoint)) == 0


@pytest.mark.parametrize(
    "snapshot",
    [
        FullSnapshot("-1", "105", ()),
        FullSnapshot("100", " 105", ()),
        FullSnapshot("100", "105", (MessageEvidence("m", "t", "+1", "1", ()),)),
        FullSnapshot("100", "105", (MessageEvidence("m", "t", "1", "1.0", ()),)),
    ],
)
def test_full_persistence_rejects_noncanonical_numeric_metadata_without_writes(
    snapshot: FullSnapshot,
) -> None:
    with pytest.raises(GmailHttpError, match="snapshot_invalid"), SessionLocal.begin() as session:
        persist_full_snapshot(session, snapshot, query="exact query")

    with SessionLocal() as session:
        assert session.scalar(select(func.count()).select_from(IngestionRun)) == 0
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 0
        assert session.scalar(select(func.count()).select_from(IngestionCheckpoint)) == 0


@pytest.mark.no_db
def test_main_acquires_lock_and_checks_source_before_first_provider_call(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    events: list[str] = []
    source = object()

    class FakeClient:
        def __init__(self, config: GmailConfig) -> None:
            pass

        def full_snapshot(self, query: str) -> FullSnapshot:
            assert events == ["lock", "source"]
            events.append("network")
            return FullSnapshot("100", "100", ())

    class Transaction:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            pass

    monkeypatch.setattr(gmail_module.GmailConfig, "from_env", lambda: config())
    monkeypatch.setattr(gmail_module, "GmailClient", FakeClient)
    monkeypatch.setattr(gmail_module.SessionLocal, "begin", lambda: Transaction())
    monkeypatch.setattr(gmail_module, "acquire_import_lock", lambda session: events.append("lock"))
    monkeypatch.setattr(gmail_module, "_source", lambda session: events.append("source") or source)
    monkeypatch.setattr(
        gmail_module,
        "persist_full_snapshot",
        lambda session, snapshot, *, query, source=None: (
            events.append("persist") or GmailRunReport("SUCCEEDED", 0)
        ),
    )

    assert gmail_module.main(["full"]) == 0
    assert events == ["lock", "source", "network", "persist"]


@pytest.mark.no_db
def test_main_never_scans_when_another_full_run_holds_the_lock(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    class FakeClient:
        def __init__(self, config: GmailConfig) -> None:
            pass

        def full_snapshot(self, query: str) -> FullSnapshot:
            nonlocal provider_calls
            provider_calls += 1
            return FullSnapshot("200", "200", ())

    class Transaction:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            pass

    def reject_lock(session: object) -> None:
        raise ImportBusyError("busy")

    monkeypatch.setattr(gmail_module.GmailConfig, "from_env", lambda: config())
    monkeypatch.setattr(gmail_module, "GmailClient", FakeClient)
    monkeypatch.setattr(gmail_module.SessionLocal, "begin", lambda: Transaction())
    monkeypatch.setattr(gmail_module, "acquire_import_lock", reject_lock)

    assert gmail_module.main(["full"]) == 1
    assert provider_calls == 0


@pytest.mark.no_db
def test_main_never_scans_when_the_source_kill_switch_is_disabled(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    provider_calls = 0

    class FakeClient:
        def __init__(self, config: GmailConfig) -> None:
            pass

        def full_snapshot(self, query: str) -> FullSnapshot:
            nonlocal provider_calls
            provider_calls += 1
            return FullSnapshot("200", "200", ())

    class Transaction:
        def __enter__(self) -> object:
            return object()

        def __exit__(self, *args: object) -> None:
            pass

    def disabled_source(session: object) -> object:
        raise ImportDisabledError("disabled")

    monkeypatch.setattr(gmail_module.GmailConfig, "from_env", lambda: config())
    monkeypatch.setattr(gmail_module, "GmailClient", FakeClient)
    monkeypatch.setattr(gmail_module.SessionLocal, "begin", lambda: Transaction())
    monkeypatch.setattr(gmail_module, "acquire_import_lock", lambda session: None)
    monkeypatch.setattr(gmail_module, "_source", disabled_source)

    assert gmail_module.main(["full"]) == 1
    assert provider_calls == 0


def test_full_persistence_binds_checkpoint_to_canonical_query_hash() -> None:
    snapshot = FullSnapshot(
        start_history_id="100",
        end_history_id="100",
        messages=(),
    )

    with SessionLocal.begin() as session:
        persist_full_snapshot(session, snapshot, query="  exact query  ")

    with SessionLocal() as session:
        checkpoint = session.scalar(select(IngestionCheckpoint))
        assert checkpoint is not None
        assert checkpoint.query_hash == canonical_hash("exact query")


def test_checkpoint_cannot_reference_a_run_owned_by_another_source() -> None:
    with pytest.raises(IntegrityError), SessionLocal.begin() as session:
        first = IngestionSource(source_key="first", kind="GMAIL")
        second = IngestionSource(source_key="second", kind="GMAIL")
        session.add_all((first, second))
        session.flush()
        run = IngestionRun(source_id=first.id, mode="FULL", status="SUCCEEDED", summary={})
        session.add(run)
        session.flush()
        session.add(
            IngestionCheckpoint(
                source_id=second.id,
                checkpoint_key="gmail_history_id",
                checkpoint_value="100",
                query_hash=canonical_hash("query"),
                succeeded_run_id=run.id,
            )
        )
        session.flush()


def test_full_persistence_is_evidence_only_advances_to_h0_and_replay_is_idempotent() -> None:
    snapshot = FullSnapshot(
        start_history_id="100",
        end_history_id="105",
        messages=(
            MessageEvidence("m1", "t1", "101", "1", ("INBOX",)),
            MessageEvidence("m2", "t1", "102", "2", ("SENT",)),
        ),
    )

    with SessionLocal.begin() as session:
        first = persist_full_snapshot(session, snapshot, query="query")
    with SessionLocal.begin() as session:
        second = persist_full_snapshot(session, snapshot, query="query")

    assert first.pending_review == 1
    assert second.pending_review == 0
    with SessionLocal() as session:
        checkpoint = session.scalar(select(IngestionCheckpoint))
        assert checkpoint is not None and checkpoint.checkpoint_value == "100"
        assert session.scalar(select(func.count()).select_from(SourceRecord)) == 3
        assert session.scalar(select(func.count()).select_from(SourceRecordRevision)) == 3
        assert session.scalar(select(func.count()).select_from(SourceRecordObservation)) == 6
        assert session.scalar(select(func.count()).select_from(IngestionItem)) == 1
        assert session.scalar(select(func.count()).select_from(IngestionRun)) == 2
        assert session.scalar(select(func.count()).select_from(Coop)) == 0
        assert session.scalar(select(func.count()).select_from(AuditEvent)) == 0


@pytest.mark.no_db
def test_http_errors_never_render_response_or_request_secrets() -> None:
    responses = authorized_responses()
    responses["oauth2.googleapis.com/token"] = [
        (500, {"error": "client-secret refresh-token private-body"})
    ]

    with pytest.raises(GmailHttpError) as caught:
        GmailClient(config(), FakeTransport(responses)).authorize()

    serialized = json.dumps({"error": str(caught.value)})
    assert "client-secret" not in serialized
    assert "refresh-token" not in serialized
    assert "private-body" not in serialized
