import base64

import pytest

from app.ingestion.gmail import GmailConfig
from app.ingestion.gmail_semantic import (
    FullMessageClient,
    SemanticInputError,
    Verdict,
    decide,
    parse_message,
)

pytestmark = pytest.mark.no_db


def msg(mid, text, *, sender="partner@example.net", when=1000, labels=None, html=False):
    encoded = base64.urlsafe_b64encode(text.encode()).decode().rstrip("=")
    return {
        "id": mid,
        "threadId": "thread1",
        "internalDate": str(when),
        "labelIds": labels or [],
        "payload": {
            "mimeType": "multipart/alternative",
            "headers": [
                {"name": "From", "value": sender},
                {"name": "To", "value": "me@example.net"},
                {"name": "Subject", "value": "Test"},
            ],
            "parts": [
                {"mimeType": "text/html" if html else "text/plain", "body": {"data": encoded}}
            ],
        },
    }


def test_nested_mime_plain_preferred_quotes_stripped():
    raw = msg("m1", "wrong", html=True)
    raw["payload"]["parts"] = [
        {
            "mimeType": "multipart/alternative",
            "parts": [
                {
                    "mimeType": "text/html",
                    "body": {"data": base64.urlsafe_b64encode(b"<b>wrong</b>").decode()},
                },
                {
                    "mimeType": "text/plain",
                    "body": {
                        "data": base64.urlsafe_b64encode(
                            b"New offer\n> old accepted fee: 900 EUR"
                        ).decode()
                    },
                },
            ],
        },
        {"mimeType": "application/pdf", "filename": "terms.pdf", "body": {"attachmentId": "a"}},
    ]
    assert parse_message(raw).text == "New offer"
    assert parse_message(msg("m2", "<p>Hallo <b>Welt</b></p>", html=True)).text == "Hallo Welt"


def test_bounded_and_incomplete_mime_fail_closed():
    for mutate in [
        lambda r: r["payload"]["parts"][0]["body"].update(data="!"),
        lambda r: r["payload"].update(parts=[]),
        lambda r: r["payload"]["parts"][0].update(body={"attachmentId": "x"}),
    ]:
        raw = msg("m1", "hello")
        mutate(raw)
        with pytest.raises(SemanticInputError):
            parse_message(raw)
    with pytest.raises(SemanticInputError, match="response_too_large"):
        parse_message(msg("m1", "x" * 400_000))
    raw = msg("m1", "hello")
    raw["payload"]["parts"] = [{"mimeType": "multipart/alternative", "parts": [raw["payload"]]}]
    with pytest.raises(SemanticInputError):
        parse_message(raw)


def test_proposal_not_agreement_and_invoice_not_payment():
    candidate = parse_message(msg("m1", "Collaboration offer: fee 500 EUR; deadline 2026-11-30"))
    result = decide([candidate], own_address="me@example.net", existing_track=False)
    assert result.lead.verdict == Verdict.ACCEPT
    assert result.fee.verdict == Verdict.REVIEW
    assert result.deadline.verdict == Verdict.REVIEW
    assert result.status.verdict == Verdict.NO_CHANGE
    invoice = parse_message(msg("m2", "Invoice: fee 500 EUR, paid?", when=2000))
    result = decide([candidate, invoice], own_address="me@example.net", existing_track=True)
    assert result.fee.verdict == Verdict.REVIEW
    assert result.status.verdict == Verdict.NO_CHANGE


def test_matching_opposite_party_offer_acceptance_with_evidence():
    offer = parse_message(
        msg("m1", "Collaboration offer: fee 500 EUR; deadline 2026-11-30", when=1000)
    )
    reply = parse_message(
        msg("m2", "We accept fee 500 EUR, deadline 2026-11-30", sender="me@example.net", when=2000)
    )
    result = decide([offer, reply], own_address="me@example.net", existing_track=True)
    assert result.lead.verdict == Verdict.NO_CHANGE
    assert result.status.value == "CONFIRMED"
    assert result.fee.value == {
        "fee_amount_minor": 50000,
        "fee_currency": "EUR",
        "fee_status": "AGREED",
    }
    assert result.deadline.value == "2026-11-30"
    assert len(result.fee.evidence) == 2
    assert result.fee.evidence[0].assertion_sha256 != result.fee.evidence[1].assertion_sha256


