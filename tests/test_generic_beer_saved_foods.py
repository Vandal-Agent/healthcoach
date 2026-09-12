from __future__ import annotations

import ast
import re
import sys
import tempfile
import types as module_types
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from google import genai as _google_genai  # noqa: F401
except ImportError:
    google_module = sys.modules.setdefault(
        "google",
        module_types.ModuleType("google"),
    )
    genai_module = module_types.ModuleType("google.genai")
    genai_module.types = module_types.ModuleType("google.genai.types")
    google_module.genai = genai_module
    sys.modules["google.genai"] = genai_module
    sys.modules["google.genai.types"] = genai_module.types

try:
    import requests as _requests  # noqa: F401
except ImportError:
    sys.modules["requests"] = module_types.ModuleType("requests")

from food import database, library, resolver
from food.nutrition_lookup import is_trusted_nutrition_source
from scripts import import_generic_beer_saved_foods as importer


def load_fluid_quantity_resolver():
    tree = ast.parse(Path("app.py").read_text(encoding="utf-8"))
    names = {
        "resolve_packaged_serving_multiplier",
        "resolve_non_restaurant_quantity",
    }
    helpers = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef) and node.name in names
    ]
    future = ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations")],
        level=0,
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[future, *helpers], type_ignores=[])
    )
    namespace = {
        "re": re,
        "get_portion_profile": lambda **kwargs: None,
    }
    exec(compile(module, "app.py", "exec"), namespace)
    return namespace["resolve_non_restaurant_quantity"]


class GenericBeerSavedFoodsTests(unittest.TestCase):
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
            patch.object(resolver, "DATABASE_PATH", self.database_path),
            patch.object(importer, "DATABASE_PATH", self.database_path),
            *[
                patch.object(module, "initialize_database", initialize_test_database)
                for module in (database, library, resolver)
            ],
        ]
        for patcher in self.patchers:
            patcher.start()
        database.initialize_database()

    def tearDown(self) -> None:
        for patcher in reversed(self.patchers):
            patcher.stop()
        self.temporary_directory.cleanup()

    def test_import_is_verified_complete_and_idempotent(self) -> None:
        first = importer.import_beer_defaults()
        second = importer.import_beer_defaults()

        self.assertEqual(first["foods_created"], 3)
        self.assertEqual(second["foods_created"], 0)
        self.assertEqual(second["foods_reused"], 3)
        self.assertEqual(first["alias_conflicts"], [])
        self.assertTrue(is_trusted_nutrition_source("coronausa.com"))
        self.assertTrue(is_trusted_nutrition_source("sierranevada.com"))

        with database.get_connection(self.database_path) as connection:
            foods = connection.execute(
                """
                SELECT canonical_name, food_type, serving_amount, serving_unit
                FROM foods
                ORDER BY food_id
                """
            ).fetchall()
            entries = connection.execute(
                "SELECT COUNT(*) FROM food_entries"
            ).fetchone()[0]
            pantry = connection.execute(
                "SELECT COUNT(*) FROM pantry_items"
            ).fetchone()[0]

        self.assertEqual(len(foods), 3)
        self.assertTrue(all(row["food_type"] == "drink" for row in foods))
        self.assertTrue(all(row["serving_amount"] == 12 for row in foods))
        self.assertTrue(all(row["serving_unit"] == "fl oz" for row in foods))
        self.assertEqual(entries, 0)
        self.assertEqual(pantry, 0)

    def test_common_names_resolve_and_ounces_scale(self) -> None:
        importer.import_beer_defaults()
        resolve_quantity = load_fluid_quantity_resolver()

        cases = (
            ("Mexican lager beer", "Corona Extra Mexican Lager", 148.0),
            ("Mexican logger beer", "Corona Extra Mexican Lager", 148.0),
            ("pale ale beer", "Sierra Nevada Pale Ale", 175.0),
            ("IPA beer", "Sierra Nevada Hop Hunter IPA", 194.0),
        )
        for phrase, expected_name, calories in cases:
            with self.subTest(phrase=phrase):
                result = resolver.resolve_food(food_name=phrase)
                self.assertTrue(result["found"])
                self.assertEqual(
                    result["food"]["canonical_name"],
                    expected_name,
                )
                multiplier = resolve_quantity(
                    food_id=int(result["food"]["food_id"]),
                    quantity=1.0,
                    quantity_description=None,
                    serving_amount=result["food"]["serving_amount"],
                    serving_unit=result["food"]["serving_unit"],
                    size="32 ounces",
                )
                self.assertAlmostEqual(multiplier, 32 / 12)
                self.assertAlmostEqual(
                    float(result["nutrition"]["calories"]) * multiplier,
                    calories * 32 / 12,
                )

    def test_conflicting_alias_is_preserved_not_duplicated(self) -> None:
        existing = library.add_food_with_nutrition(
            canonical_name="Mexican logger beer",
            serving_description="1 serving",
            serving_amount=1.0,
            serving_unit="serving",
            verification_status="verified",
            verification_source="user_entered",
            calories=200.0,
            food_type="drink",
        )

        result = importer.import_beer_defaults()

        self.assertIn("Mexican logger beer", result["alias_conflicts"])
        match = resolver.resolve_food(food_name="Mexican logger beer")
        self.assertEqual(
            match["food"]["food_id"],
            existing["food"]["food_id"],
        )
        corrected = resolver.resolve_food(food_name="Mexican lager beer")
        self.assertEqual(
            corrected["food"]["canonical_name"],
            "Corona Extra Mexican Lager",
        )


if __name__ == "__main__":
    unittest.main()
