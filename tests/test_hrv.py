from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import MagicMock, patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


def tracker_row(
    day: str,
    *,
    hrv: str = "",
) -> list[str]:
    return [
        f"{day} 08:00 AM",
        "8000",
        "2500",
        "600",
        "7.0",
        "55",
        "230",
        hrv,
        "1800",
        "120",
        "30",
        "31.5",
        "70",
        "120",
        "78",
        f"{day} 07:00 AM",
    ]


class HrvReportingTests(unittest.TestCase):
    def test_webhook_accepts_positive_and_ignores_zero_hrv(self) -> None:
        for supplied, expected in ((28.2, 28.2), (0, "")):
            with self.subTest(supplied=supplied):
                sheet = MagicMock()
                with (
                    patch.object(
                        app,
                        "get_current_sheet",
                        return_value=sheet,
                    ),
                    patch.object(
                        app,
                        "get_today_row_index_and_row",
                        return_value=(None, None, [app.HEADERS]),
                    ),
                    patch.object(
                        app,
                        "update_or_insert_today",
                    ) as update,
                ):
                    response = app.app.test_client().post(
                        "/webhook",
                        json={"hrv": supplied},
                    )

                self.assertEqual(response.status_code, 200)
                self.assertEqual(
                    update.call_args.args[1][7],
                    expected,
                )

    def test_missing_and_zero_hrv_remain_not_recorded(self) -> None:
        missing = app.row_to_metrics(
            tracker_row("08/23/2026")
        )
        zero = app.row_to_metrics(
            tracker_row("08/23/2026", hrv="0")
        )

        self.assertIsNone(missing["hrv"])
        self.assertIsNone(zero["hrv"])
        self.assertIn(
            "HRV: not recorded",
            app.build_progress_message("Current status", missing),
        )

    def test_current_status_shows_recorded_hrv(self) -> None:
        metrics = app.row_to_metrics(
            tracker_row("08/23/2026", hrv="28.2")
        )

        self.assertEqual(metrics["hrv"], 28.2)
        self.assertIn(
            "HRV: 28.2 ms",
            app.build_progress_message("Current status", metrics),
        )

    def test_health_history_summarizes_hrv(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 23),
            days=7,
            rows=[
                tracker_row("08/22/2026", hrv="27.0"),
                tracker_row("08/23/2026", hrv="30.0"),
            ],
        )

        self.assertEqual(history["average_hrv"], 28.5)
        self.assertEqual(history["hrv_change"], 3.0)
        self.assertEqual(history["hrv_entries"], 2)

        message = app.format_health_history(history)
        self.assertIn("HRV 30 ms", message)
        self.assertIn("Average HRV: 28.5 ms", message)
        self.assertIn("Recorded HRV change: +3.0 ms", message)
        self.assertIn("HRV recorded: 2/7 days", message)

    def test_heart_health_report_includes_hrv_without_rating(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 23),
            days=7,
            rows=[
                tracker_row("08/22/2026", hrv="27.0"),
                tracker_row("08/23/2026", hrv="30.0"),
            ],
        )

        message = app.format_heart_health_report(history)

        self.assertIn(
            "HRV: average 28.5 ms; recorded change +3.0 ms; "
            "recorded 2/7 days",
            message,
        )
        self.assertNotIn("good HRV", message)
        self.assertNotIn("poor HRV", message)
        self.assertIn("does not diagnose", message)


if __name__ == "__main__":
    unittest.main()
