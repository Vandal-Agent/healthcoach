import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


SAVED_RECIPE = {
    "saved_recipe_id": 5,
    "food_id": 42,
    "canonical_name": "Chicken Pepper Bowl",
    "meal_type": "dinner",
    "summary": "A quick bowl.",
    "ingredients": [
        {"name": "Chicken", "amount": "4 ounces", "source": "pantry"}
    ],
    "preparation_steps": ["Cook and serve."],
    "calories": 450,
    "protein_g": 42,
    "carbohydrates_g": 40,
    "fat_g": 13,
    "fiber_g": 8,
    "sugar_g": 6,
    "sodium_mg": 520,
}


class SavedRecipeMenuTests(unittest.TestCase):
    def test_food_menu_routes_to_saved_recipes(self) -> None:
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
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Saved recipes",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_recipes",
        )
        self.assertIn("Saved Recipes Menu", send.call_args.args[0])

    def test_browse_lists_saved_recipes(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "saved_recipes",
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
                "list_saved_recipes",
                return_value=[SAVED_RECIPE],
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Browse saved recipes",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_recipe_browse",
        )
        self.assertIn("Chicken Pepper Bowl", send.call_args.args[0])

    def test_recipe_details_include_ingredients_and_preparation(self) -> None:
        message = app.format_saved_recipe_details(SAVED_RECIPE)
        keyboard = app.menu_reply_markup(message)

        self.assertIn("4 ounces Chicken", message)
        self.assertIn("1. Cook and serve.", message)
        self.assertIn(["Log Recipe"], keyboard["keyboard"])

    def test_confirmed_saved_recipe_logs_existing_food(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "saved_recipe_log_confirmation",
            "known_data": {
                "saved_recipe_id": 5,
                "saved_recipe_meal": "dinner",
                "saved_recipe_servings": 1.5,
            },
        }
        logged = {"calories": 675, "protein_g": 63}

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "get_saved_recipe",
                return_value=SAVED_RECIPE,
            ),
            patch.object(
                app,
                "add_food_entry",
                return_value=logged,
            ) as add_entry,
            patch.object(app, "sync_food_ledger_totals_to_sheet"),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Log Recipe",
                }
            })

        self.assertEqual(add_entry.call_args.kwargs["food_id"], 42)
        self.assertEqual(add_entry.call_args.kwargs["quantity"], 1.5)
        self.assertEqual(
            add_entry.call_args.kwargs["logging_source"],
            "recipe",
        )
        self.assertTrue(
            add_entry.call_args.kwargs["quantity_is_estimated"]
        )
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_recipes",
        )
        self.assertIn("Saved Recipe logged", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
