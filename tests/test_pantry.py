from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from food import database, library, pantry


class PantryTests(unittest.TestCase):
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
        saved = library.add_food_with_nutrition(
            canonical_name="Test Black Beans",
            serving_description="1/2 cup",
            serving_amount=0.5,
            serving_unit="cup",
            verification_status="verified",
            verification_source="user_package_label",
            calories=110,
            protein_g=7,
            carbohydrates_g=21,
            fat_g=0,
            fiber_g=10,
            sugar_g=1,
            sodium_mg=85,
            brand="Test Brand",
        )
        self.food_id = int(saved["food"]["food_id"])

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_schema_contains_pantry_items(self) -> None:
        result = database.initialize_database()

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("pantry_items", result["tables"])

    def test_version_five_database_migrates_to_pantry_schema(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute("DROP TABLE pantry_items")
            connection.execute(
                "DELETE FROM schema_version WHERE version >= 6"
            )
            connection.commit()

        result = database.initialize_database()

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("pantry_items", result["tables"])

    def test_parses_natural_item_list_and_removes_duplicates(self) -> None:
        items = pantry.parse_pantry_item_list(
            "chicken breast, romaine; tomatoes\nChicken Breast"
        )

        self.assertEqual(
            items,
            ["chicken breast", "romaine", "tomatoes"],
        )

    def test_manual_items_are_presence_only_and_not_duplicated(self) -> None:
        first = pantry.add_pantry_items(
            ["Chicken breast", "Romaine"]
        )
        second = pantry.add_pantry_items(
            ["chicken breast", "Tomatoes"]
        )

        self.assertEqual(len(first["created"]), 2)
        self.assertEqual(len(second["created"]), 1)
        self.assertEqual(len(second["existing"]), 1)
        self.assertEqual(
            [item["display_name"] for item in pantry.list_pantry_items()],
            ["Chicken breast", "Romaine", "Tomatoes"],
        )

    def test_connector_and_punctuation_variants_are_not_duplicated(self) -> None:
        pantry.add_pantry_item(
            display_name="Cilantro Lime Rice",
            source="manual",
        )

        result = pantry.add_pantry_items(
            ["Cilantro & Lime Rice"],
            source="shelf_photo",
        )

        self.assertEqual(result["created"], [])
        self.assertEqual(len(result["existing"]), 1)
        self.assertEqual(
            [item["display_name"] for item in pantry.list_pantry_items()],
            ["Cilantro Lime Rice"],
        )

    def test_bulk_items_can_record_shelf_photo_source(self) -> None:
        result = pantry.add_pantry_items(
            ["Black beans", "Rice"],
            source="shelf_photo",
        )

        self.assertEqual(len(result["created"]), 2)
        self.assertEqual(
            {item["source"] for item in pantry.list_pantry_items()},
            {"shelf_photo"},
        )

    def test_version_eleven_migration_preserves_pantry_items(self) -> None:
        pantry.add_pantry_item(display_name="Rice", source="manual")
        with database.get_connection(self.database_path) as connection:
            connection.execute(
                "DELETE FROM schema_version WHERE version >= 12"
            )
            connection.commit()

        result = database.initialize_database()
        pantry.add_pantry_item(
            display_name="Black beans",
            source="shelf_photo",
        )

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertEqual(
            [item["display_name"] for item in pantry.list_pantry_items()],
            ["Black beans", "Rice"],
        )

    def test_new_items_have_safe_organization_defaults(self) -> None:
        manual = pantry.add_pantry_item(
            display_name="Cucumbers",
            source="manual",
        )
        shelf = pantry.add_pantry_item(
            display_name="Tomato Paste",
            source="shelf_photo",
        )

        self.assertEqual(manual["storage_area"], "unsorted")
        self.assertEqual(manual["food_category"], "unsorted")
        self.assertEqual(shelf["storage_area"], "pantry_shelf")
        self.assertEqual(shelf["food_category"], "unsorted")

    def test_updates_only_pantry_organization(self) -> None:
        added = pantry.add_pantry_item(
            display_name="Chicken breast",
            source="manual",
        )

        updated = pantry.update_pantry_item_organization(
            added["pantry_item_id"],
            storage_area="freezer",
            food_category="protein",
        )

        self.assertEqual(updated["display_name"], "Chicken breast")
        self.assertEqual(updated["source"], "manual")
        self.assertEqual(updated["storage_area"], "freezer")
        self.assertEqual(updated["food_category"], "protein")

    def test_rename_preserves_link_and_organization(self) -> None:
        added = pantry.add_pantry_item(
            display_name="Original chicken",
            source="manual",
            storage_area="freezer",
            food_category="protein",
        )

        renamed = pantry.rename_pantry_item(
            added["pantry_item_id"],
            display_name="Chicken breast",
        )

        self.assertEqual(renamed["display_name"], "Chicken breast")
        self.assertEqual(renamed["normalized_name"], "chicken_breast")
        self.assertEqual(renamed["source"], "manual")
        self.assertEqual(renamed["storage_area"], "freezer")
        self.assertEqual(renamed["food_category"], "protein")

    def test_rename_rejects_an_existing_pantry_name(self) -> None:
        first = pantry.add_pantry_item(
            display_name="Black beans",
            source="manual",
        )
        pantry.add_pantry_item(
            display_name="Rice and beans",
            source="manual",
        )

        with self.assertRaisesRegex(
            ValueError,
            "already exists",
        ):
            pantry.rename_pantry_item(
                first["pantry_item_id"],
                display_name="Rice & beans",
            )

        names = [item["display_name"] for item in pantry.list_pantry_items()]
        self.assertEqual(names, ["Black beans", "Rice and beans"])

    def test_version_twelve_migration_preserves_and_unsorts_items(self) -> None:
        pantry.add_pantry_item(display_name="Rice", source="manual")
        with database.get_connection(self.database_path) as connection:
            connection.execute(
                "DROP INDEX IF EXISTS idx_pantry_items_food_category"
            )
            connection.execute(
                "DROP INDEX IF EXISTS idx_pantry_items_storage_area"
            )
            connection.execute(
                "ALTER TABLE pantry_items DROP COLUMN food_category"
            )
            connection.execute(
                "ALTER TABLE pantry_items DROP COLUMN storage_area"
            )
            connection.execute(
                "DELETE FROM schema_version WHERE version >= 13"
            )
            connection.commit()

        result = database.initialize_database()
        item = pantry.list_pantry_items()[0]

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertEqual(item["display_name"], "Rice")
        self.assertEqual(item["storage_area"], "unsorted")
        self.assertEqual(item["food_category"], "unsorted")

    def test_scanned_item_keeps_food_and_active_nutrition_link(self) -> None:
        added = pantry.add_pantry_item(
            display_name="Test Black Beans",
            food_id=self.food_id,
            source="barcode",
            barcode_text="036000291452",
        )

        self.assertTrue(added["created"])
        item = pantry.list_pantry_items()[0]
        self.assertEqual(item["food_id"], self.food_id)
        self.assertEqual(item["source"], "barcode")
        self.assertEqual(item["calories"], 110)
        self.assertEqual(item["protein_g"], 7)

        pantry.add_pantry_item(
            display_name="test black beans",
            source="manual",
        )
        refreshed = pantry.list_pantry_items()[0]
        self.assertEqual(refreshed["food_id"], self.food_id)
        self.assertEqual(refreshed["source"], "barcode")

    def test_link_nutrition_preserves_pantry_identity_and_organization(
        self,
    ) -> None:
        added = pantry.add_pantry_item(
            display_name="Beans on pantry shelf",
            source="shelf_photo",
            storage_area="pantry_shelf",
            food_category="canned_jarred",
        )

        linked = pantry.link_pantry_item_to_food(
            added["pantry_item_id"],
            food_id=self.food_id,
            source="saved_food",
        )

        self.assertEqual(linked["display_name"], "Beans on pantry shelf")
        self.assertEqual(linked["storage_area"], "pantry_shelf")
        self.assertEqual(linked["food_category"], "canned_jarred")
        self.assertEqual(linked["food_id"], self.food_id)
        self.assertEqual(linked["source"], "saved_food")
        self.assertEqual(linked["calories"], 110)

    def test_link_rejects_food_without_usable_calories(self) -> None:
        incomplete = library.add_food_with_nutrition(
            canonical_name="Incomplete Food",
            serving_description="1 serving",
            serving_amount=1,
            serving_unit="serving",
            verification_status="estimated",
            verification_source="user_entered",
            calories=None,
        )
        added = pantry.add_pantry_item(display_name="Unknown product")

        with self.assertRaisesRegex(ValueError, "usable active nutrition"):
            pantry.link_pantry_item_to_food(
                added["pantry_item_id"],
                food_id=int(incomplete["food"]["food_id"]),
            )

        item = pantry.list_pantry_items()[0]
        self.assertIsNone(item["food_id"])

    def test_remove_and_clear_do_not_change_saved_food(self) -> None:
        first = pantry.add_pantry_item(display_name="Rice")
        pantry.add_pantry_item(display_name="Cucumbers")

        self.assertTrue(
            pantry.remove_pantry_item(first["pantry_item_id"])
        )
        self.assertFalse(
            pantry.remove_pantry_item(first["pantry_item_id"])
        )
        self.assertEqual(pantry.clear_pantry(), 1)
        self.assertEqual(pantry.list_pantry_items(), [])
        self.assertIsNotNone(library.get_food(self.food_id))


if __name__ == "__main__":
    unittest.main()
