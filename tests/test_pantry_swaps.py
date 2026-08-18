from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from food.pantry_advisor import (
    generate_smart_pantry_swaps,
    validate_pantry_swaps,
)


def pantry_items() -> list[dict]:
    return [
        {"display_name": "Chicken breast"},
        {
            "display_name": "Regular mayonnaise",
            "serving_description": "1 tablespoon",
            "calories": 100,
            "fat_g": 11,
            "sodium_mg": 90,
        },
    ]


def swap(*, item_name: str = "Regular mayonnaise") -> dict:
    return {
        "pantry_item_name": item_name,
        "suggested_replacement": (
            "Plain nonfat Greek yogurt or a lighter mayonnaise"
        ),
        "why_it_helps": (
            "This can reduce calorie and saturated-fat density while "
            "keeping a creamy texture."
        ),
        "shopping_tip": (
            "Compare saturated fat, sodium, and added sugar per tablespoon."
        ),
        "heart_health_note": (
            "Choosing less saturated fat supports a heart-healthy pattern."
        ),
        "evidence_basis": "known_nutrition",
    }


class FakeModels:
    def __init__(self, parsed: dict) -> None:
        self.parsed = parsed
        self.kwargs = None

    def generate_content(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(parsed=self.parsed, text=None)


class FakeClient:
    def __init__(self, parsed: dict) -> None:
        self.models = FakeModels(parsed)
        self.closed = False

    def close(self) -> None:
        self.closed = True


class PantrySwapTests(unittest.TestCase):
    def test_accepts_grounded_swap_with_known_nutrition(self) -> None:
        swaps = [swap()]

        result = validate_pantry_swaps(
            swaps,
            pantry_items=pantry_items(),
        )

        self.assertEqual(result, swaps)

    def test_accepts_no_forced_swaps(self) -> None:
        self.assertEqual(
            validate_pantry_swaps([], pantry_items=pantry_items()),
            [],
        )

    def test_rejects_unavailable_pantry_item(self) -> None:
        with self.assertRaisesRegex(ValueError, "unavailable"):
            validate_pantry_swaps(
                [swap(item_name="Butter")],
                pantry_items=pantry_items(),
            )

    def test_rejects_claimed_nutrition_when_item_has_none(self) -> None:
        proposed = swap(item_name="Chicken breast")

        with self.assertRaisesRegex(ValueError, "unavailable"):
            validate_pantry_swaps(
                [proposed],
                pantry_items=pantry_items(),
            )

    def test_generator_uses_pantry_data_and_closes_client(self) -> None:
        parsed = {"swaps": [swap()]}
        client = FakeClient(parsed)

        with patch(
            "food.pantry_advisor.get_client",
            return_value=client,
        ):
            result = generate_smart_pantry_swaps(
                pantry_items=pantry_items(),
            )

        self.assertEqual(len(result), 1)
        self.assertTrue(client.closed)
        prompt = client.models.kwargs["contents"]
        self.assertIn("Regular mayonnaise", prompt)
        self.assertIn('"fat_g": 11', prompt)
        self.assertIn("Return fewer than three, or none", prompt)
        self.assertIn("Do not diagnose", prompt)


if __name__ == "__main__":
    unittest.main()
