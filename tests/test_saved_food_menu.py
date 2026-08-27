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

FINDER_FOOD = {
    "food_id": 79,
    "canonical_name": "Protein bar",
    "brand": "Homemade",
    "restaurant": None,
    "food_type": "food",
    "serving_description": "1 bar (53 g)",
    "verification_status": "verified",
    "verification_source": "usda.gov",
    "version_number": 1,
    "calories": 218,
    "protein_g": 16.1,
    "carbohydrates_g": 20.4,
    "fat_g": 8.1,
    "fiber_g": None,
    "sugar_g": None,
    "sodium_mg": None,
    "pantry_count": 0,
    "pantry_locations": None,
    "is_saved_recipe": 0,
    "favorite_count": 0,
    "log_count": 1,
    "last_logged_date": "2026-08-26",
    "is_entered_food": False,
}


class SavedFoodMenuTests(unittest.TestCase):
    def test_food_menu_opens_food_library(self) -> None:
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
                    "text": "Food Library",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_foods",
        )
        self.assertIn("Food Library Menu", send.call_args.args[0])

    def test_menu_exposes_edit_and_delete(self) -> None:
        message = app.healthcoach_saved_foods_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("Food Library Menu", message)
        self.assertIn("1. Find a food", message)
        self.assertIn("4. Edit entered food", message)
        self.assertIn("5. Remove entered food", message)
        self.assertIn(
            ["Find a food", "Browse entered foods"],
            keyboard["keyboard"],
        )

    def test_find_food_opens_search_prompt(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "saved_foods",
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
                    "text": "Find a food",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "food_finder_query",
        )
        self.assertIn(
            "What food would you like to find?",
            send.call_args.args[0],
        )

    def test_details_offer_edit_and_delete(self) -> None:
        message = app.format_saved_food_details(SAVED_FOOD)
        keyboard = app.menu_reply_markup(message)

        self.assertIn(
            ["Edit Entered Food", "Remove Entered Food"],
            keyboard["keyboard"],
        )

    def test_food_library_search_finds_provider_food(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_finder_query",
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
                "search_food_locations",
                return_value=[FINDER_FOOD],
            ) as search,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "homemade protein bars",
                }
            })

        search.assert_called_once_with("homemade protein bars")
        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "food_finder_results",
        )
        self.assertIn("Protein bar — Homemade", send.call_args.args[0])
        self.assertIn("Logged before", send.call_args.args[0])

    def test_food_finder_details_explain_every_location(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_finder_results",
            "known_data": {
                "food_finder_query": "protein bar",
                "food_finder_ids": [79],
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
                "get_food_location",
                return_value=FINDER_FOOD,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "1"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "food_finder_details",
        )
        message = send.call_args.args[0]
        self.assertIn("Food Finder — Details", message)
        self.assertIn("Entered Foods list: No", message)
        self.assertIn("My Pantry: No", message)
        self.assertIn("Saved Recipes: No", message)
        self.assertIn("Food history: 1 entry", message)

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
        self.assertIn("Entered Food Edit Menu", send.call_args.args[0])

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
