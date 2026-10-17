from __future__ import annotations

import argparse
import hashlib
import hmac
import json
import os
import re
import sys
import urllib.parse
import uuid
from collections import defaultdict
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any, Protocol

import httpx
from fastapi.encoders import jsonable_encoder
from sqlalchemy import select, text
from sqlalchemy.engine import make_url
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
EXPECTED_QUERY_SHA256 = "e362825c8b3be3c500d6da685c7e0b2043641fa2136fd801acf57395154f967d"
EXPECTED_ACCOUNT_SHA256 = "d260486537baa3f006b0e0e3168df3ba58b784b56f0846e150690af7457b14bb"
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
HISTORY_FIELDS = (
    "history(id,messages/id,messagesAdded/message/id,messagesDeleted/message/id,"
    "labelsAdded/message/id,labelsRemoved/message/id),nextPageToken,historyId"
)
DECIMAL_INTEGER_PATTERN = re.compile(r"(?:0|[1-9][0-9]*)\Z")
SANITIZED_ERROR_CODES = frozenset(
    {
        "account_contract_invalid",
        "account_invalid",
        "account_mismatch",
        "bootstrap_consumed",
        "configuration_missing",
        "execution_not_armed",
        "full_required",
        "history_list_failed",
        "history_response_invalid",
        "history_unavailable",
        "checkpoint_mismatch",
        "import_busy",
        "import_disabled",
        "message_fetch_failed",
        "message_response_invalid",
        "messages_list_failed",
        "messages_response_invalid",
        "messages_unavailable",
        "oauth_refresh_failed",
        "oauth_response_invalid",
        "pilot_consumed",
        "profile_failed",
        "profile_invalid",
        "query_contract_invalid",
        "redirect_rejected",
        "response_invalid",
        "scope_invalid",
        "scope_missing",
        "snapshot_invalid",
        "tokeninfo_failed",
        "transport_unavailable",
        "unexpected_error",
    }
)
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")


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


class PilotConsumedError(RuntimeError):
    pass


def canonical_hash(value: Any) -> str:
    payload = json.dumps(jsonable_encoder(value), sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode()).hexdigest()


def account_fingerprint(account: str) -> str:
    return hashlib.sha256(account.strip().casefold().encode()).hexdigest()


def execution_query_from_env() -> str:
    """Require an explicit one-shot arm signal and an immutable query contract."""
    if os.environ.get("GMAIL_IMPORT_ARMED") != "true":
        raise GmailConfigError("execution_not_armed")
    query = os.environ.get("GMAIL_IMPORT_QUERY", DEFAULT_IMPORT_QUERY).strip()
    expected_hash = os.environ.get("GMAIL_IMPORT_QUERY_SHA256", "")
    if (
        not query
        or expected_hash != EXPECTED_QUERY_SHA256
        or not hmac.compare_digest(canonical_hash(query), expected_hash)
    ):
        raise GmailConfigError("query_contract_invalid")
    return query


def acquire_import_lock(session: Session) -> None:
    locked = session.scalar(text("SELECT pg_try_advisory_xact_lock(:key)"), {"key": LOCK_KEY})
    if not locked:
        raise ImportBusyError("another ingestion operation holds the advisory lock")


def attest_collector_database(session: Session) -> None:
    raw_url = os.getenv("DATABASE_URL", "")
    try:
        url = make_url(raw_url)
    except Exception as error:  # SQLAlchemy exposes multiple URL parse errors
        raise GmailError("collector database identity invalid") from error
    if url.username != "coop_evidence_collector" or not url.database or not url.host:
        raise GmailError("collector database identity invalid")
    row = session.execute(
        text(
            "SELECT session_user, current_user, current_database(), "
            "inet_server_addr()::text, inet_server_port()"
        )
    ).one()
    if (
        len(row) != 5
        or row[0] != "coop_evidence_collector"
        or row[1] != "coop_evidence_collector"
        or row[2] != url.database
        or not isinstance(row[3], str)
        or not row[3]
        or row[4] != (url.port or 5432)
    ):
        raise GmailError("collector database identity invalid")


class HttpTransport(Protocol):
    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]: ...


