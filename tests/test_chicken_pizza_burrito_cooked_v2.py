from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from food import database, library, pantry, recipes
from scripts import import_chicken_pizza_burrito_recipe as original_importer
from scripts import update_chicken_pizza_burrito_cooked_v2 as updater


class ChickenPizzaBurritoCookedV2Tests(unittest.TestCase):
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
            patch.object(pantry, "DATABASE_PATH", self.database_path),
            patch.object(recipes, "DATABASE_PATH", self.database_path),
            *[
                patch.object(
                    module,
                    "initialize_database",
                    initialize_test_database,
                )
                for module in (database, library, pantry, recipes)
            ],
        ]
        for patcher in self.patchers:
            patcher.start()
        database.initialize_database()
        original_importer.create_recipe()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def _recipe_version_count(self, food_id: int) -> int:
        with database.get_connection(self.database_path) as connection:
            return int(
                connection.execute(
                    "SELECT COUNT(*) FROM nutrition_versions "
                    "WHERE food_id = ?",
                    (int(food_id),),
                ).fetchone()[0]
            )

    def test_updates_recipe_from_cooked_weight_and_is_idempotent(self) -> None:
        original = updater._find_recipe()
        original_version_id = int(original["nutrition_version_id"])

        first = updater.update_recipe()
        updated = first["recipe"]

        self.assertTrue(first["updated"])
        self.assertEqual(updated["yield_servings"], 5)
        self.assertAlmostEqual(updated["calories"], 513.425, places=2)
        self.assertAlmostEqual(updated["protein_g"], 40.99, places=2)
        self.assertAlmostEqual(updated["carbohydrates_g"], 29.397, places=2)
        self.assertAlmostEqual(updated["fat_g"], 25.139, places=2)
        self.assertAlmostEqual(updated["fiber_g"], 1.794, places=2)
        self.assertAlmostEqual(updated["sugar_g"], 4.738, places=2)
        self.assertAlmostEqual(updated["sodium_mg"], 1345.309, places=2)
        self.assertNotEqual(
            int(updated["nutrition_version_id"]),
            original_version_id,
        )
        self.assertEqual(self._recipe_version_count(updated["food_id"]), 2)

        links = recipes.list_saved_recipe_ingredients(
            int(updated["saved_recipe_id"])
        )
        self.assertEqual(len(links), 9)
        self.assertEqual(
            links[0]["canonical_name"],
            "Chicken Breast, Meat Only, Cooked, Roasted",
        )
        self.assertEqual(links[0]["amount_description"], "15 oz")
        self.assertIn("No mozzarella is used", updated["estimate_notes"])
        self.assertFalse(
            any(
                "mozzarella" in ingredient["name"].lower()
                for ingredient in updated["ingredients"]
            )
        )

        second = updater.update_recipe()
        self.assertFalse(second["updated"])
        self.assertEqual(self._recipe_version_count(updated["food_id"]), 2)

    def test_update_does_not_log_recipe_as_eaten(self) -> None:
        updater.update_recipe()

        with database.get_connection(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM food_entries"
            ).fetchone()[0]

        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
