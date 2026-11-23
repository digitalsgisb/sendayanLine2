"""Smart Checksheet tablet protocol: shift acknowledgements, pending shifts, shared downtime timer.

Runs the real tracker with fake GPIO/MQTT, so no hardware or network is touched.
"""
import json
import os
import runpy
import sys
import tempfile
import types
import unittest
from datetime import datetime
from unittest import mock

SYSTEM_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(SYSTEM_DIR, "sdy2.py")
if SYSTEM_DIR not in sys.path:
    sys.path.insert(0, SYSTEM_DIR)

PUBLISHED = []


class RecordingMQTTClient:
    def __init__(self, *_args, **_kwargs):
        self.on_connect = None
        self.on_message = None

    def subscribe(self, topics, *_args, **_kwargs):
        PUBLISHED.append(("SUBSCRIBE", topics))

    def publish(self, topic, payload=None, *_args, **_kwargs):
        PUBLISHED.append((topic, payload))

    def will_set(self, *_args, **_kwargs): pass
    def connect(self, *_args, **_kwargs): pass
    def loop_start(self): pass
    def loop_stop(self): pass
    def disconnect(self): pass


def load_tracker(database_path):
    """Run sdy2.py through one loop iteration and return the live module globals."""
    gpio = types.ModuleType("RPi.GPIO")
    gpio.BCM = gpio.IN = gpio.PUD_UP = 1
    gpio.setmode = gpio.setup = gpio.cleanup = lambda *_a, **_k: None
    gpio.input = lambda *_a, **_k: 1
    rpi = types.ModuleType("RPi")
    rpi.GPIO = gpio
    client_module = types.ModuleType("paho.mqtt.client")
    client_module.Client = RecordingMQTTClient
    mqtt_module = types.ModuleType("paho.mqtt")
    mqtt_module.client = client_module
    paho = types.ModuleType("paho")
    paho.mqtt = mqtt_module
    requests_module = types.ModuleType("requests")
    requests_module.post = lambda *_a, **_k: None
    modules = {"RPi": rpi, "RPi.GPIO": gpio, "paho": paho, "paho.mqtt": mqtt_module,
               "paho.mqtt.client": client_module, "requests": requests_module}
    with mock.patch.dict(sys.modules, modules), mock.patch.dict(
        os.environ, {"SDY2_SQLITE_PATH": database_path, "SDY2_API_ENABLED": "false"}
    ), mock.patch("time.sleep", side_effect=KeyboardInterrupt):
        copy = runpy.run_path(SCRIPT_PATH, run_name="__main__")
    # run_path returns a copy; the functions keep using the original globals.
    return copy["receive_shift_form"].__globals__


def results(topic):
    return [json.loads(payload) for name, payload in PUBLISHED if name == topic]


def form(date="2026-11-10", shift="Day", overtime=False, hours="8:00 AM to 4:15 PM"):
    return {"prodDate": date, "shift": shift, "overtime": overtime, "workingTime": hours,
            "productionLine": "Sendayan Line 2", "supervisor": "S", "lineLeader": "L",
            "formingOperator": ["A"], "waterjetOperator": ["B"], "assemblyOperator": ["C"],
            "qualityOperator": "Q", "event_id": "evt-" + date + shift}


def message(topic, payload):
    body = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
    return types.SimpleNamespace(topic=topic, payload=body)


