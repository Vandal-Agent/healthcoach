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
    "heart_healthy_pick": False,
    "heart_healthy_reason": "",
}

HEART_HEALTHY_SAVED_RECIPE = {
    **SAVED_RECIPE,
    "heart_healthy_pick": True,
    "heart_healthy_reason": (
        "Uses lean chicken and vegetables with moderate sodium."
    ),
}

BUILDER_INGREDIENT = {
    "food_id": 12,
    "nutrition_version_id": 21,
    "name": "Browned Ground Turkey",
    "amount_description": "3 oz",
    "serving_multiplier": 1.0,
    "serving_description": "3 oz",
    "nutrition": {
        "calories": 170.0,
        "protein_g": 22.0,
        "carbohydrates_g": 0.0,
        "fat_g": 9.0,
        "fiber_g": 0.0,
        "sugar_g": 0.0,
        "sodium_mg": 80.0,
    },
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
        self.assertIn(
            ["Log Recipe", "Edit Recipe"],
            keyboard["keyboard"],
        )
        self.assertIn(["Delete Recipe"], keyboard["keyboard"])

    def test_heart_healthy_pick_appears_in_list_and_details(self) -> None:
        choices = app.format_saved_recipe_choices(
            [HEART_HEALTHY_SAVED_RECIPE]
        )
        details = app.format_saved_recipe_details(
            HEART_HEALTHY_SAVED_RECIPE
        )

        self.assertIn("Heart-Healthy Pick", choices)
        self.assertIn("Heart-Healthy Pick", details)
        self.assertIn(
            HEART_HEALTHY_SAVED_RECIPE["heart_healthy_reason"],
            details,
        )
        self.assertIn("not a medical rating", details)

    def test_unlabeled_recipe_does_not_gain_heart_healthy_pick(self) -> None:
        choices = app.format_saved_recipe_choices([SAVED_RECIPE])
        details = app.format_saved_recipe_details(SAVED_RECIPE)

        self.assertNotIn("Heart-Healthy Pick", choices)
        self.assertNotIn("Heart-Healthy Pick", details)

    def test_saved_recipe_menu_exposes_edit_and_delete(self) -> None:
        message = app.healthcoach_saved_recipes_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("2. Create recipe", message)
        self.assertIn("3. Edit saved recipe", message)
        self.assertIn("4. Delete saved recipe", message)
        self.assertIn(
            ["Browse saved recipes", "Create recipe"],
            keyboard["keyboard"],
        )
        self.assertIn(["Edit saved recipe"], keyboard["keyboard"])
        self.assertIn(["Delete saved recipe"], keyboard["keyboard"])

    def test_create_recipe_starts_builder(self) -> None:
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
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Create recipe",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_builder_name",
        )
        self.assertIn("What should this recipe be called", send.call_args.args[0])

    def test_builder_adds_calculated_ingredient(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_builder_ingredient_amount",
            "known_data": {
                "_recipe_builder_name": "Turkey Bowl",
                "_recipe_builder_meal_type": "dinner",
                "_recipe_builder_yield": 2,
                "_recipe_builder_ingredients": [],
                "_recipe_builder_selected_food_id": 12,
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
                "prepare_recipe_ingredient",
                return_value=BUILDER_INGREDIENT,
            ) as prepare,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "3 oz"}
            })

        prepare.assert_called_once_with(
            food_id=12,
            amount_description="3 oz",
        )
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_builder_ingredient_menu",
        )
        self.assertIn("Per serving so far", send.call_args.args[0])

    def test_builder_confirmation_saves_without_logging(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_builder_confirmation",
            "known_data": {
                "_recipe_builder_name": "Turkey Bowl",
                "_recipe_builder_meal_type": "dinner",
                "_recipe_builder_yield": 2,
                "_recipe_builder_ingredients": [BUILDER_INGREDIENT],
                "_recipe_builder_summary": "Simple turkey bowl.",
                "_recipe_builder_steps": ["Heat and serve."],
            },
        }
        created_recipe = {
            **SAVED_RECIPE,
            "saved_recipe_id": 8,
            "canonical_name": "Turkey Bowl",
            "yield_servings": 2,
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "create_saved_recipe_from_ingredients",
                return_value={
                    "created": True,
                    "recipe": created_recipe,
                },
            ) as create,
            patch.object(app, "add_food_entry") as add_entry,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Save Recipe",
                }
            })

        create.assert_called_once()
        add_entry.assert_not_called()
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_recipe_details",
        )
        self.assertIn("Nothing was logged", send.call_args.args[0])

    def test_edit_selection_opens_edit_menu(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "saved_recipe_edit_select",
            "known_data": {"saved_recipe_ids": [5]},
        }
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
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "1"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_recipe_edit_menu",
        )
        self.assertIn("Saved Recipe Edit Menu", send.call_args.args[0])

    def test_confirmed_name_edit_updates_recipe(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "saved_recipe_edit_confirmation",
            "known_data": {
                "saved_recipe_id": 5,
                "_saved_recipe_edit_kind": "name",
                "_saved_recipe_edit_value": "Garden Chicken Bowl",
            },
        }
        updated_recipe = dict(SAVED_RECIPE)
        updated_recipe["canonical_name"] = "Garden Chicken Bowl"
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
                "update_saved_recipe",
                return_value=updated_recipe,
            ) as update_recipe,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        update_recipe.assert_called_once_with(
            5,
            name="Garden Chicken Bowl",
        )
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_recipe_details",
        )
        self.assertIn("Saved Recipe updated", send.call_args.args[0])

    def test_confirmed_nutrition_edit_creates_new_version(self) -> None:
        nutrition = {
            "calories": 400.0,
            "protein_g": 45.0,
            "carbohydrates_g": 32.0,
            "fat_g": 11.0,
            "fiber_g": 7.0,
            "sugar_g": 5.0,
            "sodium_mg": 480.0,
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "saved_recipe_edit_nutrition_confirmation",
            "known_data": {
                "saved_recipe_id": 5,
                "_saved_recipe_edit_nutrition": nutrition,
            },
        }
        updated_recipe = dict(SAVED_RECIPE)
        updated_recipe["version_number"] = 2
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
                "update_saved_recipe_nutrition",
                return_value=updated_recipe,
            ) as update_nutrition,
            patch.object(app, "update_conversation"),
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        update_nutrition.assert_called_once_with(5, **nutrition)
        self.assertIn("version 2", send.call_args.args[0])

    def test_confirmed_delete_preserves_logged_history_message(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "saved_recipe_delete_confirmation",
            "known_data": {"saved_recipe_id": 5},
        }
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
                "delete_saved_recipe",
                return_value=SAVED_RECIPE,
            ) as delete,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        delete.assert_called_once_with(5)
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_recipes",
        )
        self.assertIn(
            "Previously logged meals were not changed",
            send.call_args.args[0],
        )

    def test_recipe_edit_text_parsers(self) -> None:
        ingredients = app.parse_saved_recipe_ingredients(
            "4 ounces | chicken breast\n1/2 cup | peppers"
        )
        steps = app.parse_saved_recipe_steps(
            "1. Cook chicken.\n2. Add peppers."
        )

        self.assertEqual(ingredients[0]["amount"], "4 ounces")
        self.assertEqual(ingredients[1]["name"], "peppers")
        self.assertEqual(steps, ["Cook chicken.", "Add peppers."])

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