@pytest.mark.parametrize(
    "withdrawal",
    [
        "We cancel the collaboration.",
        "We withdraw our agreement.",
        "We decline the collaboration.",
        "Wir sagen die Kooperation ab.",
        "The collaboration is canceled.",
        "We terminate our agreement.",
        "We cannot proceed with this project.",
        "Wir ziehen unsere Zusage zurück.",
    ],
)
def test_later_withdrawal_without_terms_blocks_prior_agreement(withdrawal):
    offer = parse_message(msg("m1", "Collaboration offer: fee 500 EUR; deadline 2026-11-30"))
    acceptance = parse_message(
        msg("m2", "We accept fee 500 EUR, deadline 2026-11-30", sender="me@example.net", when=2000)
    )
    withdrawn = parse_message(msg("m3", withdrawal, when=3000))
    result = decide(
        [offer, acceptance, withdrawn], own_address="me@example.net", existing_track=True
    )
    assert result.fee.verdict == Verdict.REVIEW
    assert result.deadline.verdict == Verdict.REVIEW
    assert result.status.verdict == Verdict.REVIEW
    assert any(ref.message_id == "m3" for ref in result.fee.evidence)


def test_prior_decline_does_not_invalidate_later_new_offer_and_acceptance():
    decline = parse_message(msg("m0", "We decline the collaboration.", when=500))
    offer = parse_message(msg("m1", "Offer fee 500 EUR", when=1000))
    acceptance = parse_message(
        msg("m2", "We accept fee 500 EUR", sender="me@example.net", when=2000)
    )
    result = decide([decline, offer, acceptance], own_address="me@example.net", existing_track=True)
    assert result.fee.verdict == Verdict.ACCEPT
    assert result.status.verdict == Verdict.ACCEPT


def test_changed_fee_and_draft_acceptance_review():
    offer = parse_message(msg("m1", "Offer fee 500 EUR"))
    reply = parse_message(msg("m2", "We accept fee 600 EUR", sender="me@example.net", when=2000))
    assert (
        decide([offer, reply], own_address="me@example.net", existing_track=True).fee.verdict
        == Verdict.REVIEW
    )
    draft = parse_message(
        msg("m2", "We accept fee 500 EUR", sender="me@example.net", when=2000, labels=["DRAFT"])
    )
    assert (
        decide([offer, draft], own_address="me@example.net", existing_track=True).fee.verdict
        == Verdict.REVIEW
    )


def test_quoted_acceptance_ignored_and_cross_thread_rejected():
    offer = parse_message(msg("m1", "Offer fee 500 EUR\n> We accept fee 500 EUR"))
    assert (
        decide([offer], own_address="me@example.net", existing_track=True).fee.verdict
        == Verdict.REVIEW
    )
    second = msg("m2", "fee 500 EUR")
    second["threadId"] = "other"
    with pytest.raises(SemanticInputError, match="cross_thread_review_required"):
        decide([offer, parse_message(second)], own_address="me@example.net", existing_track=True)


def test_conditional_or_negated_terms_never_auto_accept():
    offer = parse_message(msg("m1", "Offer fee 500 EUR"))
    for text in ("We do not accept fee 500 EUR", "If approved, we accept fee 500 EUR"):
        reply = parse_message(msg("m2", text, sender="me@example.net", when=2000))
        result = decide([offer, reply], own_address="me@example.net", existing_track=True)
        assert result.fee.verdict == Verdict.REVIEW
        assert result.status.verdict == Verdict.REVIEW


