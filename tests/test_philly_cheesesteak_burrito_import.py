from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from food import database, library, pantry, recipes
from food.resolver import is_trusted_saved_food
from scripts import import_philly_cheesesteak_burrito as importer


class PhillyCheesesteakBurritoImportTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = Path(self.temporary_directory.name) / "food.db"
        original_initialize = database.initialize_database

        def initialize_test_database(database_path=None):
            return original_initialize(self.database_path)

        self.patchers = [
            patch.object(module, "DATABASE_PATH", self.database_path)
            for module in (database, library, pantry, recipes)
        ] + [
            patch.object(module, "initialize_database", initialize_test_database)
            for module in (database, library, pantry, recipes)
        ]
        for patcher in self.patchers:
            patcher.start()
        database.initialize_database()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_recipe_is_created_once_from_ten_linked_foods(self) -> None:
        first = importer.create_recipe()
        second = importer.create_recipe()

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        recipe = first["recipe"]
        self.assertEqual(recipe["canonical_name"], "Philly Cheesesteak Burrito")
        self.assertEqual(recipe["yield_servings"], 10)
        self.assertEqual(
            len(recipes.list_saved_recipe_ingredients(int(recipe["saved_recipe_id"]))),
            10,
        )
        for linked in recipes.list_saved_recipe_ingredients(
            int(recipe["saved_recipe_id"])
        ):
            self.assertTrue(is_trusted_saved_food(library.get_food(linked["food_id"])))
        self.assertAlmostEqual(recipe["calories"], 456.6, places=1)
        self.assertAlmostEqual(recipe["protein_g"], 42.2, places=1)
        self.assertAlmostEqual(recipe["carbohydrates_g"], 29.9, places=1)
        self.assertAlmostEqual(recipe["fat_g"], 17.4, places=1)

    def test_import_does_not_log_food(self) -> None:
        importer.create_recipe()
        with database.get_connection(self.database_path) as connection:
            count = connection.execute("SELECT COUNT(*) FROM food_entries").fetchone()[0]
        self.assertEqual(count, 0)


if __name__ == "__main__":
    unittest.main()
