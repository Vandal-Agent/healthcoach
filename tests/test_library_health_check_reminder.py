from __future__ import annotations

from datetime import datetime
import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app


class LibraryHealthCheckReminderTests(unittest.TestCase):
    def test_new_day_resets_weekly_health_check_reminder(self) -> None:
        state = {
            "date": "08/29/2026",
            "library_health_check_reminder_sent": True,
            "telegram_update_offset": 12,
        }

        reset = app.reset_state_for_new_day(state, "08/30/2026")

        self.assertFalse(reset["library_health_check_reminder_sent"])
        self.assertEqual(reset["telegram_update_offset"], 12)

    def test_reminder_becomes_due_sunday_at_930(self) -> None:
        before = datetime(2026, 8, 30, 9, 29, tzinfo=app.PACIFIC_TZ)
        on_time = datetime(2026, 8, 30, 9, 30, tzinfo=app.PACIFIC_TZ)

        self.assertFalse(
            app.weekly_library_health_check_reminder_due(before, {})
        )
        self.assertTrue(
            app.weekly_library_health_check_reminder_due(on_time, {})
        )

    def test_late_start_sunday_still_sends_once(self) -> None:
        sunday_noon = datetime(
            2026,
            8,
            30,
            12,
            0,
            tzinfo=app.PACIFIC_TZ,
        )

        self.assertTrue(
            app.weekly_library_health_check_reminder_due(sunday_noon, {})
        )
        self.assertFalse(
            app.weekly_library_health_check_reminder_due(
                sunday_noon,
                {"library_health_check_reminder_sent": True},
            )
        )

    def test_reminder_is_not_due_on_another_day(self) -> None:
        monday = datetime(2026, 8, 31, 9, 30, tzinfo=app.PACIFIC_TZ)

        self.assertFalse(
            app.weekly_library_health_check_reminder_due(monday, {})
        )

    def test_reminder_explains_path_and_read_only_behavior(self) -> None:
        with patch.object(app, "send_telegram_msg", return_value=True) as send:
            sent = app.send_library_health_check_reminder()

        self.assertTrue(sent)
        message = send.call_args.args[0]
        self.assertIn("Sunday Food Library check-in", message)
        self.assertIn("Food Library → Library health check", message)
        self.assertIn("without changing anything automatically", message)


if __name__ == "__main__":
    unittest.main()
