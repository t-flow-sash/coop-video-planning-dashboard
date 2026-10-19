from __future__ import annotations

from uuid import UUID

# These are the only milestone-one demonstration cooperations that may be
# removed during a reviewed production import. Never infer synthetic records
# from titles, audit source, counts, or other mutable user data.
SYNTHETIC_SEED_COOP_IDS = frozenset(
    {
        UUID("00000000-0000-4000-8000-000000000301"),
        UUID("00000000-0000-4000-8000-000000000302"),
        UUID("00000000-0000-4000-8000-000000000303"),
        UUID("00000000-0000-4000-8000-000000000304"),
        UUID("00000000-0000-4000-8000-000000000305"),
        UUID("00000000-0000-4000-8000-000000000306"),
    }
)
