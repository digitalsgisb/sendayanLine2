import ast
from datetime import datetime, timedelta
import json
import os
import unittest
import uuid


SCRIPT_PATH = os.path.join(os.path.dirname(os.path.dirname(__file__)), "sdy2.py")


def read_source():
    with open(SCRIPT_PATH, encoding="utf-8") as script_file:
        return script_file.read()


def load_window_functions():
    source = read_source()
    tree = ast.parse(source, filename=SCRIPT_PATH)
    names = {
        "parse_shift_entry_datetime",
        "shift_entry_window_is_active",
        "clear_shift_entry_window",
        "start_shift_entry_window",
        "expire_shift_entry_window",
    }
    functions = [
        node for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    calls = {"reset": 0, "checkpoint": 0}
    namespace = {
        "datetime": datetime,
        "timedelta": timedelta,
        "SHIFT_ENTRY_GRACE_MINUTES": 45,
        "shift_entry_window_started_at": None,
        "shift_entry_window_ends_at": None,
        "shift_total_output": 7,
        "reset_production_runtime_counters": lambda: calls.__setitem__("reset", calls["reset"] + 1),
        "checkpoint_runtime_state": lambda **_kwargs: calls.__setitem__("checkpoint", calls["checkpoint"] + 1),
    }
    exec(compile(ast.Module(body=functions, type_ignores=[]), SCRIPT_PATH, "exec"), namespace)
    return namespace, calls


class RuntimeFeatureTests(unittest.TestCase):
    def test_line2_sensor_and_timer_rules_are_preserved(self):
        source = read_source()
        self.assertIn("LIMIT_SWITCH_COUNT_DEBOUNCE_TIME = 2.0", source)
        self.assertIn("current_machine_state = get_stable_machine_state(accept_counts=production_tracking_enabled)", source)
        self.assertIn("WJ_COUNT_DEBOUNCE_TIME = 0.15", source)
        self.assertIn("if duration < 10:", source)
        self.assertIn("current_cycle_time = (batch_run_time / total_output) / 60.0", source)

    def test_shared_lifecycle_features_are_wired(self):
        source = read_source()
        self.assertIn("STATUS_WAITING_FOR_SHIFT = 7", source)
        self.assertIn("advance_shift_schedule(now)", source)
        self.assertIn("production_tracking_enabled = active_shift or buffering_shift_entry", source)
        self.assertIn("restore_runtime_state()", source)
        self.assertIn('"action": "GENERATE_PDF"', source)
        self.assertIn("GoogleSheetsSender(google_queue, current_web_app_url, requests.post)", source)
        self.assertNotIn("process_gsheets_queue()", source, "Sheets must never be sent from the sensor loop")
        self.assertLess(source.index("restore_runtime_state()"), source.index("mqtt_client = mqtt.Client()"))

    def test_google_request_id_is_deterministic_for_logical_retries(self):
        source = read_source()
        tree = ast.parse(source, filename=SCRIPT_PATH)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "build_google_request_id"
        )
        namespace = {"json": json, "uuid": uuid, "LINE_CODE": "SDY2"}
        exec(compile(ast.Module(body=[function], type_ignores=[]), SCRIPT_PATH, "exec"), namespace)

        row = ["SHIFT-1", "2026-08-03", "M1", "1.00-2.00", 17, 19, "LOT", 0]
        first = namespace["build_google_request_id"]("Hourly_Data", row)
        second = namespace["build_google_request_id"]("Hourly_Data", list(row))
        self.assertEqual(first, second)
        self.assertNotEqual(
            first,
            namespace["build_google_request_id"]("Hourly_Data", row[:-1] + [1]),
        )

    def test_entry_window_is_exactly_45_minutes(self):
        namespace, calls = load_window_functions()
        boundary = datetime.now()
        self.assertTrue(namespace["start_shift_entry_window"](boundary))
        self.assertTrue(namespace["shift_entry_window_is_active"](boundary + timedelta(minutes=44, seconds=59)))
        self.assertFalse(namespace["shift_entry_window_is_active"](boundary + timedelta(minutes=45)))
        self.assertEqual(calls["reset"], 0)

    def test_expired_window_discards_unassigned_counts(self):
        namespace, calls = load_window_functions()
        boundary = datetime.now()
        namespace["start_shift_entry_window"](boundary)
        self.assertTrue(namespace["expire_shift_entry_window"](boundary + timedelta(minutes=45)))
        self.assertEqual(calls["reset"], 1)
        self.assertIsNone(namespace["shift_entry_window_ends_at"])

    def test_shift_end_finalizes_hour_before_pdf(self):
        source = read_source()
        tree = ast.parse(source, filename=SCRIPT_PATH)
        function = next(
            node for node in tree.body
            if isinstance(node, ast.FunctionDef) and node.name == "execute_end_shift"
        )
        actions = []


        namespace = {
            "current_shift": {
                "shift_id": "TEST-SHIFT", "model": "M1", "lot_number": "L1",
                "scheduled_end_at": "2026-08-03T20:00:00",
            },
            "hourly_output": 3,
            "base_time_this_hour": 1.0,
            "lost_time_this_hour": 0.0,
            "hourly_rest_time": 0.0,
            "downtime_session": {"status": "idle"},
            "shift_total_output": 3,
            "total_output": 3,
            "total_rejects": 0,
            "waterjet_a_output": 2,
            "waterjet_b_output": 1,
            "forming_output": 4,
            "wj1_count": 1,
            "wj2_count": 1,
            "mf_count": 4,
            "LINE_CODE": "SDY2",
            "datetime": datetime,
            "enqueue_google_item": lambda item: actions.append("pdf" if item["action"] == "GENERATE_PDF" else "?"),
            "build_google_request_id": lambda *_args: "pdf-request-id",
            "push_hourly_to_sheets": lambda **_kwargs: actions.append("hourly"),
            "reset_shift_data": lambda **_kwargs: actions.append("reset"),
            "checkpoint_and_queue_event": lambda *_args, **_kwargs: actions.append("event"),
        }
        exec(compile(ast.Module(body=[function], type_ignores=[]), SCRIPT_PATH, "exec"), namespace)
        namespace["execute_end_shift"]("19.00-20.00", "TEST")
        self.assertEqual(actions, ["hourly", "reset", "event", "pdf"])

if __name__ == "__main__":
    unittest.main()
