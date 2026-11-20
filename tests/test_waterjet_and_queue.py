"""Waterjet/forming counting, the durable Google Sheets queue, URL config and outbox cleanup."""
import json
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timedelta, timezone

SYSTEM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
for path in (SYSTEM_DIR, TESTS_DIR):
    if path not in sys.path:
        sys.path.insert(0, path)

from sdy_google_sender import MemoryGoogleQueue, send_next_google_item  # noqa: E402
from sdy_persistence import ProductionSQLiteStore  # noqa: E402
from test_tablet_protocol import PUBLISHED, RecordingMQTTClient, load_tracker, message, results  # noqa: E402

VALID_URL = "https://script.google.com/macros/s/NEW-DEPLOYMENT/exec"


class TrackerTestCase(unittest.TestCase):
    def setUp(self):
        PUBLISHED.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.g = load_tracker(os.path.join(self.directory.name, "sdy2.db"))
        # load_tracker shuts the tracker down; give it a live store and queue again.
        self.store = ProductionSQLiteStore(os.path.join(self.directory.name, "live.db"))
        self.g["persistence_store"] = self.store
        self.g["google_queue"] = self.store
        self.g["google_sender"] = None
        PUBLISHED.clear()

    def tearDown(self):
        self.g["persistence_store"] = None
        self.store.close()
        self.directory.cleanup()


class WaterjetCountingTests(TrackerTestCase):
    """Line 2 site rule: each waterjet counts a HIGH -> LOW edge. GPIO 1 = HIGH, 0 = LOW."""

    def setUp(self):
        super().setUp()
        self.g["wj1_prev"] = self.g["wj2_prev"] = 1
        self.g["last_wj1_count_time"] = self.g["last_wj2_count_time"] = -100.0

    def feed(self, p1, p2, t, accept=True):
        self.g["apply_waterjet_sample"](p1, p2, t, accept)

    def counts(self):
        return (self.g["wj1_count"], self.g["wj2_count"], self.g["shift_total_output"])

    def test_wj1_counts_each_high_to_low_edge_with_150ms_debounce(self):
        self.feed(0, 1, 0.0)                        # HIGH -> LOW edge counts at once
        self.assertEqual(self.counts(), (1, 0, 1))
        self.feed(0, 1, 5.0)                        # staying LOW never counts again
        self.assertEqual(self.counts(), (1, 0, 1))
        self.feed(1, 1, 5.05)
        self.feed(0, 1, 5.10)                       # new edge long after the last count
        self.assertEqual(self.counts(), (2, 0, 2))
        self.feed(1, 1, 5.15)
        self.feed(0, 1, 5.20)                       # 0.10 s after the last count: bounce, ignored
        self.assertEqual(self.counts(), (2, 0, 2))
        self.assertEqual((self.g["waterjet_a_output"], self.g["hourly_output"], self.g["total_output"]), (2, 2, 2))

    def test_wj2_counts_each_high_to_low_edge_with_150ms_debounce(self):
        self.feed(1, 0, 0.0)
        self.assertEqual(self.counts(), (0, 1, 1))
        self.feed(1, 1, 0.05)
        self.feed(1, 0, 0.10)                       # 0.10 s later: bounce, ignored
        self.assertEqual(self.counts(), (0, 1, 1))
        self.feed(1, 1, 0.20)
        self.feed(1, 0, 0.30)                       # 0.30 s after the last count: counts
        self.assertEqual(self.counts(), (0, 2, 2))
        self.assertEqual(self.g["waterjet_b_output"], 2)

    def test_sensor_low_at_startup_does_not_count(self):
        self.g["wj1_prev"] = self.g["wj2_prev"] = 0
        self.feed(0, 0, 0.0)
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_edges_outside_normal_production_are_never_counted_later(self):
        self.feed(0, 0, 0.0, accept=False)          # e.g. REST
        self.feed(0, 0, 1.0, accept=True)           # still LOW back in NORMAL: no edge
        self.assertEqual(self.counts(), (0, 0, 0))

    def test_both_waterjets_add_up_and_drive_cycle_time(self):
        self.g["batch_run_time"] = 120.0
        self.feed(0, 0, 1.0)                        # both edges in the same sample
        self.assertEqual(self.counts(), (1, 1, 2))
        self.assertAlmostEqual(self.g["current_cycle_time"], 1.0)   # 120 s / 2 products / 60

    def test_model_change_resets_model_counts_but_not_shift_panels(self):
        self.g["receive_shift_form"](__import__("test_tablet_protocol").form(), datetime(2026, 11, 10, 10, 0))
        self.feed(1, 0, 0.0)
        self.g["mf_count"], self.g["forming_output"] = 3, 3
        self.g["push_hourly_to_sheets"](is_model_change=True)
        self.assertEqual((self.g["wj1_count"], self.g["wj2_count"], self.g["mf_count"], self.g["total_output"]), (0, 0, 0, 0))
        self.assertEqual((self.g["waterjet_b_output"], self.g["forming_output"], self.g["shift_total_output"]), (1, 3, 1))

    def test_shift_reset_clears_every_count(self):
        self.feed(1, 0, 0.0)
        self.g["forming_output"] = self.g["mf_count"] = 7
        self.g["reset_production_runtime_counters"]()
        self.assertEqual((self.g["wj2_count"], self.g["waterjet_b_output"], self.g["forming_output"], self.g["mf_count"]), (0, 0, 0, 0))

    def test_counters_survive_a_restart_snapshot(self):
        for key, value in (("waterjet_a_output", 3), ("waterjet_b_output", 4), ("forming_output", 5),
                           ("wj1_count", 1), ("wj2_count", 2), ("mf_count", 6)):
            self.g[key] = value
        counters = self.g["build_runtime_snapshot"]()["counters"]
        self.assertEqual([counters[k] for k in ("waterjet_a_output", "waterjet_b_output", "forming_output",
                                                "wj1_count", "wj2_count", "mf_count")], [3, 4, 5, 1, 2, 6])

    def test_live_payload_has_new_panels_and_site_fields(self):
        self.g["shift_total_output"], self.g["forming_output"] = 9, 4
        self.g["wj1_count"], self.g["wj2_count"], self.g["mf_count"] = 2, 3, 4
        payload = self.g["publish_live_data"]()
        self.assertEqual((payload["waterjet_output"], payload["total_product_count"], payload["forming_output"]), (9, 9, 4))
        self.assertEqual((payload["wj1_count"], payload["wj2_count"], payload["wj_total_count"], payload["mf_count"]), (2, 3, 5, 4))

    def test_line2_pins_and_timing(self):
        self.assertEqual((self.g["PROX_SENSOR_1_PIN"], self.g["PROX_SENSOR_2_PIN"], self.g["LIMIT_SWITCH_PIN"]), (5, 27, 17))
        self.assertEqual((self.g["WJ_COUNT_DEBOUNCE_TIME"], self.g["LIMIT_SWITCH_COUNT_DEBOUNCE_TIME"]), (0.15, 2.0))
        self.assertEqual(self.g["LINE_CODE"], "SDY2")

    def test_forming_limit_switch_no_longer_changes_main_output(self):
        with open(os.path.join(SYSTEM_DIR, "sdy2.py"), encoding="utf-8") as source_file:
            source = source_file.read()
        forming_block = source[source.index("# --- FORMING COUNTING"):source.index("# --- STATE LOGIC")]
        self.assertIn("forming_output += 1", forming_block)
        self.assertIn("mf_count += 1", forming_block)
        for main_counter in ("hourly_output +=", "total_output +=", "current_cycle_time ="):
            self.assertNotIn(main_counter, forming_block)


