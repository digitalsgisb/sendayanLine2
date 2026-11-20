from datetime import datetime
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sdy_shift_schedule import calculate_shift_end_datetime, hour_slot_for_shift_end


class ShiftScheduleTests(unittest.TestCase):
    def test_day_without_overtime(self):
        end = calculate_shift_end_datetime(
            "2026-08-03", "Day", False, "8:00 AM to 4:15 PM"
        )
        self.assertEqual(end, datetime(2026, 8, 3, 16, 15))
        self.assertEqual(hour_slot_for_shift_end(end), "16.00-17.00")

    def test_day_with_overtime(self):
        end = calculate_shift_end_datetime(
            "2026-08-03", "Day", True, "8:00 AM to 8:00 PM"
        )
        self.assertEqual(end, datetime(2026, 8, 3, 20, 0))
        self.assertEqual(hour_slot_for_shift_end(end), "19.00-20.00")

    def test_night_without_overtime_crosses_midnight(self):
        end = calculate_shift_end_datetime(
            "2026-08-03", "Night", False, "4:15 PM to 12:30 AM"
        )
        self.assertEqual(end, datetime(2026, 8, 4, 0, 30))

    def test_night_with_overtime_crosses_midnight(self):
        end = calculate_shift_end_datetime(
            "2026-08-03", "Night", True, "8:00 PM to 8:00 AM"
        )
        self.assertEqual(end, datetime(2026, 8, 4, 8, 0))

    def test_fallback_uses_shift_and_overtime(self):
        end = calculate_shift_end_datetime("2026-08-03", "Day", "true", "-")
        self.assertEqual(end, datetime(2026, 8, 3, 20, 0))


if __name__ == "__main__":
    unittest.main()
