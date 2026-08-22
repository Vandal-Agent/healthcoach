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

HEART_HEALTHY_RECIPE_IDEA = {
    **RECIPE_IDEA,
    "heart_healthy_pick": True,
    "heart_healthy_reason": (
        "Uses lean chicken and vegetables with moderate sodium."
    ),
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

        with database.get_connection(self.database_path) as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(saved_recipes)"
                ).fetchall()
            }
        self.assertIn("heart_healthy_pick", columns)
        self.assertIn("heart_healthy_reason", columns)

    def test_version_nine_database_adds_heart_health_fields(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute(
                "ALTER TABLE saved_recipes "
                "DROP COLUMN heart_healthy_reason"
            )
            connection.execute(
                "ALTER TABLE saved_recipes "
                "DROP COLUMN heart_healthy_pick"
            )
            connection.execute(
                "DELETE FROM schema_version WHERE version = 10"
            )
            connection.commit()

        result = database.initialize_database()

        self.assertEqual(result["schema_version"]["version"], 10)
        with database.get_connection(self.database_path) as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(saved_recipes)"
                ).fetchall()
            }
        self.assertIn("heart_healthy_pick", columns)
        self.assertIn("heart_healthy_reason", columns)

    def test_version_six_database_migrates_to_saved_recipes(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute("DROP TABLE saved_recipes")
            connection.execute(
                "DELETE FROM schema_version WHERE version IN (7, 8)"
            )
            connection.commit()

        result = database.initialize_database()

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
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

    def test_preserves_heart_healthy_pick_when_recipe_is_saved(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            HEART_HEALTHY_RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]

        self.assertTrue(saved["heart_healthy_pick"])
        self.assertEqual(
            saved["heart_healthy_reason"],
            HEART_HEALTHY_RECIPE_IDEA["heart_healthy_reason"],
        )
        listed = recipes.list_saved_recipes()[0]
        self.assertTrue(listed["heart_healthy_pick"])

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

    def test_updates_recipe_details_and_name(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]

        updated = recipes.update_saved_recipe(
            int(saved["saved_recipe_id"]),
            name="Chicken Garden Bowl",
            meal_type="lunch",
            summary="An updated quick lunch.",
            ingredients=[
                {
                    "name": "Chicken breast",
                    "amount": "3 ounces",
                    "source": "pantry",
                },
            ],
            preparation_steps=["Cook, slice, and serve."],
        )

        self.assertEqual(updated["canonical_name"], "Chicken Garden Bowl")
        self.assertEqual(updated["meal_type"], "lunch")
        self.assertEqual(updated["ingredients"][0]["amount"], "3 ounces")
        self.assertEqual(updated["version_number"], 1)

    def test_non_material_edit_preserves_heart_healthy_pick(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            HEART_HEALTHY_RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]

        updated = recipes.update_saved_recipe(
            int(saved["saved_recipe_id"]),
            name="Heart-Healthy Chicken Bowl",
            preparation_steps=["Cook carefully and serve."],
        )

        self.assertTrue(updated["heart_healthy_pick"])
        self.assertEqual(
            updated["heart_healthy_reason"],
            HEART_HEALTHY_RECIPE_IDEA["heart_healthy_reason"],
        )

    def test_ingredient_edit_clears_heart_healthy_pick(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            HEART_HEALTHY_RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]

        updated = recipes.update_saved_recipe(
            int(saved["saved_recipe_id"]),
            ingredients=[
                {
                    "name": "Chicken breast",
                    "amount": "3 ounces",
                    "source": "pantry",
                },
            ],
        )

        self.assertFalse(updated["heart_healthy_pick"])
        self.assertEqual(updated["heart_healthy_reason"], "")

    def test_nutrition_edit_versions_future_logs_only(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            HEART_HEALTHY_RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]
        old_entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 15),
            meal_category="dinner",
            food_id=int(saved["food_id"]),
            quantity=1,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )

        updated = recipes.update_saved_recipe_nutrition(
            int(saved["saved_recipe_id"]),
            calories=390,
            protein_g=44,
            carbohydrates_g=30,
            fat_g=11,
            fiber_g=7,
            sugar_g=5,
            sodium_mg=480,
        )
        new_entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 16),
            meal_category="dinner",
            food_id=int(saved["food_id"]),
            quantity=1,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )

        self.assertEqual(updated["version_number"], 2)
        self.assertEqual(updated["verification_status"], "estimated")
        self.assertFalse(updated["heart_healthy_pick"])
        self.assertEqual(updated["heart_healthy_reason"], "")
        self.assertEqual(old_entry["calories"], 450)
        self.assertEqual(new_entry["calories"], 390)

    def test_delete_preserves_food_and_logged_history(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]
        entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 16),
            meal_category="dinner",
            food_id=int(saved["food_id"]),
            quantity=1,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )

        deleted = recipes.delete_saved_recipe(
            int(saved["saved_recipe_id"])
        )

        self.assertEqual(deleted["canonical_name"], "Chicken Pepper Bowl")
        self.assertIsNone(
            recipes.get_saved_recipe(int(saved["saved_recipe_id"]))
        )
        self.assertIsNotNone(library.get_food(int(saved["food_id"])))
        history = ledger.list_food_entries(entry_date=date(2026, 8, 16))
        self.assertEqual(history[0]["food_entry_id"], entry["food_entry_id"])
        self.assertEqual(history[0]["calories"], 450)

    def test_rename_rejects_existing_recipe_identity(self) -> None:
        first = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]
        second_idea = dict(RECIPE_IDEA)
        second_idea["name"] = "Second Recipe"
        recipes.save_pantry_meal_idea(
            second_idea,
            meal_type="lunch",
        )

        with self.assertRaisesRegex(ValueError, "already uses"):
            recipes.update_saved_recipe(
                int(first["saved_recipe_id"]),
                name="Second Recipe",
            )


if __name__ == "__main__":
    unittest.main()
