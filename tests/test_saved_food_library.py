from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

import food.database as database
import food.ledger as ledger
import food.library as library


class SavedFoodLibraryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temp.name) / "healthcoach_food.db"
        )

        self.patches = [
            patch.object(
                database,
                "DATABASE_PATH",
                self.database_path,
            ),
            patch.object(
                library,
                "DATABASE_PATH",
                self.database_path,
            ),
            patch.object(
                ledger,
                "DATABASE_PATH",
                self.database_path,
            ),
        ]

        for item in self.patches:
            item.start()

        database.initialize_database(self.database_path)

    def tearDown(self):
        for item in reversed(self.patches):
            item.stop()

        self.temp.cleanup()

    def create_food(self):
        return library.add_food_with_nutrition(
            canonical_name="Test Bowl",
            serving_description="1 serving",
            serving_amount=1,
            serving_unit="serving",
            verification_status="verified",
            verification_source="user_entered",
            calories=500,
            protein_g=40,
            carbohydrates_g=50,
            fat_g=15,
            fiber_g=8,
            sugar_g=5,
            sodium_mg=900,
            food_type="meal",
        )

    def test_lists_user_saved_foods(self):
        created = self.create_food()
        foods = library.list_user_saved_foods()

        self.assertEqual(len(foods), 1)
        self.assertEqual(
            foods[0]["food_id"],
            created["food"]["food_id"],
        )
        self.assertEqual(foods[0]["calories"], 500)

    def test_new_version_affects_only_future_logs(self):
        created = self.create_food()
        food_id = created["food"]["food_id"]

        old_entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 12),
            meal_category="dinner",
            food_id=food_id,
            quantity=1,
            logging_source="manual",
            user_confirmed=True,
        )

        new_version = library.add_user_nutrition_version(
            food_id=food_id,
            calories=450,
            protein_g=42,
            carbohydrates_g=44,
            fat_g=13,
            fiber_g=9,
            sugar_g=4,
            sodium_mg=800,
        )

        new_entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 13),
            meal_category="dinner",
            food_id=food_id,
            quantity=1,
            logging_source="manual",
            user_confirmed=True,
        )

        self.assertEqual(old_entry["calories"], 500)
        self.assertEqual(new_entry["calories"], 450)
        self.assertNotEqual(
            old_entry["nutrition_version_id"],
            new_version["nutrition_version_id"],
        )
        self.assertEqual(
            new_entry["nutrition_version_id"],
            new_version["nutrition_version_id"],
        )

    def test_rejects_negative_nutrition(self):
        created = self.create_food()

        with self.assertRaises(ValueError):
            library.add_user_nutrition_version(
                food_id=created["food"]["food_id"],
                calories=-1,
                protein_g=40,
                carbohydrates_g=50,
                fat_g=15,
                fiber_g=8,
                sugar_g=5,
                sodium_mg=900,
            )

    def test_renames_saved_food_and_serving(self):
        created = self.create_food()

        updated = library.update_user_saved_food_identity(
            food_id=created["food"]["food_id"],
            canonical_name="Tracy's Home Salad",
            serving_description="1 home salad",
        )

        self.assertEqual(updated["canonical_name"], "Tracy's Home Salad")
        self.assertEqual(updated["serving_description"], "1 home salad")
        self.assertEqual(updated["calories"], 500)

    def test_rename_rejects_duplicate_identity(self):
        first = self.create_food()
        library.add_food_with_nutrition(
            canonical_name="Second Bowl",
            serving_description="1 serving",
            serving_amount=1,
            serving_unit="serving",
            verification_status="verified",
            verification_source="user_entered",
            calories=400,
            protein_g=30,
            carbohydrates_g=40,
            fat_g=12,
            fiber_g=7,
            sugar_g=4,
            sodium_mg=700,
        )

        with self.assertRaisesRegex(ValueError, "already uses"):
            library.update_user_saved_food_identity(
                food_id=first["food"]["food_id"],
                canonical_name="Second Bowl",
            )

    def test_archive_hides_saved_food_and_preserves_history(self):
        created = self.create_food()
        food_id = created["food"]["food_id"]
        entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 16),
            meal_category="dinner",
            food_id=food_id,
            quantity=1,
            logging_source="manual",
            user_confirmed=True,
        )

        archived = library.archive_user_saved_food(food_id)

        self.assertEqual(archived["canonical_name"], "Test Bowl")
        self.assertEqual(library.list_user_saved_foods(), [])
        self.assertIsNotNone(library.get_food(food_id))
        history = ledger.list_food_entries(entry_date=date(2026, 8, 16))
        self.assertEqual(history[0]["food_entry_id"], entry["food_entry_id"])
        self.assertEqual(history[0]["calories"], 500)


if __name__ == "__main__":
    unittest.main()
