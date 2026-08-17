from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from food import database, ledger, library, recipes


RECIPE_IDEA = {
    "name": "Chicken Pepper Bowl",
    "summary": "A quick chicken and vegetable bowl.",
    "ingredients": [
        {
            "name": "Chicken breast",
            "amount": "4 ounces",
            "source": "pantry",
        },
        {
            "name": "green peppers",
            "amount": "1/2 cup",
            "source": "pantry",
        },
    ],
    "preparation_steps": [
        "Cook the chicken.",
        "Add the peppers and serve.",
    ],
    "calories": 450,
    "protein_g": 42,
    "carbohydrates_g": 40,
    "fat_g": 13,
    "fiber_g": 8,
    "sugar_g": 6,
    "sodium_mg": 520,
    "estimate_notes": "Nutrition is estimated.",
}


class SavedRecipeTests(unittest.TestCase):
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
            patch.object(ledger, "DATABASE_PATH", self.database_path),
            patch.object(recipes, "DATABASE_PATH", self.database_path),
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
                ledger,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                recipes,
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

    def test_schema_contains_saved_recipes(self) -> None:
        result = database.initialize_database()

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("saved_recipes", result["tables"])

    def test_version_six_database_migrates_to_saved_recipes(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute("DROP TABLE saved_recipes")
            connection.execute(
                "DELETE FROM schema_version WHERE version = 7"
            )
            connection.commit()

        result = database.initialize_database()

        self.assertEqual(result["schema_version"]["version"], 7)
        self.assertIn("saved_recipes", result["tables"])

    def test_saves_lists_and_reads_complete_recipe(self) -> None:
        result = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )

        self.assertTrue(result["created"])
        recipe = result["recipe"]
        self.assertEqual(recipe["canonical_name"], "Chicken Pepper Bowl")
        self.assertEqual(recipe["meal_type"], "dinner")
        self.assertEqual(recipe["calories"], 450)
        self.assertEqual(recipe["ingredients"][0]["amount"], "4 ounces")
        self.assertEqual(len(recipe["preparation_steps"]), 2)
        self.assertEqual(len(recipes.list_saved_recipes()), 1)

    def test_duplicate_recipe_is_not_overwritten(self) -> None:
        first = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )
        changed = dict(RECIPE_IDEA)
        changed["calories"] = 999
        second = recipes.save_pantry_meal_idea(
            changed,
            meal_type="dinner",
        )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["recipe"]["calories"], 450)

    def test_saved_recipe_logs_from_existing_food_snapshot(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]

        entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 16),
            meal_category="dinner",
            food_id=int(saved["food_id"]),
            quantity=1.5,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )

        self.assertEqual(entry["calories"], 675)
        self.assertEqual(entry["protein_g"], 63)
        self.assertEqual(entry["logging_source"], "recipe")

    def test_rejects_recipe_without_preparation(self) -> None:
        invalid = dict(RECIPE_IDEA)
        invalid["preparation_steps"] = []

        with self.assertRaises(ValueError):
            recipes.save_pantry_meal_idea(
                invalid,
                meal_type="dinner",
            )


if __name__ == "__main__":
    unittest.main()
