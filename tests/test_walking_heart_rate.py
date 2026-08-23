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
    walking_heart_rate: str = "",
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
        "30",
        "31.5",
        walking_heart_rate,
    ]


class WalkingHeartRateTests(unittest.TestCase):
    def test_legacy_rows_keep_walking_heart_rate_missing(self) -> None:
        metrics = app.row_to_metrics(
            tracker_row("08/22/2026")[:12]
        )

        self.assertIsNone(metrics["walking_heart_rate_average"])
        self.assertIn(
            "Walking heart rate: not recorded",
            app.build_progress_message("Current status", metrics),
        )

    def test_status_shows_walking_heart_rate(self) -> None:
        metrics = app.row_to_metrics(
            tracker_row(
                "08/22/2026",
                walking_heart_rate="92.4",
            )
        )

        self.assertEqual(
            metrics["walking_heart_rate_average"],
            92.4,
        )
        self.assertIn(
            "Walking heart rate: 92.4 bpm",
            app.build_progress_message("Current status", metrics),
        )

    def test_status_hides_concatenated_walking_heart_rate(self) -> None:
        metrics = app.row_to_metrics(
            tracker_row(
                "08/22/2026",
                walking_heart_rate="707070700",
            )
        )

        self.assertIsNone(metrics["walking_heart_rate_average"])
        self.assertIn(
            "Walking heart rate: not recorded",
            app.build_progress_message("Current status", metrics),
        )

    def test_schema_appends_walking_heart_rate_column(self) -> None:
        sheet = MagicMock()
        sheet.col_count = 12

        changed = app.ensure_health_tracker_schema(sheet)

        self.assertTrue(changed)
        sheet.add_cols.assert_called_once_with(1)
        sheet.update_cell.assert_called_once_with(
            1,
            13,
            "Walking Heart Rate Average",
        )

    def test_missing_sync_preserves_existing_walking_heart_rate(self) -> None:
        existing = tracker_row(
            "08/22/2026",
            walking_heart_rate="92.4",
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
            "A2:M2",
        )
        merged = sheet.update.call_args.kwargs["values"][0]
        self.assertEqual(merged[12], "92.4")

    def test_webhook_accepts_walking_heart_rate(self) -> None:
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
                json={"walking_heart_rate_average": 92.4},
            )

        self.assertEqual(response.status_code, 200)
        saved_row = update.call_args.args[1]
        self.assertEqual(saved_row[12], 92.4)

    def test_webhook_ignores_concatenated_walking_heart_rate(self) -> None:
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
                json={
                    "walking_heart_rate_average": 707070700,
                },
            )

        self.assertEqual(response.status_code, 200)
        saved_row = update.call_args.args[1]
        self.assertEqual(saved_row[12], "")

    def test_health_history_summarizes_walking_heart_rate(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 22),
            days=7,
            rows=[
                tracker_row(
                    "08/21/2026",
                    walking_heart_rate="90",
                ),
                tracker_row(
                    "08/22/2026",
                    walking_heart_rate="94",
                ),
            ],
        )

        self.assertEqual(history["average_walking_heart_rate"], 92.0)
        self.assertEqual(history["walking_heart_rate_change"], 4.0)
        self.assertEqual(history["walking_heart_rate_entries"], 2)
        message = app.format_health_history(history)
        self.assertIn("walking HR 94 bpm", message)
        self.assertIn("Average walking heart rate: 92 bpm", message)
        self.assertIn(
            "Recorded walking heart-rate change: +4.0 bpm",
            message,
        )
        self.assertIn(
            "Walking heart rate recorded: 2/7 days",
            message,
        )


if __name__ == "__main__":
    unittest.main()
