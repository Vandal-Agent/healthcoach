from __future__ import annotations

import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from food import database, ledger, library


class FoodWorkflowTests(unittest.TestCase):
    ENTRY_DATE = "2026-08-11"

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
        ]
        for patcher in self.patchers:
            patcher.start()

        database.initialize_database()
        result = library.add_food_with_nutrition(
            canonical_name="Test Protein Bar",
            serving_description="1 bar",
            serving_amount=1,
            serving_unit="bar",
            verification_status="verified",
            verification_source="test label",
            calories=200,
            protein_g=20,
            carbohydrates_g=24,
            fat_g=8,
            fiber_g=5,
            sugar_g=6,
            sodium_mg=180,
        )
        self.food_id = result["food"]["food_id"]

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def add_entry(
        self,
        *,
        quantity: float = 1,
        meal: str = "breakfast",
        source: str = "telegram_ai",
    ):
        return ledger.add_food_entry(
            entry_date=self.ENTRY_DATE,
            meal_category=meal,
            food_id=self.food_id,
            quantity=quantity,
            logging_source=source,
            original_text="test food entry",
        )

    def test_connection_closes_after_context_manager(self) -> None:
        connection = database.get_connection(self.database_path)
        with connection:
            connection.execute("SELECT 1")

        with self.assertRaises(sqlite3.ProgrammingError):
            connection.execute("SELECT 1")

    def test_schema_initializes_at_current_version(self) -> None:
        result = database.initialize_database()
        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("food_entries", result["tables"])
        self.assertIn("food_favorites", result["tables"])
        self.assertIn("barcode_mappings", result["tables"])
        self.assertIn("pantry_items", result["tables"])

    def test_logging_scales_nutrition_and_totals(self) -> None:
        entry = self.add_entry(quantity=1.5)

        self.assertEqual(entry["quantity"], 1.5)
        self.assertEqual(entry["calories"], 300)
        self.assertEqual(entry["protein_g"], 30)

        totals = ledger.get_daily_totals(self.ENTRY_DATE)
        self.assertEqual(totals["calories"], 300)
        self.assertEqual(totals["protein_g"], 30)
        self.assertEqual(totals["fiber_g"], 7.5)

    def test_quantity_edit_recalculates_snapshot_and_totals(self) -> None:
        entry = self.add_entry()
        updated = ledger.update_food_entry(
            entry["food_entry_id"],
            quantity=2,
        )

        self.assertEqual(updated["quantity"], 2)
        self.assertEqual(updated["calories"], 400)
        self.assertEqual(updated["protein_g"], 40)

        totals = ledger.get_daily_totals(self.ENTRY_DATE)
        self.assertEqual(totals["calories"], 400)
        self.assertEqual(totals["protein_g"], 40)

    def test_meal_edit_moves_entry_without_changing_nutrition(self) -> None:
        entry = self.add_entry()
        updated = ledger.update_food_entry(
            entry["food_entry_id"],
            meal_category="school snack",
        )

        self.assertEqual(updated["meal_category"], "school snack")
        self.assertEqual(updated["calories"], 200)
        self.assertEqual(
            ledger.list_food_entries(
                entry_date=self.ENTRY_DATE,
                meal_category="breakfast",
            ),
            [],
        )
        moved = ledger.list_food_entries(
            entry_date=self.ENTRY_DATE,
            meal_category="school snack",
        )
        self.assertEqual(len(moved), 1)

    def test_different_sources_can_share_one_meal(self) -> None:
        sources = [
            "telegram_ai",
            "telegram_manual",
            "barcode",
            "recipe",
            "manual",
            "loseit",
        ]

        for source in sources:
            self.add_entry(source=source)

        entries = ledger.list_food_entries(
            entry_date=self.ENTRY_DATE,
            meal_category="breakfast",
        )
        self.assertEqual(len(entries), len(sources))
        self.assertEqual(
            {entry["logging_source"] for entry in entries},
            set(sources),
        )

    def test_meal_edit_can_join_entries_from_other_sources(self) -> None:
        moved = self.add_entry(meal="breakfast", source="recipe")
        self.add_entry(meal="lunch", source="barcode")

        updated = ledger.update_food_entry(
            moved["food_entry_id"],
            meal_category="lunch",
        )

        self.assertEqual(updated["meal_category"], "lunch")
        lunch_entries = ledger.list_food_entries(
            entry_date=self.ENTRY_DATE,
            meal_category="lunch",
        )
        self.assertEqual(len(lunch_entries), 2)

    def test_delete_entry_recalculates_totals(self) -> None:
        entry = self.add_entry(quantity=2)
        self.assertTrue(
            ledger.delete_food_entry(entry["food_entry_id"])
        )
        self.assertFalse(
            ledger.delete_food_entry(entry["food_entry_id"])
        )

        totals = ledger.get_daily_totals(self.ENTRY_DATE)
        self.assertEqual(totals["calories"], 0)
        self.assertEqual(totals["protein_g"], 0)

    def test_favorite_save_list_and_delete(self) -> None:
        entry = self.add_entry(quantity=1.3, meal="school snack")
        favorite = ledger.save_food_favorite_from_entry(
            entry["food_entry_id"]
        )

        self.assertEqual(favorite["food_id"], self.food_id)
        self.assertEqual(favorite["quantity"], 1.3)
        self.assertEqual(favorite["meal_category"], "school snack")

        favorites = ledger.list_food_favorites()
        self.assertEqual(len(favorites), 1)
        self.assertEqual(favorites[0]["canonical_name"], "Test Protein Bar")

        self.assertTrue(
            ledger.delete_food_favorite(
                favorite["food_favorite_id"]
            )
        )
        self.assertEqual(ledger.list_food_favorites(), [])

    def test_copy_food_entries_preserves_exact_snapshot(self) -> None:
        source = self.add_entry(quantity=1.5, meal="breakfast")
        library.add_user_nutrition_version(
            food_id=self.food_id,
            calories=250,
            protein_g=25,
            carbohydrates_g=26,
            fat_g=9,
            fiber_g=6,
            sugar_g=5,
            sodium_mg=170,
        )

        copied = ledger.copy_food_entries_to_date(
            source_date=self.ENTRY_DATE,
            target_date="2026-08-12",
        )

        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0]["calories"], source["calories"])
        self.assertEqual(copied[0]["protein_g"], source["protein_g"])
        self.assertEqual(
            copied[0]["nutrition_version_id"],
            source["nutrition_version_id"],
        )
        self.assertEqual(copied[0]["logging_source"], "telegram_manual")

    def test_copy_can_select_one_meal(self) -> None:
        self.add_entry(meal="breakfast")
        self.add_entry(meal="dinner")

        copied = ledger.copy_food_entries_to_date(
            source_date=self.ENTRY_DATE,
            target_date="2026-08-12",
            meal_category="dinner",
        )

        self.assertEqual(len(copied), 1)
        self.assertEqual(copied[0]["meal_category"], "dinner")

    def test_copy_rejects_occupied_target_meal_atomically(self) -> None:
        self.add_entry(meal="breakfast")
        self.add_entry(meal="dinner")
        ledger.add_food_entry(
            entry_date="2026-08-12",
            meal_category="dinner",
            food_id=self.food_id,
            quantity=0.5,
            logging_source="telegram_manual",
        )

        with self.assertRaisesRegex(ValueError, "Nothing was copied"):
            ledger.copy_food_entries_to_date(
                source_date=self.ENTRY_DATE,
                target_date="2026-08-12",
            )

        target_entries = ledger.list_food_entries(
            entry_date="2026-08-12"
        )
        self.assertEqual(len(target_entries), 1)
        self.assertEqual(target_entries[0]["meal_category"], "dinner")


if __name__ == "__main__":
    unittest.main()