class SheetsUrlConfigTests(TrackerTestCase):
    def test_tracker_listens_for_its_config_topic(self):
        self.g["on_connect"](RecordingMQTTClient(), None, None, 0)
        topics = [entry for name, entry in PUBLISHED if name == "SUBSCRIBE"][0]
        self.assertIn(("sdy2/config", 1), topics)

    def test_valid_url_is_applied_saved_and_acknowledged(self):
        self.g["on_message"](None, None, message("sdy2/config", {"google_web_app_url": VALID_URL}))
        self.assertEqual(self.g["current_web_app_url"](), VALID_URL)
        self.assertEqual(self.store.get_setting("google_web_app_url"), VALID_URL)
        ack = results("sdy2/config_result")[-1]
        self.assertEqual((ack["status"], ack["google_web_app_url_source"]), ("applied", "smartchecksheet"))
        self.assertEqual(self.g["load_web_app_url"](), (VALID_URL, "smartchecksheet"))

    def test_invalid_url_is_refused_and_the_old_one_kept(self):
        before = self.g["current_web_app_url"]()
        self.g["on_message"](None, None, message("sdy2/config", {"google_web_app_url": "http://evil.example/exec"}))
        self.assertEqual(self.g["current_web_app_url"](), before)
        self.assertEqual(results("sdy2/config_result")[-1]["status"], "rejected")

    def test_workspace_links_are_accepted_and_lookalikes_refused(self):
        valid = self.g["is_valid_web_app_url"]
        self.assertTrue(valid("https://script.google.com/a/macros/example.com/s/AKfycbTESTDEPLOYMENT/exec"))
        self.assertTrue(valid(self.g["DEFAULT_WEB_APP_URL"]))
        for bad in ("https://script.google.com/macros/s/AKfycbTESTDEPLOYMENT/dev",
                    "https://script.google.com.evil.example/macros/s/AKfycbTESTDEPLOYMENT/exec",
                    "https://script.google.com/macros/s/AKfyc bTEST/exec"):
            self.assertFalse(valid(bad), bad)

    def test_empty_url_returns_to_the_built_in_fallback(self):
        self.g["apply_line_config"]({"google_web_app_url": VALID_URL})
        self.g["apply_line_config"]({"google_web_app_url": ""})
        self.assertEqual(self.g["current_web_app_url"](), self.g["DEFAULT_WEB_APP_URL"])
        self.assertIsNone(self.store.get_setting("google_web_app_url"))


