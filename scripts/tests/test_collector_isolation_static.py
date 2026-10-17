from __future__ import annotations

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


class CollectorIsolationStaticTests(unittest.TestCase):
    def test_collector_uses_only_dedicated_database_credentials(self) -> None:
        manifests = [
            (ROOT / "infrastructure/import/gmail-import-cronjobs.yaml").read_text(),
            (ROOT / "infrastructure/import/gmail-recurring-cronjobs.yaml").read_text(),
        ]
        for manifest in manifests:
            self.assertIn("COLLECTOR_DATABASE_URL", manifest)
            self.assertIn("key: COLLECTOR_DATABASE_URL", manifest)
            self.assertNotIn("key: DATABASE_URL", manifest)

    def test_migration_reconciles_collector_acl_after_every_upgrade(self) -> None:
        manifest = (ROOT / "infrastructure/migration/migration-job.yaml").read_text()
        self.assertIn(
            "python -m app.database_access provision && alembic upgrade head && "
            "python -m app.database_access reconcile",
            manifest,
        )
        self.assertIn("COLLECTOR_DATABASE_URL", manifest)


if __name__ == "__main__":
    unittest.main()