from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


def tracker_row(
    day: str,
    *,
    weight: str = "",
    sleep: str = "",
    resting_heart_rate: str = "",
    exercise: str = "",
    cardio_fitness: str = "",
    walking_heart_rate: str = "",
    systolic: str = "",
    diastolic: str = "",
    measured_at: str = "",
) -> list[str]:
    return [
        f"{day} 08:00 AM",
        "8000",
        "2500",
        "600",
        sleep,
        resting_heart_rate,
        weight,
        "40",
        "1800",
        "120",
        exercise,
        cardio_fitness,
        walking_heart_rate,
        systolic,
        diastolic,
        measured_at,
    ]


class HeartHealthReportTests(unittest.TestCase):
    def sample_history(self) -> dict:
        return app.build_health_history_data(
            reference_date=date(2026, 8, 23),
            days=7,
            rows=[
                tracker_row(
                    "08/22/2026",
                    weight="230.3",
                    sleep="6.2",
                    resting_heart_rate="53",
                    exercise="17",
                    cardio_fitness="30.5",
                    walking_heart_rate="70",
                ),
                tracker_row(
                    "08/23/2026",
                    weight="231.0",
                    sleep="6.7",
                    resting_heart_rate="49",
                    exercise="0",
                    cardio_fitness="30.7",
                    walking_heart_rate="69",
                    systolic="107",
                    diastolic="74",
                    measured_at="08/23/2026 06:52 AM",
                ),
            ],
        )

    def test_reports_menu_contains_heart_health(self) -> None:
        message = app.healthcoach_reports_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("4. Heart health", message)
        self.assertIn(
            ["Heart health"],
            keyboard["keyboard"],
        )
        self.assertIn("5. Back", message)

    def test_reports_routes_to_heart_health_period_menu(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "reports",
            "known_data": {},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Heart health",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "heart_health_report",
        )
        self.assertIn(
            "Choose how much recorded heart-health history",
            send.call_args.args[0],
        )

    def test_period_selection_loads_report(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "heart_health_report",
            "known_data": {},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "get_formatted_heart_health_report",
                return_value="Heart Health Report - Last 14 Days",
            ) as get_report,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "14 days",
                }
            })

        self.assertEqual(get_report.call_args.kwargs["days"], 14)
        self.assertIn("Last 14 Days", send.call_args.args[0])

    def test_back_returns_to_reports_menu(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "heart_health_report",
            "known_data": {},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Back",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "reports",
        )
        self.assertIn("Reports Menu", send.call_args.args[0])

    def test_report_summarizes_recorded_trends_without_rating(self) -> None:
        message = app.format_heart_health_report(
            self.sample_history()
        )

        self.assertIn("Heart Health Report - Last 7 Days", message)
        self.assertIn("Resting heart rate: average 51 bpm", message)
        self.assertIn("recorded change -4.0 bpm", message)
        self.assertIn("Cardio Fitness: average 30.6 mL/kg/min", message)
        self.assertIn("Walking heart rate: average 69.5 bpm", message)
        self.assertIn("Blood pressure: average 107/74 mmHg", message)
        self.assertIn("latest 107/74 mmHg on Sun Aug 23", message)
        self.assertIn("Exercise Minutes: average 8.5 min", message)
        self.assertIn("Heart-Healthy Pick", message)
        self.assertIn("does not diagnose", message)
        self.assertNotIn("normal blood pressure", message.lower())
        self.assertNotIn("risk level", message.lower())

    def test_missing_measurements_remain_not_available(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 23),
            days=30,
            rows=[],
        )

        message = app.format_heart_health_report(history)

        self.assertIn("average not available", message)
        self.assertIn("recorded 0/30 days", message)
        self.assertIn(
            "Missing readings stay missing and are never treated as zero",
            message,
        )
        self.assertLessEqual(len(message), 4096)


if __name__ == "__main__":
    unittest.main()
