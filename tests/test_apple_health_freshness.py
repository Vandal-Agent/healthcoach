from __future__ import annotations

from datetime import datetime
import unittest
from unittest.mock import MagicMock, patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 8, 28, 12, 0)
        if tz is not None:
            return tz.localize(value)
        return value


def tracker_row(
    *,
    source_day: str = "08/28/2026",
) -> list[str]:
    return [
        "08/28/2026 12:00 PM",
        "8000",
        "2500",
        "600",
        "7.0",
        "54",
        "230",
        "28",
        "1800",
        "120",
        "30",
        "30.5",
        "74",
        "",
        "",
        "",
        f"{source_day} 07:00 AM",
        f"{source_day} 07:05 AM",
        f"{source_day} 08:00 AM",
        f"{source_day} 09:00 AM",
    ]


class AppleHealthFreshnessTests(unittest.TestCase):
    def post_webhook(self, payload: dict):
        sheet = MagicMock()
        with (
            patch.object(app, "get_current_sheet", return_value=sheet),
            patch.object(
                app,
                "get_today_row_index_and_row",
                return_value=(None, None, [app.HEADERS]),
            ),
            patch.object(app, "update_or_insert_today") as update,
            patch.object(app, "datetime", FixedDateTime),
        ):
            response = app.app.test_client().post(
                "/webhook",
                json=payload,
            )
        return response, update.call_args.args[1]

    def test_webhook_saves_current_values_with_source_times(self) -> None:
        response, saved_row = self.post_webhook({
            "rhr": 54,
            "rhr_measured_at": "2026-08-28T07:00:00-07:00",
            "hrv": 28,
            "hrv_measured_at": "Aug 28, 2026 at 7:05 AM",
            "cardio_fitness": 30.5,
            "cardio_fitness_measured_at": "8/28/26, 8:00 AM",
            "walking_heart_rate_average": 74,
            "walking_heart_rate_measured_at": (
                "08/28/2026 09:00 AM"
            ),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved_row[5], 54.0)
        self.assertEqual(saved_row[7], 28.0)
        self.assertEqual(saved_row[11], 30.5)
        self.assertEqual(saved_row[12], 74.0)
        self.assertEqual(
            saved_row[16:20],
            [
                "08/28/2026 07:00 AM",
                "08/28/2026 07:05 AM",
                "08/28/2026 08:00 AM",
                "08/28/2026 09:00 AM",
            ],
        )

    def test_webhook_rejects_values_measured_on_an_older_day(self) -> None:
        response, saved_row = self.post_webhook({
            "rhr": 54,
            "rhr_measured_at": "2026-08-27T07:00:00-07:00",
            "hrv": 28,
            "hrv_measured_at": "2026-08-27T07:05:00-07:00",
            "cardio_fitness": 30.5,
            "cardio_fitness_measured_at": (
                "2026-08-27T08:00:00-07:00"
            ),
            "walking_heart_rate_average": 74,
            "walking_heart_rate_measured_at": (
                "2026-08-27T09:00:00-07:00"
            ),
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved_row[5], "")
        self.assertEqual(saved_row[7], "")
        self.assertEqual(saved_row[11], "")
        self.assertEqual(saved_row[12], "")
        self.assertEqual(saved_row[16:20], ["", "", "", ""])

    def test_legacy_payload_without_source_times_still_works(self) -> None:
        response, saved_row = self.post_webhook({
            "rhr": 54,
            "hrv": 28,
            "cardio_fitness": 30.5,
            "walking_heart_rate_average": 74,
        })

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            [saved_row[5], saved_row[7], saved_row[11], saved_row[12]],
            [54.0, 28.0, 30.5, 74.0],
        )
        self.assertEqual(saved_row[16:20], ["", "", "", ""])

    def test_tracker_parser_hides_stale_timestamped_values(self) -> None:
        stale = app.row_to_metrics(
            tracker_row(source_day="08/27/2026")
        )
        legacy = app.row_to_metrics(tracker_row()[:16])

        for key in (
            "rhr",
            "hrv",
            "cardio_fitness",
            "walking_heart_rate_average",
        ):
            self.assertIsNone(stale[key])
            self.assertIsNotNone(legacy[key])

    def test_status_displays_verified_measurement_times(self) -> None:
        metrics = app.row_to_metrics(tracker_row())

        message = app.build_progress_message("Current status", metrics)

        self.assertIn("Resting heart rate: 54 bpm at 7:00 AM", message)
        self.assertIn("HRV: 28 ms at 7:05 AM", message)
        self.assertIn(
            "Cardio fitness: 30.5 mL/kg/min at 8:00 AM",
            message,
        )
        self.assertIn("Walking heart rate: 74 bpm at 9:00 AM", message)

    def test_legacy_update_clears_an_unrelated_old_source_time(self) -> None:
        existing = tracker_row()
        incoming = tracker_row()
        incoming[5] = "55"
        incoming[16] = ""
        sheet = MagicMock()
        sheet.get_all_values.return_value = [app.HEADERS, existing]

        app.update_or_insert_today(
            sheet,
            incoming,
            datetime(2026, 8, 28, 13, 0),
        )

        merged = sheet.update.call_args.kwargs["values"][0]
        self.assertEqual(merged[5], "55")
        self.assertEqual(merged[16], "")
        self.assertEqual(
            sheet.update.call_args.kwargs["range_name"],
            "A2:T2",
        )

    def test_daily_insight_records_use_only_fresh_timestamped_values(
        self,
    ) -> None:
        record = app.build_daily_health_records([
            tracker_row(source_day="08/27/2026")
        ])[0]

        self.assertIsNone(record["resting_heart_rate"])
        self.assertIsNone(record["hrv"])
        self.assertIsNone(record["cardio_fitness"])
        self.assertIsNone(record["walking_heart_rate"])


if __name__ == "__main__":
    unittest.main()
