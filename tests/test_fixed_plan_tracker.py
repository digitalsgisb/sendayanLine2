"""The real tracker with fake GPIO/MQTT: the tablet plan is fixed for the hour and a model/lot change splits the hour."""
import os
import tempfile
import unittest
from datetime import datetime

from test_tablet_protocol import PUBLISHED, load_tracker, results


class Clock(datetime):
    moment = datetime(2026, 11, 24, 10, 20)

    @classmethod
    def now(cls, tz=None):
        return cls.moment if tz is None else cls.moment.astimezone(tz)


class FixedPlanTrackerTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.g = load_tracker(os.path.join(self.directory.name, "line.db"))
        self.g["checkpoint_and_queue_event"] = lambda *_a, **_k: None
        self.g["queue_google_sheet_row"] = lambda *_a, **_k: None
        self.g["datetime"] = Clock
        Clock.moment = datetime(2026, 11, 24, 10, 20)
        self.g["current_shift"].update(shift_id="20261124-Day-L1", model="D27A", lot_number="L1", standard_cycle=1.0,
                                       scheduled_start_at="2026-11-24T08:00:00", scheduled_end_at="2026-11-24T20:00:00")
        self.g["hourly_plan_state"].update(planned_lost=0.0, segment_started_at=None)
        self.g["current_mode"] = self.g["MODE_NORMAL"]
        PUBLISHED.clear()

    def tearDown(self):
        self.g["persistence_store"] = None
        self.directory.cleanup()

    def test_tablet_plan_is_the_full_hour_and_machine_downtime_does_not_lower_it(self):
        self.g["base_time_this_hour"], self.g["lost_time_this_hour"] = 20 * 60, 10 * 60  # 10 min of machine downtime
        self.assertEqual(self.g["publish_live_data"]()["live_plan"], 60)
        self.g["hourly_plan_state"]["planned_lost"] = 15 * 60  # a 15-minute planned stop
        self.assertEqual(self.g["publish_live_data"]()["live_plan"], 45)

    def test_a_model_change_closes_the_old_model_row_with_its_own_minutes(self):
        self.g["hourly_output"], self.g["base_time_this_hour"] = 18, 20 * 60
        self.g["on_message"](None, None, type("M", (), {"topic": "nodered/modeltarget", "payload": b'{"model": "D74A", "lot_number": "L9", "standard_cycle": 2.0}'})())
        row = results("smartchecksheet/hourly")[-1]
        self.assertEqual((row["model"], row["lot_number"], row["hour_slot"], row["plan"], row["actual"]), ("D27A", "L1", "10.00-11.00", 20, 18))
        self.assertEqual(self.g["current_shift"]["model"], "D74A")
        # The new model's part runs 10:20-11:00 at its own cycle (2 min): 20.
        self.assertEqual(self.g["publish_live_data"]()["live_plan"], 20)
        Clock.moment = datetime(2026, 11, 24, 11, 0, 1)
        self.g["hourly_output"] = 19
        self.g["push_hourly_to_sheets"](is_model_change=False)
        row = results("smartchecksheet/hourly")[-1]
        self.assertEqual((row["model"], row["hour_slot"], row["plan"], row["actual"]), ("D74A", "10.00-11.00", 20, 19))

    def test_a_lot_change_splits_without_resetting_the_model_count(self):
        self.g["hourly_output"], self.g["base_time_this_hour"], self.g["total_output"] = 5, 20 * 60, 40
        self.g["close_segment_for_setup"]({"lot_number": "L2"})
        row = results("smartchecksheet/hourly")[-1]
        self.assertEqual((row["lot_number"], row["plan"], row["actual"]), ("L1", 20, 5))
        self.assertEqual(self.g["total_output"], 40)

    def test_changeover_with_nothing_made_stays_with_the_new_model(self):
        self.g["current_mode"] = self.g["MODE_MODEL_CHANGE"]
        self.g["hourly_output"], self.g["base_time_this_hour"] = 0, 5 * 60
        self.g["close_segment_for_setup"]({"model": "D74A"})
        self.assertEqual(results("smartchecksheet/hourly"), [])


    def test_one_changeover_makes_no_empty_rows(self):
        """25/11 SDY1: Model Change on, off, model sent, on again, model sent again - four empty rows before; none now."""
        self.g["hourly_output"], self.g["base_time_this_hour"], self.g["total_output"] = 0, 5 * 60, 120
        message = lambda topic, payload: self.g["on_message"](None, None, type("M", (), {"topic": topic, "payload": payload})())
        message(self.g["MQTT_TOPIC_MODE"], b"MODEL CHANGE")
        message(self.g["MQTT_TOPIC_MODE"], b"NORMAL")
        message("nodered/modeltarget", b'{"model": "3N0_HEV", "lot_number": "4G.0281-A"}')
        message(self.g["MQTT_TOPIC_MODE"], b"MODEL CHANGE")
        message("nodered/modeltarget", b'{"model": "3N0_HEV", "lot_number": "4G.0281-A"}')
        self.assertEqual(results("smartchecksheet/hourly"), [])
        self.assertEqual((self.g["current_shift"]["model"], self.g["total_output"]), ("3N0_HEV", 0))


if __name__ == "__main__":
    unittest.main()
