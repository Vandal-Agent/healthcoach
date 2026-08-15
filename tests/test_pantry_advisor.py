from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from food.pantry_advisor import (
    generate_pantry_meal_ideas,
    scale_pantry_meal_nutrition,
    validate_pantry_meal_ideas,
)


def pantry_items() -> list[dict]:
    return [
        {"display_name": "Chicken breast"},
        {"display_name": "black olives"},
        {"display_name": "green peppers"},
    ]


def idea(
    name: str,
    *,
    calories: float = 450,
    additional: int = 1,
) -> dict:
    ingredients = [
        {
            "name": "Chicken breast",
            "amount": "4 ounces",
            "source": "pantry",
        }
    ]
    ingredients.extend(
        {
            "name": f"extra {index}",
            "amount": "1 serving",
            "source": "additional",
        }
        for index in range(additional)
    )
    return {
        "name": name,
        "summary": "A balanced bowl.",
        "ingredients": ingredients,
        "preparation_steps": ["Cook and serve."],
        "calories": calories,
        "protein_g": 40,
        "carbohydrates_g": 35,
        "fat_g": 14,
        "fiber_g": 7,
        "sugar_g": 5,
        "sodium_mg": 500,
        "daily_fit": "Adds protein and fiber.",
        "estimate_notes": "Portions are estimated.",
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


class PantryAdvisorTests(unittest.TestCase):
    def test_accepts_three_grounded_lunch_ideas(self) -> None:
        ideas = [idea("Idea A"), idea("Idea B"), idea("Idea C")]

        result = validate_pantry_meal_ideas(
            ideas,
            pantry_items=pantry_items(),
            meal_type="lunch",
        )

        self.assertEqual(result, ideas)

    def test_rejects_calories_over_meal_limit(self) -> None:
        ideas = [
            idea("Idea A", calories=501),
            idea("Idea B"),
            idea("Idea C"),
        ]

        with self.assertRaisesRegex(ValueError, "calorie limit"):
            validate_pantry_meal_ideas(
                ideas,
                pantry_items=pantry_items(),
                meal_type="lunch",
            )

    def test_rejects_more_than_two_additional_ingredients(self) -> None:
        ideas = [
            idea("Idea A", additional=3),
            idea("Idea B"),
            idea("Idea C"),
        ]

        with self.assertRaisesRegex(ValueError, "more than two"):
            validate_pantry_meal_ideas(
                ideas,
                pantry_items=pantry_items(),
                meal_type="dinner",
            )

    def test_rejects_claimed_pantry_item_not_available(self) -> None:
        ideas = [idea("Idea A"), idea("Idea B"), idea("Idea C")]
        ideas[0]["ingredients"][0]["name"] = "salmon"

        with self.assertRaisesRegex(ValueError, "unavailable"):
            validate_pantry_meal_ideas(
                ideas,
                pantry_items=pantry_items(),
                meal_type="dinner",
            )

    def test_generator_includes_daily_totals_and_closes_client(self) -> None:
        parsed = {
            "ideas": [idea("Idea A"), idea("Idea B"), idea("Idea C")]
        }
        client = FakeClient(parsed)

        with patch(
            "food.pantry_advisor.get_client",
            return_value=client,
        ):
            result = generate_pantry_meal_ideas(
                pantry_items=pantry_items(),
                meal_type="lunch",
                daily_totals={"calories": 700, "protein_g": 35},
            )

        self.assertEqual(len(result), 3)
        self.assertTrue(client.closed)
        prompt = client.models.kwargs["contents"]
        self.assertIn('"calories": 700', prompt)
        self.assertIn('"protein_g": 35', prompt)
        self.assertIn("at or below\n   500 calories", prompt)

    def test_scales_all_nutrients_by_servings(self) -> None:
        result = scale_pantry_meal_nutrition(
            idea("Idea A"),
            servings=1.5,
        )

        self.assertEqual(result["calories"], 675)
        self.assertEqual(result["protein_g"], 60)
        self.assertEqual(result["fiber_g"], 10.5)


if __name__ == "__main__":
    unittest.main()