class TabletProtocolTests(unittest.TestCase):
    def setUp(self):
        PUBLISHED.clear()
        self.directory = tempfile.TemporaryDirectory()
        self.g = load_tracker(os.path.join(self.directory.name, "sdy2.db"))
        self.events = []
        self.g["checkpoint_and_queue_event"] = lambda kind, payload, **_k: self.events.append((kind, payload))
        self.g["last_production_day"] = self.g["production_day_key"](datetime(2026, 11, 10, 10, 0))
        PUBLISHED.clear()

    def tearDown(self):
        self.g["persistence_store"] = None
        self.directory.cleanup()

    def test_live_snapshot_identifies_the_line_and_carries_tablet_fields(self):
        payload = self.g["publish_live_data"]()
        for key in ("line_code", "hourly_plan", "live_plan", "downtime_session", "pending_shift",
                    "server_ms", "supervisor", "forming_operator", "quality_inspector"):
            self.assertIn(key, payload)
        self.assertEqual(payload["line_code"], "SDY2")

    def test_listens_for_the_shared_downtime_topic(self):
        self.g["on_connect"](RecordingMQTTClient(), None, None, 0)
        topics = [entry for name, entry in PUBLISHED if name == "SUBSCRIBE"][0]
        self.assertIn(("nodered/downtime_session", 1), topics)

    def test_current_shift_is_acknowledged_as_active(self):
        self.assertTrue(self.g["receive_shift_form"](form(), datetime(2026, 11, 10, 10, 0)))
        self.assertEqual(results("sdy2/shift_form_result")[-1]["status"], "active")
        self.assertEqual(self.g["current_shift"]["shift_id"], "20261110-Day-SendayanLine2")
        self.assertEqual(self.events[-1][0], "shift.started")

    def test_future_shift_waits_instead_of_starting_early(self):
        self.assertTrue(self.g["receive_shift_form"](form(), datetime(2026, 11, 10, 6, 0)))
        self.assertEqual(results("sdy2/shift_form_result")[-1]["status"], "pending")
        self.assertEqual(self.g["current_shift"]["shift_id"], "NO PROD")
        self.assertEqual(self.g["pending_shift_form"]["shift"], "Day")
        # It starts by itself once its start time arrives.
        self.assertTrue(self.g["advance_shift_schedule"](datetime(2026, 11, 10, 8, 1)))
        self.assertEqual(self.g["current_shift"]["shift_id"], "20261110-Day-SendayanLine2")
        self.assertIsNone(self.g["pending_shift_form"])

    def test_finished_and_conflicting_forms_are_refused_with_a_reason(self):
        moment = datetime(2026, 11, 10, 10, 0)
        self.g["receive_shift_form"](form(), moment)
        PUBLISHED.clear()
        self.assertFalse(self.g["receive_shift_form"](form(date="2026-11-09"), moment))
        refused = results("sdy2/shift_form_result")[-1]
        self.assertEqual(refused["status"], "rejected")
        self.assertIn("already ended", refused["message"])
        # A refused form must not disturb the shift that is running.
        self.assertEqual(self.g["current_shift"]["shift_id"], "20261110-Day-SendayanLine2")

    def test_shared_downtime_timer_start_stop_log(self):
        self.g["receive_shift_form"](form(), datetime(2026, 11, 10, 10, 0))
        PUBLISHED.clear()
        data = {"downtimeCategory": "Machine", "machineIssue": "Jam", "presetTime": 0}
        started = self.g["process_downtime_command"]({"action": "start", "revision": 0, "command_id": "c1", "data": data})
        self.assertEqual(started["error"], "")
        self.assertEqual(started["state"]["status"], "running")
        self.assertEqual(self.g["current_mode"], "DOWN")
        session = started["state"]["id"]
        stale = self.g["process_downtime_command"]({"action": "stop", "revision": 0, "session_id": session})
        self.assertIn("Another device", stale["error"])
        stopped = self.g["process_downtime_command"]({"action": "stop", "revision": 1, "session_id": session, "command_id": "c2"})
        self.assertEqual(stopped["state"]["status"], "stopped")
        self.assertEqual(self.g["current_mode"], "NORMAL")
        logged = self.g["process_downtime_command"]({"action": "log", "revision": 2, "session_id": session, "command_id": "c3",
                                                      "data": {"actionTaken": "Cleared jam"}})
        self.assertEqual(logged["error"], "")
        self.assertEqual(self.g["downtime_session"]["status"], "idle")
        self.assertEqual(self.events[-1][0], "downtime.recorded")
        self.assertEqual(self.events[-1][1]["action_taken"], "Cleared jam")
        self.assertEqual([r["command_id"] for r in results("sdy2/downtime_result")], ["c1", None, "c2", "c3"])

    def test_timer_needs_an_active_shift_and_mode_is_locked_while_it_runs(self):
        start = {"action": "start", "revision": 0, "data": {"downtimeCategory": "Machine", "machineIssue": "Jam"}}
        self.assertIn("active production shift", self.g["process_downtime_command"](start)["error"])
        self.g["receive_shift_form"](form(), datetime(2026, 11, 10, 10, 0))
        self.g["process_downtime_command"](start)
        self.g["on_message"](None, None, message("nodered/mode", b"REST"))
        self.assertEqual(self.g["current_mode"], "DOWN")

    def test_tablet_count_adjustment_and_reject_event_fields(self):
        self.g["receive_shift_form"](form(), datetime(2026, 11, 10, 10, 0))
        self.g["hourly_output"] = self.g["total_output"] = self.g["shift_total_output"] = 5
        self.g["on_message"](None, None, message("nodered/adjust_count", {"adjust": -1, "reason": "double count", "event_id": "a1"}))
        self.assertEqual((self.g["hourly_output"], self.g["total_output"], self.g["shift_total_output"]), (4, 4, 4))
        self.assertEqual(self.events[-1][0], "count.adjusted")
        self.assertEqual(self.events[-1][1]["adjustment"], -1)
        self.g["on_message"](None, None, message("nodered/reject", {
            "totalSlabReject": 2, "slabRejectCode": "S1", "totalRejectNG": 1, "ngRejectCode": "N1", "event_id": "r1"}))
        kind, payload = self.events[-1]
        self.assertEqual(kind, "reject.recorded")
        self.assertEqual((payload["slab_quantity"], payload["slab_code"], payload["ng_code"]), (2, "S1", "N1"))

    def test_open_timer_is_closed_and_logged_when_the_shift_ends(self):
        self.g["receive_shift_form"](form(), datetime(2026, 11, 10, 10, 0))
        self.g["process_downtime_command"]({"action": "start", "revision": 0,
                                            "data": {"downtimeCategory": "Machine", "machineIssue": "Jam"}})
        self.g["execute_end_shift"](end_reason="TEST")
        self.assertEqual(self.g["downtime_session"]["status"], "idle")
        kinds = [kind for kind, _ in self.events]
        self.assertIn("downtime.recorded", kinds)
        self.assertIn("shift.ended", kinds)



