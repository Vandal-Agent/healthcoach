from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import patch

import pytz

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


class WeightGoalsMenuTests(unittest.TestCase):
    def test_reports_menu_contains_goals(self) -> None:
        message = app.healthcoach_reports_menu_text()
        keyboard = app.menu_reply_markup(message)
        self.assertIn("3. Goals", message)
        self.assertIn(["Goals"], keyboard["keyboard"])

    def test_reports_routes_to_goals_menu(self) -> None:
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
                    "text": "Goals",
                }
            })

        self.assertEqual(update.call_args.kwargs["current_step"], "goals")
        self.assertIn("Goals Menu", send.call_args.args[0])

    def test_back_from_active_goal_returns_to_goals(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "goal_view",
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

        self.assertEqual(update.call_args.kwargs["current_step"], "goals")
        self.assertIn("Goals Menu", send.call_args.args[0])

    def test_goal_update_saves_only_when_requested(self) -> None:
        goal = {
            "weight_goal_id": 4,
            "target_weight": 215.0,
            "target_date": "2026-10-17",
        }
        with (
            patch.object(app, "get_active_weight_goal", return_value=goal),
            patch.object(
                app,
                "get_weight_goal_health_inputs",
                return_value={
                    "current_weight": 230.6,
                    "weight_date": date(2026, 8, 19),
                    "average_daily_burn": 2800,
                    "burn_days": 7,
                },
            ),
            patch.object(
                app,
                "save_weight_goal_calculation",
                side_effect=lambda goal_id, result: {
                    "weight_goal_id": goal_id,
                    **result,
                },
            ) as save,
        ):
            message = app.update_and_format_weight_goal(
                reference_date=date(2026, 8, 19)
            )

        save.assert_called_once()
        self.assertIn("Weight Goal Updated", message)
        self.assertIn("Saved calorie target", message)
        self.assertIn("1.85 lb per week", message)

    def test_progress_uses_saved_target_without_recalculating(self) -> None:
        today = datetime.now(pytz.timezone("US/Pacific")).date()
        with (
            patch.object(
                app,
                "get_latest_weight_goal_calculation",
                return_value={
                    "calorie_target_low": 1800,
                    "calorie_target_high": 1950,
                },
            ),
            patch.object(
                app,
                "list_food_entries",
                return_value=[
                    {"calories": 1200},
                    {"calories": None},
                ],
            ),
            patch.object(app, "calculate_weight_goal") as calculate,
        ):
            message = app.format_goal_calorie_progress(today)

        calculate.assert_not_called()
        self.assertIn("Eaten: 1200", message)
        self.assertIn("Remaining: 600-750", message)
        self.assertIn("1 logged item(s)", message)

    def test_progress_is_neutral_when_over_range(self) -> None:
        today = datetime.now(pytz.timezone("US/Pacific")).date()
        with (
            patch.object(
                app,
                "get_latest_weight_goal_calculation",
                return_value={
                    "calorie_target_low": 1800,
                    "calorie_target_high": 1950,
                },
            ),
            patch.object(
                app,
                "list_food_entries",
                return_value=[{"calories": 2050}],
            ),
        ):
            message = app.format_goal_calorie_progress(today)

        self.assertIn("100 calories above", message)
        self.assertIn("Tomorrow starts fresh", message)

    def test_historical_food_does_not_show_today_progress(self) -> None:
        message = app.format_goal_calorie_progress(date(2026, 1, 1))
        self.assertEqual(message, "")


if __name__ == "__main__":
    unittest.main()
