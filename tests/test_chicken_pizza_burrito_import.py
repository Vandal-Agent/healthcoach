from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from food import database, library, pantry, recipes
from scripts import import_chicken_pizza_burrito_recipe as importer


class ChickenPizzaBurritoImportTests(unittest.TestCase):
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
                patch.object(module, "initialize_database", initialize_test_database)
                for module in (database, library, pantry, recipes)
            ],
        ]
        for patcher in self.patchers:
            patcher.start()
        database.initialize_database()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_recipe_is_created_once_with_complete_details(self) -> None:
        first = importer.create_recipe()
        second = importer.create_recipe()

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        recipe = first["recipe"]
        self.assertEqual(recipe["canonical_name"], "Chicken Pizza Burritos")
        self.assertEqual(recipe["yield_servings"], 10)
        self.assertEqual(len(recipe["ingredients"]), 16)
        self.assertEqual(
            len(
                recipes.list_saved_recipe_ingredients(
                    int(recipe["saved_recipe_id"])
                )
            ),
            11,
        )
        self.assertGreater(recipe["calories"], 600)
        self.assertGreater(recipe["protein_g"], 50)
        self.assertIn("Trace nutrition", recipe["estimate_notes"])

    def test_recipe_import_does_not_log_food(self) -> None:
        importer.create_recipe()

        with database.get_connection(self.database_path) as connection:
            count = connection.execute(
                "SELECT COUNT(*) FROM food_entries"
            ).fetchone()[0]

        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
