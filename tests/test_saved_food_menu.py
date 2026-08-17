import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


SAVED_FOOD = {
    "food_id": 42,
    "canonical_name": "tracys home salad",
    "serving_description": "1 serving",
    "version_number": 2,
    "verification_source": "user_entered",
    "calories": 120,
    "protein_g": 5,
    "carbohydrates_g": 10,
    "fat_g": 6,
    "fiber_g": 4,
    "sugar_g": 5,
    "sodium_mg": 420,
}


class SavedFoodMenuTests(unittest.TestCase):
    def test_menu_exposes_edit_and_delete(self) -> None:
        message = app.healthcoach_saved_foods_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("3. Edit saved food", message)
        self.assertIn("4. Delete saved food", message)
        self.assertIn(["Edit saved food"], keyboard["keyboard"])
        self.assertIn(["Delete saved food"], keyboard["keyboard"])

    def test_details_offer_edit_and_delete(self) -> None:
        message = app.format_saved_food_details(SAVED_FOOD)
        keyboard = app.menu_reply_markup(message)

        self.assertIn(
            ["Edit Saved Food", "Delete Saved Food"],
            keyboard["keyboard"],
        )

    def test_edit_selection_opens_edit_menu(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "saved_food_edit_select",
            "known_data": {"_saved_food_ids": [42]},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "list_user_saved_foods",
                return_value=[SAVED_FOOD],
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "1"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_food_edit_menu",
        )
        self.assertIn("Saved Food Edit Menu", send.call_args.args[0])

    def test_confirmed_rename_updates_saved_food(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "saved_food_identity_confirmation",
            "known_data": {
                "_saved_food_edit_id": 42,
                "_saved_food_identity_kind": "canonical_name",
                "_saved_food_identity_value": "Tracy's Home Salad",
            },
        }
        updated = dict(SAVED_FOOD)
        updated["canonical_name"] = "Tracy's Home Salad"
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "list_user_saved_foods",
                return_value=[SAVED_FOOD],
            ),
            patch.object(
                app,
                "update_user_saved_food_identity",
                return_value=updated,
            ) as rename,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        rename.assert_called_once_with(
            food_id=42,
            canonical_name="Tracy's Home Salad",
        )
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_food_details",
        )
        self.assertIn("Saved Food updated", send.call_args.args[0])

    def test_confirmed_delete_archives_saved_food(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "saved_food_delete_confirmation",
            "known_data": {"_saved_food_edit_id": 42},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "list_user_saved_foods",
                return_value=[SAVED_FOOD],
            ),
            patch.object(
                app,
                "archive_user_saved_food",
                return_value=SAVED_FOOD,
            ) as archive,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        archive.assert_called_once_with(42)
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_foods",
        )
        self.assertIn(
            "Previously logged entries were not changed",
            send.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
