from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from food import database, library, pantry
from scripts import import_chicken_pizza_burrito_staples as importer


class RecipeStaplesImportTests(unittest.TestCase):
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

    def test_import_is_complete_and_idempotent(self) -> None:
        first = importer.import_staples()
        second = importer.import_staples()

        self.assertEqual(first["foods_created"], len(importer.STAPLES))
        self.assertEqual(first["pantry_items_ready"], len(importer.STAPLES))
        self.assertEqual(second["foods_created"], 0)
        self.assertEqual(second["foods_reused"], len(importer.STAPLES))
        self.assertEqual(
            len(pantry.list_pantry_items()),
            len(importer.STAPLES),
        )

    def test_conflicting_existing_food_stops_before_import(self) -> None:
        item = importer.STAPLES[0]
        library.add_food_with_nutrition(
            canonical_name=item["canonical_name"],
            serving_description=item["serving_description"],
            serving_amount=item["serving_amount"],
            serving_unit=item["serving_unit"],
            brand=item["brand"],
            restaurant=None,
            food_type="food",
            verification_status="verified",
            verification_source="user_entered",
            calories=999,
            protein_g=0,
            carbohydrates_g=0,
            fat_g=0,
            fiber_g=0,
            sugar_g=0,
            sodium_mg=0,
        )

        with self.assertRaisesRegex(RuntimeError, "Stopped safely"):
            importer.import_staples()

        self.assertEqual(pantry.list_pantry_items(), [])


if __name__ == "__main__":
    unittest.main()
