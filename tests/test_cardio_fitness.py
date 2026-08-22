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
    cardio_fitness: str = "",
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
        cardio_fitness,
    ]


class CardioFitnessTests(unittest.TestCase):
    def test_legacy_rows_keep_cardio_fitness_missing(self) -> None:
        metrics = app.row_to_metrics(
            tracker_row("08/22/2026")[:11]
        )

        self.assertIsNone(metrics["cardio_fitness"])
        self.assertIn(
            "Cardio fitness: not recorded",
            app.build_progress_message("Current status", metrics),
        )

    def test_status_shows_cardio_fitness(self) -> None:
        metrics = app.row_to_metrics(
            tracker_row(
                "08/22/2026",
                cardio_fitness="31.7",
            )
        )

        self.assertEqual(metrics["cardio_fitness"], 31.7)
        self.assertIn(
            "Cardio fitness: 31.7 mL/kg/min",
            app.build_progress_message("Current status", metrics),
        )

    def test_schema_appends_cardio_fitness_column(self) -> None:
        sheet = MagicMock()
        sheet.col_count = 11

        changed = app.ensure_health_tracker_schema(sheet)

        self.assertTrue(changed)
        sheet.add_cols.assert_called_once_with(1)
        sheet.update_cell.assert_called_once_with(
            1,
            12,
            "Cardio Fitness",
        )

    def test_missing_sync_preserves_existing_cardio_fitness(self) -> None:
        existing = tracker_row(
            "08/22/2026",
            cardio_fitness="31.7",
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
            "A2:L2",
        )
        merged = sheet.update.call_args.kwargs["values"][0]
        self.assertEqual(merged[11], "31.7")

    def test_webhook_accepts_cardio_fitness(self) -> None:
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
                json={"cardio_fitness": 31.7},
            )

        self.assertEqual(response.status_code, 200)
        saved_row = update.call_args.args[1]
        self.assertEqual(saved_row[11], 31.7)

    def test_health_history_summarizes_cardio_fitness(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 22),
            days=7,
            rows=[
                tracker_row(
                    "08/21/2026",
                    cardio_fitness="30.5",
                ),
                tracker_row(
                    "08/22/2026",
                    cardio_fitness="31.5",
                ),
            ],
        )

        self.assertEqual(history["average_cardio_fitness"], 31.0)
        self.assertEqual(history["cardio_fitness_change"], 1.0)
        self.assertEqual(history["cardio_fitness_entries"], 2)
        message = app.format_health_history(history)
        self.assertIn("cardio fitness 31.5", message)
        self.assertIn(
            "Average Cardio Fitness: 31 mL/kg/min",
            message,
        )
        self.assertIn(
            "Recorded Cardio Fitness change: +1.0 mL/kg/min",
            message,
        )
        self.assertIn("Cardio Fitness recorded: 2/7 days", message)

    def test_empty_thirty_day_history_fits_telegram_limit(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 22),
            days=30,
            rows=[],
        )

        message = app.format_health_history(history)

        self.assertLessEqual(len(message), 4096)
        self.assertIn("— means not recorded", message)


if __name__ == "__main__":
    unittest.main()
