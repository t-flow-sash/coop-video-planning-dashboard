from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import urllib.error
import urllib.parse
import urllib.request
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import (
    IngestionCheckpoint,
    IngestionItem,
    IngestionRun,
    IngestionSource,
    SourceRecord,
    SourceRecordObservation,
    SourceRecordRevision,
)

SOURCE_KEY = "gmail-open-coops-v1"
LOCK_KEY = 764239817
GMAIL_READONLY_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"
IDENTITY_SCOPES = frozenset(
    {
        "openid",
        "email",
        "profile",
        "https://www.googleapis.com/auth/userinfo.email",
        "https://www.googleapis.com/auth/userinfo.profile",
    }
)
DEFAULT_IMPORT_QUERY = (
    "in:anywhere -in:chats after:2026/01/01 "
    "{label:Legal to:legal@tsh-social.com from:legal@tsh-social.com}"
)
TOKEN_URL = "https://oauth2.googleapis.com/token"
TOKENINFO_URL = "https://oauth2.googleapis.com/tokeninfo"
GMAIL_API = "https://gmail.googleapis.com/gmail/v1/users/me"
MESSAGE_FIELDS = "id,threadId,historyId,internalDate,labelIds"
DECIMAL_INTEGER_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z")


class GmailError(RuntimeError):
    """Sanitized Gmail adapter failure."""


class GmailConfigError(GmailError):
    pass


class GmailAuthError(GmailError):
    pass


class GmailHttpError(GmailError):
    pass


class ImportBusyError(RuntimeError):
    pass


class ImportDisabledError(RuntimeError):
    pass


