from __future__ import annotations

import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

# Menu routing tests use a synthetic chat ID and must not inherit the
# production Telegram allowlist from the server environment.
app.CHAT_ID = None


class PantryMenuTests(unittest.TestCase):
    def test_food_menu_contains_separate_pantry_entry(self) -> None:
        message = app.healthcoach_food_menu_text()

        self.assertIn("7. My Pantry", message)
        self.assertIn("8. Photo tools", message)
        self.assertIn("11. Back", message)

        keyboard = app.menu_reply_markup(message)
        self.assertIn(["My Pantry"], keyboard["keyboard"])

    def test_pantry_menu_exposes_foundation_actions(self) -> None:
        message = app.healthcoach_pantry_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("View pantry", message)
        self.assertIn("Add items manually", message)
        self.assertIn("Scan product into Pantry", message)
        self.assertIn("Get meal ideas", message)
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
            ["Get meal ideas"],
            keyboard["keyboard"],
        )

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
