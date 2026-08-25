import unittest
from datetime import date
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

from food.interpreter import FoodInterpretation

app.CHAT_ID = None


class FoodLoggingFailureTests(unittest.TestCase):
    def test_food_clarification_cancel_words_always_close_entry(self) -> None:
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "clarification",
            "original_message": "home salad",
            "known_data": {
                "food_name": "salad",
                "quantity": 1.0,
                "meal_category": "lunch",
                "missing_fields": ["quantity_description"],
            },
            "missing_fields": ["quantity_description"],
        }

        for cancel_word in ("cancel", "exit", "quit", "close"):
            with self.subTest(cancel_word=cancel_word):
                with (
                    patch.object(
                        app,
                        "get_active_conversation",
                        return_value=conversation,
                    ),
                    patch.object(
                        app,
                        "cancel_conversation",
                    ) as cancel,
                    patch.object(
                        app,
                        "interpret_food_message",
                    ) as interpret,
                    patch.object(app, "send_telegram_msg") as send,
                ):
                    app.process_telegram_update({
                        "message": {
                            "chat": {"id": 123},
                            "message_id": 52,
                            "text": cancel_word,
                        }
                    })

                cancel.assert_called_once_with(123)
                interpret.assert_not_called()
                self.assertIn(
                    "Food entry cancelled",
                    send.call_args.args[0],
                )
                self.assertTrue(send.call_args.kwargs["remove_keyboard"])

    def test_bare_quantity_count_gets_clear_unit_prompt(self) -> None:
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "clarification",
            "original_message": "home salad",
            "known_data": {
                "food_name": "salad",
                "quantity": 1.0,
                "meal_category": "lunch",
                "missing_fields": ["quantity_description"],
            },
            "missing_fields": ["quantity_description"],
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "interpret_food_message") as interpret,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 53,
                    "text": "1",
                }
            })

        interpret.assert_not_called()
        response = send.call_args.args[0]
        self.assertIn("1 serving", response)
        self.assertIn("4 oz", response)
        self.assertIn("Reply Cancel", response)

    def test_saved_unbranded_food_converts_ounces_to_one_serving(self) -> None:
        interpretation = FoodInterpretation(
            is_food_logging_request=True,
            food_name="Beef steak sirloin",
            quantity=4,
            quantity_description="ounces",
            meal_category="breakfast",
            missing_fields=[],
            assumptions=[],
            confidence=0.99,
        )
        resolution = {
            "found": True,
            "food": {
                "food_id": 61,
                "canonical_name": "Beef steak sirloin",
                "restaurant": None,
                "brand": None,
                "serving_amount": 4,
                "serving_unit": "ounces",
                "verification_source": "user_package_label",
            },
            "nutrition": {
                "calories": 220,
                "protein_g": 23,
            },
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=None,
            ),
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
            patch.object(app, "start_conversation") as start,
            patch.object(app, "update_conversation"),
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 51,
                    "text": (
                        "I had 4 ounces of Beef steak sirloin "
                        "for breakfast"
                    ),
                }
            })

        pending = start.call_args.kwargs[
            "known_data"
        ]["_pending_components"]
        self.assertEqual(pending[0]["quantity"], 1.0)
        response = send.call_args.args[0]
        self.assertIn("220 calories", response)
        self.assertIn("23 g", response)

    def test_failed_confirmation_is_closed_and_explained(self) -> None:
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "nutrition_confirmation",
            "original_message": "I had an apple for lunch",
            "known_data": {
                "meal_category": "lunch",
                "_pending_components": [
                    {
                        "food_id": 42,
                        "canonical_name": "Apple, raw",
                        "quantity": 1.0,
                    }
                ],
            },
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "find_recent_duplicate_entry",
                return_value=None,
            ),
            patch.object(
                app,
                "add_food_entry",
                side_effect=ValueError("Food entry test failure."),
            ),
            patch.object(app.logging, "exception"),
            patch.object(app, "cancel_conversation") as cancel,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 50,
                    "text": "Log It",
                }
            })

        cancel.assert_called_once_with(123)
        response = send.call_args.args[0]
        self.assertIn("Food entry test failure", response)
        self.assertIn("start a new entry", response)

    def test_early_protein_history_uses_food_ledger(self) -> None:
        entries = [
            {"meal_category": "breakfast", "protein_g": 12},
            {"meal_category": "school snack", "protein_g": 8},
            {"meal_category": "lunch", "protein_g": 20},
            {"meal_category": "dinner", "protein_g": 50},
        ]

        with patch.object(
            app,
            "list_food_entries",
            return_value=entries,
        ) as list_entries:
            result = app.get_food_ledger_early_protein_for_week(
                date(2026, 8, 17)
            )

        self.assertEqual(len(result), 7)
        self.assertEqual(set(result.values()), {40.0})
        self.assertEqual(list_entries.call_count, 7)


if __name__ == "__main__":
    unittest.main()
