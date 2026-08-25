from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

from food import database, library, resolver
from food.interpreter import FoodInterpretation


app.CHAT_ID = None


class SavedFoodCloseMatchingTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "healthcoach_food.db"
        )

        original_initialize = database.initialize_database

        def initialize_test_database(database_path=None):
            return original_initialize(self.database_path)

        self.patchers = [
            patch.object(database, "DATABASE_PATH", self.database_path),
            patch.object(library, "DATABASE_PATH", self.database_path),
            patch.object(resolver, "DATABASE_PATH", self.database_path),
            patch.object(
                database,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                library,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                resolver,
                "initialize_database",
                initialize_test_database,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

        database.initialize_database()
        self._add_food("Tracy's Home Salad", calories=420)
        self._add_food("Chippers", calories=140)

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def _add_food(self, name: str, *, calories: float) -> None:
        library.add_food_with_nutrition(
            canonical_name=name,
            serving_description="1 serving",
            serving_amount=1,
            serving_unit="serving",
            verification_status="verified",
            verification_source="user_entered",
            calories=calories,
            protein_g=5,
            carbohydrates_g=20,
            fat_g=6,
            fiber_g=2,
            sugar_g=5,
            sodium_mg=100,
        )

    def test_possessive_close_name_finds_unique_saved_food(self) -> None:
        result = resolver.resolve_food(
            food_name="tracy salad",
            serving_description="standard",
        )

        self.assertTrue(result["found"])
        self.assertEqual(result["matched_by"], "controlled_fallback")
        self.assertEqual(
            result["food"]["canonical_name"],
            "Tracy's Home Salad",
        )

    def test_simple_plural_close_name_finds_unique_saved_food(self) -> None:
        result = resolver.resolve_food(
            food_name="chipper",
            serving_description="standard",
        )

        self.assertTrue(result["found"])
        self.assertEqual(result["matched_by"], "controlled_fallback")
        self.assertEqual(result["food"]["canonical_name"], "Chippers")

    def test_generic_single_word_does_not_select_personal_food(self) -> None:
        result = resolver.resolve_food(
            food_name="salad",
            serving_description="standard",
        )

        self.assertFalse(result["found"])

    def test_close_match_is_labeled_on_nutrition_review(self) -> None:
        message = app.format_pending_nutrition_confirmation(
            [
                {
                    "role": "Food",
                    "canonical_name": "Chippers",
                    "quantity": 1,
                    "calories": 140,
                    "protein_g": 1,
                    "verification_source": "user_entered",
                    "matched_by": "controlled_fallback",
                    "requested_food_name": "chipper",
                }
            ],
            meal_category="dessert",
        )

        self.assertIn("Close Saved Food match", message)
        self.assertIn("chipper → Chippers", message)
        self.assertIn("Review this match", message)
        self.assertIn("Nothing has been logged yet", message)

    def test_waiting_for_food_checks_saved_names_when_model_rejects(self) -> None:
        conversation = {
            "conversation_type": "food_meal",
            "current_step": "awaiting_food",
            "known_data": {
                "meal_category": "dessert",
                "restaurant": None,
                "_entry_date": "2026-08-24",
            },
        }
        rejected = FoodInterpretation(
            is_food_logging_request=False,
            missing_fields=[],
            assumptions=[],
            confidence=0.8,
        )
        close_match = {
            "found": True,
            "matched_by": "controlled_fallback",
            "food": {"canonical_name": "Chippers"},
            "nutrition": {"calories": 140},
        }

        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "interpret_food_message",
                return_value=rejected,
            ),
            patch.object(
                app,
                "resolve_food",
                return_value=close_match,
            ),
            patch.object(app, "start_conversation") as start,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "message_id": 60,
                    "text": "chipper",
                }
            })

        self.assertEqual(
            start.call_args.kwargs["current_step"],
            "clarification",
        )
        known_data = start.call_args.kwargs["known_data"]
        self.assertEqual(known_data["food_name"], "chipper")
        self.assertEqual(known_data["meal_category"], "dessert")
        self.assertEqual(
            start.call_args.kwargs["missing_fields"],
            ["quantity"],
        )
        self.assertIn("How many did you have", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