@pytest.mark.parametrize(
    "reply_text",
    [
        "We agree to meet. We propose fee 500 EUR",
        "We agree to meet; we propose fee 500 EUR",
        "We agree to meet, but propose fee 500 EUR",
        "We agree to a meeting. Fee 500 EUR",
        "We accept the collaboration. Fee 500 EUR",
        "We accept fee 500 EUR if the project goes ahead",
        "We accept fee 500 EUR subject to approval",
    ],
)
def test_unrelated_conditional_or_counteroffer_fee_never_confirms(reply_text):
    offer = parse_message(msg("m1", "Offer fee 500 EUR"))
    reply = parse_message(msg("m2", reply_text, sender="me@example.net", when=2000))
    result = decide([offer, reply], own_address="me@example.net", existing_track=True)
    assert result.fee.verdict == Verdict.REVIEW
    assert result.status.verdict == Verdict.REVIEW


@pytest.mark.parametrize(
    "reply_text",
    [
        "We agree to meet. We propose deadline 2026-11-30",
        "We agree to a meeting; deadline 2026-11-30",
        "We accept deadline 2026-11-30 if approved",
    ],
)
def test_unrelated_or_conditional_deadline_never_confirms(reply_text):
    offer = parse_message(msg("m1", "Offer deadline 2026-11-30"))
    reply = parse_message(msg("m2", reply_text, sender="me@example.net", when=2000))
    result = decide([offer, reply], own_address="me@example.net", existing_track=True)
    assert result.deadline.verdict == Verdict.REVIEW
    assert result.status.verdict == Verdict.REVIEW


@pytest.mark.parametrize(
    "text",
    [
        "No collaboration possible",
        "We decline the collaboration",
        "We cannot do a collaboration",
        "Keine Zusammenarbeit möglich",
        "Wir lehnen die Zusammenarbeit ab",
    ],
)
def test_declined_or_negated_inbound_lead_requires_review(text):
    result = decide(
        [parse_message(msg("m1", text))], own_address="me@example.net", existing_track=False
    )
    assert result.lead.verdict == Verdict.REVIEW


def test_explicit_term_acceptance_after_unrelated_sentence_retains_evidence():
    offer = parse_message(msg("m1", "Offer fee 500 EUR"))
    reply = parse_message(
        msg("m2", "We agree to meet. We accept fee 500 EUR.", sender="me@example.net", when=2000)
    )
    result = decide([offer, reply], own_address="me@example.net", existing_track=True)
    assert result.fee.verdict == Verdict.ACCEPT
    assert result.fee.value["fee_amount_minor"] == 50000
    assert len(result.fee.evidence) == 2
    assert result.status.verdict == Verdict.ACCEPT


def test_dotted_german_deadline_preserves_explicit_acceptance():
    offer = parse_message(msg("m1", "Angebot frist 30.11.2026"))
    reply = parse_message(
        msg("m2", "Einverstanden frist 30.11.2026", sender="me@example.net", when=2000)
    )
    result = decide([offer, reply], own_address="me@example.net", existing_track=True)
    assert result.deadline.verdict == Verdict.ACCEPT
    assert result.deadline.value == "2026-11-30"


def test_attachment_and_sender_spoofing_do_not_auto_accept():
    offer = parse_message(msg("m1", "Offer fee 500 EUR"))
    reply_raw = msg("m2", "We accept fee 500 EUR", sender="me@example.net", when=2000)
    reply_raw["payload"]["parts"].append(
        {"mimeType": "application/pdf", "filename": "contract.pdf", "body": {"attachmentId": "x"}}
    )
    reply = parse_message(reply_raw)
    assert (
        decide([offer, reply], own_address="me@example.net", existing_track=True).fee.verdict
        == Verdict.REVIEW
    )
    spoof = parse_message(msg("m2", "We accept fee 500 EUR", sender="notme@example.net", when=2000))
    assert (
        decide([offer, spoof], own_address="me@example.net", existing_track=True).fee.verdict
        == Verdict.REVIEW
    )


def test_html_blockquote_not_counted_and_conditional_lead_review():
    html_msg = parse_message(
        msg(
            "m1",
            "<p>Offer fee 500 EUR</p><blockquote>We accept fee 500 EUR</blockquote>",
            html=True,
        )
    )
    assert (
        decide([html_msg], own_address="me@example.net", existing_track=True).fee.verdict
        == Verdict.REVIEW
    )
    conditional = parse_message(msg("m1", "Would you consider a collaboration?"))
    assert (
        decide([conditional], own_address="me@example.net", existing_track=False).lead.verdict
        == Verdict.REVIEW
    )


