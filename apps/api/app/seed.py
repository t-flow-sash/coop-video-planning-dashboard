from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy.orm import Session

from app.db import SessionLocal
from app.models import AuditEvent, Contact, Coop, CoopContact, Organization
from app.synthetic_seeds import SYNTHETIC_SEED_COOP_IDS

ORGS = [
    ("00000000-0000-4000-8000-000000000101", "Northstar Labs", "BRAND"),
    ("00000000-0000-4000-8000-000000000102", "Maple & Moss", "BRAND"),
    ("00000000-0000-4000-8000-000000000103", "Brightside Collective", "AGENCY"),
]
CONTACTS = [
    (
        "00000000-0000-4000-8000-000000000201",
        "00000000-0000-4000-8000-000000000103",
        "Avery Stone",
        "avery.stone@example.invalid",
    ),
    (
        "00000000-0000-4000-8000-000000000202",
        "00000000-0000-4000-8000-000000000101",
        "Jordan North",
        "jordan.north@example.invalid",
    ),
    (
        "00000000-0000-4000-8000-000000000203",
        "00000000-0000-4000-8000-000000000102",
        "Morgan Maple",
        "morgan.maple@example.invalid",
    ),
]
COOPS = [
    ("00000000-0000-4000-8000-000000000301", "Northstar launch video", "NEW", None, None),
    (
        "00000000-0000-4000-8000-000000000302",
        "Maple autumn short",
        "QUALIFYING",
        None,
        None,
    ),
    (
        "00000000-0000-4000-8000-000000000303",
        "Northstar product review",
        "IN_PRODUCTION",
        None,
        None,
    ),
    (
        "00000000-0000-4000-8000-000000000304",
        "Maple scheduled campaign",
        "SCHEDULED",
        datetime(2027, 2, 15, 12, 0, tzinfo=UTC),
        425000,
    ),
    (
        "00000000-0000-4000-8000-000000000305",
        "Archived launch",
        "COMPLETED",
        datetime(2026, 8, 20, 8, 0, tzinfo=UTC),
        300000,
    ),
    ("00000000-0000-4000-8000-000000000306", "Declined concept", "DECLINED", None, None),
]


def _seed(session: Session) -> None:
    for id_value, name, kind in ORGS:
        org_id = UUID(id_value)
        if session.get(Organization, org_id) is None:
            session.add(
                Organization(
                    id=org_id,
                    record_key=f"org-{org_id.hex}",
                    name=name,
                    normalized_name=name.lower(),
                    kind=kind,
                )
            )
    session.flush()
    for id_value, organization_id, display_name, email in CONTACTS:
        contact_id = UUID(id_value)
        if session.get(Contact, contact_id) is None:
            first_name, last_name = display_name.split(" ", 1)
            session.add(
                Contact(
                    id=contact_id,
                    record_key=f"contact-{contact_id.hex}",
                    organization_id=UUID(organization_id),
                    first_name=first_name,
                    last_name=last_name,
                    display_name=display_name,
                    email=email,
                    normalized_email=email.lower(),
                )
            )
    session.flush()
    now = datetime.now(UTC)
    for index, (id_value, title, status, release_at, fee) in enumerate(COOPS):
        coop_id = UUID(id_value)
        if session.get(Coop, coop_id) is not None:
            continue
        brand_id = UUID(ORGS[index % 2][0])
        coop = Coop(
            id=coop_id,
            record_key=f"coop-{coop_id.hex}",
            is_synthetic_seed=coop_id in SYNTHETIC_SEED_COOP_IDS,
            title=title,
            status=status,
            brand_organization_id=brand_id,
            agency_organization_id=UUID(ORGS[2][0]),
            summary="Synthetic milestone-one seed record.",
            deliverables=[{"version": 1, "kind": "VIDEO", "quantity": 1}],
            next_action=None if status in {"COMPLETED", "DECLINED"} else "Review next step",
            release_at=release_at,
            fee_amount_minor=fee,
            fee_currency="EUR" if fee is not None else None,
            needs_review=True,
            uncertainty="Synthetic demonstration record; not production-reviewed.",
            completed_at=now if status == "COMPLETED" else None,
        )
        session.add(coop)
        session.flush()
        contact_id = UUID(CONTACTS[0][0] if index < 3 else CONTACTS[(index % 2) + 1][0])
        session.add(CoopContact(coop_id=coop_id, contact_id=contact_id, role="PRIMARY"))
        session.add(
            AuditEvent(
                coop_id=coop_id,
                action="COOP_CREATED",
                actor_type="SYSTEM",
                actor_id=None,
                source="SEED",
                changes={"status": {"old": None, "new": status}},
                request_id=f"seed-{coop_id}",
            )
        )


def seed_database() -> None:
    with SessionLocal() as session, session.begin():
        _seed(session)


if __name__ == "__main__":
    seed_database()
    print("Synthetic seed data is ready.")
