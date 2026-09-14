from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

from food import database, library, recipes, resolver
from food.interpreter import FoodInterpretation


app.CHAT_ID = None


class SavedItemSuggestionTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "healthcoach_food.db"
        )

        original_initialize = database.initialize_database

        def initialize_test_database(database_path=None):
            return original_initialize(self.database_path)

        self.patchers = [
            patch.object(database, "DATABASE_PATH", self.database_path),
            patch.object(library, "DATABASE_PATH", self.database_path),
            patch.object(recipes, "DATABASE_PATH", self.database_path),
            patch.object(resolver, "DATABASE_PATH", self.database_path),
            patch.object(
                database,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                library,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                recipes,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                resolver,
                "initialize_database",
                initialize_test_database,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

        database.initialize_database()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def add_saved_food(self, name: str) -> dict:
        saved = library.add_food_with_nutrition(
            canonical_name=name,
            serving_description="1 serving",
            serving_amount=1,
            serving_unit="serving",
            verification_status="verified",
            verification_source="user_entered",
            calories=400,
            protein_g=30,
            carbohydrates_g=35,
            fat_g=15,
            fiber_g=3,
            sugar_g=4,
            sodium_mg=600,
        )
        return saved["food"]

    def add_saved_recipe(self, name: str) -> dict:
        return recipes.save_pantry_meal_idea(
            {
                "name": name,
                "summary": "A saved recipe.",
                "ingredients": [
                    {
                        "name": "Chicken",
                        "amount": "1 serving",
                        "source": "additional",
                    }
                ],
                "preparation_steps": ["Cook and serve."],
                "calories": 450,
                "protein_g": 35,
                "carbohydrates_g": 40,
                "fat_g": 16,
                "fiber_g": 4,
                "sugar_g": 5,
                "sodium_mg": 650,
                "estimate_notes": "Saved by the user.",
            },
            meal_type="dinner",
        )

    def test_missing_generic_name_suggests_relevant_foods_and_recipes(
        self,
    ) -> None:
        self.add_saved_food("Philly Cheesesteak Burrito")
        self.add_saved_food("Greek Yogurt")
        self.add_saved_recipe("Chicken Pizza Burrito")

        result = resolver.resolve_food(food_name="burrito")

        self.assertFalse(result["found"])
        self.assertEqual(
            [
                (item["item_type"], item["canonical_name"])
                for item in result.get("suggestions", [])
            ],
            [
                ("recipe", "Chicken Pizza Burrito"),
                ("food", "Philly Cheesesteak Burrito"),
            ],
        )

    def test_uncertain_philly_burrito_lists_burritos_not_philly_foods(
        self,
    ) -> None:
        self.add_saved_food("Philly Breakfast Burrito")
        self.add_saved_food("Philly Cheesesteak Burrito")
        self.add_saved_food("Philly Cheesesteak")
        self.add_saved_recipe("Chicken Pizza Burrito")

        result = resolver.resolve_food(food_name="philly burrito")

        self.assertFalse(result["found"])
        self.assertEqual(
            [
                item["canonical_name"]
                for item in result.get("suggestions", [])
            ],
            [
                "Chicken Pizza Burrito",
                "Philly Breakfast Burrito",
                "Philly Cheesesteak Burrito",
            ],
        )

    def test_uncertain_chicken_soup_lists_soups_not_chicken_wraps(
        self,
    ) -> None:
        self.add_saved_food("Chicken Noodle Soup")
        self.add_saved_food("Chicken Rice Soup")
        self.add_saved_food("Tomato Soup")
        self.add_saved_food("Chicken Caesar Wrap")

        result = resolver.resolve_food(food_name="chicken soup")

        self.assertFalse(result["found"])
        self.assertEqual(
            [
                item["canonical_name"]
                for item in result.get("suggestions", [])
            ],
            [
                "Chicken Noodle Soup",
                "Chicken Rice Soup",
                "Tomato Soup",
            ],
        )

    def test_compound_food_requires_both_sides_of_and(self) -> None:
        self.add_saved_food("Classic Mac and Cheese")
        self.add_saved_food("Spicy Mac and Cheese")
        self.add_saved_food("Macaroni Salad")
        self.add_saved_food("String Cheese")

        result = resolver.resolve_food(food_name="mac and cheese")

        self.assertFalse(result["found"])
        self.assertEqual(
            [
                item["canonical_name"]
                for item in result.get("suggestions", [])
            ],
            [
                "Classic Mac and Cheese",
                "Spicy Mac and Cheese",
            ],
        )

    def test_food_kind_before_with_is_used_for_relevance(self) -> None:
        self.add_saved_food("Caesar Salad")
        self.add_saved_food("Chicken Noodle Soup")
        self.add_saved_food("Grilled Chicken Wrap")

        result = resolver.resolve_food(food_name="salad with chicken")

        self.assertFalse(result["found"])
        self.assertEqual(
            [
                item["canonical_name"]
                for item in result.get("suggestions", [])
            ],
            ["Caesar Salad"],
        )

    def test_ampersand_compound_requires_both_food_terms(self) -> None:
        self.add_saved_food("Classic Mac and Cheese")
        self.add_saved_food("Spicy Mac and Cheese")
        self.add_saved_food("String Cheese")

        result = resolver.resolve_food(food_name="mac & cheese")

        self.assertFalse(result["found"])
        self.assertEqual(
            [
                item["canonical_name"]
                for item in result.get("suggestions", [])
            ],
            ["Classic Mac and Cheese", "Spicy Mac and Cheese"],
        )

    def test_trailing_serving_word_does_not_replace_food_kind(self) -> None:
        self.add_saved_food("Pepperoni Pizza")
        self.add_saved_food("Whole Wheat Bread Slice")

        result = resolver.resolve_food(food_name="pizza slice")

        self.assertFalse(result["found"])
        self.assertEqual(
            [
                item["canonical_name"]
                for item in result.get("suggestions", [])
            ],
            ["Pepperoni Pizza"],
        )

    def test_trailing_serving_word_prefers_complete_food_category(self) -> None:
        self.add_saved_food("Chicken Rice Bowl")
        self.add_saved_food("Veggie Rice Bowl")
        self.add_saved_food("Bean Rice Burrito")
        self.add_saved_food("Cilantro Lime Rice")

        result = resolver.resolve_food(food_name="rice bowl")

        self.assertFalse(result["found"])
        self.assertEqual(
            [
                item["canonical_name"]
                for item in result.get("suggestions", [])
            ],
            ["Chicken Rice Bowl", "Veggie Rice Bowl"],
        )

    def test_confirmed_uncertain_food_offers_saved_item_choices(
        self,
    ) -> None:
        self.add_saved_food("Philly Cheesesteak Burrito")
        self.add_saved_recipe("Chicken Pizza Burrito")
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "confirmation",
            "known_data": {
                "food_name": "burrito",
                "quantity": 1,
                "meal_category": "lunch",
                "missing_fields": [],
                "assumptions": [],
                "is_combo_meal": False,
            },
            "original_message": "Add a burrito for lunch",
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "lookup_official_nutrition",
                return_value={"found": False},
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 10,
                    "text": "Correct",
                }
            })

        self.assertTrue(update.called)
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_item_selection",
        )
        message = send.call_args.args[0]
        self.assertIn("Chicken Pizza Burrito (Recipe)", message)
        self.assertIn("Philly Cheesesteak Burrito (Food)", message)
        self.assertIn("None of these", message)

    def test_selecting_suggested_item_asks_for_servings(self) -> None:
        suggestions = [
            {
                "item_type": "recipe",
                "saved_recipe_id": 7,
                "food_id": 42,
                "canonical_name": "Chicken Pizza Burrito",
                "serving_description": "1 saved recipe serving",
            },
            {
                "item_type": "food",
                "saved_recipe_id": None,
                "food_id": 43,
                "canonical_name": "Philly Cheesesteak Burrito",
                "serving_description": "1 burrito",
            },
        ]
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "saved_item_selection",
            "known_data": {
                "food_name": "burrito",
                "meal_category": "lunch",
                "_saved_item_suggestions": suggestions,
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
                "interpret_food_message",
                return_value=FoodInterpretation(
                    is_food_logging_request=False,
                    missing_fields=[],
                    assumptions=[],
                    confidence=0.0,
                ),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 11,
                    "text": "1",
                }
            })

        self.assertTrue(update.called)
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_item_servings",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"][
                "_selected_saved_item"
            ]["canonical_name"],
            "Chicken Pizza Burrito",
        )
        self.assertIn("How many servings", send.call_args.args[0])
        self.assertIn("Chicken Pizza Burrito", send.call_args.args[0])

    def test_recipe_servings_reach_nutrition_confirmation(self) -> None:
        saved = self.add_saved_recipe("Chicken Pizza Burrito")
        recipe = saved["recipe"]
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "saved_item_servings",
            "known_data": {
                "food_name": "burrito",
                "meal_category": "dinner",
                "_selected_saved_item": {
                    "item_type": "recipe",
                    "saved_recipe_id": recipe["saved_recipe_id"],
                    "food_id": recipe["food_id"],
                    "canonical_name": recipe["canonical_name"],
                    "serving_description": "1 saved recipe serving",
                },
            },
            "original_message": "Add a burrito for dinner",
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "interpret_food_message",
                return_value=FoodInterpretation(
                    is_food_logging_request=False,
                    missing_fields=[],
                    assumptions=[],
                    confidence=0.0,
                ),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 12,
                    "text": "0.5",
                }
            })

        self.assertTrue(update.called)
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "nutrition_confirmation",
        )
        components = update.call_args.kwargs["known_data"][
            "_pending_components"
        ]
        self.assertEqual(len(components), 1)
        self.assertEqual(components[0]["role"], "Recipe")
        self.assertEqual(components[0]["quantity"], 0.5)
        self.assertEqual(
            components[0]["canonical_name"],
            "Chicken Pizza Burrito",
        )
        self.assertIn("Verified nutrition", send.call_args.args[0])

    def test_none_of_these_preserves_meal_and_requests_new_description(
        self,
    ) -> None:
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "saved_item_selection",
            "known_data": {
                "food_name": "burrito",
                "meal_category": "lunch",
                "_entry_date": "2026-09-12",
                "_saved_item_suggestions": [
                    {
                        "item_type": "food",
                        "food_id": 43,
                        "canonical_name": "Philly Cheesesteak Burrito",
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
                "interpret_food_message",
                return_value=FoodInterpretation(
                    is_food_logging_request=False,
                    missing_fields=[],
                    assumptions=[],
                    confidence=0.0,
                ),
            ),
            patch.object(app, "cancel_conversation") as cancel,
            patch.object(app, "start_conversation") as start,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 13,
                    "text": "None of these",
                }
            })

        self.assertTrue(start.called)
        self.assertEqual(
            start.call_args.kwargs["current_step"],
            "awaiting_food",
        )
        self.assertEqual(
            start.call_args.kwargs["known_data"],
            {
                "meal_category": "lunch",
                "restaurant": None,
                "_entry_date": "2026-09-12",
            },
        )
        cancel.assert_called_once_with(123)
        self.assertIn("describe the food", send.call_args.args[0].lower())

    def test_half_symbol_is_accepted_as_saved_item_servings(self) -> None:
        food = self.add_saved_food("Philly Cheesesteak Burrito")
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "saved_item_servings",
            "known_data": {
                "food_name": "burrito",
                "meal_category": "lunch",
                "_selected_saved_item": {
                    "item_type": "food",
                    "saved_recipe_id": None,
                    "food_id": food["food_id"],
                    "canonical_name": food["canonical_name"],
                    "serving_description": "1 serving",
                },
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
                "interpret_food_message",
                return_value=FoodInterpretation(
                    is_food_logging_request=False,
                    missing_fields=[],
                    assumptions=[],
                    confidence=0.0,
                ),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg"),
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 14,
                    "text": "½",
                }
            })

        self.assertTrue(update.called)
        self.assertEqual(
            update.call_args.kwargs["known_data"][
                "_pending_components"
            ][0]["quantity"],
            0.5,
        )

    def test_suggestion_list_has_tappable_number_buttons(self) -> None:
        message = app.format_saved_item_suggestions(
            [
                {
                    "item_type": "recipe",
                    "canonical_name": "Chicken Pizza Burrito",
                },
                {
                    "item_type": "food",
                    "canonical_name": "Philly Cheesesteak Burrito",
                },
            ]
        )

        keyboard = app.menu_reply_markup(message)

        self.assertIsNotNone(keyboard)
        self.assertEqual(keyboard["keyboard"], [["1", "2", "3"]])
        self.assertTrue(keyboard["one_time_keyboard"])

    def test_saved_item_serving_prompt_has_tappable_amounts(self) -> None:
        keyboard = app.menu_reply_markup(
            "How many servings of Philly Cheesesteak Burrito did you "
            "have?\n\nChoose 0.5, 1, 1.5, or 2."
        )

        self.assertIsNotNone(keyboard)
        self.assertEqual(
            keyboard["keyboard"],
            [["0.5", "1", "1.5", "2"]],
        )
        self.assertTrue(keyboard["one_time_keyboard"])

    def test_archived_saved_food_cannot_continue_from_old_choice(
        self,
    ) -> None:
        food = self.add_saved_food("Philly Cheesesteak Burrito")
        library.archive_user_saved_food(food["food_id"])
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "saved_item_servings",
            "known_data": {
                "food_name": "burrito",
                "meal_category": "lunch",
                "_selected_saved_item": {
                    "item_type": "food",
                    "saved_recipe_id": None,
                    "food_id": food["food_id"],
                    "canonical_name": food["canonical_name"],
                },
            },
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "prompt_for_corrected_food") as prompt,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 15,
                    "text": "1",
                }
            })

        self.assertFalse(update.called)
        self.assertEqual(prompt.call_args.kwargs["known_data"], conversation["known_data"])
        self.assertIn("no longer available", prompt.call_args.kwargs["message"])

    def test_deleted_saved_recipe_cannot_continue_from_old_choice(
        self,
    ) -> None:
        saved = self.add_saved_recipe("Chicken Pizza Burrito")
        recipe = saved["recipe"]
        recipes.delete_saved_recipe(recipe["saved_recipe_id"])
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "saved_item_servings",
            "known_data": {
                "food_name": "burrito",
                "meal_category": "dinner",
                "_selected_saved_item": {
                    "item_type": "recipe",
                    "saved_recipe_id": recipe["saved_recipe_id"],
                    "food_id": recipe["food_id"],
                    "canonical_name": recipe["canonical_name"],
                },
            },
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "prompt_for_corrected_food") as prompt,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 16,
                    "text": "1",
                }
            })

        self.assertFalse(update.called)
        self.assertEqual(prompt.call_args.kwargs["known_data"], conversation["known_data"])
        self.assertIn("no longer available", prompt.call_args.kwargs["message"])

    def test_consolidated_saved_food_cannot_continue_from_old_choice(
        self,
    ) -> None:
        primary = self.add_saved_food("Breakfast Burrito")
        duplicate = self.add_saved_food("Morning Breakfast Burrito")
        with database.get_connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO food_consolidations (
                    duplicate_food_id,
                    primary_food_id,
                    created_at
                )
                VALUES (?, ?, ?)
                """,
                (
                    duplicate["food_id"],
                    primary["food_id"],
                    "2026-09-14T00:00:00Z",
                ),
            )
            connection.commit()

        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "saved_item_servings",
            "known_data": {
                "food_name": "burrito",
                "meal_category": "breakfast",
                "_selected_saved_item": {
                    "item_type": "food",
                    "saved_recipe_id": None,
                    "food_id": duplicate["food_id"],
                    "canonical_name": duplicate["canonical_name"],
                },
            },
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "prompt_for_corrected_food") as prompt,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 17,
                    "text": "1",
                }
            })

        self.assertFalse(update.called)
        self.assertEqual(prompt.call_args.kwargs["known_data"], conversation["known_data"])
        self.assertIn("no longer available", prompt.call_args.kwargs["message"])

    def test_saved_item_is_rechecked_at_final_log_confirmation(
        self,
    ) -> None:
        food = self.add_saved_food("Philly Cheesesteak Burrito")
        library.archive_user_saved_food(food["food_id"])
        conversation = {
            "conversation_type": "food_interpretation",
            "current_step": "nutrition_confirmation",
            "known_data": {
                "food_name": "burrito",
                "meal_category": "lunch",
                "_entry_date": "2026-09-13",
                "_pending_components": [
                    {
                        "role": "Food",
                        "food_id": food["food_id"],
                        "canonical_name": food["canonical_name"],
                        "quantity": 1,
                        "saved_item_type": "food",
                        "saved_recipe_id": None,
                    }
                ],
            },
            "original_message": "Add a burrito for lunch",
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
                return_value={
                    "food_entry_id": 1,
                    "calories": 400,
                },
            ),
            patch.object(app, "sync_food_ledger_totals_to_sheet"),
            patch.object(app, "complete_conversation"),
            patch.object(app, "cancel_conversation"),
            patch.object(app, "start_conversation") as start,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 18,
                    "text": "Log It",
                }
            })

        self.assertEqual(
            start.call_args.kwargs["current_step"],
            "awaiting_food",
        )
        self.assertEqual(
            start.call_args.kwargs["known_data"]["meal_category"],
            "lunch",
        )
        self.assertIn("no longer available", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
