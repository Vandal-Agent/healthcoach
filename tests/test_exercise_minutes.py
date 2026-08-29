from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, call, patch

with patch("logging.basicConfig"):
    import app

from loseit_coaching import (
    build_food_coaching,
    build_weekly_health_report,
)

app.CHAT_ID = None


def tracker_row(
    day: str,
    *,
    exercise_minutes: str = "",
) -> list[str]:
    return [
        f"{day} 08:00 AM",
        "8000",
        "2500",
        "600",
        "7.0",
        "60",
        "230",
        "40",
        "1800",
        "120",
        exercise_minutes,
    ]


class ExerciseMinutesTests(unittest.TestCase):
    def test_legacy_rows_keep_exercise_missing(self) -> None:
        metrics = app.row_to_metrics(
            tracker_row("08/22/2026")[:10]
        )

        self.assertIsNone(metrics["exercise_minutes"])

    def test_current_rows_read_exercise_minutes(self) -> None:
        metrics = app.row_to_metrics(
            tracker_row(
                "08/22/2026",
                exercise_minutes="37",
            )
        )

        self.assertEqual(metrics["exercise_minutes"], 37.0)
        self.assertIn(
            "Exercise: 37 min",
            app.build_progress_message("Current status", metrics),
        )

    def test_schema_appends_only_the_new_column(self) -> None:
        sheet = MagicMock()
        sheet.col_count = 10

        changed = app.ensure_health_tracker_schema(sheet)

        self.assertTrue(changed)
        sheet.add_cols.assert_called_once_with(10)
        self.assertEqual(
            sheet.update_cell.call_args_list,
            [
                call(1, 11, "Exercise Minutes"),
                call(1, 12, "Cardio Fitness"),
                call(1, 13, "Walking Heart Rate Average"),
                call(1, 14, "Blood Pressure Systolic"),
                call(1, 15, "Blood Pressure Diastolic"),
                call(1, 16, "Blood Pressure Measured At"),
                call(1, 17, "RHR Measured At"),
                call(1, 18, "HRV Measured At"),
                call(1, 19, "Cardio Fitness Measured At"),
                call(1, 20, "Walking Heart Rate Measured At"),
            ],
        )

    def test_missing_webhook_value_preserves_existing_exercise(self) -> None:
        existing = tracker_row(
            "08/22/2026",
            exercise_minutes="42",
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

        self.assertEqual(
            sheet.update.call_args.kwargs["range_name"],
            "A2:T2",
        )
        merged = sheet.update.call_args.kwargs["values"][0]
        self.assertEqual(merged[10], "42")

    def test_webhook_accepts_exercise_minutes(self) -> None:
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
                json={"exercise_minutes": 31},
            )

        self.assertEqual(response.status_code, 200)
        saved_row = update.call_args.args[1]
        self.assertEqual(saved_row[10], 31.0)

    def test_health_history_summarizes_exercise(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 22),
            days=7,
            rows=[
                tracker_row(
                    "08/21/2026",
                    exercise_minutes="20",
                ),
                tracker_row(
                    "08/22/2026",
                    exercise_minutes="40",
                ),
            ],
        )

        self.assertEqual(
            history["average_exercise_minutes"],
            30.0,
        )
        self.assertEqual(history["exercise_entries"], 2)
        message = app.format_health_history(history)
        self.assertIn("exercise 40 min", message)
        self.assertIn("Average Exercise Minutes: 30 min", message)

    def test_coaching_and_weekly_report_show_exercise(self) -> None:
        coaching = build_food_coaching(
            total_burn=2500,
            steps=8000,
            exercise_minutes=35,
            food_data={
                "source": "food_ledger",
                "food_data_complete": False,
                "entry_count": 0,
                "totals": {},
            },
        )
        self.assertIn("Exercise: 35 min", coaching)

        weekly = build_weekly_health_report([
            {
                "date": "2026-08-21",
                "exercise_minutes": 20,
            },
            {
                "date": "2026-08-22",
                "exercise_minutes": 40,
            },
        ])
        self.assertIn("Average Exercise Minutes: 30", weekly)
        self.assertIn(
            "Days at or above 30 Exercise Minutes: 1/2",
            weekly,
        )


if __name__ == "__main__":
    unittest.main()
