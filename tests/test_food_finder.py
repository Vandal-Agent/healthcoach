from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from food import database, finder, ledger, library, pantry


class FoodFinderTests(unittest.TestCase):
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
            patch.object(finder, "DATABASE_PATH", self.database_path),
            patch.object(library, "DATABASE_PATH", self.database_path),
            patch.object(ledger, "DATABASE_PATH", self.database_path),
            patch.object(pantry, "DATABASE_PATH", self.database_path),
            patch.object(
                database,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                finder,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                library,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                ledger,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                pantry,
                "initialize_database",
                initialize_test_database,
            ),
        ]
        for patcher in self.patchers:
            patcher.start()

        database.initialize_database()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def create_food(
        self,
        *,
        name: str,
        brand: str | None,
        source: str,
    ) -> int:
        created = library.add_food_with_nutrition(
            canonical_name=name,
            brand=brand,
            serving_description="1 bar (53 g)",
            serving_amount=1,
            serving_unit="bar",
            verification_status="verified",
            verification_source=source,
            calories=218,
            protein_g=16.1,
            carbohydrates_g=20.4,
            fat_g=8.1,
        )
        return int(created["food"]["food_id"])

    def test_finds_usda_food_by_name_and_brand(self) -> None:
        food_id = self.create_food(
            name="Protein bar",
            brand="Homemade",
            source="usda.gov",
        )
        self.create_food(
            name="Peanut Butter Chip Bar",
            brand="IQBAR",
            source="fdc.nal.usda.gov",
        )

        results = finder.search_food_locations("homemade protein bars")

        self.assertEqual([result["food_id"] for result in results], [food_id])
        self.assertFalse(results[0]["is_entered_food"])

    def test_reports_pantry_favorite_recipe_and_history_locations(self) -> None:
        food_id = self.create_food(
            name="Homemade Protein Bars",
            brand="Homemade",
            source="user_entered",
        )
        pantry.add_pantry_item(
            display_name="Protein Bars",
            food_id=food_id,
            source="saved_food",
        )
        entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 26),
            meal_category="breakfast",
            food_id=food_id,
            quantity=1,
            logging_source="manual",
            user_confirmed=True,
        )
        ledger.save_food_favorite_from_entry(
            int(entry["food_entry_id"]),
        )
        with database.get_connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO saved_recipes (
                    food_id,
                    meal_type,
                    summary,
                    ingredients_json,
                    preparation_steps_json,
                    estimate_notes,
                    created_at,
                    updated_at
                )
                VALUES (?, 'lunch', '', '[]', '[]', '', ?, ?)
                """,
                (
                    food_id,
                    database.current_timestamp(),
                    database.current_timestamp(),
                ),
            )
            connection.commit()

        result = finder.get_food_location(food_id)

        self.assertIsNotNone(result)
        self.assertEqual(result["pantry_count"], 1)
        self.assertIn("Protein Bars", result["pantry_locations"])
        self.assertEqual(result["is_saved_recipe"], 1)
        self.assertEqual(result["favorite_count"], 1)
        self.assertEqual(result["log_count"], 1)
        self.assertEqual(result["last_logged_date"], "2026-08-26")
        self.assertTrue(result["is_entered_food"])

    def test_rejects_blank_or_one_character_search(self) -> None:
        with self.assertRaises(ValueError):
            finder.search_food_locations("p")


if __name__ == "__main__":
    unittest.main()
