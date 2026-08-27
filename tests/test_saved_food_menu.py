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
    "barcode_count": 0,
    "log_count": 1,
    "last_logged_date": "2026-08-26",
    "is_entered_food": False,
}

PANTRY_ITEM = {
    "pantry_item_id": 9,
    "display_name": "Protein bar",
    "food_id": 79,
    "storage_area": "unsorted",
    "food_category": "unsorted",
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
        self.assertIn("Barcode mapping: No", message)
        self.assertIn("Food history: 1 entry", message)

    def test_food_finder_details_offer_management(self) -> None:
        message = app.format_food_finder_details(FINDER_FOOD)
        keyboard = app.menu_reply_markup(message)

        self.assertIn(
            ["Manage Food", "Search again"],
            keyboard["keyboard"],
        )

    def test_provider_food_management_is_source_aware(self) -> None:
        with patch.object(app, "finder_pantry_items", return_value=[]):
            message = app.format_food_library_manage(FINDER_FOOD)

        self.assertIn("Add a personal search name", message)
        self.assertIn("Add to Pantry", message)
        self.assertIn("Change nutrition", message)
        self.assertIn("Review removal options", message)

    def test_manage_from_food_finder_details(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_finder_details",
            "known_data": {"food_finder_food_id": 79},
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
            patch.object(app, "list_pantry_items", return_value=[]),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Manage Food"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "food_library_manage",
        )
        self.assertIn("Food Library — Manage Food", send.call_args.args[0])

    def test_confirmed_personal_search_name_preserves_source_name(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_library_name_confirmation",
            "known_data": {
                "food_finder_food_id": 79,
                "food_library_name_is_alias": True,
                "food_library_new_name": "Tracy's protein bars",
            },
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "save_food_alias") as alias,
            patch.object(
                app,
                "get_food_location",
                return_value=FINDER_FOOD,
            ),
            patch.object(app, "list_pantry_items", return_value=[]),
            patch.object(app, "update_conversation"),
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        alias.assert_called_once_with(
            food_id=79,
            alias_text="Tracy's protein bars",
        )
        self.assertTrue(
            any(
                "verified source name was preserved" in call.args[0]
                for call in send.call_args_list
            )
        )

    def test_confirmed_add_to_pantry_links_existing_nutrition(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_library_add_pantry_confirmation",
            "known_data": {"food_finder_food_id": 79},
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
            patch.object(
                app,
                "list_pantry_items",
                side_effect=[[], [PANTRY_ITEM]],
            ),
            patch.object(
                app,
                "add_pantry_item",
                return_value=PANTRY_ITEM,
            ) as add,
            patch.object(app, "update_conversation"),
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        add.assert_called_once_with(
            display_name="Protein bar",
            food_id=79,
            source="saved_food",
        )
        self.assertIn("Food Library — Pantry Item", send.call_args.args[0])

    def test_food_library_nutrition_correction_uses_versioned_editor(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_library_manage",
            "known_data": {"food_finder_food_id": 79},
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
                "message": {
                    "chat": {"id": 123},
                    "text": "Change nutrition",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "saved_food_edit_calories",
        )
        self.assertTrue(
            update.call_args.kwargs["known_data"]["_food_finder_return"]
        )
        self.assertIn("past logs remain preserved", send.call_args.args[0])

    def test_provider_record_cannot_delete_history(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_library_manage",
            "known_data": {"food_finder_food_id": 79},
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
            patch.object(app, "list_pantry_items", return_value=[]),
            patch.object(app, "archive_user_saved_food") as archive,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Delete"}
            })

        archive.assert_not_called()
        self.assertIn("nothing personal to delete", send.call_args.args[0])

    def test_confirmed_pantry_organization_is_scoped_to_pantry(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_library_pantry_organization_confirmation",
            "known_data": {
                "food_finder_food_id": 79,
                "food_library_pantry_item_id": 9,
                "food_library_pantry_storage": "pantry_shelf",
                "food_library_pantry_category": "snacks",
            },
        }
        organized = dict(PANTRY_ITEM)
        organized.update({
            "storage_area": "pantry_shelf",
            "food_category": "snacks",
        })
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "update_pantry_item_organization",
                return_value=organized,
            ) as update_item,
            patch.object(
                app,
                "list_pantry_items",
                return_value=[organized],
            ),
            patch.object(app, "update_conversation"),
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Yes"}
            })

        update_item.assert_called_once_with(
            9,
            storage_area="pantry_shelf",
            food_category="snacks",
        )
        self.assertIn("Food Library — Pantry Item", send.call_args.args[0])

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
