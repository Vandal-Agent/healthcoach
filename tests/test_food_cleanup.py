from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from food import cleanup, database, finder, ledger, library, pantry, resolver


class FoodCleanupTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "healthcoach_food.db"
        )
        original_initialize = database.initialize_database

        def initialize_test_database(database_path=None):
            return original_initialize(self.database_path)

        modules = (
            database,
            cleanup,
            finder,
            ledger,
            library,
            pantry,
            resolver,
        )
        self.patchers = [
            patch.object(database, "DATABASE_PATH", self.database_path),
            patch.object(cleanup, "DATABASE_PATH", self.database_path),
            patch.object(finder, "DATABASE_PATH", self.database_path),
            patch.object(ledger, "DATABASE_PATH", self.database_path),
            patch.object(library, "DATABASE_PATH", self.database_path),
            patch.object(pantry, "DATABASE_PATH", self.database_path),
            patch.object(resolver, "DATABASE_PATH", self.database_path),
        ]
        self.patchers.extend(
            patch.object(
                module,
                "initialize_database",
                initialize_test_database,
            )
            for module in modules
        )
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
        serving: str,
        serving_unit: str,
        calories: float,
        source: str = "user_entered",
    ) -> dict:
        return library.add_food_with_nutrition(
            canonical_name=name,
            brand=brand,
            serving_description=serving,
            serving_amount=1,
            serving_unit=serving_unit,
            verification_status="verified",
            verification_source=source,
            calories=calories,
            protein_g=8,
            carbohydrates_g=10,
            fat_g=4,
        )

    def create_duplicate_pair(self) -> tuple[dict, dict]:
        first = self.create_food(
            name="Protein bar",
            brand="Homemade",
            serving="1 bar (26.5 g)",
            serving_unit="bar",
            calories=109,
        )
        second = self.create_food(
            name="Homemade Protein Bars",
            brand=None,
            serving="1 small bar",
            serving_unit="small bar",
            calories=109,
            source="usda.gov",
        )
        return first, second

    def test_schema_migrates_food_cleanup_tables(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute(
                "DELETE FROM schema_version WHERE version >= 14"
            )
            connection.execute("DROP TABLE food_duplicate_reviews")
            connection.execute("DROP TABLE food_consolidations")
            connection.commit()

        result = database.initialize_database()

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("food_consolidations", result["tables"])
        self.assertIn("food_duplicate_reviews", result["tables"])

    def test_lists_conservative_possible_duplicates(self) -> None:
        first, second = self.create_duplicate_pair()
        self.create_food(
            name="Peanut butter",
            brand="Kirkland",
            serving="2 tbsp",
            serving_unit="tbsp",
            calories=190,
        )

        pairs = cleanup.list_possible_food_duplicates()

        self.assertEqual(len(pairs), 1)
        self.assertEqual(
            {
                int(pairs[0]["first"]["food_id"]),
                int(pairs[0]["second"]["food_id"]),
            },
            {
                int(first["food"]["food_id"]),
                int(second["food"]["food_id"]),
            },
        )

    def test_keep_separate_removes_pair_from_future_reviews(self) -> None:
        first, second = self.create_duplicate_pair()

        cleanup.mark_foods_keep_separate(
            int(first["food"]["food_id"]),
            int(second["food"]["food_id"]),
        )

        self.assertEqual(cleanup.list_possible_food_duplicates(), [])

    def test_consolidation_redirects_future_links_and_preserves_history(self):
        primary, duplicate = self.create_duplicate_pair()
        primary_id = int(primary["food"]["food_id"])
        duplicate_id = int(duplicate["food"]["food_id"])
        duplicate_version_id = int(
            duplicate["nutrition"]["nutrition_version_id"]
        )
        pantry.add_pantry_item(
            display_name="Homemade protein bars",
            food_id=duplicate_id,
            source="saved_food",
        )
        library.save_barcode_mapping(
            barcode="034000052004",
            food_id=duplicate_id,
        )
        entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 27),
            meal_category="breakfast",
            food_id=duplicate_id,
            quantity=1,
            logging_source="manual",
            user_confirmed=True,
        )
        ledger.save_food_favorite_from_entry(int(entry["food_entry_id"]))
        database.save_portion_profile(
            phrase="one homemade protein bar",
            food_id=duplicate_id,
            estimated_amount=1,
            estimated_unit="serving",
        )

        recipe_food = self.create_food(
            name="Breakfast prep",
            brand=None,
            serving="1 serving",
            serving_unit="serving",
            calories=200,
            source="user_entered",
        )
        recipe_food_id = int(recipe_food["food"]["food_id"])
        timestamp = database.current_timestamp()
        with database.get_connection(self.database_path) as connection:
            recipe_cursor = connection.execute(
                """
                INSERT INTO saved_recipes (
                    food_id, meal_type, summary, ingredients_json,
                    preparation_steps_json, estimate_notes,
                    yield_servings, created_at, updated_at
                )
                VALUES (?, 'lunch', '', '[]', '[]', '', 1, ?, ?)
                """,
                (recipe_food_id, timestamp, timestamp),
            )
            connection.execute(
                """
                INSERT INTO saved_recipe_ingredients (
                    saved_recipe_id, position, food_id,
                    nutrition_version_id, amount_description,
                    serving_multiplier, created_at, updated_at
                )
                VALUES (?, 1, ?, ?, '1 bar', 1, ?, ?)
                """,
                (
                    recipe_cursor.lastrowid,
                    duplicate_id,
                    duplicate_version_id,
                    timestamp,
                    timestamp,
                ),
            )
            connection.commit()

        result = cleanup.consolidate_food_records(
            primary_food_id=primary_id,
            duplicate_food_id=duplicate_id,
        )

        self.assertTrue(result["historical_records_preserved"])
        with database.get_connection(self.database_path) as connection:
            pantry_food_id = connection.execute(
                "SELECT food_id FROM pantry_items"
            ).fetchone()["food_id"]
            barcode_food_id = connection.execute(
                "SELECT food_id FROM barcode_mappings"
            ).fetchone()["food_id"]
            favorite_food_id = connection.execute(
                "SELECT food_id FROM food_favorites"
            ).fetchone()["food_id"]
            profile_food_id = connection.execute(
                "SELECT food_id FROM portion_profiles"
            ).fetchone()["food_id"]
            historical_food_id = connection.execute(
                "SELECT food_id FROM food_entries"
            ).fetchone()["food_id"]
            ingredient = connection.execute(
                """
                SELECT food_id, nutrition_version_id
                FROM saved_recipe_ingredients
                """
            ).fetchone()
            duplicate_food = connection.execute(
                "SELECT * FROM foods WHERE food_id = ?",
                (duplicate_id,),
            ).fetchone()
            duplicate_nutrition = connection.execute(
                """
                SELECT * FROM nutrition_versions
                WHERE nutrition_version_id = ?
                """,
                (duplicate_version_id,),
            ).fetchone()

        self.assertEqual(pantry_food_id, primary_id)
        self.assertEqual(barcode_food_id, primary_id)
        self.assertEqual(favorite_food_id, primary_id)
        self.assertEqual(profile_food_id, primary_id)
        self.assertEqual(historical_food_id, duplicate_id)
        self.assertEqual(ingredient["food_id"], duplicate_id)
        self.assertEqual(
            ingredient["nutrition_version_id"],
            duplicate_version_id,
        )
        self.assertEqual(
            duplicate_food["verification_source"],
            "consolidated_food_record",
        )
        self.assertIsNotNone(duplicate_nutrition)

        resolved = resolver.resolve_food(
            food_name="Homemade Protein Bars",
            serving_description="1 small bar",
        )
        self.assertTrue(resolved["found"])
        self.assertEqual(resolved["food"]["food_id"], primary_id)
        self.assertIsNone(finder.get_food_location(duplicate_id))
        primary_location = finder.get_food_location(primary_id)
        self.assertEqual(primary_location["log_count"], 1)
        self.assertEqual(primary_location["last_logged_date"], "2026-08-27")
        self.assertEqual(primary_location["recipe_ingredient_count"], 1)
        self.assertEqual(cleanup.list_possible_food_duplicates(), [])

        with database.get_connection(self.database_path) as connection:
            connection.execute("DELETE FROM food_favorites")
            connection.commit()
        favorite = ledger.save_food_favorite_from_entry(
            int(entry["food_entry_id"])
        )
        self.assertEqual(favorite["food_id"], primary_id)

    def test_saved_recipe_food_cannot_be_consolidated(self) -> None:
        primary, duplicate = self.create_duplicate_pair()
        primary_id = int(primary["food"]["food_id"])
        duplicate_id = int(duplicate["food"]["food_id"])
        timestamp = database.current_timestamp()
        with database.get_connection(self.database_path) as connection:
            connection.execute(
                """
                INSERT INTO saved_recipes (
                    food_id, meal_type, summary, ingredients_json,
                    preparation_steps_json, estimate_notes,
                    yield_servings, created_at, updated_at
                )
                VALUES (?, 'lunch', '', '[]', '[]', '', 1, ?, ?)
                """,
                (duplicate_id, timestamp, timestamp),
            )
            connection.commit()

        with self.assertRaisesRegex(ValueError, "Saved Recipe"):
            cleanup.consolidate_food_records(
                primary_food_id=primary_id,
                duplicate_food_id=duplicate_id,
            )


if __name__ == "__main__":
    unittest.main()
