"""Read-only, bounded Gmail full-message semantic candidate extraction.

No DB write, scheduler, model, or automatic domain mutation. Caller must supply a
reviewed stable business-track key and exact source-revision references before
converting candidates to a reviewed_domain_import manifest. Never log raw MIME.
"""

from __future__ import annotations

import base64
import hashlib
import html
import json
import re
import urllib.parse
from dataclasses import dataclass
from datetime import date
from email.utils import getaddresses, parseaddr
from enum import StrEnum
from html.parser import HTMLParser
from typing import Any

import httpx

from app.ingestion.gmail import GMAIL_API, GmailClient, GmailConfig

MAX_RESPONSE = 512_000
MAX_TEXT = 64_000
MAX_PARTS = 64
MAX_DEPTH = 8


class SemanticInputError(ValueError):
    """Sanitized rejection; do not attach untrusted payload in logs."""


class BoundedJsonTransport:
    """HTTP response bound enforced while streaming, before JSON decode."""

    def request(
        self,
        method: str,
        url: str,
        *,
        headers: dict[str, str] | None = None,
        body: bytes | None = None,
    ) -> tuple[int, dict[str, Any]]:
        if method not in {"GET", "POST"} or (
            method == "POST" and url != "https://oauth2.googleapis.com/token"
        ):
            raise SemanticInputError("method_not_allowed")
        try:
            with (
                httpx.Client(timeout=30, follow_redirects=False, trust_env=True) as client,
                client.stream(method, url, headers=headers, content=body) as response,
            ):
                if 300 <= response.status_code < 400:
                    raise SemanticInputError("redirect_rejected")
                content = bytearray()
                for chunk in response.iter_bytes():
                    if len(content) + len(chunk) > MAX_RESPONSE:
                        raise SemanticInputError("response_too_large")
                    content.extend(chunk)
                data = json.loads(content) if content else {}
                if not isinstance(data, dict):
                    raise SemanticInputError("invalid_response")
                return response.status_code, data
        except (httpx.HTTPError, ValueError, UnicodeError) as error:
            if isinstance(error, SemanticInputError):
                raise
            raise SemanticInputError("transport_or_json_invalid") from error


class FullMessageClient:
    """Reuse production OAuth refresh/tokeninfo/profile validation; GET-only Gmail."""

    def __init__(self, config: GmailConfig, transport: Any = None):
        self._client = GmailClient(config, transport or BoundedJsonTransport())

    def fetch(self, message_id: str) -> ParsedMessage:
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", message_id):
            raise SemanticInputError("invalid_message_id")
        authorized = self._client.authorize()
        url = f"{GMAIL_API}/messages/{urllib.parse.quote(message_id, safe='')}?format=full"
        status, raw = self._client._json("GET", url, token=authorized.access_token)
        if status != 200:
            raise SemanticInputError("message_fetch_failed")
        if len(json.dumps(raw, ensure_ascii=False).encode()) > MAX_RESPONSE:
            raise SemanticInputError("response_too_large")
        if raw.get("id") != message_id:
            raise SemanticInputError("message_id_mismatch")
        return parse_message(raw)

    def thread_inventory(self, thread_id: str) -> dict[str, tuple[int, tuple[str, ...]]]:
        """Current complete Gmail thread IDs plus metadata, without retaining message bodies."""
        if not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", thread_id):
            raise SemanticInputError("invalid_thread_id")
        authorized = self._client.authorize()
        url = f"{GMAIL_API}/threads/{urllib.parse.quote(thread_id, safe='')}?format=metadata"
        status, raw = self._client._json("GET", url, token=authorized.access_token)
        if status != 200:
            raise SemanticInputError("thread_fetch_failed")
        try:
            if len(json.dumps(raw, ensure_ascii=False).encode()) > MAX_RESPONSE:
                raise SemanticInputError("response_too_large")
        except (TypeError, ValueError, RecursionError) as error:
            raise SemanticInputError("thread_inventory_invalid") from error
        entries = raw.get("messages")
        if (
            raw.get("id") != thread_id
            or not isinstance(entries, list)
            or not 1 <= len(entries) <= 100
        ):
            raise SemanticInputError("thread_inventory_invalid")
        inventory: dict[str, tuple[int, tuple[str, ...]]] = {}
        for item in entries:
            if not isinstance(item, dict):
                raise SemanticInputError("thread_inventory_invalid")
            mid, timestamp, labels = item.get("id"), item.get("internalDate"), item.get("labelIds")
            if (
                not isinstance(mid, str)
                or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", mid)
                or mid in inventory
                or item.get("threadId") != thread_id
                or not isinstance(timestamp, str)
                or not timestamp.isascii()
                or not timestamp.isdigit()
                or not isinstance(labels, list)
                or not all(isinstance(label, str) for label in labels)
                or len(set(labels)) != len(labels)
            ):
                raise SemanticInputError("thread_inventory_invalid")
            inventory[mid] = (int(timestamp), tuple(sorted(labels)))
        return inventory


