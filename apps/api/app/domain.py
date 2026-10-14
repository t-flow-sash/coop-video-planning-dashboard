from __future__ import annotations

import re
from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, ConfigDict, Field, model_validator


class CoopStatus(StrEnum):
    NEW = "NEW"
    QUALIFYING = "QUALIFYING"
    NEGOTIATING = "NEGOTIATING"
    CONFIRMED = "CONFIRMED"
    IN_PRODUCTION = "IN_PRODUCTION"
    REVIEW = "REVIEW"
    SCHEDULED = "SCHEDULED"
    PUBLISHED = "PUBLISHED"
    COMPLETED = "COMPLETED"
    DECLINED = "DECLINED"
    CANCELLED = "CANCELLED"


TERMINAL_STATUSES = frozenset({CoopStatus.COMPLETED, CoopStatus.DECLINED, CoopStatus.CANCELLED})
OPEN_STATUSES = frozenset(CoopStatus) - TERMINAL_STATUSES

REGULAR_TRANSITIONS: dict[CoopStatus, set[CoopStatus]] = {
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
}

REOPEN_TARGETS = {
    CoopStatus.DECLINED: CoopStatus.QUALIFYING,
    CoopStatus.CANCELLED: CoopStatus.CONFIRMED,
    CoopStatus.COMPLETED: CoopStatus.PUBLISHED,
}


def validate_transition(
    current: CoopStatus, target: CoopStatus, *, release_at: datetime | None
) -> None:
    if target not in REGULAR_TRANSITIONS[current]:
        raise ValueError(f"Transition from {current} to {target} is not allowed")
    if target is CoopStatus.SCHEDULED and release_at is None:
        raise ValueError("release_at is required for SCHEDULED")


def normalize_email(value: str) -> str:
    return value.strip().lower()


def normalize_name(value: str) -> str:
    return " ".join(value.split()).lower()


class Deliverable(BaseModel):
    model_config = ConfigDict(extra="forbid")

    version: int = Field(default=1, ge=1, le=1)
    kind: str = Field(min_length=1, max_length=100)
    quantity: int = Field(default=1, ge=1, le=1000)
    description: str | None = Field(default=None, max_length=1000)


class MoneyFields(BaseModel):
    fee_amount_minor: int | None = Field(default=None, ge=0)
    fee_currency: str | None = None

    @model_validator(mode="after")
    def validate_pair(self) -> MoneyFields:
        if (self.fee_amount_minor is None) != (self.fee_currency is None):
            raise ValueError("fee_amount_minor and fee_currency must be set together")
        if self.fee_currency is not None:
            currency = self.fee_currency.upper()
            if re.fullmatch(r"[A-Z]{3}", currency) is None:
                raise ValueError("fee_currency must be a three-letter ISO 4217 code")
            self.fee_currency = currency
        return self
