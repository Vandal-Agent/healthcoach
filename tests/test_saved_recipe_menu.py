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


def pantry_food(food_id: int) -> dict:
    return {
        "food_id": food_id,
        "canonical_name": f"Pantry Food {food_id}",
        "serving_description": "1 serving",
        "nutrition_ready": True,
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
        self.assertIn("3. Import recipe", message)
        self.assertIn("4. Edit saved recipe", message)
        self.assertIn("5. Delete saved recipe", message)
        self.assertIn(
            ["Browse saved recipes", "Create recipe"],
            keyboard["keyboard"],
        )
        self.assertIn(["Edit saved recipe"], keyboard["keyboard"])
        self.assertIn(["Delete saved recipe"], keyboard["keyboard"])

    def test_import_recipe_accepts_text_and_reaches_review(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_import_input",
            "known_data": {},
        }
        draft = {
            "readable": True,
            "recipe_name": "Imported Turkey",
            "meal_type": "dinner",
            "yield_servings": 2,
            "summary": "Quick turkey.",
            "ingredients": [
                {
                    "ingredient_name": "ground turkey",
                    "amount_description": "6 oz",
                    "brand": None,
                    "optional": False,
                    "trace_only": False,
                }
            ],
            "preparation_steps": ["Cook and serve."],
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "parse_recipe_text", return_value=draft),
            patch.object(
                app,
                "find_recipe_ingredient_food",
                return_value={"food_id": 12, "canonical_name": "Turkey"},
            ),
            patch.object(
                app,
                "prepare_recipe_ingredient",
                return_value=BUILDER_INGREDIENT,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Imported recipe text",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_builder_confirmation",
        )
        self.assertIn("Recipe Builder Review", send.call_args.args[0])
        self.assertIn("Nothing has been saved", send.call_args.args[0])

    def test_major_imported_ingredient_cannot_be_excluded(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_import_ingredient",
            "known_data": {
                "_recipe_import_pending": [{
                    "ingredient_name": "chicken breast",
                    "amount_description": "16 oz",
                    "optional": False,
                    "trace_only": False,
                }],
            },
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
                "message": {"chat": {"id": 123}, "text": "Exclude"}
            })

        update.assert_not_called()
        self.assertIn("major ingredient", send.call_args.args[0])

    def test_missing_imported_ingredient_has_tappable_choices(self) -> None:
        message = app.format_recipe_import_missing({
            "ingredient_name": "white onion",
            "amount_description": "1 medium",
            "optional": False,
            "trace_only": False,
        })

        keyboard = app.menu_reply_markup(message)

        self.assertIn(["Try verified lookup"], keyboard["keyboard"])
        self.assertIn(["Add new saved food"], keyboard["keyboard"])
        self.assertIn(["Cancel"], keyboard["keyboard"])

    def test_typed_saved_food_requires_substitution_confirmation(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_import_ingredient",
            "known_data": {
                "_recipe_import_pending": [{
                    "ingredient_name": "turkey pepperoni",
                    "amount_description": "9 slices",
                    "optional": False,
                    "trace_only": False,
                }],
            },
        }
        selected = {
            "food_id": 17,
            "canonical_name": "Mission Flour Tortilla, Soft Taco",
            "serving_description": "1 tortilla",
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "find_recipe_ingredient_food",
                return_value=selected,
            ),
            patch.object(app, "prepare_recipe_ingredient") as prepare,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Mission Flour Tortilla, Soft Taco",
                }
            })

        prepare.assert_not_called()
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_import_saved_food_confirmation",
        )
        self.assertIn("9 slices turkey pepperoni", send.call_args.args[0])
        self.assertIn("Mission Flour Tortilla", send.call_args.args[0])
        self.assertIn(
            ["Yes", "No"],
            app.menu_reply_markup(send.call_args.args[0])["keyboard"],
        )

    def test_saved_food_substitution_can_be_rejected(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_import_saved_food_confirmation",
            "known_data": {
                "_recipe_import_pending": [{
                    "ingredient_name": "turkey pepperoni",
                    "amount_description": "9 slices",
                    "optional": False,
                    "trace_only": False,
                }],
                "_recipe_import_selected_food": {
                    "food_id": 17,
                    "canonical_name": "Mission Flour Tortilla, Soft Taco",
                    "serving_description": "1 tortilla",
                },
            },
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "prepare_recipe_ingredient") as prepare,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "No"}
            })

        prepare.assert_not_called()
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_import_ingredient",
        )
        self.assertNotIn(
            "_recipe_import_selected_food",
            update.call_args.kwargs["known_data"],
        )
        self.assertIn("9 slices turkey pepperoni", send.call_args.args[0])

    def test_confirmed_saved_food_preserves_conversion_help(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_import_saved_food_confirmation",
            "known_data": {
                "_recipe_import_pending": [{
                    "ingredient_name": "turkey pepperoni",
                    "amount_description": "9 slices",
                    "optional": False,
                    "trace_only": False,
                }],
                "_recipe_import_selected_food": {
                    "food_id": 18,
                    "canonical_name": "Hormel Original Pepperoni",
                    "serving_description": "30 g",
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
                "prepare_recipe_ingredient",
                side_effect=ValueError("That amount cannot be converted"),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_import_ingredient",
        )
        current = update.call_args.kwargs["known_data"][
            "_recipe_import_pending"
        ][0]
        self.assertEqual(current["candidate_food_id"], 18)
        self.assertEqual(current["candidate_serving_description"], "30 g")
        self.assertIn("Hormel Original Pepperoni", send.call_args.args[0])
        self.assertIn("Saved Food serving: 30 g", send.call_args.args[0])
        self.assertIn(
            ["Use one Saved Food serving"],
            app.menu_reply_markup(send.call_args.args[0])["keyboard"],
        )

    def test_one_saved_food_serving_is_an_explicit_amount_choice(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_import_ingredient",
            "known_data": {
                "_recipe_builder_ingredients": [],
                "_recipe_import_pending": [{
                    "ingredient_name": "turkey pepperoni",
                    "amount_description": "9 slices",
                    "candidate_food_id": 18,
                    "candidate_food_name": "Hormel Original Pepperoni",
                    "candidate_serving_description": "30 g",
                    "conversion_error": "That amount cannot be converted",
                    "optional": False,
                    "trace_only": False,
                }],
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
                "message": {
                    "chat": {"id": 123},
                    "text": "Use one Saved Food serving",
                }
            })

        self.assertEqual(
            prepare.call_args.kwargs["amount_description"],
            "1 serving",
        )
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_builder_confirmation",
        )
        self.assertIn("Recipe Builder Review", send.call_args.args[0])

    def test_failed_exact_lookup_offers_confirmed_generic_match(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_import_ingredient",
            "known_data": {
                "_recipe_import_pending": [{
                    "ingredient_name": "white onion",
                    "amount_description": "1 medium",
                    "brand": None,
                    "optional": False,
                    "trace_only": False,
                }],
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
                "lookup_official_nutrition",
                return_value={
                    "found": False,
                    "notes": ["No exact portion was available."],
                },
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Try verified lookup",
                }
            })

        pending = update.call_args.kwargs["known_data"][
            "_recipe_import_pending"
        ]
        self.assertEqual(pending[0]["generic_lookup_name"], "onion")
        self.assertIn("Try verified generic match: onion", send.call_args.args[0])
        keyboard = app.menu_reply_markup(send.call_args.args[0])
        self.assertIn(["Try verified generic match"], keyboard["keyboard"])
        self.assertIn(["Enter simpler description"], keyboard["keyboard"])

    def test_generic_lookup_still_requires_confirmation(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_import_ingredient",
            "known_data": {
                "_recipe_import_pending": [{
                    "ingredient_name": "white onion",
                    "amount_description": "1 medium",
                    "generic_lookup_name": "onion",
                    "brand": None,
                    "optional": False,
                    "trace_only": False,
                }],
            },
        }
        result = {
            "found": True,
            "food": {
                "canonical_name": "Onions, raw",
                "serving_description": "1 Medium Onion (110 g)",
                "serving_amount": 1.0,
                "serving_unit": "Medium Onion",
                "brand": None,
            },
            "nutrition": {
                "calories": 44.0,
                "protein_g": 1.2,
                "carbohydrates_g": 10.3,
                "fat_g": 0.1,
                "fiber_g": 1.9,
                "sugar_g": 4.7,
                "sodium_mg": 4.0,
            },
            "verification": {"source": "USDA FoodData Central"},
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
                return_value=result,
            ) as lookup,
            patch.object(
                app,
                "ingredient_serving_multiplier",
                return_value=1.0,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Try verified generic match",
                }
            })

        self.assertEqual(lookup.call_args.kwargs["food_name"], "onion")
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_import_verified_confirmation",
        )
        self.assertTrue(
            update.call_args.kwargs["known_data"][
                "_recipe_import_verified_is_generic"
            ]
        )
        self.assertIn("Verified generic match", send.call_args.args[0])

    def test_verified_result_is_preserved_when_amount_needs_help(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_import_ingredient",
            "known_data": {
                "_recipe_import_pending": [{
                    "ingredient_name": "marinara",
                    "amount_description": "1 cup",
                    "brand": None,
                    "optional": False,
                    "trace_only": False,
                }],
            },
        }
        result = {
            "found": True,
            "food": {
                "canonical_name": "Marinara",
                "serving_description": "125 g",
                "serving_amount": 125.0,
                "serving_unit": "g",
            },
            "nutrition": {
                "calories": 80.0,
                "protein_g": 2.0,
                "carbohydrates_g": 12.0,
                "fat_g": 2.0,
                "fiber_g": 2.0,
                "sugar_g": 7.0,
                "sodium_mg": 400.0,
            },
            "verification": {"source": "verified source"},
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
                return_value=result,
            ),
            patch.object(
                app,
                "ingredient_serving_multiplier",
                side_effect=ValueError("cannot be converted"),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Try verified lookup",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_import_verified_amount",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"][
                "_recipe_import_verified_result"
            ],
            result,
        )
        self.assertIn("result has been preserved", send.call_args.args[0])

    def test_verified_import_lookup_requires_confirmation_before_save(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_import_ingredient",
            "known_data": {
                "_recipe_import_pending": [{
                    "ingredient_name": "chicken breast",
                    "amount_description": "4 oz",
                    "brand": None,
                    "optional": False,
                    "trace_only": False,
                }],
            },
        }
        result = {
            "found": True,
            "food": {
                "canonical_name": "Chicken breast, cooked",
                "serving_description": "4 oz",
                "serving_amount": 4.0,
                "serving_unit": "oz",
                "brand": None,
            },
            "nutrition": {
                "calories": 187.0,
                "protein_g": 35.0,
                "carbohydrates_g": 0.0,
                "fat_g": 4.0,
                "fiber_g": 0.0,
                "sugar_g": 0.0,
                "sodium_mg": 80.0,
            },
            "verification": {"source": "USDA FoodData Central"},
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
                return_value=result,
            ),
            patch.object(
                app,
                "ingredient_serving_multiplier",
                return_value=1.0,
            ),
            patch.object(app, "add_food_with_nutrition") as add,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Try verified lookup",
                }
            })

        add.assert_not_called()
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_import_verified_confirmation",
        )
        self.assertIn("Save this as a Saved Food", send.call_args.args[0])

    def test_trace_ingredient_exclusion_is_shown_in_review(self) -> None:
        message = app.format_recipe_builder_review({
            "_recipe_builder_name": "Test Recipe",
            "_recipe_builder_meal_type": "dinner",
            "_recipe_builder_yield": 1,
            "_recipe_builder_ingredients": [BUILDER_INGREDIENT],
            "_recipe_import_excluded": ["1 tsp dried parsley"],
        })

        self.assertIn("Excluded from nutrition", message)
        self.assertIn("1 tsp dried parsley", message)

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

    def test_yield_opens_first_numbered_pantry_page(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_builder_yield",
            "known_data": {"_recipe_builder_name": "Pantry Dinner"},
        }
        foods = [pantry_food(food_id) for food_id in range(1, 13)]
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "list_recipe_pantry_foods",
                return_value=foods,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "10"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "recipe_builder_ingredient_select",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"]["_recipe_builder_food_ids"],
            list(range(1, 11)),
        )
        self.assertIn("Choose from My Pantry", send.call_args.args[0])
        self.assertIn("Page 1 of 2", send.call_args.args[0])
        self.assertNotIn("Pantry Food 11", send.call_args.args[0])

    def test_pantry_next_shows_second_page(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "recipe_builder_ingredient_select",
            "known_data": {
                "_recipe_builder_pantry_page": 1,
                "_recipe_builder_pantry_total_pages": 2,
            },
        }
        foods = [pantry_food(food_id) for food_id in range(1, 13)]
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "list_recipe_pantry_foods",
                return_value=foods,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Next"}
            })

        self.assertEqual(
            update.call_args.kwargs["known_data"]["_recipe_builder_food_ids"],
            [11, 12],
        )
        self.assertIn("Page 2 of 2", send.call_args.args[0])
        self.assertIn("Pantry Food 11", send.call_args.args[0])

    def test_pantry_chooser_keyboard_has_numbers_and_navigation(self) -> None:
        message = app.format_recipe_builder_food_choices(
            [pantry_food(1), pantry_food(2)],
            page=1,
            total_pages=2,
            nutrition_needed_count=3,
        )
        keyboard = app.menu_reply_markup(message)

        self.assertIn(["1", "2"], keyboard["keyboard"])
        self.assertIn(["Previous", "Next"], keyboard["keyboard"])
        self.assertIn("3 other Pantry item(s) need", message)

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