def canonical_hash(value: Any) -> str:
    payload = json.dumps(jsonable_encoder(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def acquire_import_lock(session: Session) -> None:
    locked = session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
    if not locked:
        raise ImportBusyError("another ingestion operation holds the advisory lock")


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


class _RejectRedirectHandler(urllib.request.HTTPRedirectHandler):
    def redirect_request(
        self,
        req: urllib.request.Request,
        fp: Any,
        code: int,
        msg: str,
        headers: Any,
        newurl: str,
    ) -> None:
        return None


class UrllibTransport:
    """Small JSON transport; urllib honors HTTP(S)_PROXY and NO_PROXY."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        request = urllib.request.Request(url, data=body, headers=headers or {}, method=method)
        try:
            with urllib.request.build_opener(_RejectRedirectHandler()).open(
                request, timeout=30
            ) as response:
                raw = response.read()
                status = response.status
        except urllib.error.HTTPError as error:
            raw = error.read()
            status = error.code
        except (OSError, urllib.error.URLError, TimeoutError) as error:
            raise GmailHttpError("transport_unavailable") from error
        if 300 <= status < 400:
            raise GmailHttpError("redirect_rejected")
        try:
            payload = json.loads(raw) if raw else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GmailHttpError("response_invalid") from error
        if not isinstance(payload, dict):
            raise GmailHttpError("response_invalid")
        return status, payload


@dataclass(frozen=True)
class GmailConfig:
    client_id: str
    client_secret: str
    refresh_token: str
    account: str

    @classmethod
    def from_env(cls) -> GmailConfig:
        names = (
            "GMAIL_CLIENT_ID",
            "GMAIL_CLIENT_SECRET",
            "GMAIL_REFRESH_TOKEN",
            "GMAIL_ACCOUNT",
        )
        values = {name: os.environ.get(name, "").strip() for name in names}
        if any(not value for value in values.values()):
            raise GmailConfigError("configuration_missing")
        return cls(
            values["GMAIL_CLIENT_ID"],
            values["GMAIL_CLIENT_SECRET"],
            values["GMAIL_REFRESH_TOKEN"],
            values["GMAIL_ACCOUNT"],
        )


@dataclass(frozen=True)
class AuthorizedSession:
    access_token: str
    history_id: str


@dataclass(frozen=True)
class MessageEvidence:
    message_id: str
    thread_id: str
    history_id: str
    internal_date: str
    label_ids: tuple[str, ...]

    def metadata(self) -> dict[str, Any]:
        return {
            "evidence_only": True,
            "history_id": self.history_id,
            "internal_date": self.internal_date,
            "label_ids": list(self.label_ids),
            "message_id": self.message_id,
            "thread_id": self.thread_id,
        }


@dataclass(frozen=True)
class FullSnapshot:
    start_history_id: str
    end_history_id: str
    messages: tuple[MessageEvidence, ...]


@dataclass(frozen=True)
class GmailRunReport:
    status: str
    pending_review: int


class GmailClient:
    def __init__(self, config: GmailConfig, transport: HttpTransport | None = None) -> None:
        self._config = config
        self._transport = transport or UrllibTransport()

    def _json(
        self,
        method: str,
        url: str,
        *,
        token: str | None = None,
        body: bytes | None = None,
        content_type: str | None = None,
    ) -> tuple[int, dict[str, Any]]:
        headers = {"Accept": "application/json"}
        if token is not None:
            headers["Authorization"] = f"Bearer {token}"
        if content_type is not None:
            headers["Content-Type"] = content_type
        try:
            return self._transport.request(method, url, headers=headers, body=body)
        except GmailError:
            raise
        except Exception as error:
            raise GmailHttpError("transport_unavailable") from error

    @staticmethod
    def _required_text(payload: dict[str, Any], key: str, error_code: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise GmailHttpError(error_code)
        return value

    @staticmethod
    def _required_decimal(payload: dict[str, Any], key: str, error_code: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or DECIMAL_INTEGER_PATTERN.fullmatch(value) is None:
            raise GmailHttpError(error_code)
        return value

    def authorize(self) -> AuthorizedSession:
        form = urllib.parse.urlencode(
            {
                "client_id": self._config.client_id,
                "client_secret": self._config.client_secret,
                "refresh_token": self._config.refresh_token,
                "grant_type": "refresh_token",
            }
        ).encode()
        status, token_payload = self._json(
            "POST",
            TOKEN_URL,
            body=form,
            content_type="application/x-www-form-urlencoded",
        )
        if status != 200:
            raise GmailHttpError("oauth_refresh_failed")
        access_token = self._required_text(token_payload, "access_token", "oauth_response_invalid")

        tokeninfo_url = f"{TOKENINFO_URL}?{urllib.parse.urlencode({'access_token': access_token})}"
        status, tokeninfo = self._json("GET", tokeninfo_url)
        if status != 200:
            raise GmailAuthError("tokeninfo_failed")
        raw_scope = self._required_text(tokeninfo, "scope", "scope_missing")
        scopes = set(raw_scope.split())
        gmail_scopes = {
            scope for scope in scopes if "gmail" in scope or scope == "https://mail.google.com/"
        }
        if gmail_scopes != {GMAIL_READONLY_SCOPE} or scopes - gmail_scopes - IDENTITY_SCOPES:
            raise GmailAuthError("scope_invalid")
        if "email" in tokeninfo:
            token_email = tokeninfo["email"]
            if (
                not isinstance(token_email, str)
                or not token_email
                or token_email != token_email.strip()
                or any(character.isspace() for character in token_email)
            ):
                raise GmailAuthError("account_invalid")
            if token_email.casefold() != self._config.account.casefold():
                raise GmailAuthError("account_mismatch")

        status, profile = self._json("GET", f"{GMAIL_API}/profile", token=access_token)
        if status != 200:
            raise GmailAuthError("profile_failed")
        profile_email = self._required_text(profile, "emailAddress", "profile_invalid")
        if profile_email.casefold() != self._config.account.casefold():
            raise GmailAuthError("account_mismatch")
        history_id = self._required_decimal(profile, "historyId", "profile_invalid")
        return AuthorizedSession(access_token, history_id)

    def _get(self, path: str, params: dict[str, str], token: str) -> tuple[int, dict[str, Any]]:
        query = urllib.parse.urlencode(params)
        return self._json("GET", f"{GMAIL_API}/{path}?{query}", token=token)

    def _message(self, message_id: str, token: str) -> MessageEvidence:
        safe_id = urllib.parse.quote(message_id, safe="")
        status, payload = self._get(
            f"messages/{safe_id}", {"format": "metadata", "fields": MESSAGE_FIELDS}, token
        )
        if status != 200:
            raise GmailHttpError("message_fetch_failed")
        labels = payload.get("labelIds", [])
        if not isinstance(labels, list) or not all(isinstance(label, str) for label in labels):
            raise GmailHttpError("message_response_invalid")
        response_id = self._required_text(payload, "id", "message_response_invalid")
        if response_id != message_id:
            raise GmailHttpError("message_response_invalid")
        return MessageEvidence(
            response_id,
            self._required_text(payload, "threadId", "message_response_invalid"),
            self._required_decimal(payload, "historyId", "message_response_invalid"),
            self._required_decimal(payload, "internalDate", "message_response_invalid"),
            tuple(sorted(set(labels))),
        )

    def full_snapshot(self, query: str) -> FullSnapshot:
        authorized = self.authorize()
        message_ids = self._query_message_ids(query, authorized.access_token)
        evidence = tuple(
            self._message(message_id, authorized.access_token) for message_id in sorted(message_ids)
        )
        status, end_profile = self._json(
            "GET", f"{GMAIL_API}/profile", token=authorized.access_token
        )
        if status != 200:
            raise GmailHttpError("profile_failed")
        end_history_id = self._required_decimal(end_profile, "historyId", "profile_invalid")
        end_email = self._required_text(end_profile, "emailAddress", "profile_invalid")
        if end_email.casefold() != self._config.account.casefold():
            raise GmailAuthError("account_mismatch")
        return FullSnapshot(authorized.history_id, end_history_id, evidence)

    def _query_message_ids(self, query: str, token: str) -> set[str]:
        message_ids: set[str] = set()
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        while True:
            params = {"q": query, "maxResults": "500", "fields": "messages/id,nextPageToken"}
            if page_token is not None:
                params["pageToken"] = page_token
            status, payload = self._get("messages", params, token)
            if status == 429 or status >= 500:
                raise GmailHttpError("messages_unavailable")
            if status != 200:
                raise GmailHttpError("messages_list_failed")
            messages = payload.get("messages", [])
            if not isinstance(messages, list):
                raise GmailHttpError("messages_response_invalid")
            for message in messages:
                if not isinstance(message, dict):
                    raise GmailHttpError("messages_response_invalid")
                message_ids.add(self._required_text(message, "id", "messages_response_invalid"))
            raw_next = payload.get("nextPageToken")
            if raw_next is None:
                break
            if (
                not isinstance(raw_next, str)
                or not raw_next.strip()
                or raw_next in seen_page_tokens
            ):
                raise GmailHttpError("messages_response_invalid")
            seen_page_tokens.add(raw_next)
            page_token = raw_next
        return message_ids


def _source(session: Session) -> IngestionSource:
    source = session.scalar(select(IngestionSource).where(IngestionSource.source_key == SOURCE_KEY))
    if source is None:
        source = IngestionSource(source_key=SOURCE_KEY, kind="GMAIL")
        session.add(source)
        session.flush()
    if not source.enabled:
        raise ImportDisabledError("ingestion source is disabled")
    return source


def _record(
    session: Session,
    source: IngestionSource,
    record_type: str,
    external_id: str,
    metadata: dict[str, Any],
) -> SourceRecordRevision:
    record = session.scalar(
        select(SourceRecord).where(
            SourceRecord.source_id == source.id,
            SourceRecord.record_type == record_type,
            SourceRecord.external_id == external_id,
        )
    )
    if record is None:
        record = SourceRecord(source_id=source.id, record_type=record_type, external_id=external_id)
        session.add(record)
        session.flush()
    content_hash = canonical_hash(metadata)
    revision = session.scalar(
        select(SourceRecordRevision).where(
            SourceRecordRevision.source_record_id == record.id,
            SourceRecordRevision.content_hash == content_hash,
        )
    )
    if revision is None:
        revision = SourceRecordRevision(
            source_id=source.id,
            source_record_id=record.id,
            content_hash=content_hash,
            metadata_json=metadata,
        )
        session.add(revision)
        session.flush()
    return revision


def _pending_item(
    session: Session,
    source: IngestionSource,
    run: IngestionRun,
    thread_id: str,
    payload: dict[str, Any],
) -> bool:
    candidate_key = f"gmail-thread-{thread_id}"[:160]
    payload_hash = canonical_hash(payload)
    exists = session.scalar(
        select(IngestionItem.id)
        .join(IngestionRun, IngestionRun.id == IngestionItem.run_id)
        .where(
            IngestionRun.source_id == source.id,
            IngestionItem.candidate_key == candidate_key,
            IngestionItem.payload_hash == payload_hash,
            IngestionItem.status == "PENDING_REVIEW",
        )
        .limit(1)
    )
    if exists is not None:
        return False
    session.add(
        IngestionItem(
            run_id=run.id,
            candidate_key=candidate_key,
            payload_hash=payload_hash,
            status="PENDING_REVIEW",
            reason_code="EVIDENCE_ONLY",
        )
    )
    return True


def persist_full_snapshot(
    session: Session,
    snapshot: FullSnapshot,
    *,
    query: str,
    source: IngestionSource | None = None,
) -> GmailRunReport:
    if (
        not isinstance(query, str)
        or not query.strip()
        or not isinstance(snapshot.start_history_id, str)
        or DECIMAL_INTEGER_PATTERN.fullmatch(snapshot.start_history_id) is None
        or not isinstance(snapshot.end_history_id, str)
        or DECIMAL_INTEGER_PATTERN.fullmatch(snapshot.end_history_id) is None
    ):
        raise GmailHttpError("snapshot_invalid")
    seen_message_ids: set[str] = set()
    for message in snapshot.messages:
        if (
            not isinstance(message, MessageEvidence)
            or not isinstance(message.message_id, str)
            or not message.message_id.strip()
            or not isinstance(message.thread_id, str)
            or not message.thread_id.strip()
            or not isinstance(message.history_id, str)
            or DECIMAL_INTEGER_PATTERN.fullmatch(message.history_id) is None
            or not isinstance(message.internal_date, str)
            or DECIMAL_INTEGER_PATTERN.fullmatch(message.internal_date) is None
            or not isinstance(message.label_ids, tuple)
            or any(not isinstance(label, str) or not label.strip() for label in message.label_ids)
            or message.message_id in seen_message_ids
        ):
            raise GmailHttpError("snapshot_invalid")
        seen_message_ids.add(message.message_id)
    if source is None:
        acquire_import_lock(session)
        source = _source(session)
    run = IngestionRun(source_id=source.id, mode="FULL", status="RUNNING", summary={})
    session.add(run)
    session.flush()
    by_thread: dict[str, list[MessageEvidence]] = defaultdict(list)
    for message in snapshot.messages:
        by_thread[message.thread_id].append(message)
        revision = _record(session, source, "GMAIL_MESSAGE", message.message_id, message.metadata())
        session.add(
            SourceRecordObservation(
                source_id=source.id,
                run_id=run.id,
                source_record_revision_id=revision.id,
            )
        )
    pending = 0
    for thread_id, messages in sorted(by_thread.items()):
        message_ids = sorted({message.message_id for message in messages})
        metadata = {
            "evidence_only": True,
            "message_ids": message_ids,
            "thread_id": thread_id,
        }
        revision = _record(session, source, "GMAIL_THREAD", thread_id, metadata)
        session.add(
            SourceRecordObservation(
                source_id=source.id,
                run_id=run.id,
                source_record_revision_id=revision.id,
            )
        )
        pending += _pending_item(session, source, run, thread_id, metadata)
    run.status = "SUCCEEDED"
    run.finished_at = datetime.now(UTC)
    run.summary = {
        "messages": len(snapshot.messages),
        "pending_review": pending,
        "threads": len(by_thread),
        "scan_changed_during_full": snapshot.start_history_id != snapshot.end_history_id,
    }
    checkpoint = session.get(IngestionCheckpoint, (source.id, "gmail_history_id"))
    query_hash = canonical_hash(query.strip())
    if checkpoint is None:
        checkpoint = IngestionCheckpoint(
            source_id=source.id,
            checkpoint_key="gmail_history_id",
            checkpoint_value=snapshot.start_history_id,
            query_hash=query_hash,
            succeeded_run_id=run.id,
        )
        session.add(checkpoint)
    else:
        checkpoint.checkpoint_value = snapshot.start_history_id
        checkpoint.query_hash = query_hash
        checkpoint.succeeded_run_id = run.id
        checkpoint.updated_at = datetime.now(UTC)
    session.flush()
    return GmailRunReport("SUCCEEDED", pending)


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingestion.gmail")
    parser.add_argument("command", choices=("full",))
    parser.parse_args(argv)
    try:
        gmail_config = GmailConfig.from_env()
        client = GmailClient(gmail_config)
        query = os.environ.get("GMAIL_IMPORT_QUERY", DEFAULT_IMPORT_QUERY).strip()
        if not query:
            raise GmailConfigError("query_missing")
        with SessionLocal.begin() as session:
            acquire_import_lock(session)
            source = _source(session)
            snapshot = client.full_snapshot(query)
            report = persist_full_snapshot(session, snapshot, query=query, source=source)
    except Exception:
        _print({"error": "gmail_import_failed", "ok": False, "status": "FAILED"})
        return 1
    ok = report.status == "SUCCEEDED"
    _print({"ok": ok, "pending_review": report.pending_review, "status": report.status})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
