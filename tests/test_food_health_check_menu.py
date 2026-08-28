import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


HEALTH_FOOD = {
    "food_id": 79,
    "canonical_name": "Protein bar",
    "brand": "Homemade",
    "restaurant": None,
    "food_type": "food",
    "serving_description": "1 bar (26.5 g)",
    "verification_status": "verified",
    "verification_source": "user_entered",
    "version_number": 3,
    "calories": 109,
    "protein_g": 8.1,
    "carbohydrates_g": 10.2,
    "fat_g": 4,
    "fiber_g": None,
    "sugar_g": None,
    "sodium_mg": None,
    "health_check_reason": ["fiber", "sugar", "sodium"],
    "pantry_count": 0,
    "pantry_locations": None,
    "is_saved_recipe": 0,
    "recipe_ingredient_count": 0,
    "favorite_count": 0,
    "barcode_count": 0,
    "log_count": 8,
    "last_logged_date": "2026-08-26",
    "is_entered_food": True,
}


def health_report(**updates):
    report = {
        "food_count": 12,
        "pantry_count": 8,
        "pantry_nutrition": [{"pantry_item_id": 4}],
        "pantry_organization": [{"pantry_item_id": 5}],
        "nutrition_gaps": [HEALTH_FOOD],
        "source_review": [],
        "source_rechecks": [],
        "preserved_versions": 4,
    }
    report.update(updates)
    return report


class FoodHealthCheckMenuTests(unittest.TestCase):
    def test_food_library_menu_exposes_health_check(self) -> None:
        message = app.healthcoach_saved_foods_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("7. Library health check", message)
        self.assertIn("8. Back", message)
        self.assertIn(
            ["Library health check"],
            keyboard["keyboard"],
        )

    def test_opens_read_only_health_check_summary(self) -> None:
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
            patch.object(
                app,
                "build_food_library_health_check",
                return_value=health_report(),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Library health check",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "food_health_check",
        )
        message = send.call_args.args[0]
        self.assertIn("Food Library Health Check", message)
        self.assertIn("1 need attention", message)
        self.assertIn("Previous Nutrition Versions on current foods: 4", message)
        self.assertIn("never changes or deletes", message)

    def test_nutrition_gap_category_lists_reasons(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_health_check",
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
                "build_food_library_health_check",
                return_value=health_report(),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Review nutrition gaps",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "food_health_check_list",
        )
        self.assertEqual(
            update.call_args.kwargs["known_data"]["food_health_check_ids"],
            [79],
        )
        message = send.call_args.args[0]
        self.assertIn("Food Library Health Check — Nutrition Details", message)
        self.assertIn("Missing: fiber, sugar, sodium", message)

    def test_health_check_food_selection_opens_manageable_details(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_health_check_list",
            "known_data": {
                "food_health_check_category": "nutrition_gaps",
                "food_health_check_page": 0,
                "food_health_check_ids": [79],
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
                "build_food_library_health_check",
                return_value=health_report(),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "1"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "food_health_check_item",
        )
        message = send.call_args.args[0]
        self.assertIn("Why it is listed: Missing: fiber, sugar, sodium", message)
        self.assertIn("Reply Manage Food, Back, or Cancel", message)
        self.assertNotIn("Search again", message)

    def test_manage_food_preserves_health_check_return(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_health_check_item",
            "known_data": {
                "food_health_check_category": "nutrition_gaps",
                "food_health_check_page": 2,
                "food_health_check_food_id": 79,
            },
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "show_food_library_manage") as manage,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Manage Food",
                }
            })

        manage.assert_called_once_with(
            chat_id=123,
            food_id=79,
            return_data={
                "_food_health_check_category": "nutrition_gaps",
                "_food_health_check_page": 2,
            },
        )

    def test_health_check_routes_to_existing_pantry_nutrition_queue(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_health_check",
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
                "build_food_library_health_check",
                return_value=health_report(),
            ),
            patch.object(app, "show_pantry_nutrition_queue") as queue,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Complete Pantry nutrition",
                }
            })

        queue.assert_called_once_with(
            chat_id=123,
            page=0,
            skipped_ids=[],
        )

    def test_manage_back_returns_to_health_check_item(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "food_library_manage",
            "known_data": {
                "food_finder_food_id": 79,
                "_food_health_check_category": "nutrition_gaps",
                "_food_health_check_page": 0,
            },
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "get_food_location", return_value=HEALTH_FOOD),
            patch.object(
                app,
                "build_food_library_health_check",
                return_value=health_report(),
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Back"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "food_health_check_item",
        )
        self.assertIn(
            "Food Library Health Check — Item",
            send.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
