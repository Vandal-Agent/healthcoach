from __future__ import annotations

import ast
import re
import unittest
from pathlib import Path


def load_quantity_resolver(converter):
    tree = ast.parse(Path("app.py").read_text())
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "resolve_non_restaurant_quantity"
    )
    future = ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations")],
        level=0,
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[future, helper], type_ignores=[])
    )
    namespace = {
        "re": re,
        "resolve_packaged_serving_multiplier": converter,
    }
    exec(compile(module, "app.py", "exec"), namespace)
    return namespace["resolve_non_restaurant_quantity"]


class NonRestaurantQuantityTests(unittest.TestCase):
    def setUp(self) -> None:
        self.conversion_calls = []

        def converter(**kwargs):
            self.conversion_calls.append(kwargs)
            return 9.5

        self.resolve = load_quantity_resolver(converter)
        self.common = {
            "food_id": 12,
            "serving_amount": 1,
            "serving_unit": "item",
        }

    def resolve_quantity(self, quantity, description):
        return self.resolve(
            quantity=quantity,
            quantity_description=description,
            **self.common,
        )

    def test_plain_homemade_item_uses_numeric_count(self) -> None:
        result = self.resolve_quantity(1, None)
        self.assertEqual(result, 1)
        self.assertEqual(self.conversion_calls, [])

    def test_plain_banana_count_uses_numeric_quantity(self) -> None:
        result = self.resolve_quantity(2, "2 bananas")
        self.assertEqual(result, 2)
        self.assertEqual(self.conversion_calls, [])

    def test_missing_plain_quantity_defaults_to_one(self) -> None:
        result = self.resolve_quantity(None, "homemade protein bar")
        self.assertEqual(result, 1)
        self.assertEqual(self.conversion_calls, [])

    def test_explicit_portions_use_converter(self) -> None:
        descriptions = (
            "4 ounces",
            "113 grams",
            "1 serving",
            "2 servings",
            "a handful",
        )

        for description in descriptions:
            with self.subTest(description=description):
                self.conversion_calls.clear()
                result = self.resolve_quantity(2, description)
                self.assertEqual(result, 9.5)
                self.assertEqual(len(self.conversion_calls), 1)
                self.assertEqual(
                    self.conversion_calls[0]["quantity_description"],
                    description,
                )




def load_packaged_resolver():
    tree = ast.parse(Path("app.py").read_text())
    helper = next(
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name == "resolve_packaged_serving_multiplier"
    )
    future = ast.ImportFrom(
        module="__future__",
        names=[ast.alias(name="annotations")],
        level=0,
    )
    module = ast.fix_missing_locations(
        ast.Module(body=[future, helper], type_ignores=[])
    )
    namespace = {
        "re": re,
        "get_portion_profile": lambda **kwargs: None,
    }
    exec(compile(module, "app.py", "exec"), namespace)
    return namespace["resolve_packaged_serving_multiplier"]


class FluidOunceQuantityTests(unittest.TestCase):
    def test_separate_quantity_and_ounce_unit_scale_normally(self) -> None:
        resolver = load_packaged_resolver()

        result = resolver(
            food_id=26,
            quantity=4,
            quantity_description="ounces",
            serving_amount=4,
            serving_unit="ounces",
        )

        self.assertAlmostEqual(result, 1.0)

    def test_separate_quantity_and_gram_unit_scale_normally(self) -> None:
        resolver = load_packaged_resolver()

        result = resolver(
            food_id=26,
            quantity=113,
            quantity_description="grams",
            serving_amount=113,
            serving_unit="grams",
        )

        self.assertAlmostEqual(result, 1.0)

    def test_separate_quantity_and_serving_unit_scale_normally(self) -> None:
        resolver = load_packaged_resolver()

        result = resolver(
            food_id=26,
            quantity=1,
            quantity_description="serving",
            serving_amount=4,
            serving_unit="ounces",
        )

        self.assertAlmostEqual(result, 1.0)

    def test_bottle_size_routes_as_fluid_ounces(self) -> None:
        calls = []

        def converter(**kwargs):
            calls.append(kwargs)
            return 16.9 / 12

        resolver = load_quantity_resolver(converter)

        result = resolver(
            food_id=25,
            quantity=1,
            quantity_description="bottle",
            serving_amount=12,
            serving_unit="fl oz",
            size="16.9 ounces",
        )

        self.assertAlmostEqual(result, 16.9 / 12)
        self.assertEqual(
            calls[0]["quantity_description"],
            "16.9 oz",
        )

    def test_fluid_ounces_scale_from_base_serving(self) -> None:
        resolver = load_packaged_resolver()

        result = resolver(
            food_id=25,
            quantity=1,
            quantity_description="16.9 oz",
            serving_amount=12,
            serving_unit="fl oz",
        )

        self.assertAlmostEqual(result, 16.9 / 12)

    def test_food_weight_ounces_still_scale_normally(self) -> None:
        resolver = load_packaged_resolver()

        result = resolver(
            food_id=26,
            quantity=1,
            quantity_description="4 oz",
            serving_amount=4,
            serving_unit="oz",
        )

        self.assertAlmostEqual(result, 1.0)


if __name__ == "__main__":
    unittest.main()
