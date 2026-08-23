import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


YESTERDAY_ENTRIES = [
    {
        "food_entry_id": 1,
        "meal_category": "breakfast",
        "food_id": 10,
        "canonical_name": "Protein Bar",
        "quantity": 1,
        "calories": 200,
        "protein_g": 20,
    },
    {
        "food_entry_id": 2,
        "meal_category": "dinner",
        "food_id": 20,
        "canonical_name": "Chicken Bowl",
        "quantity": 1,
        "calories": 550,
        "protein_g": 45,
    },
]


class SameAsYesterdayMenuTests(unittest.TestCase):
    def test_food_menu_exposes_same_as_yesterday(self) -> None:
        message = app.healthcoach_food_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("6. Same as yesterday", message)
        self.assertIn(["Same as yesterday"], keyboard["keyboard"])

    def test_action_reviews_food_before_copying(self) -> None:
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
            patch.object(
                app,
                "list_food_entries",
                return_value=YESTERDAY_ENTRIES,
            ),
            patch.object(app, "copy_food_entries_to_date") as copy,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Same as yesterday",
                }
            })

        copy.assert_not_called()
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "same_yesterday_review",
        )
        self.assertIn("Yesterday's Food Review", send.call_args.args[0])
        self.assertIn("Protein Bar", send.call_args.args[0])
        self.assertIn("Chicken Bowl", send.call_args.args[0])

    def test_review_can_choose_one_meal(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "same_yesterday_review",
            "known_data": {
                "same_yesterday_source_date": "2026-08-15",
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
                "list_food_entries",
                return_value=YESTERDAY_ENTRIES,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Copy one meal",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "same_yesterday_meal_select",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"]
            ["same_yesterday_available_meals"],
            ["breakfast", "dinner"],
        )
        self.assertIn(
            "Choose a meal from yesterday to copy",
            send.call_args.args[0],
        )

    def test_copy_confirmation_calls_atomic_ledger_helper(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "same_yesterday_confirmation",
            "known_data": {
                "same_yesterday_source_date": "2026-08-15",
                "same_yesterday_meal": "dinner",
            },
        }
        copied = [dict(YESTERDAY_ENTRIES[1])]
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "list_food_entries",
                return_value=YESTERDAY_ENTRIES,
            ),
            patch.object(
                app,
                "copy_food_entries_to_date",
                return_value=copied,
            ) as copy,
            patch.object(app, "sync_food_ledger_totals_to_sheet"),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Yes",
                }
            })

        self.assertEqual(
            copy.call_args.kwargs["source_date"].isoformat(),
            "2026-08-15",
        )
        self.assertEqual(copy.call_args.kwargs["meal_category"], "dinner")
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "food",
        )
        self.assertIn("Copied food to today", send.call_args.args[0])
        self.assertIn("Calories added: 550", send.call_args.args[0])

    def test_duplicate_copy_error_returns_to_review(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "same_yesterday_confirmation",
            "known_data": {
                "same_yesterday_source_date": "2026-08-15",
                "same_yesterday_meal": None,
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
                "list_food_entries",
                return_value=YESTERDAY_ENTRIES,
            ),
            patch.object(
                app,
                "copy_food_entries_to_date",
                side_effect=ValueError(
                    "Food is already recorded. Nothing was copied."
                ),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Yes",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "same_yesterday_review",
        )
        self.assertIn("Nothing was copied", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
