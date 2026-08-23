from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import MagicMock, patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 8, 22, 12, 0)
        if tz is not None:
            return tz.localize(value)
        return value


def tracker_row(
    day: str,
    *,
    systolic: str = "",
    diastolic: str = "",
    measured_at: str = "",
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
        "70",
        systolic,
        diastolic,
        measured_at,
    ]


class BloodPressureTests(unittest.TestCase):
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

    def test_status_shows_paired_reading_and_measurement_time(self):
        metrics = app.row_to_metrics(
            tracker_row(
                "08/22/2026",
                systolic="121",
                diastolic="79",
                measured_at="08/22/2026 07:15 AM",
            )
        )

        message = app.build_progress_message("Current status", metrics)
        self.assertIn(
            "Blood pressure: 121/79 mmHg at 7:15 AM",
            message,
        )

    def test_row_hides_unpaired_or_wrong_day_reading(self):
        unpaired = app.row_to_metrics(
            tracker_row(
                "08/22/2026",
                systolic="121",
                measured_at="08/22/2026 07:15 AM",
            )
        )
        stale = app.row_to_metrics(
            tracker_row(
                "08/22/2026",
                systolic="121",
                diastolic="79",
                measured_at="08/21/2026 07:15 AM",
            )
        )

        self.assertIsNone(unpaired["blood_pressure_systolic"])
        self.assertIsNone(stale["blood_pressure_systolic"])

    def test_webhook_accepts_current_iso_timestamp(self):
        response, saved_row = self.post_webhook(
            {
                "blood_pressure_systolic": 121,
                "blood_pressure_diastolic": 79,
                "blood_pressure_measured_at": (
                    "2026-08-22T07:15:00-07:00"
                ),
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved_row[13:15], [121.0, 79.0])
        self.assertEqual(saved_row[15], "08/22/2026 07:15 AM")

    def test_webhook_accepts_direct_iphone_start_date_text(self):
        response, saved_row = self.post_webhook(
            {
                "blood_pressure_systolic": 121,
                "blood_pressure_diastolic": 79,
                "blood_pressure_measured_at": (
                    "8/22/26, 7:15\u202fAM"
                ),
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved_row[13:15], [121.0, 79.0])
        self.assertEqual(saved_row[15], "08/22/2026 07:15 AM")

    def test_webhook_rejects_multiple_iphone_dates(self):
        response, saved_row = self.post_webhook(
            {
                "blood_pressure_systolic": 121,
                "blood_pressure_diastolic": 79,
                "blood_pressure_measured_at": (
                    "8/22/26, 12:00\u202fPM\n"
                    "8/22/26, 7:00\u202fAM"
                ),
            }
        )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(saved_row[13:16], ["", "", ""])

    def test_missing_sync_preserves_existing_paired_reading(self):
        existing = tracker_row(
            "08/22/2026",
            systolic="121",
            diastolic="79",
            measured_at="08/22/2026 07:15 AM",
        )
        incoming = tracker_row("08/22/2026")[:13]
        sheet = MagicMock()
        sheet.get_all_values.return_value = [app.HEADERS, existing]

        app.update_or_insert_today(
            sheet,
            incoming,
            datetime(2026, 8, 22, 9, 0),
        )

        self.assertEqual(
            sheet.update.call_args.kwargs["range_name"],
            "A2:P2",
        )
        merged = sheet.update.call_args.kwargs["values"][0]
        self.assertEqual(
            merged[13:16],
            ["121", "79", "08/22/2026 07:15 AM"],
        )

    def test_webhook_rejects_partial_stale_and_malformed_readings(self):
        payloads = [
            {
                "blood_pressure_systolic": 121,
                "blood_pressure_measured_at": (
                    "2026-08-22T07:15:00-07:00"
                ),
            },
            {
                "blood_pressure_systolic": 121,
                "blood_pressure_diastolic": 79,
                "blood_pressure_measured_at": (
                    "2026-08-21T07:15:00-07:00"
                ),
            },
            {
                "blood_pressure_systolic": 121121,
                "blood_pressure_diastolic": 7979,
                "blood_pressure_measured_at": (
                    "2026-08-22T07:15:00-07:00"
                ),
            },
        ]

        for payload in payloads:
            with self.subTest(payload=payload):
                response, saved_row = self.post_webhook(payload)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(saved_row[13:16], ["", "", ""])

    def test_health_history_summarizes_only_valid_pairs(self):
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 22),
            days=7,
            rows=[
                tracker_row(
                    "08/21/2026",
                    systolic="120",
                    diastolic="80",
                    measured_at="08/21/2026 07:00 AM",
                ),
                tracker_row(
                    "08/22/2026",
                    systolic="124",
                    diastolic="76",
                    measured_at="08/22/2026 07:15 AM",
                ),
            ],
        )

        self.assertEqual(
            history["average_blood_pressure_systolic"],
            122.0,
        )
        self.assertEqual(
            history["average_blood_pressure_diastolic"],
            78.0,
        )
        self.assertEqual(history["blood_pressure_entries"], 2)
        message = app.format_health_history(history)
        self.assertIn("blood pressure 124/76 mmHg", message)
        self.assertIn("Average blood pressure: 122/78 mmHg", message)
        self.assertIn("Blood pressure recorded: 2/7 days", message)

    def test_thirty_day_history_with_readings_fits_telegram_limit(self):
        rows = [
            tracker_row(
                f"08/{day:02d}/2026",
                systolic="120",
                diastolic="80",
                measured_at=f"08/{day:02d}/2026 07:00 AM",
            )
            for day in range(1, 23)
        ]
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 22),
            days=30,
            rows=rows,
        )

        self.assertLessEqual(len(app.format_health_history(history)), 4096)


if __name__ == "__main__":
    unittest.main()