class HttpxTransport:
    """JSON transport with an explicit HTTP/1.1-capable HTTPS proxy tunnel."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        try:
            with httpx.Client(timeout=30, follow_redirects=False, trust_env=True) as client:
                response = client.request(method, url, headers=headers, content=body)
        except httpx.HTTPError as error:
            raise GmailHttpError("transport_unavailable") from error
        if 300 <= response.status_code < 400:
            raise GmailHttpError("redirect_rejected")
        try:
            payload = response.json() if response.content else {}
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise GmailHttpError("response_invalid") from error
        if not isinstance(payload, dict):
            raise GmailHttpError("response_invalid")
        return response.status_code, payload


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
        expected_account_hash = os.environ.get("GMAIL_ACCOUNT_SHA256", "")
        if expected_account_hash != EXPECTED_ACCOUNT_SHA256 or not hmac.compare_digest(
            account_fingerprint(values["GMAIL_ACCOUNT"]), expected_account_hash
        ):
            raise GmailConfigError("account_contract_invalid")
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
class DeltaSnapshot:
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
        self._transport = transport or HttpxTransport()

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
        # The production token is deliberately reused from the Influencer agent and
        # therefore has additional grants. Runtime capability stays read-only: this
        # adapter only implements GETs and still requires gmail.readonly explicitly.
        if GMAIL_READONLY_SCOPE not in scopes:
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

    @classmethod
    def _history_message_ids(cls, payload: dict[str, Any]) -> set[str]:
        history = payload.get("history", [])
        if not isinstance(history, list):
            raise GmailHttpError("history_response_invalid")
        message_ids: set[str] = set()
        for entry in history:
            if not isinstance(entry, dict):
                raise GmailHttpError("history_response_invalid")
            cls._required_decimal(entry, "id", "history_response_invalid")
            direct_messages = entry.get("messages", [])
            if not isinstance(direct_messages, list):
                raise GmailHttpError("history_response_invalid")
            for message in direct_messages:
                if not isinstance(message, dict):
                    raise GmailHttpError("history_response_invalid")
                message_ids.add(cls._required_text(message, "id", "history_response_invalid"))
            for event_key in (
                "messagesAdded",
                "messagesDeleted",
                "labelsAdded",
                "labelsRemoved",
            ):
                events = entry.get(event_key, [])
                if not isinstance(events, list):
                    raise GmailHttpError("history_response_invalid")
                for event in events:
                    if not isinstance(event, dict) or not isinstance(event.get("message"), dict):
                        raise GmailHttpError("history_response_invalid")
                    message_ids.add(
                        cls._required_text(
                            event["message"], "id", "history_response_invalid"
                        )
                    )
        return message_ids

    def delta_snapshot(self, start_history_id: str, query: str) -> DeltaSnapshot:
        if DECIMAL_INTEGER_PATTERN.fullmatch(start_history_id) is None:
            raise GmailHttpError("history_response_invalid")
        authorized = self.authorize()
        changed_message_ids: set[str] = set()
        page_token: str | None = None
        seen_page_tokens: set[str] = set()
        end_history_id = start_history_id
        while True:
            params = {
                "startHistoryId": start_history_id,
                "maxResults": "500",
                "fields": HISTORY_FIELDS,
            }
            if page_token is not None:
                params["pageToken"] = page_token
            status, payload = self._get("history", params, authorized.access_token)
            if status == 404:
                raise GmailHttpError("full_required")
            if status == 429 or status >= 500:
                raise GmailHttpError("history_unavailable")
            if status != 200:
                raise GmailHttpError("history_list_failed")
            changed_message_ids.update(self._history_message_ids(payload))
            end_history_id = self._required_decimal(
                payload, "historyId", "history_response_invalid"
            )
            raw_next = payload.get("nextPageToken")
            if raw_next is None:
                break
            if (
                not isinstance(raw_next, str)
                or not raw_next.strip()
                or raw_next in seen_page_tokens
            ):
                raise GmailHttpError("history_response_invalid")
            seen_page_tokens.add(raw_next)
            page_token = raw_next
        matching_ids = (
            self._query_message_ids(query, authorized.access_token)
            if changed_message_ids
            else set()
        )
        evidence = tuple(
            self._message(message_id, authorized.access_token)
            for message_id in sorted(changed_message_ids & matching_ids)
        )
        return DeltaSnapshot(start_history_id, end_history_id, evidence)

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
        raise ImportDisabledError("ingestion source is not provisioned")
    if not source.enabled:
        raise ImportDisabledError("ingestion source is disabled")
    return source


PILOT_SLOT = "gmail-full-pilot-v1"
PILOT_RUN_ID = uuid.uuid5(uuid.NAMESPACE_URL, f"coop-video-planning:{PILOT_SLOT}")


def claim_pilot_slot(session: Session, source: IngestionSource) -> uuid.UUID:
    """Persist the one-shot claim before any OAuth or Gmail network access."""
    if session.get(IngestionRun, PILOT_RUN_ID) is not None:
        raise PilotConsumedError("gmail pilot slot already consumed")
    run = IngestionRun(
        id=PILOT_RUN_ID,
        source_id=source.id,
        mode="FULL",
        status="RUNNING",
        summary={"pilot_slot": PILOT_SLOT},
    )
    session.add(run)
    session.flush()
    return run.id


def load_claimed_pilot_run(
    session: Session, source: IngestionSource, run_id: uuid.UUID
) -> IngestionRun:
    run = session.get(IngestionRun, run_id)
    if run is None or run.source_id != source.id or run.status != "RUNNING":
        raise PilotConsumedError("gmail pilot claim is not executable")
    return run


def mark_claimed_pilot_failed(
    session: Session,
    run_id: uuid.UUID,
    error_code: str,
) -> None:
    sanitized_code = error_code if error_code in SANITIZED_ERROR_CODES else "unexpected_error"
    run = session.scalar(
        select(IngestionRun)
        .join(IngestionSource, IngestionSource.id == IngestionRun.source_id)
        .where(
            IngestionRun.id == run_id,
            IngestionSource.source_key == SOURCE_KEY,
        )
    )
    if run is None:
        raise PilotConsumedError("gmail pilot claim does not exist")
    if run.status == "FAILED" and run.error_code == sanitized_code:
        return
    if run.status != "RUNNING":
        raise PilotConsumedError("gmail pilot claim is not terminalizable")
    run.status = "FAILED"
    run.error_code = sanitized_code
    run.finished_at = datetime.now(UTC)
    session.flush()


def terminalize_claim_failure(run_id: uuid.UUID, error_code: str) -> bool:
    """Best-effort bounded retry that does not depend on source enablement or the import lock."""
    for _ in range(3):
        try:
            with SessionLocal.begin() as session:
                attest_collector_database(session)
                mark_claimed_pilot_failed(session, run_id, error_code)
        except Exception:
            continue
        return True
    return False


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
    run: IngestionRun | None = None,
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
    if run is None:
        # Operational runs are built inside one transaction under the shared
        # advisory lock. They become visible only at commit, already terminal.
        # The separately supplied RUNNING run is reserved for the one-shot pilot.
        run = IngestionRun(source_id=source.id, mode="FULL", status="SUCCEEDED", summary={})
        session.add(run)
        session.flush()
    elif run.source_id != source.id or run.status != "RUNNING":
        raise PilotConsumedError("gmail pilot claim is not executable")
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


def persist_delta_snapshot(
    session: Session,
    snapshot: DeltaSnapshot,
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
    query_hash = canonical_hash(query.strip())
    checkpoint = session.get(IngestionCheckpoint, (source.id, "gmail_history_id"))
    if checkpoint is None:
        raise GmailHttpError("full_required")
    if checkpoint.query_hash != query_hash:
        raise GmailConfigError("query_contract_invalid")
    if checkpoint.checkpoint_value != snapshot.start_history_id:
        raise GmailHttpError("checkpoint_mismatch")
    run = IngestionRun(source_id=source.id, mode="DELTA", status="SUCCEEDED", summary={})
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
        metadata = {
            "evidence_only": True,
            "message_ids": sorted({message.message_id for message in messages}),
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
        "end_history_id": snapshot.end_history_id,
        "messages": len(snapshot.messages),
        "pending_review": pending,
        "start_history_id": snapshot.start_history_id,
        "threads": len(by_thread),
    }
    checkpoint.checkpoint_value = snapshot.end_history_id
    checkpoint.succeeded_run_id = run.id
    checkpoint.updated_at = datetime.now(UTC)
    session.flush()
    return GmailRunReport("SUCCEEDED", pending)


def _print(value: dict[str, Any]) -> None:
    print(json.dumps(value, sort_keys=True, separators=(",", ":")))


def sanitized_error_code(error: Exception) -> str:
    if isinstance(error, GmailError) and str(error) in SANITIZED_ERROR_CODES:
        return str(error)
    if isinstance(error, ImportBusyError):
        return "import_busy"
    if isinstance(error, ImportDisabledError):
        return "import_disabled"
    if isinstance(error, PilotConsumedError):
        return "pilot_consumed"
    return "unexpected_error"


def _operational_report(command: str, client: GmailClient, query: str) -> GmailRunReport:
    with SessionLocal.begin() as session:
        attest_collector_database(session)
        acquire_import_lock(session)
        source = _source(session)
        checkpoint = session.get(IngestionCheckpoint, (source.id, "gmail_history_id"))
        query_hash = canonical_hash(query.strip())
        if command == "bootstrap":
            if checkpoint is not None:
                raise GmailConfigError("bootstrap_consumed")
            full_snapshot = client.full_snapshot(query)
            return persist_full_snapshot(session, full_snapshot, query=query, source=source)
        if checkpoint is None:
            raise GmailHttpError("full_required")
        if checkpoint.query_hash != query_hash:
            raise GmailConfigError("query_contract_invalid")
        delta_snapshot = client.delta_snapshot(checkpoint.checkpoint_value, query)
        return persist_delta_snapshot(session, delta_snapshot, query=query, source=source)


def record_operational_failure(mode: str, error_code: str) -> bool:
    sanitized_code = error_code if error_code in SANITIZED_ERROR_CODES else "unexpected_error"
    try:
        with SessionLocal.begin() as session:
            attest_collector_database(session)
            source = session.scalar(
                select(IngestionSource).where(IngestionSource.source_key == SOURCE_KEY)
            )
            if source is None:
                return False
            now = datetime.now(UTC)
            session.add(
                IngestionRun(
                    source_id=source.id,
                    mode=mode,
                    status="FAILED",
                    summary={"operation": mode.casefold()},
                    error_code=sanitized_code,
                    finished_at=now,
                )
            )
        return True
    except Exception:
        return False


def _main_operational(command: str) -> int:
    mode = "FULL" if command == "bootstrap" else "DELTA"
    try:
        query = execution_query_from_env()
        client = GmailClient(GmailConfig.from_env())
        report = _operational_report(command, client, query)
    except Exception as error:
        error_code = sanitized_error_code(error)
        run_status = "FAILED" if record_operational_failure(mode, error_code) else "UNKNOWN"
        _print(
            {
                "error": "gmail_import_failed",
                "error_code": error_code,
                "ok": False,
                "run_status": run_status,
                "status": "FAILED",
            }
        )
        return 1
    _print({"ok": True, "pending_review": report.pending_review, "status": report.status})
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ingestion.gmail")
    parser.add_argument("command", choices=("full", "bootstrap", "delta"))
    arguments = parser.parse_args(argv)
    if arguments.command != "full":
        return _main_operational(arguments.command)
    run_id: uuid.UUID | None = None
    try:
        query = execution_query_from_env()
        gmail_config = GmailConfig.from_env()
        client = GmailClient(gmail_config)
        with SessionLocal.begin() as session:
            attest_collector_database(session)
            acquire_import_lock(session)
            source = _source(session)
            run_id = claim_pilot_slot(session, source)
        with SessionLocal.begin() as session:
            attest_collector_database(session)
            acquire_import_lock(session)
            source = _source(session)
            run = load_claimed_pilot_run(session, source, run_id)
            snapshot = client.full_snapshot(query)
            report = persist_full_snapshot(session, snapshot, query=query, source=source, run=run)
    except Exception as error:
        error_code = sanitized_error_code(error)
        claim_status = "NOT_CLAIMED"
        if run_id is not None:
            claim_status = "FAILED" if terminalize_claim_failure(run_id, error_code) else "UNKNOWN"
        _print(
            {
                "claim_status": claim_status,
                "error": "gmail_import_failed",
                "error_code": error_code,
                "ok": False,
                "status": "FAILED",
            }
        )
        return 1
    ok = report.status == "SUCCEEDED"
    _print({"ok": ok, "pending_review": report.pending_review, "status": report.status})
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
