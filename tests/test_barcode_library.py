from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from food import database, library
from food.barcode_provider import lookup_local_barcode_nutrition


class BarcodeLibraryTests(unittest.TestCase):
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
        ]
        for patcher in self.patchers:
            patcher.start()

        database.initialize_database()
        saved = library.add_food_with_nutrition(
            canonical_name="Test Cereal",
            serving_description="1 cup (40 g)",
            serving_amount=40,
            serving_unit="g",
            verification_status="verified",
            verification_source="user_package_label",
            calories=150,
            protein_g=4,
            carbohydrates_g=30,
            fat_g=2,
            fiber_g=3,
            sugar_g=8,
            sodium_mg=180,
            brand="Test Brand",
        )
        self.food_id = int(saved["food"]["food_id"])

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_schema_contains_barcode_mappings(self) -> None:
        result = database.initialize_database()
        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("barcode_mappings", result["tables"])

    def test_version_four_database_migrates_to_barcode_schema(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute("DROP TABLE barcode_mappings")
            connection.execute(
                "DELETE FROM schema_version WHERE version = 5"
            )
            connection.commit()

        result = database.initialize_database()

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("barcode_mappings", result["tables"])

    def test_equivalent_gtin_resolves_saved_food(self) -> None:
        mapping = library.save_barcode_mapping(
            barcode="036000291452",
            food_id=self.food_id,
        )

        self.assertEqual(mapping["food_id"], self.food_id)

        saved = library.get_barcode_food("0036000291452")
        self.assertIsNotNone(saved)
        self.assertEqual(saved["canonical_name"], "Test Cereal")
        self.assertEqual(saved["calories"], 150)

    def test_local_lookup_returns_active_nutrition(self) -> None:
        library.save_barcode_mapping(
            barcode="036000291452",
            food_id=self.food_id,
        )

        result = lookup_local_barcode_nutrition(
            "036000291452"
        )

        self.assertTrue(result["found"])
        self.assertEqual(
            result["provider"],
            "healthcoach_local_barcode",
        )
        self.assertEqual(result["saved_food_id"], self.food_id)
        self.assertEqual(result["nutrition"]["protein_g"], 4)

    def test_mapping_can_be_reassigned_after_confirmation(self) -> None:
        second = library.add_food_with_nutrition(
            canonical_name="Corrected Cereal",
            serving_description="1 cup (40 g)",
            serving_amount=40,
            serving_unit="g",
            verification_status="verified",
            verification_source="user_package_label",
            calories=160,
            protein_g=5,
        )
        second_food_id = int(second["food"]["food_id"])

        library.save_barcode_mapping(
            barcode="036000291452",
            food_id=self.food_id,
        )
        library.save_barcode_mapping(
            barcode="0036000291452",
            food_id=second_food_id,
        )

        saved = library.get_barcode_food("036000291452")
        self.assertEqual(saved["food_id"], second_food_id)
        self.assertEqual(saved["canonical_name"], "Corrected Cereal")


if __name__ == "__main__":
    unittest.main()
