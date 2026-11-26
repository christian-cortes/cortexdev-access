"""Pruebas del catálogo y los avisos de proyectos externos."""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app import db, external, status  # noqa: E402


class ExternalTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        db.configure(str(Path(self.tmp.name) / "portal.db"))
        db.init_db()
        external.seed()
        with db.db() as conn:
            conn.execute("UPDATE external_projects SET active = 0 WHERE id != 1")

    def tearDown(self):
        db.configure(None)
        self.tmp.cleanup()

    def check(self, ok):
        async def fake(services):
            return [{
                "ok": int(ok), "state": "online" if ok else "offline",
                "code": 200 if ok else 503, "latency_ms": 1, "error": None,
            } for _ in services]

        with patch.object(external.probe, "probe_all", fake):
            asyncio.run(external.run_checks())

    def test_seed_once_preserves_edits_and_inactive_projects(self):
        with db.db() as conn:
            count = conn.execute("SELECT COUNT(*) FROM external_projects").fetchone()[0]
            conn.execute("UPDATE external_projects SET company = 'Otra', name = 'Editado' WHERE id = 1")
        external.seed()
        self.assertEqual(count, 21)
        self.assertEqual(len(external.projects()), 1)
        self.assertEqual(external.projects()[0]["company"], "Otra")
        self.assertEqual(external.projects()[0]["name"], "Editado")

    def test_down_and_recovery_only_notify_on_transition(self):
        with patch.object(external.notify, "publish") as publish:
            for _ in range(status.FAIL_THRESHOLD - 1):
                self.check(False)
            self.assertEqual(external.projects()[0]["state"], "unknown")
            publish.assert_not_called()
            self.check(False)
            self.check(False)
            self.assertEqual(external.projects()[0]["state"], "offline")
            self.assertEqual(publish.call_count, 1)
            self.assertEqual(publish.call_args.args[1], "service.down")
            self.assertEqual(publish.call_args.kwargs["scope"], "external-1")
            for _ in range(status.OK_THRESHOLD):
                self.check(True)
            self.assertEqual(external.projects()[0]["state"], "online")
            self.assertEqual(publish.call_count, 2)
            self.assertEqual(publish.call_args.args[1], "service.recovered")

    def test_url_change_discards_old_status_and_streak(self):
        with patch.object(external.notify, "publish"):
            for _ in range(status.FAIL_THRESHOLD):
                self.check(False)
            with db.db() as conn:
                conn.execute("UPDATE external_projects SET url = 'https://new.example/' WHERE id = 1")
            self.assertEqual(external.projects()[0]["state"], "unknown")
            self.check(False)
            self.assertEqual(external.projects()[0]["state"], "unknown")

    def test_existing_catalog_receives_icons_once_without_overwriting_edits(self):
        with db.db() as conn:
            conn.execute("DELETE FROM meta WHERE key = 'external_icons_seeded'")
            conn.execute("UPDATE external_projects SET local_slug = NULL")
            conn.execute("UPDATE external_projects SET name = 'Renombrado' WHERE id = 1")
            conn.execute("UPDATE external_projects SET url = 'https://otro.example' WHERE id = 4")
            conn.execute("UPDATE external_projects SET local_slug = 'propio' WHERE id = 9")
        external.seed()
        with db.db() as conn:
            rows = {r["id"]: r["local_slug"] for r in conn.execute(
                "SELECT id, local_slug FROM external_projects"
            )}
            self.assertEqual(rows[1], "centro-apoyo")
            self.assertIsNone(rows[4])
            self.assertEqual(rows[9], "propio")
            conn.execute("UPDATE external_projects SET local_slug = NULL WHERE id = 1")
        external.seed()
        self.assertIsNone(external.projects()[0]["local_slug"])

    def test_edit_during_probe_does_not_save_outdated_result(self):
        async def fake(services):
            with db.db() as conn:
                conn.execute("UPDATE external_projects SET url = 'https://new.example/' WHERE id = 1")
            return [{"ok": 1, "state": "online", "code": 200, "latency_ms": 1, "error": None}]

        with patch.object(external.probe, "probe_all", fake):
            asyncio.run(external.run_checks())
        self.assertEqual(external.projects()[0]["state"], "unknown")
        self.assertIsNone(external.projects()[0]["checked_at"])


if __name__ == "__main__":
    unittest.main()
