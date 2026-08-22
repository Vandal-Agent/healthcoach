from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


def tracker_row(
    day: str,
    *,
    resting_heart_rate: str = "",
    exercise_minutes: str = "",
) -> list[str]:
    return [
        f"{day} 08:00 AM",
        "8000",
        "2500",
        "600",
        "7.0",
        resting_heart_rate,
        "230",
        "40",
        "1800",
        "120",
        exercise_minutes,
    ]


class RestingHeartRateTests(unittest.TestCase):
    def test_missing_resting_heart_rate_remains_missing(self) -> None:
        metrics = app.row_to_metrics(tracker_row("08/22/2026"))

        self.assertIsNone(metrics["rhr"])
        self.assertIn(
            "Resting heart rate: not recorded",
            app.build_progress_message("Current status", metrics),
        )

    def test_status_shows_existing_resting_heart_rate(self) -> None:
        metrics = app.row_to_metrics(
            tracker_row(
                "08/22/2026",
                resting_heart_rate="62",
            )
        )

        self.assertEqual(metrics["rhr"], 62.0)
        self.assertIn(
            "Resting heart rate: 62 bpm",
            app.build_progress_message("Current status", metrics),
        )

    def test_missing_sync_preserves_existing_resting_heart_rate(self) -> None:
        existing = tracker_row(
            "08/22/2026",
            resting_heart_rate="61",
        )
        incoming = tracker_row("08/22/2026")
        sheet = MagicMock()
        sheet.get_all_values.return_value = [
            app.HEADERS,
            existing,
        ]

        app.update_or_insert_today(
            sheet,
            incoming,
            datetime(2026, 8, 22, 9, 0),
        )

        merged = sheet.update.call_args.kwargs["values"][0]
        self.assertEqual(merged[5], "61")

    def test_webhook_accepts_existing_rhr_key(self) -> None:
        sheet = MagicMock()
        with (
            patch.object(app, "get_current_sheet", return_value=sheet),
            patch.object(
                app,
                "get_today_row_index_and_row",
                return_value=(None, None, [app.HEADERS]),
            ),
            patch.object(app, "update_or_insert_today") as update,
        ):
            response = app.app.test_client().post(
                "/webhook",
                json={"rhr": 63},
            )

        self.assertEqual(response.status_code, 200)
        saved_row = update.call_args.args[1]
        self.assertEqual(saved_row[5], 63.0)

    def test_health_history_summarizes_resting_heart_rate(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 22),
            days=7,
            rows=[
                tracker_row(
                    "08/21/2026",
                    resting_heart_rate="60",
                ),
                tracker_row(
                    "08/22/2026",
                    resting_heart_rate="64",
                ),
            ],
        )

        self.assertEqual(history["average_resting_heart_rate"], 62.0)
        self.assertEqual(history["resting_heart_rate_entries"], 2)
        message = app.format_health_history(history)
        self.assertIn("resting HR 64 bpm", message)
        self.assertIn("Average resting heart rate: 62 bpm", message)
        self.assertIn("Resting heart rate recorded: 2/7 days", message)


if __name__ == "__main__":
    unittest.main()
