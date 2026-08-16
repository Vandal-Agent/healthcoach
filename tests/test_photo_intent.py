import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


FOOD_ESTIMATE = {
    "readable": True,
    "dish_name": "Test Meal",
    "visible_components": ["chicken"],
    "calories_low": 400,
    "calories_high": 500,
    "protein_low": 30,
    "protein_high": 40,
    "carbohydrates_low": 35,
    "carbohydrates_high": 45,
    "fat_low": 12,
    "fat_high": 18,
    "portion_assumptions": ["one plate"],
    "uncertainty_notes": ["portion size"],
}


class PhotoIntentTests(unittest.TestCase):
    def test_chooser_has_all_photo_destinations(self) -> None:
        message = app.healthcoach_photo_intent_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("Estimate or log this meal", message)
        self.assertIn("Read a restaurant menu", message)
        self.assertIn("Scan a product barcode", message)
        self.assertIn("My Pantry", message)
        self.assertIn(
            ["Add scanned product to Pantry"],
            keyboard["keyboard"],
        )

    def test_unprompted_photo_opens_chooser_without_analysis(self) -> None:
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=None,
            ),
            patch.object(app, "start_conversation") as start,
            patch.object(app, "download_telegram_photo") as download,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "photo": [{"file_id": "photo-1"}],
                }
            })

        download.assert_not_called()
        self.assertEqual(
            start.call_args.kwargs["current_step"],
            "photo_intent",
        )
        self.assertEqual(
            start.call_args.kwargs["known_data"]["photo_file_id"],
            "photo-1",
        )
        self.assertIn(
            "What should I do with this photo?",
            send.call_args.args[0],
        )

    def test_estimate_caption_bypasses_chooser(self) -> None:
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=None,
            ),
            patch.object(app, "start_conversation") as start,
            patch.object(
                app,
                "download_telegram_photo",
                return_value=(b"photo", "image/jpeg"),
            ),
            patch.object(
                app,
                "analyze_food_photo",
                return_value=FOOD_ESTIMATE,
            ) as analyze,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "photo": [{"file_id": "photo-1"}],
                    "caption": "Estimate this meal",
                }
            })

        analyze.assert_called_once()
        self.assertEqual(start.call_count, 2)
        self.assertEqual(
            start.call_args_list[0].kwargs["current_step"],
            "await_food_photo",
        )
        self.assertIn(
            "Estimated Meal Nutrition",
            send.call_args.args[0],
        )

    def test_chooser_selection_reuses_photo_for_menu(self) -> None:
        chooser = {
            "conversation_type": "healthcoach_menu",
            "current_step": "photo_intent",
            "known_data": {
                "photo_file_id": "photo-2",
                "photo_caption": "",
            },
        }
        routed = {
            "conversation_type": "healthcoach_menu",
            "current_step": "await_menu_photo",
            "known_data": {},
        }
        menu_result = {
            "readable": False,
            "restaurant_name": None,
            "candidates": [],
            "notes": [],
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                side_effect=[chooser, routed],
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(
                app,
                "download_telegram_photo",
                return_value=(b"photo", "image/jpeg"),
            ),
            patch.object(
                app,
                "analyze_menu_photo",
                return_value=menu_result,
            ) as analyze,
            patch.object(app, "send_telegram_msg"),
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Read restaurant menu",
                }
            })

        self.assertEqual(
            update.call_args_list[0].kwargs["current_step"],
            "await_menu_photo",
        )
        analyze.assert_called_once_with(
            b"photo",
            mime_type="image/jpeg",
            user_context="",
        )

    def test_chooser_can_route_barcode_to_pantry(self) -> None:
        chooser = {
            "conversation_type": "healthcoach_menu",
            "current_step": "photo_intent",
            "known_data": {
                "photo_file_id": "photo-3",
                "photo_caption": "",
            },
        }
        routed = {
            "conversation_type": "healthcoach_menu",
            "current_step": "await_barcode_photo",
            "known_data": {"pantry_scan_mode": True},
        }
        barcode_result = {
            "found": True,
            "saved_food_id": 42,
            "food": {
                "canonical_name": "Test Product",
                "serving_description": "1 serving",
            },
            "nutrition": {"calories": 100},
            "verification": {"source": "test"},
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                side_effect=[chooser, routed],
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(
                app,
                "download_telegram_photo",
                return_value=(b"photo", "image/jpeg"),
            ),
            patch.object(
                app,
                "read_barcode_photo",
                return_value={
                    "readable": True,
                    "barcode": "036000291452",
                },
            ),
            patch.object(
                app,
                "lookup_barcode_nutrition",
                return_value=barcode_result,
            ),
            patch.object(app, "send_telegram_msg"),
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Add scanned product to Pantry",
                }
            })

        self.assertEqual(
            update.call_args_list[0].kwargs["current_step"],
            "await_barcode_photo",
        )
        self.assertTrue(
            update.call_args_list[0]
            .kwargs["known_data"]["pantry_scan_mode"]
        )


if __name__ == "__main__":
    unittest.main()
