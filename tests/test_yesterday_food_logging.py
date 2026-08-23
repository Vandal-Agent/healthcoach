from __future__ import annotations

import unittest
from datetime import date, datetime
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

from food.interpreter import FoodInterpretation


app.CHAT_ID = None


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 8, 23, 7, 30)
        if tz is not None:
            return tz.localize(value)
        return value


def apple_interpretation(*, meal_category=None, missing_fields=None):
    return FoodInterpretation(
        is_food_logging_request=True,
        food_name="apple",
        quantity=1.0,
        meal_category=meal_category,
        missing_fields=list(missing_fields or []),
        assumptions=[],
        confidence=0.99,
        clarification_question=(
            "Which meal was this?"
            if missing_fields
            else None
        ),
    )


class YesterdayFoodLoggingTests(unittest.TestCase):
    def test_food_menu_exposes_yesterday_logging(self):
        message = app.healthcoach_food_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("2. Log food for yesterday", message)
        self.assertIn(
            ["Log food", "Log food for yesterday"],
            keyboard["keyboard"],
        )
        self.assertIn("6. Same as yesterday", message)
        self.assertIn("14. Back", message)

    def test_menu_action_starts_previous_day_food_entry(self):
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food",
            "known_data": {},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "datetime", FixedDateTime),
            patch.object(app, "start_conversation") as start,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Log food for yesterday",
                }
            })

        self.assertEqual(
            start.call_args.kwargs["conversation_type"],
            "yesterday_food_logging",
        )
        self.assertEqual(
            start.call_args.kwargs["known_data"]["_entry_date"],
            "2026-08-22",
        )
        self.assertIn(
            "Logging food for yesterday — Sat Aug 22, 2026",
            send.call_args.args[0],
        )

    def test_natural_yesterday_phrase_keeps_date_and_all_meals(self):
        interpretation = apple_interpretation(
            missing_fields=["meal_category"]
        )
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=None,
            ),
            patch.object(app, "datetime", FixedDateTime),
            patch.object(
                app,
                "interpret_food_message",
                return_value=interpretation,
            ) as interpret,
            patch.object(app, "start_conversation") as start,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "I had an apple yesterday",
                }
            })

        interpret.assert_called_once_with("I had an apple")
        known_data = start.call_args.kwargs["known_data"]
        self.assertEqual(known_data["_entry_date"], "2026-08-22")
        self.assertEqual(
            known_data["_accumulated_text"],
            "I had an apple",
        )
        meal_options = update.call_args.kwargs["known_data"][
            "_meal_options"
        ]
        self.assertEqual(len(meal_options), 7)
        self.assertIn("breakfast", meal_options)
        self.assertIn("dinner", meal_options)
        self.assertIn(
            "Date: Yesterday — Sat Aug 22, 2026",
            send.call_args.args[0],
        )

    def test_menu_followup_uses_stored_previous_day(self):
        conversation = {
            "conversation_type": "yesterday_food_logging",
            "current_step": "awaiting_food",
            "known_data": {"_entry_date": "2026-08-22"},
        }
        interpretation = apple_interpretation(
            meal_category="lunch"
        )
        resolution = {
            "found": True,
            "food": {
                "food_id": 42,
                "canonical_name": "Apple, raw",
                "restaurant": None,
                "verification_source": "fdc.nal.usda.gov",
            },
            "nutrition": {
                "calories": 95.0,
                "protein_g": 0.5,
            },
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "datetime", FixedDateTime),
            patch.object(
                app,
                "interpret_food_message",
                return_value=interpretation,
            ),
            patch.object(
                app,
                "resolve_food",
                return_value=resolution,
            ),
            patch.object(app, "cancel_conversation"),
            patch.object(app, "start_conversation") as start,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "I had an apple for lunch",
                }
            })

        self.assertEqual(
            start.call_args.kwargs["current_step"],
            "nutrition_confirmation",
        )
        self.assertEqual(
            start.call_args.kwargs["known_data"]["_entry_date"],
            "2026-08-22",
        )
        self.assertIn(
            "Date: Yesterday — Sat Aug 22, 2026",
            send.call_args.args[0],
        )

    def test_arbitrary_past_date_is_not_selected(self):
        entry_date, cleaned = app.extract_yesterday_food_intent(
            "I had an apple on August 20",
            reference_date=date(2026, 8, 23),
        )

        self.assertIsNone(entry_date)
        self.assertEqual(cleaned, "I had an apple on August 20")

    def test_confirmed_food_logs_to_yesterday_and_shows_totals(self):
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "nutrition_confirmation",
            "original_message": "I had an apple yesterday for lunch",
            "known_data": {
                "meal_category": "lunch",
                "_entry_date": "2026-08-22",
                "_pending_components": [
                    {
                        "food_id": 42,
                        "canonical_name": "Apple, raw",
                        "quantity": 1.0,
                        "calories": 95.0,
                        "protein_g": 0.5,
                    }
                ],
            },
        }
        totals = {
            "calories": 1895.0,
            "protein_g": 121.5,
            "carbohydrates_g": 210.0,
            "fat_g": 63.0,
            "fiber_g": 28.0,
            "sugar_g": 61.0,
            "sodium_mg": 2200.0,
        }
        entry = {
            "food_entry_id": 77,
            "calories": 95.0,
            "protein_g": 0.5,
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "datetime", FixedDateTime),
            patch.object(
                app,
                "find_recent_duplicate_entry",
                return_value=None,
            ),
            patch.object(
                app,
                "add_food_entry",
                return_value=entry,
            ) as add,
            patch.object(
                app,
                "sync_food_ledger_totals_to_sheet",
            ) as sync,
            patch.object(
                app,
                "get_daily_totals",
                return_value=totals,
            ),
            patch.object(app, "complete_conversation"),
            patch.object(app, "start_conversation") as start,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 50,
                    "text": "Log It",
                }
            })

        self.assertEqual(
            add.call_args.kwargs["entry_date"],
            date(2026, 8, 22),
        )
        sync.assert_called_once_with(date(2026, 8, 22))
        self.assertEqual(
            start.call_args.kwargs["known_data"]["_entry_date"],
            "2026-08-22",
        )
        response = send.call_args.args[0]
        self.assertIn(
            "Yesterday's food totals — Sat Aug 22, 2026",
            response,
        )
        self.assertIn("Calories: 1895", response)
        self.assertNotIn("Goal calories today", response)


if __name__ == "__main__":
    unittest.main()
