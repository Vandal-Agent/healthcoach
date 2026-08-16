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
    sleep: str = "",
    weight: str = "",
) -> list[str]:
    return [
        f"{day} 08:00 AM",
        "",
        "",
        "",
        sleep,
        "",
        weight,
        "",
        "",
        "",
    ]


class HealthHistoryTests(unittest.TestCase):
    def test_builds_complete_range_with_missing_days(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 16),
            days=7,
            rows=[
                tracker_row(
                    "08/15/2026",
                    sleep="6.0",
                    weight="234.0",
                ),
                tracker_row(
                    "08/16/2026",
                    sleep="7.0",
                    weight="233.0",
                ),
            ],
        )

        self.assertEqual(len(history["days"]), 7)
        self.assertEqual(history["days"][0]["date"], date(2026, 8, 10))
        self.assertIsNone(history["days"][0]["weight"])
        self.assertEqual(history["average_weight"], 233.5)
        self.assertEqual(history["weight_change"], -1.0)
        self.assertEqual(history["weight_entries"], 2)
        self.assertEqual(history["average_sleep"], 6.5)
        self.assertEqual(history["sleep_entries"], 2)

    def test_history_message_shows_daily_values_and_summary(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 16),
            days=7,
            rows=[
                tracker_row(
                    "08/16/2026",
                    sleep="7.5",
                    weight="233.5",
                ),
            ],
        )

        message = app.format_health_history(history)

        self.assertIn("Health History - Last 7 Days", message)
        self.assertIn(
            "Sun Aug 16: weight 233.5 lb; sleep 7.5 h",
            message,
        )
        self.assertIn("not recorded", message)
        self.assertIn("Weight recorded: 1/7 days", message)
        self.assertIn("Sleep recorded: 1/7 days", message)

    def test_rejects_unsupported_history_period(self) -> None:
        with self.assertRaisesRegex(ValueError, "7, 14, or 30"):
            app.build_health_history_data(
                reference_date=date(2026, 8, 16),
                days=10,
                rows=[],
            )

    def test_health_menu_contains_history_action(self) -> None:
        message = app.healthcoach_health_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("4. Health history", message)
        self.assertIn(
            ["Record weight", "Health history"],
            keyboard["keyboard"],
        )

    def test_health_menu_routes_to_history_menu(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "health",
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
                    "text": "Health history",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "health_history",
        )
        self.assertIn(
            "Last 7 days",
            send.call_args.args[0],
        )

    def test_history_menu_loads_selected_period(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "health_history",
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
                "get_formatted_health_history",
                return_value="Health History - Last 14 Days",
            ) as get_history,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "14 days",
                }
            })

        self.assertEqual(
            get_history.call_args.kwargs["days"],
            14,
        )
        self.assertIn(
            "Last 14 Days",
            send.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
