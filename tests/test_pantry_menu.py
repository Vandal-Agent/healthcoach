from __future__ import annotations

import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app


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
        self.assertIn("Add pantry items", message)
        self.assertIn("Remove pantry item", message)
        self.assertIn("Clear pantry", message)
        self.assertIn(
            ["View pantry", "Add pantry items"],
            keyboard["keyboard"],
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
            patch.object(app, "update_conversation"),
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


if __name__ == "__main__":
    unittest.main()
