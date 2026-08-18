from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from food import database, pantry, shopping


class ShoppingListTests(unittest.TestCase):
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
            patch.object(pantry, "DATABASE_PATH", self.database_path),
            patch.object(shopping, "DATABASE_PATH", self.database_path),
            patch.object(
                database,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                pantry,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                shopping,
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

    def test_schema_contains_shopping_list(self) -> None:
        result = database.initialize_database()

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("shopping_list_items", result["tables"])

    def test_version_seven_database_migrates_to_shopping_list(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute("DROP TABLE shopping_list_items")
            connection.execute(
                "DELETE FROM schema_version WHERE version = 8"
            )
            connection.commit()

        result = database.initialize_database()

        self.assertEqual(result["schema_version"]["version"], 8)
        self.assertIn("shopping_list_items", result["tables"])

    def test_adds_lists_and_deduplicates_items(self) -> None:
        first = shopping.add_shopping_items(
            ["Low-sodium broth", "Greek yogurt"]
        )
        second = shopping.add_shopping_items(
            ["low-sodium broth"]
        )

        self.assertEqual(len(first["created"]), 2)
        self.assertEqual(len(second["existing"]), 1)
        self.assertEqual(
            [item["display_name"] for item in shopping.list_shopping_items()],
            ["Greek yogurt", "low-sodium broth"],
        )

    def test_swap_item_preserves_source_note(self) -> None:
        result = shopping.add_shopping_item(
            display_name="Unsalted beef broth",
            source="pantry_swap",
            source_note="Swap for regular beef broth",
        )

        self.assertTrue(result["created"])
        self.assertEqual(result["source"], "pantry_swap")
        self.assertEqual(
            result["source_note"],
            "Swap for regular beef broth",
        )

    def test_mark_purchased_moves_item_to_pantry(self) -> None:
        created = shopping.add_shopping_item(
            display_name="No-salt-added beans",
        )

        result = shopping.mark_shopping_item_purchased(
            created["shopping_list_item_id"]
        )

        self.assertEqual(
            result["shopping_item"]["display_name"],
            "No-salt-added beans",
        )
        self.assertEqual(shopping.list_shopping_items(), [])
        self.assertEqual(
            pantry.list_pantry_items()[0]["display_name"],
            "No-salt-added beans",
        )

    def test_remove_and_clear_do_not_change_pantry(self) -> None:
        pantry.add_pantry_item(display_name="Chicken breast")
        first = shopping.add_shopping_item(display_name="Oats")
        shopping.add_shopping_item(display_name="Lentils")

        self.assertTrue(
            shopping.remove_shopping_item(
                first["shopping_list_item_id"]
            )
        )
        self.assertEqual(shopping.clear_shopping_list(), 1)
        self.assertEqual(
            pantry.list_pantry_items()[0]["display_name"],
            "Chicken breast",
        )


if __name__ == "__main__":
    unittest.main()