def test_euro_symbol_and_invalid_date():
    offer = parse_message(msg("m1", "Offer fee 500 €; deadline 2026-11-30"))
    reply = parse_message(
        msg("m2", "We accept fee 500 €; deadline 2026-11-30", sender="me@example.net", when=2000)
    )
    assert (
        decide([offer, reply], own_address="me@example.net", existing_track=True).fee.value[
            "fee_amount_minor"
        ]
        == 50000
    )
    bad = parse_message(msg("m3", "deadline 2026-02-30"))
    with pytest.raises(SemanticInputError, match="term_invalid"):
        decide([bad], own_address="me@example.net", existing_track=True)


class Transport:
    def __init__(self, response):
        self.response = response
        self.calls = []

    def request(self, method, url, *, headers=None, body=None):
        self.calls.append((method, url))
        if "oauth2.googleapis.com/tokeninfo?" in url:
            return 200, {"scope": "https://www.googleapis.com/auth/gmail.readonly"}
        if url.endswith("/token"):
            return 200, {"access_token": "secret"}
        if url.endswith("/profile"):
            return 200, {"emailAddress": "me@example.net", "historyId": "1"}
        return 200, self.response


def test_current_thread_inventory_is_bounded_and_validated():
    class ThreadTransport(Transport):
        def request(self, method, url, *, headers=None, body=None):
            if "/threads/" in url:
                self.calls.append((method, url))
                return 200, {
                    "id": "thread1",
                    "messages": [
                        {"id": "m1", "threadId": "thread1", "internalDate": "100", "labelIds": []},
                        {
                            "id": "m2",
                            "threadId": "thread1",
                            "internalDate": "200",
                            "labelIds": ["INBOX"],
                        },
                    ],
                }
            return super().request(method, url, headers=headers, body=body)

    transport = ThreadTransport({})
    client = FullMessageClient(GmailConfig("id", "secret", "refresh", "me@example.net"), transport)
    assert client.thread_inventory("thread1") == {"m1": (100, ()), "m2": (200, ("INBOX",))}
    assert transport.calls[-1] == (
        "GET",
        "https://gmail.googleapis.com/gmail/v1/users/me/threads/thread1?format=metadata",
    )
    with pytest.raises(SemanticInputError, match="invalid_thread_id"):
        client.thread_inventory("../thread1")


def test_current_thread_inventory_rejects_partial_or_duplicate_responses():
    class ThreadTransport(Transport):
        def request(self, method, url, *, headers=None, body=None):
            if "/threads/" in url:
                return 200, self.response
            return super().request(method, url, headers=headers, body=body)

    for response in (
        {"id": "thread1", "messages": []},
        {"id": "thread1", "messages": [{"id": "m1"}]},
        {
            "id": "thread1",
            "messages": [{"id": "m1", "threadId": "thread1", "internalDate": "1", "labelIds": []}]
            * 2,
        },
        {
            "id": "thread1",
            "messages": [{"id": "m1", "threadId": "other", "internalDate": "1", "labelIds": []}],
        },
        {
            "id": "thread1",
            "messages": [
                {"id": str(i), "threadId": "thread1", "internalDate": "1", "labelIds": []}
                for i in range(101)
            ],
        },
    ):
        client = FullMessageClient(
            GmailConfig("id", "secret", "refresh", "me@example.net"), ThreadTransport(response)
        )
        with pytest.raises(SemanticInputError):
            client.thread_inventory("thread1")


def test_full_get_and_identity_validation():
    transport = Transport(msg("m1", "Collaboration offer"))
    client = FullMessageClient(GmailConfig("id", "secret", "refresh", "me@example.net"), transport)
    assert client.fetch("m1").text == "Collaboration offer"
    assert [m for m, _ in transport.calls] == ["POST", "GET", "GET", "GET"]
    assert transport.calls[-1][1].endswith("/messages/m1?format=full")
    with pytest.raises(SemanticInputError, match="message_id_mismatch"):
        client.fetch("m2")
    with pytest.raises(SemanticInputError, match="invalid_message_id"):
        client.fetch("../../something")
