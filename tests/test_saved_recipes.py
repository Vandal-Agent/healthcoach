from __future__ import annotations

import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from food import database, ledger, library, recipes


RECIPE_IDEA = {
    "name": "Chicken Pepper Bowl",
    "summary": "A quick chicken and vegetable bowl.",
    "ingredients": [
        {
            "name": "Chicken breast",
            "amount": "4 ounces",
            "source": "pantry",
        },
        {
            "name": "green peppers",
            "amount": "1/2 cup",
            "source": "pantry",
        },
    ],
    "preparation_steps": [
        "Cook the chicken.",
        "Add the peppers and serve.",
    ],
    "calories": 450,
    "protein_g": 42,
    "carbohydrates_g": 40,
    "fat_g": 13,
    "fiber_g": 8,
    "sugar_g": 6,
    "sodium_mg": 520,
    "estimate_notes": "Nutrition is estimated.",
}

HEART_HEALTHY_RECIPE_IDEA = {
    **RECIPE_IDEA,
    "heart_healthy_pick": True,
    "heart_healthy_reason": (
        "Uses lean chicken and vegetables with moderate sodium."
    ),
}


class SavedRecipeTests(unittest.TestCase):
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
            patch.object(recipes, "DATABASE_PATH", self.database_path),
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
            patch.object(
                recipes,
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

    def test_schema_contains_saved_recipes(self) -> None:
        result = database.initialize_database()

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("saved_recipes", result["tables"])

        with database.get_connection(self.database_path) as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(saved_recipes)"
                ).fetchall()
            }
        self.assertIn("heart_healthy_pick", columns)
        self.assertIn("heart_healthy_reason", columns)
        self.assertIn("yield_servings", columns)
        self.assertIn("saved_recipe_ingredients", result["tables"])

    def test_recipe_pantry_marks_only_complete_linked_food_ready(self) -> None:
        complete = {
            "pantry_item_id": 1,
            "display_name": "Chicken",
            "food_id": 10,
            "food_type": "food",
            "verification_status": "verified",
            "verification_source": "user_package_label",
            "calories": 120,
            "protein_g": 22,
            "carbohydrates_g": 0,
            "fat_g": 3,
            "fiber_g": 0,
            "sugar_g": 0,
            "sodium_mg": 50,
        }
        unlinked = {
            "pantry_item_id": 2,
            "display_name": "Onion",
            "food_id": None,
        }

        with patch.object(
            recipes,
            "list_pantry_items",
            return_value=[complete, unlinked],
        ):
            items = recipes.list_recipe_pantry_foods()

        self.assertTrue(items[0]["nutrition_ready"])
        self.assertFalse(items[1]["nutrition_ready"])
        self.assertIn("calories", items[1]["missing_nutrients"])

    def test_version_nine_database_adds_heart_health_fields(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute(
                "ALTER TABLE saved_recipes "
                "DROP COLUMN heart_healthy_reason"
            )
            connection.execute(
                "ALTER TABLE saved_recipes "
                "DROP COLUMN heart_healthy_pick"
            )
            connection.execute(
                "DELETE FROM schema_version WHERE version IN (10, 11)"
            )
            connection.commit()

        result = database.initialize_database()

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        with database.get_connection(self.database_path) as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(saved_recipes)"
                ).fetchall()
            }
        self.assertIn("heart_healthy_pick", columns)
        self.assertIn("heart_healthy_reason", columns)

    def add_builder_food(
        self,
        *,
        name: str,
        serving_amount: float,
        serving_unit: str,
        calories: float,
        protein_g: float,
        carbohydrates_g: float,
        fat_g: float,
        fiber_g: float,
        sugar_g: float,
        sodium_mg: float,
    ) -> dict:
        return library.add_food_with_nutrition(
            canonical_name=name,
            serving_description=(
                f"{serving_amount:g} {serving_unit}"
            ),
            serving_amount=serving_amount,
            serving_unit=serving_unit,
            verification_status="verified",
            verification_source="user_entered",
            calories=calories,
            protein_g=protein_g,
            carbohydrates_g=carbohydrates_g,
            fat_g=fat_g,
            fiber_g=fiber_g,
            sugar_g=sugar_g,
            sodium_mg=sodium_mg,
        )

    def test_recipe_builder_calculates_and_links_exact_versions(self) -> None:
        turkey = self.add_builder_food(
            name="Browned Ground Turkey",
            serving_amount=3,
            serving_unit="oz",
            calories=170,
            protein_g=22,
            carbohydrates_g=0,
            fat_g=9,
            fiber_g=0,
            sugar_g=0,
            sodium_mg=80,
        )
        cheese = self.add_builder_food(
            name="Mexican Cheese Blend",
            serving_amount=28,
            serving_unit="g",
            calories=110,
            protein_g=7,
            carbohydrates_g=1,
            fat_g=9,
            fiber_g=0,
            sugar_g=0,
            sodium_mg=170,
        )
        ingredients = [
            recipes.prepare_recipe_ingredient(
                food_id=int(turkey["food"]["food_id"]),
                amount_description="6 oz",
            ),
            recipes.prepare_recipe_ingredient(
                food_id=int(cheese["food"]["food_id"]),
                amount_description="28 g",
            ),
        ]

        result = recipes.create_saved_recipe_from_ingredients(
            name="Turkey Cheese Skillet",
            meal_type="dinner",
            yield_servings=2,
            ingredients=ingredients,
            preparation_steps=["Heat the turkey and melt in the cheese."],
            summary="A two-serving turkey skillet.",
        )

        self.assertTrue(result["created"])
        recipe = result["recipe"]
        self.assertEqual(recipe["yield_servings"], 2)
        self.assertEqual(recipe["calories"], 225)
        self.assertEqual(recipe["protein_g"], 25.5)
        self.assertEqual(result["total_nutrition"]["calories"], 450)
        links = recipes.list_saved_recipe_ingredients(
            int(recipe["saved_recipe_id"])
        )
        self.assertEqual(len(links), 2)
        self.assertEqual(links[0]["amount_description"], "6 oz")
        self.assertEqual(
            links[0]["nutrition_version_id"],
            turkey["nutrition"]["nutrition_version_id"],
        )

    def test_builder_recipe_does_not_change_when_ingredient_changes(self) -> None:
        turkey = self.add_builder_food(
            name="Recipe Turkey",
            serving_amount=3,
            serving_unit="oz",
            calories=170,
            protein_g=22,
            carbohydrates_g=0,
            fat_g=9,
            fiber_g=0,
            sugar_g=0,
            sodium_mg=80,
        )
        ingredient = recipes.prepare_recipe_ingredient(
            food_id=int(turkey["food"]["food_id"]),
            amount_description="3 oz",
        )
        saved = recipes.create_saved_recipe_from_ingredients(
            name="Versioned Turkey Recipe",
            meal_type="lunch",
            yield_servings=1,
            ingredients=[ingredient],
            preparation_steps=["Heat and serve."],
        )["recipe"]

        library.add_user_nutrition_version(
            food_id=int(turkey["food"]["food_id"]),
            calories=200,
            protein_g=20,
            carbohydrates_g=1,
            fat_g=12,
            fiber_g=0,
            sugar_g=0,
            sodium_mg=100,
        )

        unchanged = recipes.get_saved_recipe(
            int(saved["saved_recipe_id"])
        )
        self.assertEqual(unchanged["calories"], 170)
        link = recipes.list_saved_recipe_ingredients(
            int(saved["saved_recipe_id"])
        )[0]
        self.assertEqual(
            link["nutrition_version_id"],
            ingredient["nutrition_version_id"],
        )

    def test_builder_recalculation_versions_future_recipe_logs(self) -> None:
        turkey = self.add_builder_food(
            name="Recalculation Turkey",
            serving_amount=3,
            serving_unit="oz",
            calories=170,
            protein_g=22,
            carbohydrates_g=0,
            fat_g=9,
            fiber_g=0,
            sugar_g=0,
            sodium_mg=80,
        )
        food_id = int(turkey["food"]["food_id"])
        original_ingredient = recipes.prepare_recipe_ingredient(
            food_id=food_id,
            amount_description="3 oz",
        )
        saved = recipes.create_saved_recipe_from_ingredients(
            name="Recalculated Turkey Recipe",
            meal_type="dinner",
            yield_servings=1,
            ingredients=[original_ingredient],
            preparation_steps=["Heat and serve."],
        )["recipe"]
        old_entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 15),
            meal_category="dinner",
            food_id=int(saved["food_id"]),
            quantity=1,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )

        library.add_user_nutrition_version(
            food_id=food_id,
            calories=200,
            protein_g=20,
            carbohydrates_g=1,
            fat_g=12,
            fiber_g=0,
            sugar_g=0,
            sodium_mg=100,
        )
        changed_ingredient = recipes.prepare_recipe_ingredient(
            food_id=food_id,
            amount_description="6 oz",
        )
        updated = recipes.update_saved_recipe_from_ingredients(
            int(saved["saved_recipe_id"]),
            yield_servings=1,
            ingredients=[changed_ingredient],
            preparation_steps=["Heat two portions and serve."],
            summary="Updated ingredient amount.",
        )["recipe"]
        new_entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 16),
            meal_category="dinner",
            food_id=int(saved["food_id"]),
            quantity=1,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )

        self.assertEqual(old_entry["calories"], 170)
        self.assertEqual(updated["version_number"], 2)
        self.assertEqual(updated["calories"], 400)
        self.assertEqual(new_entry["calories"], 400)
        link = recipes.list_saved_recipe_ingredients(
            int(saved["saved_recipe_id"])
        )[0]
        self.assertEqual(
            link["nutrition_version_id"],
            changed_ingredient["nutrition_version_id"],
        )

    def test_recreating_deleted_builder_recipe_uses_new_nutrition(self) -> None:
        turkey = self.add_builder_food(
            name="Recreate Recipe Turkey",
            serving_amount=3,
            serving_unit="oz",
            calories=170,
            protein_g=22,
            carbohydrates_g=0,
            fat_g=9,
            fiber_g=0,
            sugar_g=0,
            sodium_mg=80,
        )
        food_id = int(turkey["food"]["food_id"])
        one_serving = recipes.prepare_recipe_ingredient(
            food_id=food_id,
            amount_description="3 oz",
        )
        original = recipes.create_saved_recipe_from_ingredients(
            name="Recreated Turkey Recipe",
            meal_type="dinner",
            yield_servings=1,
            ingredients=[one_serving],
            preparation_steps=["Heat and serve."],
        )["recipe"]
        old_entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 15),
            meal_category="dinner",
            food_id=int(original["food_id"]),
            quantity=1,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )
        recipes.delete_saved_recipe(int(original["saved_recipe_id"]))

        two_servings = recipes.prepare_recipe_ingredient(
            food_id=food_id,
            amount_description="6 oz",
        )
        recreated = recipes.create_saved_recipe_from_ingredients(
            name="Recreated Turkey Recipe",
            meal_type="dinner",
            yield_servings=1,
            ingredients=[two_servings],
            preparation_steps=["Heat both portions and serve."],
        )["recipe"]

        self.assertEqual(recreated["food_id"], original["food_id"])
        self.assertEqual(recreated["version_number"], 2)
        self.assertEqual(recreated["calories"], 340)
        self.assertEqual(old_entry["calories"], 170)

    def test_recipe_amount_conversion_is_strict(self) -> None:
        self.assertAlmostEqual(
            recipes.ingredient_serving_multiplier(
                amount_description="1 1/2 oz",
                serving_amount=3,
                serving_unit="oz",
            ),
            0.5,
        )
        self.assertAlmostEqual(
            recipes.ingredient_serving_multiplier(
                amount_description="56 g",
                serving_amount=1,
                serving_unit="oz",
            ),
            1.9753419,
            places=5,
        )
        with self.assertRaisesRegex(ValueError, "cannot be converted"):
            recipes.ingredient_serving_multiplier(
                amount_description="1 cup",
                serving_amount=28,
                serving_unit="g",
            )

    def test_version_six_database_migrates_to_saved_recipes(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute("DROP TABLE saved_recipes")
            connection.execute(
                "DELETE FROM schema_version WHERE version IN (7, 8)"
            )
            connection.commit()

        result = database.initialize_database()

        self.assertEqual(
            result["schema_version"]["version"],
            database.SCHEMA_VERSION,
        )
        self.assertIn("saved_recipes", result["tables"])

    def test_version_ten_database_migrates_to_recipe_builder(self) -> None:
        with database.get_connection(self.database_path) as connection:
            connection.execute("DROP TABLE saved_recipe_ingredients")
            connection.execute(
                "ALTER TABLE saved_recipes DROP COLUMN yield_servings"
            )
            connection.execute(
                "DELETE FROM schema_version WHERE version = 11"
            )
            connection.commit()

        result = database.initialize_database()

        self.assertEqual(result["schema_version"]["version"], 11)
        self.assertIn("saved_recipe_ingredients", result["tables"])
        with database.get_connection(self.database_path) as connection:
            columns = {
                row["name"]
                for row in connection.execute(
                    "PRAGMA table_info(saved_recipes)"
                ).fetchall()
            }
        self.assertIn("yield_servings", columns)

    def test_saves_lists_and_reads_complete_recipe(self) -> None:
        result = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )

        self.assertTrue(result["created"])
        recipe = result["recipe"]
        self.assertEqual(recipe["canonical_name"], "Chicken Pepper Bowl")
        self.assertEqual(recipe["meal_type"], "dinner")
        self.assertEqual(recipe["calories"], 450)
        self.assertEqual(recipe["ingredients"][0]["amount"], "4 ounces")
        self.assertEqual(len(recipe["preparation_steps"]), 2)
        self.assertEqual(len(recipes.list_saved_recipes()), 1)

    def test_preserves_heart_healthy_pick_when_recipe_is_saved(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            HEART_HEALTHY_RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]

        self.assertTrue(saved["heart_healthy_pick"])
        self.assertEqual(
            saved["heart_healthy_reason"],
            HEART_HEALTHY_RECIPE_IDEA["heart_healthy_reason"],
        )
        listed = recipes.list_saved_recipes()[0]
        self.assertTrue(listed["heart_healthy_pick"])

    def test_duplicate_recipe_is_not_overwritten(self) -> None:
        first = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )
        changed = dict(RECIPE_IDEA)
        changed["calories"] = 999
        second = recipes.save_pantry_meal_idea(
            changed,
            meal_type="dinner",
        )

        self.assertTrue(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(second["recipe"]["calories"], 450)

    def test_saved_recipe_logs_from_existing_food_snapshot(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]

        entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 16),
            meal_category="dinner",
            food_id=int(saved["food_id"]),
            quantity=1.5,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )

        self.assertEqual(entry["calories"], 675)
        self.assertEqual(entry["protein_g"], 63)
        self.assertEqual(entry["logging_source"], "recipe")

    def test_rejects_recipe_without_preparation(self) -> None:
        invalid = dict(RECIPE_IDEA)
        invalid["preparation_steps"] = []

        with self.assertRaises(ValueError):
            recipes.save_pantry_meal_idea(
                invalid,
                meal_type="dinner",
            )

    def test_updates_recipe_details_and_name(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]

        updated = recipes.update_saved_recipe(
            int(saved["saved_recipe_id"]),
            name="Chicken Garden Bowl",
            meal_type="lunch",
            summary="An updated quick lunch.",
            ingredients=[
                {
                    "name": "Chicken breast",
                    "amount": "3 ounces",
                    "source": "pantry",
                },
            ],
            preparation_steps=["Cook, slice, and serve."],
        )

        self.assertEqual(updated["canonical_name"], "Chicken Garden Bowl")
        self.assertEqual(updated["meal_type"], "lunch")
        self.assertEqual(updated["ingredients"][0]["amount"], "3 ounces")
        self.assertEqual(updated["version_number"], 1)

    def test_non_material_edit_preserves_heart_healthy_pick(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            HEART_HEALTHY_RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]

        updated = recipes.update_saved_recipe(
            int(saved["saved_recipe_id"]),
            name="Heart-Healthy Chicken Bowl",
            preparation_steps=["Cook carefully and serve."],
        )

        self.assertTrue(updated["heart_healthy_pick"])
        self.assertEqual(
            updated["heart_healthy_reason"],
            HEART_HEALTHY_RECIPE_IDEA["heart_healthy_reason"],
        )

    def test_ingredient_edit_clears_heart_healthy_pick(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            HEART_HEALTHY_RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]

        updated = recipes.update_saved_recipe(
            int(saved["saved_recipe_id"]),
            ingredients=[
                {
                    "name": "Chicken breast",
                    "amount": "3 ounces",
                    "source": "pantry",
                },
            ],
        )

        self.assertFalse(updated["heart_healthy_pick"])
        self.assertEqual(updated["heart_healthy_reason"], "")

    def test_nutrition_edit_versions_future_logs_only(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            HEART_HEALTHY_RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]
        old_entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 15),
            meal_category="dinner",
            food_id=int(saved["food_id"]),
            quantity=1,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )

        updated = recipes.update_saved_recipe_nutrition(
            int(saved["saved_recipe_id"]),
            calories=390,
            protein_g=44,
            carbohydrates_g=30,
            fat_g=11,
            fiber_g=7,
            sugar_g=5,
            sodium_mg=480,
        )
        new_entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 16),
            meal_category="dinner",
            food_id=int(saved["food_id"]),
            quantity=1,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )

        self.assertEqual(updated["version_number"], 2)
        self.assertEqual(updated["verification_status"], "estimated")
        self.assertFalse(updated["heart_healthy_pick"])
        self.assertEqual(updated["heart_healthy_reason"], "")
        self.assertEqual(old_entry["calories"], 450)
        self.assertEqual(new_entry["calories"], 390)

    def test_delete_preserves_food_and_logged_history(self) -> None:
        saved = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]
        entry = ledger.add_food_entry(
            entry_date=date(2026, 8, 16),
            meal_category="dinner",
            food_id=int(saved["food_id"]),
            quantity=1,
            logging_source="recipe",
            quantity_is_estimated=True,
            user_confirmed=True,
        )

        deleted = recipes.delete_saved_recipe(
            int(saved["saved_recipe_id"])
        )

        self.assertEqual(deleted["canonical_name"], "Chicken Pepper Bowl")
        self.assertIsNone(
            recipes.get_saved_recipe(int(saved["saved_recipe_id"]))
        )
        self.assertIsNotNone(library.get_food(int(saved["food_id"])))
        history = ledger.list_food_entries(entry_date=date(2026, 8, 16))
        self.assertEqual(history[0]["food_entry_id"], entry["food_entry_id"])
        self.assertEqual(history[0]["calories"], 450)

    def test_rename_rejects_existing_recipe_identity(self) -> None:
        first = recipes.save_pantry_meal_idea(
            RECIPE_IDEA,
            meal_type="dinner",
        )["recipe"]
        second_idea = dict(RECIPE_IDEA)
        second_idea["name"] = "Second Recipe"
        recipes.save_pantry_meal_idea(
            second_idea,
            meal_type="lunch",
        )

        with self.assertRaisesRegex(ValueError, "already uses"):
            recipes.update_saved_recipe(
                int(first["saved_recipe_id"]),
                name="Second Recipe",
            )


if __name__ == "__main__":
    unittest.main()