class Response:
    def __init__(self, body):
        self.body, self.text = body, json.dumps(body)

    def raise_for_status(self):
        pass

    def json(self):
        return self.body


class DurableGoogleQueueTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.directory.name, "queue.db")
        self.store = ProductionSQLiteStore(self.path)

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def test_queued_rows_survive_a_restart(self):
        self.store.enqueue_google({"tab": "Hourly_Data", "row": [1], "request_id": "r1"})
        self.store.close()
        self.store = ProductionSQLiteStore(self.path)
        self.assertEqual(self.store.peek_google()["request_id"], "r1")

    def test_same_request_id_is_queued_once(self):
        self.assertTrue(self.store.enqueue_google({"tab": "Hourly_Data", "row": [1], "request_id": "r1"}))
        self.assertFalse(self.store.enqueue_google({"tab": "Hourly_Data", "row": [1], "request_id": "r1"}))
        self.assertEqual(self.store.google_queue_count(), 1)

    def test_failed_pdf_keeps_its_request_id_backs_off_and_blocks_later_items(self):
        for queue in (self.store, MemoryGoogleQueue()):
            calls = []

            def failing_post(url, json, timeout):
                calls.append((json, timeout))
                raise RuntimeError("simulated timeout")

            queue.enqueue_google({"action": "GENERATE_PDF", "shift_id": "S1", "request_id": "pdf-1"})
            queue.enqueue_google({"tab": "Hourly_Data", "row": [2], "request_id": "row-2"})
            self.assertFalse(send_next_google_item(queue, "https://x/exec", failing_post, now=100.0))
            self.assertEqual(calls[0][1], 120)
            self.assertEqual(queue.peek_google()["next_retry_at"], 105.0)
            self.assertFalse(send_next_google_item(queue, "https://x/exec", failing_post, now=104.0))
            self.assertEqual(len(calls), 1, "retry must wait for its backoff; later items must wait too")
            send_next_google_item(queue, "https://x/exec", failing_post, now=105.0)
            self.assertEqual(calls[1][0]["request_id"], "pdf-1")
            self.assertEqual(queue.peek_google()["next_retry_at"], 115.0)

            ok = lambda url, json, timeout: Response({"status": "success"})
            self.assertTrue(send_next_google_item(queue, "https://x/exec", ok, now=115.0))
            self.assertEqual(queue.peek_google()["request_id"], "row-2")
            self.assertTrue(send_next_google_item(queue, "https://x/exec", ok, now=115.0))
            self.assertIsNone(queue.peek_google())


class OutboxRetentionTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.store = ProductionSQLiteStore(os.path.join(self.directory.name, "outbox.db"))
        for event_id in ("old-delivered", "old-undelivered", "new-delivered"):
            self.store.save_state_and_enqueue("SDY2", {}, "ACTIVE", "hourly.finalized", "S1", {}, event_id=event_id)
        old = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
        db = self.store._connection
        db.execute("UPDATE outbox_events SET created_at=? WHERE event_id LIKE 'old-%'", (old,))

    def tearDown(self):
        self.store.close()
        self.directory.cleanup()

    def install_relay_ledger(self):
        db = self.store._connection
        db.execute("""CREATE TABLE checksheet_deliveries (event_id TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
            next_attempt REAL NOT NULL DEFAULT 0, delivered_at REAL, last_error TEXT)""")
        db.execute("INSERT INTO checksheet_deliveries(event_id, attempts, delivered_at) VALUES ('old-delivered', 1, 1.0)")
        db.execute("INSERT INTO checksheet_deliveries(event_id, attempts, delivered_at) VALUES ('new-delivered', 1, 1.0)")
        db.execute("INSERT INTO checksheet_deliveries(event_id, attempts, next_attempt) VALUES ('old-undelivered', 3, 9e9)")

    def remaining(self):
        return sorted(row[0] for row in self.store._connection.execute("SELECT event_id FROM outbox_events"))

    def test_nothing_is_deleted_before_the_relay_is_installed(self):
        self.assertEqual(self.store.prune_finished_events(api_enabled=False), 0)
        self.assertEqual(len(self.remaining()), 3)

    def test_only_old_events_the_relay_delivered_are_deleted(self):
        self.install_relay_ledger()
        self.assertEqual(self.store.prune_finished_events(api_enabled=False), 1)
        self.assertEqual(self.remaining(), ["new-delivered", "old-undelivered"])
        ledger = sorted(row[0] for row in self.store._connection.execute("SELECT event_id FROM checksheet_deliveries"))
        self.assertEqual(ledger, ["new-delivered", "old-undelivered"])

    def test_enabled_api_must_also_have_sent_the_event(self):
        self.install_relay_ledger()
        self.assertEqual(self.store.prune_finished_events(api_enabled=True), 0)
        self.store.mark_sent("old-delivered")
        self.assertEqual(self.store.prune_finished_events(api_enabled=True), 1)


if __name__ == "__main__":
    unittest.main()