class ShiftOverrideTests(unittest.TestCase):
    """checksheet/shift_override: end the active shift now and start the given one from zero."""
    setUp = TabletProtocolTests.setUp
    tearDown = TabletProtocolTests.tearDown

    def override(self, **changes):
        data = form(**{k: changes.pop(k) for k in ("date", "shift", "overtime", "hours") if k in changes})
        data.update({"overrideReason": "Wrong team", "overrideBy": "Developer", "event_id": "cmd-1"}, **changes)
        return data

    def test_listens_for_the_override_topic(self):
        self.g["on_connect"](RecordingMQTTClient(), None, None, 0)
        topics = [entry for name, entry in PUBLISHED if name == "SUBSCRIBE"][0]
        self.assertIn(("checksheet/shift_override", 0), topics)

    def test_next_override_shift_id(self):
        nxt = self.g["next_override_shift_id"]
        self.assertEqual(nxt("20261110-Day-SendayanLine2", "NO PROD"), "20261110-Day-SendayanLine2")
        self.assertEqual(nxt("20261110-Night-SendayanLine2", "20261110-Day-SendayanLine2"), "20261110-Night-SendayanLine2")
        self.assertEqual(nxt("20261110-Day-SendayanLine2", "20261110-Day-SendayanLine2"), "20261110-Day-SendayanLine2-R2")
        self.assertEqual(nxt("20261110-Day-SendayanLine2", "20261110-Day-SendayanLine2-R2"), "20261110-Day-SendayanLine2-R3")

    def test_override_ends_active_shift_and_restarts_it_from_zero(self):
        ended = []
        self.g["receive_shift_form"](form(), datetime(2026, 11, 10, 9, 0))
        self.g["execute_end_shift"] = lambda **kwargs: (ended.append(kwargs), self.g["reset_shift_data"]())
        self.g.update(shift_total_output=40, total_output=40, hourly_output=7)
        self.assertTrue(self.g["override_shift"](self.override(supervisor="New"), datetime(2026, 11, 10, 10, 0)))
        self.assertEqual(ended, [{"end_reason": "OVERRIDE SHIFT END"}])
        self.assertEqual(self.g["current_shift"]["shift_id"], "20261110-Day-SendayanLine2-R2")
        self.assertEqual(self.g["current_shift"]["supervisor"], "New")
        self.assertEqual((self.g["shift_total_output"], self.g["total_output"], self.g["hourly_output"]), (0, 0, 0))
        self.assertEqual(results("sdy2/shift_form_result")[-1]["status"], "active")
        self.assertEqual(self.events[-1][0], "shift.started")

    def test_override_can_start_night_before_its_usual_time(self):
        self.g["receive_shift_form"](form(), datetime(2026, 11, 10, 9, 0))
        self.g["execute_end_shift"] = lambda **kwargs: self.g["reset_shift_data"]()
        night = self.override(shift="Night", hours="4:15 PM to 12:30 AM")
        self.assertTrue(self.g["override_shift"](night, datetime(2026, 11, 10, 14, 0)))
        self.assertEqual(self.g["current_shift"]["shift_id"], "20261110-Night-SendayanLine2")
        self.assertEqual(self.g["current_shift"]["scheduled_start_at"], "2026-11-10T14:00:00")
        self.assertIsNone(self.g["pending_shift_form"])

    def test_override_without_reason_or_after_shift_end_changes_nothing(self):
        moment = datetime(2026, 11, 10, 10, 0)
        self.g["receive_shift_form"](form(), moment)
        self.g["shift_total_output"] = 9
        events = len(self.events)
        self.assertFalse(self.g["override_shift"](self.override(overrideReason=" "), moment))
        self.assertFalse(self.g["override_shift"](self.override(date="2026-11-09"), moment))
        self.assertEqual([r["status"] for r in results("sdy2/shift_form_result")[-2:]], ["rejected", "rejected"])
        self.assertEqual(self.g["current_shift"]["shift_id"], "20261110-Day-SendayanLine2")
        self.assertEqual(self.g["shift_total_output"], 9)
        self.assertEqual(len(self.events), events)

    def test_override_arrives_through_mqtt(self):
        self.g["receive_shift_form"](form(), datetime(2026, 11, 10, 9, 0))
        self.g["execute_end_shift"] = lambda **kwargs: self.g["reset_shift_data"]()
        self.g["override_shift"] = lambda data, moment=None: PUBLISHED.append(("OVERRIDE", data["overrideReason"]))
        self.g["on_message"](None, None, message("checksheet/shift_override", self.override()))
        self.assertIn(("OVERRIDE", "Wrong team"), PUBLISHED)


if __name__ == "__main__":
    unittest.main()
