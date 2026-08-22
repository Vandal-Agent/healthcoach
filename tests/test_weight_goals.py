from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from food import database, goals


class WeightGoalTests(unittest.TestCase):
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
            patch.object(goals, "DATABASE_PATH", self.database_path),
            patch.object(
                database,
                "initialize_database",
                initialize_test_database,
            ),
            patch.object(
                goals,
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

    def test_schema_contains_weight_goal_tables(self) -> None:
        result = database.initialize_database()
        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("weight_goals", result["tables"])
        self.assertIn("weight_goal_calculations", result["tables"])

    def test_version_eight_migrates_to_weight_goals(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute("DROP TABLE weight_goal_calculations")
            connection.execute("DROP TABLE weight_goals")
            connection.execute(
                "DELETE FROM schema_version WHERE version = 9"
            )
            connection.commit()

        result = database.initialize_database()
        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("weight_goals", result["tables"])

    def test_goal_lifecycle_keeps_history(self) -> None:
        created = goals.create_weight_goal(
            start_date=date(2026, 8, 19),
            start_weight=230.6,
            target_weight=215,
            target_date=date(2026, 10, 17),
        )
        self.assertEqual(created["status"], "active")

        with self.assertRaisesRegex(ValueError, "already exists"):
            goals.create_weight_goal(
                start_date=date(2026, 8, 20),
                start_weight=230,
                target_weight=220,
                target_date=date(2026, 10, 20),
            )

        updated = goals.update_active_weight_goal(
            target_weight=214,
            target_date=date(2026, 10, 20),
        )
        self.assertEqual(updated["target_weight"], 214)

        archived = goals.archive_active_weight_goal()
        self.assertEqual(archived["status"], "archived")
        self.assertIsNone(goals.get_active_weight_goal())
        self.assertEqual(len(goals.list_weight_goals()), 1)

    def test_reachable_goal_uses_required_deficit(self) -> None:
        result = goals.calculate_weight_goal(
            current_date=date(2026, 8, 19),
            current_weight=230.6,
            target_weight=215,
            target_date=date(2026, 10, 17),
            average_daily_burn=2800,
            burn_days=7,
        )
        self.assertTrue(result["safely_reachable"])
        self.assertLess(result["required_weekly_loss"], 2)
        self.assertGreaterEqual(result["calorie_target_low"], 1500)
        self.assertEqual(
            result["calorie_target_high"]
            - result["calorie_target_low"],
            150,
        )

    def test_unreachable_goal_returns_safe_projection(self) -> None:
        result = goals.calculate_weight_goal(
            current_date=date(2026, 8, 19),
            current_weight=230.6,
            target_weight=200,
            target_date=date(2026, 9, 1),
            average_daily_burn=2300,
            burn_days=7,
        )
        self.assertFalse(result["safely_reachable"])
        self.assertEqual(result["planned_daily_deficit"], 800)
        self.assertEqual(result["calorie_target_low"], 1500)
        self.assertEqual(result["calorie_target_high"], 1650)
        self.assertGreater(result["projected_weight"], 200)
        self.assertIn("2 lb per week", result["limiting_reason"])

    def test_calculation_snapshot_is_reused_until_updated(self) -> None:
        goal = goals.create_weight_goal(
            start_date=date(2026, 8, 19),
            start_weight=230.6,
            target_weight=215,
            target_date=date(2026, 10, 17),
        )
        calculation = goals.calculate_weight_goal(
            current_date=date(2026, 8, 19),
            current_weight=230.6,
            target_weight=215,
            target_date=date(2026, 10, 17),
            average_daily_burn=2800,
            burn_days=7,
        )
        saved = goals.save_weight_goal_calculation(
            int(goal["weight_goal_id"]),
            calculation,
        )
        latest = goals.get_latest_weight_goal_calculation()
        self.assertEqual(
            latest["weight_goal_calculation_id"],
            saved["weight_goal_calculation_id"],
        )
        self.assertEqual(latest["calorie_target_low"], 1800)

    def test_requires_three_completed_burn_days(self) -> None:
        with self.assertRaisesRegex(ValueError, "three completed days"):
            goals.calculate_weight_goal(
                current_date=date(2026, 8, 19),
                current_weight=230.6,
                target_weight=215,
                target_date=date(2026, 10, 17),
                average_daily_burn=2800,
                burn_days=2,
            )


if __name__ == "__main__":
    unittest.main()