class _HTMLText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style", "head", "blockquote"}:
            self.skip += 1
        elif tag in {"br", "p", "div", "li"}:
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style", "head", "blockquote"} and self.skip:
            self.skip -= 1
        elif tag in {"p", "div", "li"}:
            self.text.append("\n")

    def handle_data(self, data: str) -> None:
        if not self.skip:
            self.text.append(data)


def _decode(body: Any) -> str:
    if not isinstance(body, dict) or not isinstance(body.get("data"), str):
        raise SemanticInputError("missing_inline_body")
    encoded = body["data"]
    if len(encoded) > MAX_TEXT * 2:
        raise SemanticInputError("body_too_large")
    try:
        data = base64.b64decode(encoded + "=" * (-len(encoded) % 4), altchars=b"-_", validate=True)
        if len(data) > MAX_TEXT:
            raise SemanticInputError("body_too_large")
        return data.decode("utf-8", errors="strict")
    except (ValueError, UnicodeError) as error:
        raise SemanticInputError("body_decode_invalid") from error


def _new_text(text: str) -> str:
    lines = []
    for line in text.splitlines():
        if re.match(
            r"\s*(>|On .+ wrote:|Am .+ schrieb:|-----Original Message-----|Von: .+|From: .+)",
            line,
            re.I,
        ):
            break
        lines.append(line)
    return "\n".join(lines).strip()


@dataclass(frozen=True)
class ParsedMessage:
    message_id: str
    thread_id: str
    internal_date: int
    sender: str
    recipient: str
    subject: str
    text: str  # Ephemeral only. Do not persist/log this object.
    labels: tuple[str, ...]
    has_attachments: bool = False


