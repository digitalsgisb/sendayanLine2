import os
import sqlite3
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdy_persistence import ProductionSQLiteStore


class PersistenceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.temp.name, "state.db")
        self.store = ProductionSQLiteStore(self.path)

    def tearDown(self):
        self.store.close()
        self.temp.cleanup()

    def test_schema_has_snapshot_and_outbox(self):
        connection = sqlite3.connect(self.path)
        try:
            rows = connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            ).fetchall()
        finally:
            connection.close()
        self.assertEqual([row[0] for row in rows], ["google_queue", "outbox_events", "runtime_state", "settings", "sqlite_sequence"])

    def test_runtime_state_survives_reopen(self):
        self.store.save_runtime_state(
            "SDY2", {"current_shift": {"shift_id": "shift-1"}, "count": 17}, "ACTIVE"
        )
        self.store.close()
        self.store = ProductionSQLiteStore(self.path)
        restored = self.store.load_runtime_state("SDY2")
        self.assertEqual(restored["current_shift"]["shift_id"], "shift-1")
        self.assertEqual(restored["count"], 17)

    def test_duplicate_event_rolls_back_matching_state(self):
        self.store.save_state_and_enqueue(
            "SDY2", {"count": 10}, "ACTIVE", "hourly.finalized",
            "shift-1", {"actual": 10}, event_id="fixed-id"
        )
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.save_state_and_enqueue(
                "SDY2", {"count": 999}, "ACTIVE", "hourly.finalized",
                "shift-1", {"actual": 999}, event_id="fixed-id"
            )
        self.assertEqual(self.store.load_runtime_state("SDY2")["count"], 10)

    def test_interrupted_delivery_returns_to_pending(self):
        self.store.save_state_and_enqueue(
            "SDY2", {"count": 1}, "ACTIVE", "shift.started",
            "shift-1", {"shift": {}}, event_id="recovery-id"
        )
        self.assertEqual(self.store.claim_next_event()["event_id"], "recovery-id")
        self.store.close()
        self.store = ProductionSQLiteStore(self.path)
        self.assertEqual(self.store.get_outbox_counts()["PENDING"], 1)


if __name__ == "__main__":
    unittest.main()
