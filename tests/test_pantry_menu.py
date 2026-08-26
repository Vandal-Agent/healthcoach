from __future__ import annotations

import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

# Menu routing tests use a synthetic chat ID and must not inherit the
# production Telegram allowlist from the server environment.
app.CHAT_ID = None


class PantryMenuTests(unittest.TestCase):
    def test_pantry_ideas_label_exact_heart_healthy_pick(self) -> None:
        base = {
            "summary": "Quick bowl",
            "ingredients": [
                {
                    "name": "Chicken breast",
                    "amount": "4 ounces",
                    "source": "pantry",
                }
            ],
            "preparation_steps": ["Cook and serve."],
            "calories": 450,
            "protein_g": 42,
            "carbohydrates_g": 40,
            "fat_g": 13,
            "fiber_g": 8,
            "sugar_g": 6,
            "sodium_mg": 520,
            "daily_fit": "Adds protein and fiber.",
            "estimate_notes": "Portions are estimated.",
        }
        ideas = [
            {**base, "name": "Idea A"},
            {
                **base,
                "name": "Idea B",
                "heart_healthy_pick": True,
                "heart_healthy_reason": (
                    "Lean protein, vegetables, and useful fiber."
                ),
            },
            {**base, "name": "Idea C"},
        ]

        message = app.format_pantry_meal_ideas(
            ideas,
            meal_type="dinner",
        )

        self.assertIn("2. Idea B — Heart-Healthy Pick", message)
        self.assertNotIn("1. Idea A — Heart-Healthy Pick", message)
        self.assertIn("Heart-healthy note: Lean protein", message)
        self.assertIn("not a medical rating", message)

        details = app.format_pantry_meal_idea_details(
            ideas[1],
            meal_type="dinner",
        )
        self.assertIn("Heart-Healthy Pick", details)
        self.assertIn("not a medical rating", details)

    def test_food_menu_contains_separate_pantry_entry(self) -> None:
        message = app.healthcoach_food_menu_text()

        self.assertIn("6. Same as yesterday", message)
        self.assertIn("9. Saved recipes", message)
        self.assertIn("10. My Pantry", message)
        self.assertIn("11. Photo tools", message)
        self.assertIn("14. Back", message)

        keyboard = app.menu_reply_markup(message)
        self.assertIn(
            ["Saved recipes", "My Pantry"],
            keyboard["keyboard"],
        )

    def test_pantry_idea_can_start_save_recipe_confirmation(self) -> None:
        idea = {
            "name": "Chicken Pepper Bowl",
            "summary": "Quick bowl",
            "ingredients": [
                {
                    "name": "Chicken breast",
                    "amount": "4 ounces",
                    "source": "pantry",
                }
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
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_meal_idea_details",
            "known_data": {
                "pantry_meal_type": "dinner",
                "pantry_meal_ideas": [idea],
                "pantry_meal_selected_index": 0,
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
                "message": {
                    "chat": {"id": 123},
                    "text": "Save Recipe",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_meal_save_confirmation",
        )
        self.assertIn("Save this recipe?", send.call_args.args[0])

    def test_confirming_save_recipe_does_not_log_food(self) -> None:
        idea = {
            "name": "Chicken Pepper Bowl",
            "ingredients": [],
            "preparation_steps": [],
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_meal_save_confirmation",
            "known_data": {
                "pantry_meal_type": "dinner",
                "pantry_meal_ideas": [idea],
                "pantry_meal_selected_index": 0,
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
                "save_pantry_meal_idea",
                return_value={"created": True},
            ) as save,
            patch.object(app, "add_food_entry") as add_entry,
            patch.object(app, "update_conversation"),
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Yes",
                }
            })

        save.assert_called_once_with(idea, meal_type="dinner")
        add_entry.assert_not_called()
        self.assertIn("Saved this recipe", send.call_args.args[0])

    def test_repeated_save_recipe_does_not_bypass_confirmation(self) -> None:
        idea = {
            "name": "Chicken Pepper Bowl",
            "ingredients": [],
            "preparation_steps": [],
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_meal_save_confirmation",
            "known_data": {
                "pantry_meal_type": "dinner",
                "pantry_meal_ideas": [idea],
                "pantry_meal_selected_index": 0,
            },
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "save_pantry_meal_idea") as save,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Save Recipe",
                }
            })

        save.assert_not_called()
        self.assertIn("Please choose Yes or No", send.call_args.args[0])

    def test_pantry_menu_exposes_foundation_actions(self) -> None:
        message = app.healthcoach_pantry_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("View pantry", message)
        self.assertIn("Add items manually", message)
        self.assertIn("Scan product into Pantry", message)
        self.assertIn("Add items from shelf photo", message)
        self.assertIn("Complete Pantry nutrition", message)
        self.assertIn("Get meal ideas", message)
        self.assertIn("Smart Pantry swaps", message)
        self.assertIn("Shopping list", message)
        self.assertIn("Remove pantry item", message)
        self.assertIn("Clear pantry", message)
        self.assertIn(
            ["View pantry", "Add items manually"],
            keyboard["keyboard"],
        )
        self.assertIn(
            ["Scan product into Pantry"],
            keyboard["keyboard"],
        )
        self.assertIn(
            ["Add items from shelf photo"],
            keyboard["keyboard"],
        )
        self.assertIn(
            ["Complete Pantry nutrition"],
            keyboard["keyboard"],
        )
        self.assertIn(
            ["Get meal ideas", "Smart Pantry swaps"],
            keyboard["keyboard"],
        )

    def test_smart_pantry_swaps_are_advisory(self) -> None:
        message = app.format_smart_pantry_swaps([
            {
                "pantry_item_name": "Regular mayonnaise",
                "suggested_replacement": "Plain Greek yogurt",
                "why_it_helps": "It can lower saturated-fat density.",
                "shopping_tip": "Compare sodium and saturated fat.",
                "heart_health_note": "Less saturated fat can fit a "
                "heart-healthy pattern.",
                "evidence_basis": "known_nutrition",
            }
        ])
        keyboard = app.menu_reply_markup(message)

        self.assertIn("Replace: Regular mayonnaise", message)
        self.assertIn("Try: Plain Greek yogurt", message)
        self.assertIn("saved package nutrition", message)
        self.assertIn("Nothing in your Pantry has been changed", message)
        self.assertIn("not a medical rating or diagnosis", message)
        self.assertEqual(
            keyboard["keyboard"],
            [
                ["Add 1"],
                ["Shopping list", "Refresh swaps"],
                ["Back", "Cancel"],
            ],
        )

    def test_pantry_routes_to_smart_swaps(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry",
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
                "show_smart_pantry_swaps",
                return_value=True,
            ) as show,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Smart Pantry swaps",
                }
            })

        show.assert_called_once_with(chat_id=123)

    def test_pantry_meal_idea_action_asks_for_meal_type(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry",
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
                "list_pantry_items",
                return_value=[{"display_name": "Chicken breast"}],
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Get meal ideas",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_meal_type",
        )
        self.assertIn(
            "What meal do you want Pantry ideas for?",
            send.call_args.args[0],
        )

    def test_pantry_meal_type_generates_lunch_ideas(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_meal_type",
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
                "show_pantry_meal_ideas",
                return_value=True,
            ) as show,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Lunch",
                }
            })

        show.assert_called_once_with(
            chat_id=123,
            meal_type="lunch",
        )

    def test_confirmed_pantry_meal_logs_estimated_entry(self) -> None:
        meal_idea = {
            "name": "Chicken Pepper Bowl",
            "calories": 450,
            "protein_g": 42,
            "carbohydrates_g": 40,
            "fat_g": 13,
            "fiber_g": 8,
            "sugar_g": 6,
            "sodium_mg": 520,
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_meal_log_confirmation",
            "known_data": {
                "pantry_meal_type": "dinner",
                "pantry_meal_ideas": [meal_idea],
                "pantry_meal_selected_index": 0,
                "pantry_meal_servings": 1.5,
            },
        }
        created = {"food": {"food_id": 88}}
        logged = {"calories": 675, "protein_g": 63}

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "add_food_with_nutrition",
                return_value=created,
            ) as add_food,
            patch.object(
                app,
                "add_food_entry",
                return_value=logged,
            ) as add_entry,
            patch.object(app, "sync_food_ledger_totals_to_sheet"),
            patch.object(app, "update_conversation"),
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Log Meal",
                }
            })

        self.assertEqual(
            add_food.call_args.kwargs["verification_status"],
            "estimated",
        )
        self.assertEqual(
            add_food.call_args.kwargs["verification_source"],
            "pantry_meal_idea",
        )
        self.assertEqual(add_entry.call_args.kwargs["quantity"], 1.5)
        self.assertTrue(
            add_entry.call_args.kwargs["quantity_is_estimated"]
        )
        self.assertTrue(add_entry.call_args.kwargs["user_confirmed"])
        self.assertIn(
            "Estimated Pantry meal logged.",
            send.call_args.args[0],
        )

    def test_barcode_product_offers_add_to_pantry(self) -> None:
        message = app.format_barcode_product(
            {
                "found": True,
                "food": {
                    "canonical_name": "Test Food",
                    "serving_description": "1 serving",
                },
                "nutrition": {"calories": 100},
                "verification": {"source": "test"},
            },
            barcode="036000291452",
        )
        keyboard = app.menu_reply_markup(message)

        self.assertIn("Reply Add to Pantry", message)
        self.assertIn(
            ["Add to Pantry", "Log It"],
            keyboard["keyboard"],
        )

    def test_pantry_list_uses_back_only_keyboard(self) -> None:
        message = app.format_pantry_items(
            [{"display_name": "Chicken breast", "source": "manual"}]
        )
        keyboard = app.menu_reply_markup(message)

        self.assertIn("1. Chicken breast", message)
        self.assertEqual(
            keyboard["keyboard"],
            [["Back", "Cancel"]],
        )

    def test_organized_pantry_view_is_grouped_and_paginated(self) -> None:
        items = [
            {
                "pantry_item_id": index,
                "display_name": f"Shelf Item {index:02d}",
                "source": "manual",
                "storage_area": "pantry_shelf",
                "food_category": "canned_jarred",
                "nutrition_version_id": 5 if index == 1 else None,
                "calories": 100 if index == 1 else None,
            }
            for index in range(1, 14)
        ]

        first_page = app.format_pantry_items(items, page=0)
        second_page = app.format_pantry_items(items, page=1)
        keyboard = app.menu_reply_markup(first_page)

        self.assertIn("My Pantry — Page 1 of 2", first_page)
        self.assertIn("PANTRY SHELF", first_page)
        self.assertIn("Canned/jarred — nutrition ready", first_page)
        self.assertIn("Nutrition ready: 1/13 items", first_page)
        self.assertNotIn("Shelf Item 13", first_page)
        self.assertIn("Shelf Item 13", second_page)
        self.assertEqual(
            keyboard["keyboard"],
            [["Next"], ["Back", "Cancel"]],
        )

    def test_food_menu_routes_to_pantry(self) -> None:
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
                    "text": "My Pantry",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry",
        )
        self.assertIn(
            "My Pantry Menu",
            send.call_args.args[0],
        )

    def test_barcode_result_can_add_product_to_pantry(self) -> None:
        result = {
            "found": True,
            "food": {
                "canonical_name": "Test Food",
                "serving_description": "1 serving",
            },
            "nutrition": {"calories": 100},
            "verification": {"source": "test"},
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "barcode_result",
            "known_data": {
                "barcode": "036000291452",
                "barcode_result": result,
                "barcode_saved": False,
                "pantry_scan_mode": True,
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
                "save_barcode_product_result",
                return_value={"food": {"food_id": 42}},
            ),
            patch.object(
                app,
                "add_pantry_item",
                return_value={"created": True},
            ) as add_pantry,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Add to Pantry",
                }
            })

        self.assertEqual(
            add_pantry.call_args.kwargs,
            {
                "display_name": "Test Food",
                "food_id": 42,
                "source": "barcode",
                "barcode_text": "036000291452",
            },
        )
        self.assertIn(
            "Added this scanned product to My Pantry.",
            send.call_args.args[0],
        )
        self.assertIn(
            "My Pantry Menu",
            send.call_args.args[0],
        )
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry",
        )

    def test_pantry_scan_action_waits_for_barcode_photo(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry",
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
                    "text": "Scan product into Pantry",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "await_barcode_photo",
        )
        self.assertTrue(
            update.call_args.kwargs["known_data"]["pantry_scan_mode"]
        )
        self.assertIn(
            "Send a clear photo of a product barcode",
            send.call_args.args[0],
        )

    def test_pantry_routes_to_nutrition_completion_queue(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry",
            "known_data": {},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "show_pantry_nutrition_queue") as show_queue,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Complete Pantry nutrition",
                }
            })

        show_queue.assert_called_once_with(chat_id=123, skipped_ids=[])

    def test_pantry_nutrition_queue_filters_and_paginates(self) -> None:
        incomplete = [
            {
                "pantry_item_id": index,
                "display_name": f"Item {index:02d}",
                "nutrition_version_id": None,
                "calories": None,
            }
            for index in range(1, 12)
        ]
        ready = {
            "pantry_item_id": 99,
            "display_name": "Ready item",
            "nutrition_version_id": 9,
            "calories": 100,
        }

        queued = app.pantry_nutrition_items(
            [*incomplete, ready],
            skipped_ids=[2],
        )
        message = app.format_pantry_nutrition_choices(
            queued,
            page=0,
            total_incomplete=11,
        )

        self.assertEqual(len(queued), 10)
        self.assertNotIn("Item 02", message)
        self.assertNotIn("Ready item", message)
        self.assertIn("Needs nutrition: 11 item(s)", message)
        self.assertIn("Page 1 of 1", message)

    def test_existing_nutrition_includes_usda_and_ranks_name_matches(
        self,
    ) -> None:
        foods = [
            {
                "food_id": 1,
                "canonical_name": "Chicken Breast",
                "brand": None,
                "restaurant": None,
                "food_type": "food",
                "verification_status": "verified",
                "verification_source": "fdc.nal.usda.gov",
                "nutrition_version_id": 11,
                "calories": 165,
            },
            {
                "food_id": 2,
                "canonical_name": "Extra Large Pitted Ripe Olives",
                "brand": "Test Brand",
                "restaurant": None,
                "food_type": "food",
                "verification_status": "verified",
                "verification_source": "fdc.nal.usda.gov",
                "nutrition_version_id": 12,
                "calories": 25,
            },
            {
                "food_id": 3,
                "canonical_name": "Restaurant Olives",
                "brand": None,
                "restaurant": "Test Restaurant",
                "food_type": "food",
                "verification_status": "verified",
                "verification_source": "fdc.nal.usda.gov",
                "nutrition_version_id": 13,
                "calories": 30,
            },
            {
                "food_id": 4,
                "canonical_name": "BLACK BEANS",
                "brand": None,
                "restaurant": None,
                "food_type": "food",
                "verification_status": "verified",
                "verification_source": "fdc.nal.usda.gov",
                "nutrition_version_id": 14,
                "calories": 110,
            },
            {
                "food_id": 5,
                "canonical_name": "Organic Extra Virgin Olive Oil",
                "brand": None,
                "restaurant": None,
                "food_type": "food",
                "verification_status": "verified",
                "verification_source": "user_entered",
                "nutrition_version_id": 15,
                "calories": 120,
            },
            {
                "food_id": 6,
                "canonical_name": "Olives",
                "brand": None,
                "restaurant": None,
                "food_type": "food",
                "verification_status": "verified",
                "verification_source": "user_entered",
                "nutrition_version_id": 16,
                "calories": 20,
            },
        ]

        with patch.object(
            app,
            "list_nutrition_ready_foods",
            return_value=foods,
        ):
            matches = app.pantry_linkable_foods("black olives")

        self.assertEqual(
            [food["food_id"] for food in matches],
            [6, 2],
        )

        message = app.format_pantry_saved_food_choices(
            matches,
            pantry_name="black olives",
        )
        self.assertIn("Only close name matches are shown", message)
        self.assertIn("fdc.nal.usda.gov", message)

    def test_existing_nutrition_rejects_generic_cheese_matches(self) -> None:
        foods = [
            {
                "food_id": 1,
                "canonical_name": "Colby-Jack Cheese",
                "brand": None,
                "restaurant": None,
                "food_type": "food",
                "verification_status": "verified",
                "verification_source": "user_entered",
                "nutrition_version_id": 11,
                "calories": 70,
            },
            {
                "food_id": 2,
                "canonical_name": "Blue Cheese Crumbles",
                "brand": None,
                "restaurant": None,
                "food_type": "food",
                "verification_status": "verified",
                "verification_source": "fdc.nal.usda.gov",
                "nutrition_version_id": 12,
                "calories": 100,
            },
        ]

        with patch.object(
            app,
            "list_nutrition_ready_foods",
            return_value=foods,
        ):
            matches = app.pantry_linkable_foods("blue cheese crumbles")

        self.assertEqual([food["food_id"] for food in matches], [2])

    def test_skipping_nutrition_item_changes_nothing(self) -> None:
        pantry_item = {
            "pantry_item_id": 42,
            "display_name": "Unlabeled food",
            "nutrition_version_id": None,
            "calories": None,
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_nutrition_options",
            "known_data": {
                "pantry_nutrition_item_id": 42,
                "pantry_nutrition_item_name": "Unlabeled food",
                "pantry_nutrition_page": 0,
                "pantry_nutrition_skipped_ids": [],
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
                "get_pantry_item_by_id",
                return_value=pantry_item,
            ),
            patch.object(app, "show_pantry_nutrition_queue") as show_queue,
            patch.object(app, "link_pantry_item_to_food") as link,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Skip",
                }
            })

        link.assert_not_called()
        show_queue.assert_called_once_with(
            chat_id=123,
            page=0,
            skipped_ids=[42],
        )
        self.assertIn("Nothing was changed", send.call_args.args[0])

    def test_saved_food_link_requires_confirmation(self) -> None:
        saved_food = {
            "food_id": 7,
            "canonical_name": "Black Beans",
            "serving_description": "1/2 cup",
            "nutrition_version_id": 8,
            "calories": 110,
            "protein_g": 7,
            "verification_source": "user_package_label",
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_nutrition_saved_food_select",
            "known_data": {
                "pantry_nutrition_item_id": 42,
                "pantry_nutrition_item_name": "Beans",
                "pantry_nutrition_saved_food_page": 0,
                "pantry_nutrition_saved_food_ids": [7],
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
                "pantry_linkable_foods",
                return_value=[saved_food],
            ),
            patch.object(
                app,
                "get_pantry_item_by_id",
                return_value={"pantry_item_id": 42, "display_name": "Beans"},
            ),
            patch.object(app, "link_pantry_item_to_food") as link,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "1"}
            })

        link.assert_not_called()
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_nutrition_saved_food_confirmation",
        )
        self.assertIn("Nothing has been linked", send.call_args.args[0])

    def test_confirmed_saved_food_links_selected_pantry_item(self) -> None:
        saved_food = {
            "food_id": 7,
            "canonical_name": "Black Beans",
            "nutrition_version_id": 8,
            "calories": 110,
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_nutrition_saved_food_confirmation",
            "known_data": {
                "pantry_nutrition_item_id": 42,
                "pantry_nutrition_food_id": 7,
            },
        }
        linked = {"pantry_item_id": 42, "display_name": "Beans"}
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "pantry_linkable_foods",
                return_value=[saved_food],
            ),
            patch.object(
                app,
                "link_pantry_item_to_food",
                return_value=linked,
            ) as link,
            patch.object(app, "finish_pantry_nutrition_link") as finish,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        link.assert_called_once_with(42, food_id=7, source="saved_food")
        finish.assert_called_once()

    def test_barcode_nutrition_links_without_adding_duplicate_pantry_item(
        self,
    ) -> None:
        result = {
            "found": True,
            "food": {"canonical_name": "Test Food"},
            "nutrition": {"calories": 100},
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "barcode_result",
            "known_data": {
                "barcode": "036000291452",
                "barcode_result": result,
                "pantry_nutrition_mode": True,
                "pantry_nutrition_item_id": 42,
            },
        }
        linked = {"pantry_item_id": 42, "display_name": "Existing item"}
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "save_barcode_product_result",
                return_value={
                    "food": {"food_id": 7, "canonical_name": "Test Food"}
                },
            ),
            patch.object(
                app,
                "link_pantry_item_to_food",
                return_value=linked,
            ) as link,
            patch.object(app, "add_pantry_item") as add_pantry,
            patch.object(app, "finish_pantry_nutrition_link") as finish,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Link nutrition",
                }
            })

        link.assert_called_once_with(
            42,
            food_id=7,
            source="barcode",
            barcode_text="036000291452",
        )
        add_pantry.assert_not_called()
        finish.assert_called_once()

    def test_barcode_nutrition_result_has_link_button(self) -> None:
        message = app.format_barcode_product(
            {
                "found": True,
                "food": {
                    "canonical_name": "Black Beans",
                    "serving_description": "1/2 cup",
                },
                "nutrition": {"calories": 110},
                "verification": {"source": "USDA"},
            },
            barcode="036000291452",
            saved=False,
            pantry_item_name="Beans on shelf",
        )

        keyboard = app.menu_reply_markup(message)

        self.assertIn("Selected Pantry item: Beans on shelf", message)
        self.assertIn(["Link nutrition"], keyboard["keyboard"])
        self.assertNotIn(
            ["Add to Pantry", "Log It"],
            keyboard["keyboard"],
        )

    def test_pantry_label_photo_requires_review_before_saving(self) -> None:
        label = {
            "readable": True,
            "serving_description": "1 cup (240 g)",
            "serving_amount": 240,
            "serving_unit": "g",
            "calories": 110,
            "protein_g": 7,
            "carbohydrates_g": 20,
            "fat_g": 0,
            "fiber_g": 6,
            "sugar_g": 1,
            "sodium_mg": 130,
            "brand": "Test Brand",
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_nutrition_label_photo",
            "known_data": {
                "pantry_nutrition_item_id": 42,
                "pantry_nutrition_item_name": "Black Beans",
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
                "download_telegram_photo",
                return_value=(b"image", "image/jpeg"),
            ),
            patch.object(
                app,
                "read_nutrition_label_photo",
                return_value=label,
            ),
            patch.object(
                app,
                "get_pantry_item_by_id",
                return_value={"pantry_item_id": 42, "display_name": "Black Beans"},
            ),
            patch.object(app, "add_food_with_nutrition") as add_food,
            patch.object(app, "link_pantry_item_to_food") as link,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "photo": [{"file_id": "photo-1"}],
                }
            })

        add_food.assert_not_called()
        link.assert_not_called()
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_nutrition_label_confirmation",
        )
        self.assertIn("Nothing has been linked or saved yet", send.call_args.args[0])

    def test_confirmed_pantry_label_saves_and_links(self) -> None:
        label = {
            "serving_description": "1 cup (240 g)",
            "serving_amount": 240,
            "serving_unit": "g",
            "calories": 110,
            "protein_g": 7,
            "carbohydrates_g": 20,
            "fat_g": 0,
            "fiber_g": 6,
            "sugar_g": 1,
            "sodium_mg": 130,
            "brand": "Test Brand",
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_nutrition_label_confirmation",
            "known_data": {
                "pantry_nutrition_item_id": 42,
                "pantry_nutrition_item_name": "Black Beans",
                "pantry_nutrition_label_result": label,
            },
        }
        linked = {"pantry_item_id": 42, "display_name": "Black Beans"}
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "add_food_with_nutrition",
                return_value={
                    "food": {"food_id": 7, "canonical_name": "Black Beans"}
                },
            ) as add_food,
            patch.object(
                app,
                "link_pantry_item_to_food",
                return_value=linked,
            ) as link,
            patch.object(app, "finish_pantry_nutrition_link") as finish,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        self.assertEqual(
            add_food.call_args.kwargs["verification_source"],
            "user_package_label",
        )
        self.assertEqual(add_food.call_args.kwargs["calories"], 110)
        link.assert_called_once_with(42, food_id=7, source="saved_food")
        finish.assert_called_once()

    def test_pantry_routes_to_paginated_organizer(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry",
            "known_data": {},
        }
        items = [
            {
                "pantry_item_id": 42,
                "display_name": "Chicken breast",
                "storage_area": "unsorted",
                "food_category": "unsorted",
            }
        ]
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "list_pantry_items", return_value=items),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Organize pantry",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_organize_select",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"]["pantry_item_ids"],
            [42],
        )
        self.assertIn("Chicken breast", send.call_args.args[0])

    def test_pantry_organizer_item_offers_rename_or_categories(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_organize_select",
            "known_data": {
                "pantry_page": 0,
                "pantry_item_ids": [42],
            },
        }
        item = {
            "pantry_item_id": 42,
            "display_name": "Chicken breast",
            "storage_area": "freezer",
            "food_category": "protein",
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "list_pantry_items", return_value=[item]),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "1"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_organize_action",
        )
        self.assertIn("Change name", send.call_args.args[0])
        self.assertIn("Change storage and food type", send.call_args.args[0])
        self.assertIn("Delete Pantry item", send.call_args.args[0])

    def test_pantry_organizer_delete_requires_confirmation(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_organize_action",
            "known_data": {
                "pantry_page": 0,
                "pantry_organize_id": 42,
                "pantry_organize_name": "Old crackers",
            },
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "remove_pantry_item") as remove,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Delete Pantry item",
                }
            })

        remove.assert_not_called()
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_organize_delete_confirmation",
        )
        self.assertIn("Only its Pantry presence", send.call_args.args[0])
        self.assertIn("Nothing has been deleted", send.call_args.args[0])

    def test_confirmed_organizer_delete_preserves_linked_data(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_organize_delete_confirmation",
            "known_data": {
                "pantry_page": 0,
                "pantry_organize_id": 42,
                "pantry_organize_name": "Old crackers",
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
                "remove_pantry_item",
                return_value=True,
            ) as remove,
            patch.object(
                app,
                "list_pantry_items",
                return_value=[{"pantry_item_id": 99}],
            ),
            patch.object(app, "show_pantry_organizer_page") as show,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        remove.assert_called_once_with(42)
        show.assert_called_once_with(chat_id=123, page=0)
        self.assertIn("Saved Food, nutrition, recipes", send.call_args.args[0])

    def test_pantry_organizer_rename_requires_confirmation(self) -> None:
        rename_conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_organize_rename",
            "known_data": {
                "pantry_page": 0,
                "pantry_organize_id": 42,
                "pantry_organize_name": "Chicken breast package",
            },
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=rename_conversation,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Chicken breast",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_organize_rename_confirmation",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"][
                "pantry_organize_new_name"
            ],
            "Chicken breast",
        )
        self.assertIn("Nothing has been changed", send.call_args.args[0])

        confirmation_conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_organize_rename_confirmation",
            "known_data": update.call_args.kwargs["known_data"],
        }
        renamed = {
            "pantry_item_id": 42,
            "display_name": "Chicken breast",
            "storage_area": "freezer",
            "food_category": "protein",
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=confirmation_conversation,
            ),
            patch.object(
                app,
                "rename_pantry_item",
                return_value=renamed,
            ) as rename,
            patch.object(app, "list_pantry_items", return_value=[renamed]),
            patch.object(app, "update_conversation"),
            patch.object(app, "send_telegram_msg") as confirmation_send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        rename.assert_called_once_with(42, display_name="Chicken breast")
        self.assertIn(
            "Pantry item renamed",
            confirmation_send.call_args_list[0].args[0],
        )

    def test_pantry_organizer_confirms_before_updating(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_organize_confirmation",
            "known_data": {
                "pantry_page": 0,
                "pantry_organize_id": 42,
                "pantry_organize_name": "Chicken breast",
                "pantry_organize_storage": "freezer",
                "pantry_organize_category": "protein",
            },
        }
        updated_item = {
            "pantry_item_id": 42,
            "display_name": "Chicken breast",
            "storage_area": "freezer",
            "food_category": "protein",
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "update_pantry_item_organization",
                return_value=updated_item,
            ) as update_item,
            patch.object(
                app,
                "list_pantry_items",
                return_value=[updated_item],
            ),
            patch.object(app, "update_conversation"),
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        update_item.assert_called_once_with(
            42,
            storage_area="freezer",
            food_category="protein",
        )
        self.assertIn(
            "Pantry organization updated",
            send.call_args_list[0].args[0],
        )

    def test_pantry_organizer_collects_storage_then_food_type(self) -> None:
        storage_conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_organize_storage",
            "known_data": {
                "pantry_page": 0,
                "pantry_organize_id": 42,
                "pantry_organize_name": "Chicken breast",
            },
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=storage_conversation,
            ),
            patch.object(app, "update_conversation") as update_storage,
            patch.object(app, "send_telegram_msg"),
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Freezer"}
            })

        self.assertEqual(
            update_storage.call_args.kwargs["current_step"],
            "pantry_organize_category",
        )
        storage_data = update_storage.call_args.kwargs["known_data"]
        self.assertEqual(
            storage_data["pantry_organize_storage"],
            "freezer",
        )

        category_conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_organize_category",
            "known_data": storage_data,
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=category_conversation,
            ),
            patch.object(app, "update_conversation") as update_category,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Protein"}
            })

        self.assertEqual(
            update_category.call_args.kwargs["current_step"],
            "pantry_organize_confirmation",
        )
        self.assertEqual(
            update_category.call_args.kwargs["known_data"][
                "pantry_organize_category"
            ],
            "protein",
        )
        self.assertIn("Freezer", send.call_args.args[0])
        self.assertIn("Protein", send.call_args.args[0])

    def test_pantry_shelf_action_waits_for_photo(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry",
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
                    "text": "Add items from shelf photo",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "await_pantry_photo",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"]["pantry_photo_names"],
            [],
        )
        self.assertIn("one Pantry shelf", send.call_args.args[0])

    def test_shelf_photo_accumulates_distinct_items_without_saving(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "await_pantry_photo",
            "known_data": {"pantry_photo_names": ["Rice"]},
        }
        analysis = {
            "readable": True,
            "items": [
                {"display_name": "rice"},
                {"display_name": "Kroger Black Beans"},
            ],
            "notes": [],
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "download_telegram_photo",
                return_value=(b"photo", "image/jpeg"),
            ),
            patch.object(
                app,
                "analyze_pantry_photo",
                return_value=analysis,
            ) as analyze,
            patch.object(app, "add_pantry_items") as add_items,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "photo": [{"file_id": "shelf-photo"}],
                }
            })

        analyze.assert_called_once_with(
            b"photo",
            mime_type="image/jpeg",
            user_context="",
        )
        add_items.assert_not_called()
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_photo_result",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"]["pantry_photo_names"],
            ["Rice", "Kroger Black Beans"],
        )
        self.assertIn("Kroger Black Beans", send.call_args.args[0])
        self.assertIn("Nothing has been added", send.call_args.args[0])

    def test_shelf_photo_error_preserves_pending_review(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "await_pantry_photo",
            "known_data": {
                "pantry_photo_names": ["Rice", "Black Beans"],
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
                "download_telegram_photo",
                return_value=(b"photo", "image/jpeg"),
            ),
            patch.object(
                app,
                "analyze_pantry_photo",
                side_effect=RuntimeError("temporary analysis error"),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "photo": [{"file_id": "shelf-photo"}],
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "await_pantry_photo",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"]["pantry_photo_names"],
            ["Rice", "Black Beans"],
        )
        self.assertIn("Nothing was added", send.call_args.args[0])

    def test_shelf_photo_review_can_remove_pending_item(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_photo_remove",
            "known_data": {
                "pantry_photo_names": ["Rice", "Black Beans"],
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
                "message": {"chat": {"id": 123}, "text": "2"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_photo_review",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"]["pantry_photo_names"],
            ["Rice"],
        )
        self.assertIn("Removed Black Beans", send.call_args.args[0])

    def test_shelf_photo_review_opens_name_editor(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_photo_review",
            "known_data": {
                "pantry_photo_names": ["Campbell's Condensed Soup"],
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
                "message": {
                    "chat": {"id": 123},
                    "text": "Edit item name",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_photo_edit",
        )
        self.assertIn("Campbell's Condensed Soup", send.call_args.args[0])
        keyboard = app.menu_reply_markup(
            app.format_pantry_photo_review(
                ["Campbell's Condensed Soup"]
            )
        )
        self.assertIn(
            ["Edit item name", "Remove an item"],
            keyboard["keyboard"],
        )

    def test_shelf_photo_name_edit_merges_pending_duplicate(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_photo_edit_name",
            "known_data": {
                "pantry_photo_names": [
                    "Cilantro & Lime Rice",
                    "Cilantro Lime Rice",
                    "Black Beans",
                ],
                "pantry_photo_edit_index": 0,
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
                "message": {
                    "chat": {"id": 123},
                    "text": "Cilantro Lime Rice",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_photo_review",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"]["pantry_photo_names"],
            ["Cilantro Lime Rice", "Black Beans"],
        )
        self.assertIn(
            "duplicate pending item was merged",
            send.call_args.args[0],
        )

    def test_shelf_photo_editor_selects_item_before_renaming(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_photo_edit",
            "known_data": {
                "pantry_photo_names": [
                    "Campbell's Condensed Soup",
                    "Black Beans",
                ],
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
                "message": {"chat": {"id": 123}, "text": "1"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry_photo_edit_name",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"][
                "pantry_photo_edit_index"
            ],
            0,
        )
        self.assertIn(
            "Current name: Campbell's Condensed Soup",
            send.call_args.args[0],
        )

    def test_shelf_photo_add_requires_explicit_review_confirmation(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_photo_review",
            "known_data": {
                "pantry_photo_names": ["Rice", "Black Beans"],
            },
        }
        result = {
            "created": [{"display_name": "Rice"}],
            "existing": [{"display_name": "Black Beans"}],
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "add_pantry_items",
                return_value=result,
            ) as add_items,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Add all to Pantry",
                }
            })

        add_items.assert_called_once_with(
            ["Rice", "Black Beans"],
            source="shelf_photo",
        )
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "pantry",
        )
        self.assertIn("Added 1 shelf-photo item", send.call_args.args[0])
        self.assertIn("1 item(s) were already there", send.call_args.args[0])

    def test_photo_in_manual_add_opens_universal_chooser(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_add_items",
            "known_data": {},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "start_conversation") as start,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "photo": [{"file_id": "test-photo"}],
                }
            })

        self.assertEqual(
            start.call_args.kwargs["current_step"],
            "photo_intent",
        )
        self.assertIn(
            "What should I do with this photo?",
            send.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
