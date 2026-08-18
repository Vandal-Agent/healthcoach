from __future__ import annotations

import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


class ShoppingListMenuTests(unittest.TestCase):
    def test_pantry_routes_to_persistent_shopping_list(self) -> None:
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
                    "text": "Shopping list",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "shopping_list",
        )
        self.assertIn("Shopping List Menu", send.call_args.args[0])

    def test_swap_can_be_added_to_shopping_list(self) -> None:
        swap = {
            "pantry_item_name": "Regular broth",
            "suggested_replacement": "Low-sodium broth",
            "why_it_helps": "Less sodium.",
            "shopping_tip": "Compare labels.",
            "heart_health_note": "Supports a lower-sodium pattern.",
            "evidence_basis": "known_nutrition",
            "available_pantry_item_name": None,
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_swaps",
            "known_data": {"pantry_swaps": [swap]},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "add_shopping_item",
                return_value={
                    "created": True,
                    "display_name": "Low-sodium broth",
                },
            ) as add,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Add 1"}
            })

        add.assert_called_once_with(
            display_name="Low-sodium broth",
            source="pantry_swap",
            source_note="Swap for Regular broth",
        )
        self.assertIn("Added Low-sodium broth", send.call_args.args[0])

    def test_swap_already_in_pantry_is_not_added(self) -> None:
        swap = {
            "pantry_item_name": "Butter",
            "suggested_replacement": "Olive oil",
            "why_it_helps": "More unsaturated fat.",
            "shopping_tip": "Use for cooking.",
            "heart_health_note": "Supports a heart-healthy pattern.",
            "evidence_basis": "general_guidance",
            "available_pantry_item_name": "Extra virgin olive oil",
        }
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "pantry_swaps",
            "known_data": {"pantry_swaps": [swap]},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "add_shopping_item") as add,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Add 1"}
            })

        add.assert_not_called()
        self.assertIn("already in My Pantry", send.call_args.args[0])

    def test_manual_items_require_confirmation(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "shopping_add_items",
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
                    "text": "lemons, low-sodium broth",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "shopping_add_confirmation",
        )
        self.assertIn("- lemons", send.call_args.args[0])
        self.assertIn("- low-sodium broth", send.call_args.args[0])

    def test_mark_purchased_moves_item_to_pantry(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "shopping_purchase_confirmation",
            "known_data": {
                "shopping_selected_id": 7,
                "shopping_selected_name": "Low-sodium broth",
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
                "mark_shopping_item_purchased",
                return_value={},
            ) as purchased,
            patch.object(app, "update_conversation"),
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        purchased.assert_called_once_with(7)
        self.assertIn("to My Pantry", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
