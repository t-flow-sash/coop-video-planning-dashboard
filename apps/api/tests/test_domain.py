from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from app.domain import (
    REGULAR_TRANSITIONS,
    CoopStatus,
    Deliverable,
    MoneyFields,
    normalize_email,
    normalize_name,
    validate_transition,
)


def test_transition_matrix_matches_architecture() -> None:
    assert {
        CoopStatus.NEW: {CoopStatus.QUALIFYING, CoopStatus.NEGOTIATING, CoopStatus.DECLINED},
        CoopStatus.QUALIFYING: {
            CoopStatus.NEGOTIATING,
            CoopStatus.CONFIRMED,
            CoopStatus.DECLINED,
        },
        CoopStatus.NEGOTIATING: {
            CoopStatus.QUALIFYING,
            CoopStatus.CONFIRMED,
            CoopStatus.DECLINED,
            CoopStatus.CANCELLED,
        },
        CoopStatus.CONFIRMED: {
            CoopStatus.NEGOTIATING,
            CoopStatus.IN_PRODUCTION,
            CoopStatus.CANCELLED,
        },
        CoopStatus.IN_PRODUCTION: {
            CoopStatus.CONFIRMED,
            CoopStatus.REVIEW,
            CoopStatus.SCHEDULED,
            CoopStatus.CANCELLED,
        },
        CoopStatus.REVIEW: {
            CoopStatus.IN_PRODUCTION,
            CoopStatus.SCHEDULED,
            CoopStatus.CANCELLED,
        },
        CoopStatus.SCHEDULED: {
            CoopStatus.REVIEW,
            CoopStatus.PUBLISHED,
            CoopStatus.CANCELLED,
        },
        CoopStatus.PUBLISHED: {CoopStatus.REVIEW, CoopStatus.COMPLETED},
        CoopStatus.COMPLETED: set(),
        CoopStatus.DECLINED: set(),
        CoopStatus.CANCELLED: set(),
    } == REGULAR_TRANSITIONS


def test_scheduled_transition_requires_release_at() -> None:
    with pytest.raises(ValueError, match="release_at"):
        validate_transition(CoopStatus.REVIEW, CoopStatus.SCHEDULED, release_at=None)

    validate_transition(
        CoopStatus.REVIEW,
        CoopStatus.SCHEDULED,
        release_at=datetime(2027, 1, 1, tzinfo=UTC),
    )


def test_invalid_transition_is_rejected() -> None:
    with pytest.raises(ValueError, match="not allowed"):
        validate_transition(CoopStatus.NEW, CoopStatus.PUBLISHED, release_at=None)


def test_money_fields_are_all_or_nothing_and_currency_is_iso_like() -> None:
    assert MoneyFields(fee_amount_minor=100, fee_currency="eur").fee_currency == "EUR"
    with pytest.raises(ValidationError):
        MoneyFields(fee_amount_minor=100, fee_currency=None)
    with pytest.raises(ValidationError):
        MoneyFields(fee_amount_minor=None, fee_currency="EUR")
    with pytest.raises(ValidationError):
        MoneyFields(fee_amount_minor=-1, fee_currency="EUR")
    with pytest.raises(ValidationError):
        MoneyFields(fee_amount_minor=100, fee_currency="EURO")


def test_deliverable_schema_is_versioned_and_forbids_unknown_fields() -> None:
    item = Deliverable(version=1, kind="VIDEO", quantity=2, description="Review")
    assert item.quantity == 2
    with pytest.raises(ValidationError):
        Deliverable(version=2, kind="VIDEO")
    with pytest.raises(ValidationError):
        Deliverable.model_validate({"version": 1, "kind": "VIDEO", "surprise": True})


def test_normalization_trims_and_lowercases() -> None:
    assert normalize_email("  Alice@Example.INVALID ") == "alice@example.invalid"
    assert normalize_name("  Example   Brand ") == "example brand"
