from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from food import database, finder, health_check, library, pantry


class FoodLibraryHealthCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temporary_directory = tempfile.TemporaryDirectory()
        self.database_path = (
            Path(self.temporary_directory.name) / "healthcoach_food.db"
        )
        original_initialize = database.initialize_database

        def initialize_test_database(database_path=None):
            return original_initialize(self.database_path)

        modules = (database, finder, library, pantry)
        self.patchers = [
            patch.object(database, "DATABASE_PATH", self.database_path),
            patch.object(finder, "DATABASE_PATH", self.database_path),
            patch.object(library, "DATABASE_PATH", self.database_path),
            patch.object(pantry, "DATABASE_PATH", self.database_path),
        ]
        self.patchers.extend(
            patch.object(
                module,
                "initialize_database",
                initialize_test_database,
            )
            for module in modules
        )
        for patcher in self.patchers:
            patcher.start()
        database.initialize_database()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def create_food(
        self,
        *,
        name: str,
        source: str,
        status: str = "verified",
        food_type: str = "food",
        fiber_g: float | None = 2,
        sugar_g: float | None = 1,
        sodium_mg: float | None = 100,
    ) -> int:
        created = library.add_food_with_nutrition(
            canonical_name=name,
            serving_description="1 serving",
            serving_amount=1,
            serving_unit="serving",
            verification_status=status,
            verification_source=source,
            food_type=food_type,
            calories=200,
            protein_g=10,
            carbohydrates_g=20,
            fat_g=8,
            fiber_g=fiber_g,
            sugar_g=sugar_g,
            sodium_mg=sodium_mg,
        )
        return int(created["food"]["food_id"])

    def test_builds_actionable_categories_without_writing(self) -> None:
        complete_id = self.create_food(
            name="Complete Food",
            source="fdc.nal.usda.gov",
        )
        incomplete_id = self.create_food(
            name="Incomplete Food",
            source="user_entered",
            fiber_g=None,
            sugar_g=None,
            sodium_mg=None,
        )
        weak_id = self.create_food(
            name="Estimated Food",
            source="visual_estimate",
            status="estimated",
        )
        self.create_food(
            name="Calculated Recipe",
            source="recipe_builder",
            status="estimated",
            food_type="recipe",
        )
        pantry.add_pantry_item(
            display_name="Loose apples",
            source="manual",
        )
        organized = pantry.add_pantry_item(
            display_name="Complete Food",
            source="saved_food",
            food_id=complete_id,
        )
        pantry.update_pantry_item_organization(
            int(organized["pantry_item_id"]),
            storage_area="pantry_shelf",
            food_category="snacks",
        )

        with database.get_connection(self.database_path) as connection:
            before = {
                table: connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()["count"]
                for table in ("foods", "nutrition_versions", "pantry_items")
            }

        report = health_check.build_food_library_health_check(
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

        self.assertEqual(report["food_count"], 4)
        self.assertEqual(report["pantry_count"], 2)
        self.assertEqual(
            [item["display_name"] for item in report["pantry_nutrition"]],
            ["Loose apples"],
        )
        self.assertEqual(
            [item["display_name"] for item in report["pantry_organization"]],
            ["Loose apples"],
        )
        self.assertEqual(
            [food["food_id"] for food in report["nutrition_gaps"]],
            [incomplete_id],
        )
        self.assertEqual(
            report["nutrition_gaps"][0]["health_check_reason"],
            ["fiber", "sugar", "sodium"],
        )
        self.assertEqual(
            [food["food_id"] for food in report["source_review"]],
            [weak_id],
        )
        self.assertEqual(report["source_rechecks"], [])

        with database.get_connection(self.database_path) as connection:
            after = {
                table: connection.execute(
                    f"SELECT COUNT(*) AS count FROM {table}"
                ).fetchone()["count"]
                for table in ("foods", "nutrition_versions", "pantry_items")
            }
        self.assertEqual(after, before)

    def test_user_sources_are_trusted_and_provider_food_can_be_due(self) -> None:
        entered_id = self.create_food(
            name="Entered Food",
            source="user_entered",
        )
        label_id = self.create_food(
            name="Package Food",
            source="user_package_label",
        )
        provider_id = self.create_food(
            name="Provider Food",
            source="fdc.nal.usda.gov",
        )
        with database.get_connection(self.database_path) as connection:
            connection.execute(
                """
                UPDATE foods
                SET last_verified_at = '2025-12-01T00:00:00+00:00'
                WHERE food_id IN (?, ?, ?)
                """,
                (entered_id, label_id, provider_id),
            )
            connection.commit()

        report = health_check.build_food_library_health_check(
            now=datetime(2026, 8, 27, tzinfo=timezone.utc),
        )

        self.assertEqual(report["source_review"], [])
        self.assertEqual(
            [food["food_id"] for food in report["source_rechecks"]],
            [provider_id],
        )

    def test_archived_food_is_not_reported(self) -> None:
        archived_id = self.create_food(
            name="Old Entered Food",
            source="user_entered",
            fiber_g=None,
            sugar_g=None,
            sodium_mg=None,
        )
        library.archive_user_saved_food(archived_id)

        report = health_check.build_food_library_health_check()

        self.assertEqual(report["food_count"], 0)
        self.assertEqual(report["nutrition_gaps"], [])
        self.assertEqual(report["source_review"], [])

    def test_counts_preserved_previous_versions_as_history(self) -> None:
        food_id = self.create_food(
            name="Versioned Food",
            source="user_entered",
        )
        library.add_user_nutrition_version(
            food_id=food_id,
            calories=210,
            protein_g=11,
            carbohydrates_g=21,
            fat_g=8,
            fiber_g=2,
            sugar_g=1,
            sodium_mg=100,
        )

        report = health_check.build_food_library_health_check()

        self.assertEqual(report["preserved_versions"], 1)
        self.assertEqual(report["nutrition_gaps"], [])

    def test_reason_labels_preserve_unknown_values(self) -> None:
        food = {
            "health_check_reason": ["fiber", "sodium"],
            "verification_status": "estimated",
            "verification_source": "visual_estimate",
            "last_verified_at": None,
        }

        self.assertEqual(
            health_check.health_check_food_reason(food, "nutrition_gaps"),
            "Missing: fiber, sodium",
        )
        self.assertIn(
            "visual_estimate",
            health_check.health_check_food_reason(food, "source_review"),
        )
        self.assertEqual(
            health_check.health_check_food_reason(food, "source_rechecks"),
            "Last checked: not recorded",
        )


if __name__ == "__main__":
    unittest.main()
