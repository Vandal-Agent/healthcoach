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


if __name__ == "__main__":
    unittest.main()
