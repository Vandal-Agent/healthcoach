import unittest
from datetime import date
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


class FoodLoggingFailureTests(unittest.TestCase):
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