def parse_message(raw: dict[str, Any]) -> ParsedMessage:
    """Recursive MIME walk; plain preferred, HTML fallback; reject incomplete MIME."""
    try:
        size = len(json.dumps(raw, ensure_ascii=False).encode())
    except (ValueError, TypeError, RecursionError) as error:
        raise SemanticInputError("payload_invalid") from error
    if not isinstance(raw, dict) or size > MAX_RESPONSE:
        raise SemanticInputError("response_too_large")
    if not isinstance(raw.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", raw["id"]):
        raise SemanticInputError("invalid_message_id")
    thread = raw.get("threadId")
    timestamp = raw.get("internalDate")
    if (
        not isinstance(thread, str)
        or not thread
        or not isinstance(timestamp, str)
        or not timestamp.isascii()
        or not timestamp.isdigit()
    ):
        raise SemanticInputError("metadata_invalid")
    labels = raw.get("labelIds", [])
    if not isinstance(labels, list) or not all(isinstance(x, str) for x in labels):
        raise SemanticInputError("metadata_invalid")
    payload = raw.get("payload")
    if not isinstance(payload, dict) or not isinstance(payload.get("headers"), list):
        raise SemanticInputError("payload_invalid")
    headers: dict[str, str] = {}
    for item in payload["headers"]:
        if (
            not isinstance(item, dict)
            or not isinstance(item.get("name"), str)
            or not isinstance(item.get("value"), str)
        ):
            raise SemanticInputError("headers_invalid")
        headers[item["name"].lower()] = item["value"]
    if not headers.get("from") or not headers.get("to"):
        raise SemanticInputError("headers_invalid")
    texts: dict[str, list[str]] = {"text/plain": [], "text/html": []}
    visited = 0
    attachments = False

    def walk(node: dict[str, Any], depth: int) -> None:
        nonlocal visited, attachments
        visited += 1
        if depth > MAX_DEPTH or visited > MAX_PARTS:
            raise SemanticInputError("mime_bound_exceeded")
        mime = node.get("mimeType")
        if not isinstance(mime, str):
            raise SemanticInputError("mime_invalid")
        if isinstance(node.get("body"), dict) and node["body"].get("attachmentId"):
            attachments = True
        children = node.get("parts", [])
        if not isinstance(children, list):
            raise SemanticInputError("mime_invalid")
        if mime.startswith("multipart/"):
            if not children:
                raise SemanticInputError("mime_incomplete")
            for child in children:
                if not isinstance(child, dict):
                    raise SemanticInputError("mime_invalid")
                walk(child, depth + 1)
        elif mime in texts and not node.get("filename"):
            decoded = _decode(node.get("body"))
            texts[mime].append(decoded)
        elif node.get("filename") or (
            isinstance(node.get("body"), dict) and node["body"].get("attachmentId")
        ):
            attachments = True

    walk(payload, 0)
    if texts["text/plain"]:
        text = "\n".join(texts["text/plain"])
    elif texts["text/html"]:
        parser = _HTMLText()
        parser.feed("\n".join(texts["text/html"]))
        text = html.unescape("".join(parser.text))
    else:
        raise SemanticInputError("no_inline_text")
    text = _new_text(text)
    if not text or len(text.encode()) > MAX_TEXT:
        raise SemanticInputError("empty_or_large_text")
    return ParsedMessage(
        raw["id"],
        thread,
        int(timestamp),
        headers["from"],
        headers["to"],
        headers.get("subject", ""),
        text,
        tuple(sorted(set(labels))),
        attachments,
    )


class Verdict(StrEnum):
    NO_CHANGE = "NO_CHANGE"
    ACCEPT = "ACCEPT"
    REVIEW = "REVIEW"


@dataclass(frozen=True)
class EvidenceRef:
    message_id: str
    thread_id: str
    assertion_sha256: str  # No raw excerpt stored in decision artifact.


@dataclass(frozen=True)
class FieldDecision:
    verdict: Verdict
    value: Any = None
    evidence: tuple[EvidenceRef, ...] = ()
    reason: str = ""


@dataclass(frozen=True)
class SemanticDecision:
    lead: FieldDecision
    status: FieldDecision
    deadline: FieldDecision
    fee: FieldDecision  # value = {fee_amount_minor, fee_currency, fee_status}


# Narrow grammar: explicit commercial signals only. Novel wording goes to review.
# A bare topic mention is not an offer of a new relationship. Restrict CREATE
# candidates to explicit inbound proposal language in the same sentence.
LEAD_OFFER = re.compile(
    r"\b(?:cooperation|collaboration|sponsorship|zusammenarbeit|kooperation)\b"
    r"[^.!?\n]*\b(?:offer|propose|angebot|vorschlag)\b|"
    r"\b(?:offer|propose|angebot|vorschlag)\b[^.!?\n]*"
    r"\b(?:cooperation|collaboration|sponsorship|zusammenarbeit|kooperation)\b",
    re.I,
)
LEAD_CONTEXT_RISK = re.compile(
    r"\b(?:past|previous|former|formerly|earlier|historical|old|already|"
    r"won't|wouldn't|don't|didn't|never|cancelled|canceled|void|"
    r"früher|vorherig\w*|ehemalig\w*|bereits|damalig\w*)\b",
    re.I,
)
ACCEPT = re.compile(
    r"\b(?:we accept|we agree|confirmed|agreed|einverstanden|bestätigen wir)\b",
    re.I,
)
PROPOSAL = re.compile(
    r"\b(?:offer|propose|suggest|would you|could you|angebot|vorschlag|würden sie|könnten sie)\b",
    re.I,
)
INVOICE = re.compile(r"\b(?:invoice|rechnung|paid|bezahlt|überwiesen)\b", re.I)
QUALIFIER = re.compile(
    r"\b(?:no|not|never|cannot|can't|if|unless|pending|subject to|"
    r"nicht|kein\w*|unter vorbehalt|falls|wenn|sofern)\b|\?",
    re.I,
)
DECLINE = re.compile(
    r"\b(?:declin\w*|reject\w*|refus\w*|lehnen\b|abgelehnt|absagen|"
    r"ablehnen|nicht möglich|unmöglich)\b",
    re.I,
)
# A later retraction is not proof of a new terminal state, but invalidates the
# earlier agreement as a current auto-import candidate. Keep this grammar narrow.
RETRACTION = re.compile(
    r"\b(?:we|they)\s+(?:will|are\s+going\s+to)\s+not\s+go\s+ahead\s+with\s+(?:this|the)\s+project\b|"
    r"\b(?:the|this)\s+deal\s+is\s+off\b|"
    r"\b(?:this|the)\s+(?:project|collaboration|cooperation|deal)\s+is\s+no\s+longer\s+going\s+ahead\b|"
    r"\b(?:we cannot|we can't|we can not)\s+proceed\s+with\s+(?:this|the)\s+project\b|"
    r"\b(?:cancel|withdraw|decline|reject|refuse|terminate)\w*\b[^.!?\n]*"
    r"\b(?:collaboration|cooperation|sponsorship|agreement)\b|"
    r"\b(?:collaboration|cooperation|sponsorship|agreement)\b[^.!?\n]*"
    r"\b(?:cancel|withdraw|decline|reject|refuse|terminate)\w*\b|"
    r"\b(?:sagen|kündigen)\b[^.!?\n]*\b(?:kooperation|zusammenarbeit|vereinbarung)\b"
    r"[^.!?\n]*\b(?:ab|auf)\b|"
    r"\b(?:kooperation|zusammenarbeit|vereinbarung)\b[^.!?\n]*"
    r"\b(?:absagen|kündigen|widerrufen)\b|"
    r"\bziehen\b[^.!?\n]*\b(?:zusage|vereinbarung)\b[^.!?\n]*\bzurück\b",
    re.I,
)
# An acceptance only licenses the assertion it governs, not an unrelated sentence
# or a new proposal. Multiple explicit terms may follow the same acceptance.
ACCEPTED_TERMS = re.compile(
    r"^\s*(?:we accept|we agree(?: to)?|confirmed|agreed|einverstanden|bestätigen wir)"
    r"\s*[:;,]?\s*(?:(?:fee|budget|honorar|vergütung|deadline|due by|until|frist|"
    r"spätestens bis)\s*[:=]?\s*(?:EUR\s*)?(?:\d[\d.,-]*)(?:\s*(?:EUR|€))?"
    r"\s*(?:[,;]|\band\b|\bund\b)?\s*)*$",
    re.I,
)
SENTENCE_BREAK = re.compile(r"(?<=[.!?])\s+|\n+")
DATE = re.compile(
    r"\b(?:deadline|due by|until|frist|spätestens bis)\s*[:=]?\s*"
    r"(\d{4}-\d{2}-\d{2}|\d{1,2}\.\d{1,2}\.\d{4})\b",
    re.I,
)
FEE = re.compile(
    r"\b(?:fee|budget|honorar|vergütung)\s*[:=]?\s*(?:EUR\s*)?"
    r"(\d{1,7}(?:[.,]\d{2})?)\s*(?:EUR\b|€)",
    re.I,
)


def _evidence(message: ParsedMessage, assertion: str) -> EvidenceRef:
    return EvidenceRef(
        message.message_id,
        message.thread_id,
        hashlib.sha256(
            (message.message_id + ":" + assertion.casefold().strip()).encode()
        ).hexdigest(),
    )


def _terms(
    message: ParsedMessage, pattern: re.Pattern[str], kind: str
) -> list[tuple[Any, EvidenceRef, str]]:
    result = []
    for sentence in SENTENCE_BREAK.split(message.text):
        for match in pattern.finditer(sentence):
            raw = match.group(1)
            try:
                value: str | int
                if kind == "date":
                    parsed_date = (
                        date.fromisoformat(raw)
                        if "-" in raw
                        else date(*reversed([int(x) for x in raw.split(".")]))
                    )
                    value = parsed_date.isoformat()
                else:
                    if "," in raw and "." in raw:
                        raise ValueError("ambiguous currency")
                    whole, _, cents = raw.replace(",", ".").partition(".")
                    value = int(whole) * 100 + int(cents or "00")
                result.append((value, _evidence(message, match.group(0)), sentence))
            except ValueError:
                raise SemanticInputError("term_invalid") from None
    return result


def _decide_term(
    messages: list[ParsedMessage], pattern: re.Pattern[str], kind: str, own_address: str
) -> FieldDecision:
    offers: list[tuple[Any, EvidenceRef, str, int, str]] = []
    accepts: list[tuple[Any, EvidenceRef, str, int, str]] = []
    uncertain: list[EvidenceRef] = []
    for msg in messages:
        terms = _terms(msg, pattern, kind)
        sender = parseaddr(msg.sender)[1].casefold()
        role = "own" if sender == own_address.casefold() else "other"
        for value, ev, sentence in terms:
            if (
                INVOICE.search(msg.text)
                or QUALIFIER.search(msg.text)
                or DECLINE.search(msg.text)
                or "DRAFT" in msg.labels
                or msg.has_attachments
            ):
                uncertain.append(ev)
            elif ACCEPTED_TERMS.fullmatch(sentence.strip().rstrip(".!?")) and not PROPOSAL.search(
                sentence
            ):
                accepts.append((value, ev, role, msg.internal_date, msg.thread_id))
            else:
                offers.append((value, ev, role, msg.internal_date, msg.thread_id))
    if not offers and not accepts and not uncertain:
        return FieldDecision(Verdict.NO_CHANGE)
    confirmed: list[tuple[Any, tuple[EvidenceRef, EvidenceRef]]] = []
    for value, acceptance, role, timestamp, thread in accepts:
        matching = [
            (ev, t)
            for v, ev, r, t, th in offers
            if v == value and r != role and t < timestamp and th == thread
        ]
        if matching:
            confirmed.append((value, (max(matching, key=lambda x: x[1])[0], acceptance)))
    # Never auto-choose between multiple claims, later changed terms or incomplete evidence.
    if len(confirmed) != 1 or len(offers) != 1 or len(accepts) != 1 or uncertain:
        refs = tuple(ev for _, ev, _, _, _ in offers + accepts) + tuple(uncertain)
        return FieldDecision(Verdict.REVIEW, evidence=refs, reason="terms_unmatched_or_conflicting")
    value, refs = confirmed[0]
    if kind == "fee":
        value = {"fee_amount_minor": value, "fee_currency": "EUR", "fee_status": "AGREED"}
    return FieldDecision(Verdict.ACCEPT, value, refs)


def _address_list(value: str) -> list[str]:
    """All parseable addresses of a header value (handles comma-separated lists)."""
    return [address for _, address in getaddresses([value]) if address]


def _address_set(message: ParsedMessage) -> set[str]:
    """Every address on the message (sender plus all To/Cc/Bcc recipients)."""
    found = {
        address.casefold()
        for address in _address_list(message.recipient) + _address_list(message.sender)
    }
    sender = parseaddr(message.sender)[1]
    if sender:
        found.add(sender.casefold())
    return found


def decide(
    messages: list[ParsedMessage], *, own_address: str, existing_track: bool
) -> SemanticDecision:
    """Tri-state per field; exact track matching and domain conflict checks are caller-owned."""
    if (
        not own_address
        or not messages
        or len(messages) > 100
        or len({m.message_id for m in messages}) != len(messages)
    ):
        raise SemanticInputError("scope_invalid")
    own = own_address.casefold()
    if any(
        not parseaddr(m.sender)[1]
        or not _address_list(m.recipient)
        # Real mail frequently carries several To/Cc/Bcc recipients; the own
        # address must be matched among all of them, not only the first.
        or own not in _address_set(m)
        or (
            parseaddr(m.sender)[1].casefold() == own
            and "DRAFT" not in m.labels
            and ("SENT" not in m.labels or "INBOX" in m.labels)
        )
        or (parseaddr(m.sender)[1].casefold() != own and "SENT" in m.labels)
        for m in messages
    ):
        raise SemanticInputError("participants_invalid")
    if len({m.thread_id for m in messages}) != 1:
        raise SemanticInputError("cross_thread_review_required")
    ordered = sorted(messages, key=lambda m: (m.internal_date, m.message_id))
    inbound = [m for m in ordered if parseaddr(m.sender)[1].casefold() != own_address.casefold()]
    lead_refs = tuple(
        _evidence(m, "lead:" + m.message_id) for m in inbound if LEAD_OFFER.search(m.text)
    )
    if existing_track:
        lead = FieldDecision(Verdict.NO_CHANGE)
    elif len(lead_refs) == 1 and not any(
        "DRAFT" in m.labels
        or m.has_attachments
        or QUALIFIER.search(m.text)
        or DECLINE.search(m.text)
        or LEAD_CONTEXT_RISK.search(m.text)
        for m in inbound
    ):
        lead = FieldDecision(Verdict.ACCEPT, {"status": "NEW"}, lead_refs)
    elif inbound:
        lead = FieldDecision(
            Verdict.REVIEW, evidence=lead_refs, reason="lead_identity_or_intent_unverified"
        )
    else:
        lead = FieldDecision(Verdict.NO_CHANGE)
    deadline = _decide_term(ordered, DATE, "date", own_address)
    fee = _decide_term(ordered, FEE, "fee", own_address)
    # A separately attached agreement could override email terms; never infer it.
    if any(m.has_attachments for m in ordered):
        if deadline.verdict != Verdict.NO_CHANGE:
            deadline = FieldDecision(
                Verdict.REVIEW, evidence=deadline.evidence, reason="attachment_requires_review"
            )
        if fee.verdict != Verdict.NO_CHANGE:
            fee = FieldDecision(
                Verdict.REVIEW, evidence=fee.evidence, reason="attachment_requires_review"
            )
    # A term-less later retraction must not leave an earlier agreement actionable.
    # Refer to the acceptance message rather than the offer; an earlier refusal
    # can precede a genuinely new offer/acceptance without invalidating it.
    by_id = {m.message_id: m for m in ordered}
    retractions = [m for m in ordered if "DRAFT" not in m.labels and RETRACTION.search(m.text)]
    for field_name in ("deadline", "fee"):
        field = deadline if field_name == "deadline" else fee
        if field.verdict != Verdict.ACCEPT:
            continue
        acceptance = by_id[field.evidence[-1].message_id]
        later = [
            m
            for m in retractions
            if (m.internal_date, m.message_id) > (acceptance.internal_date, acceptance.message_id)
            or m.message_id == acceptance.message_id
        ]
        if later:
            refs = field.evidence + tuple(_evidence(m, "retraction:" + m.message_id) for m in later)
            reviewed = FieldDecision(
                Verdict.REVIEW, evidence=refs, reason="later_retraction_requires_review"
            )
            if field_name == "deadline":
                deadline = reviewed
            else:
                fee = reviewed
    # An acceptance cannot safely advance workflow status unless an exact term is mutually accepted.
    if Verdict.REVIEW in {deadline.verdict, fee.verdict} and any(
        re.search(r"\b(?:accept|agree|confirm|einverstanden|bestätig)\w*\b", m.text, re.I)
        for m in ordered
    ):
        status = FieldDecision(Verdict.REVIEW, reason="related_terms_unresolved")
    elif deadline.verdict == Verdict.ACCEPT or fee.verdict == Verdict.ACCEPT:
        refs = tuple(dict.fromkeys(deadline.evidence + fee.evidence))
        status = FieldDecision(Verdict.ACCEPT, "CONFIRMED", refs)
    elif any(ACCEPT.search(m.text) for m in ordered):
        status = FieldDecision(Verdict.REVIEW, reason="acceptance_without_matched_term")
    else:
        status = FieldDecision(Verdict.NO_CHANGE)
    return SemanticDecision(lead, status, deadline, fee)
