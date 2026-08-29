from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import patch

from food.pantry_advisor import (
    build_pantry_goal_context,
    generate_pantry_meal_ideas,
    pantry_goal_fit_text,
    pantry_nutrition_basis_text,
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
    heart_healthy_pick: bool = False,
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
        "heart_healthy_pick": heart_healthy_pick,
        "heart_healthy_reason": (
            "Lean protein, vegetables, fiber, and moderate sodium."
            if heart_healthy_pick
            else None
        ),
    }


def valid_ideas() -> list[dict]:
    return [
        idea("Idea A", heart_healthy_pick=True),
        idea("Idea B"),
        idea("Idea C"),
    ]


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
        ideas = valid_ideas()

        result = validate_pantry_meal_ideas(
            ideas,
            pantry_items=pantry_items(),
            meal_type="lunch",
        )

        self.assertEqual(result, ideas)

    def test_rejects_calories_over_meal_limit(self) -> None:
        ideas = [
            idea("Idea A", calories=501, heart_healthy_pick=True),
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
            idea("Idea A", additional=3, heart_healthy_pick=True),
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
        ideas = valid_ideas()
        ideas[0]["ingredients"][0]["name"] = "salmon"

        with self.assertRaisesRegex(ValueError, "unavailable"):
            validate_pantry_meal_ideas(
                ideas,
                pantry_items=pantry_items(),
                meal_type="dinner",
            )

    def test_generator_includes_daily_totals_and_closes_client(self) -> None:
        parsed = {
            "ideas": [idea("Idea A"), idea("Idea B"), idea("Idea C")],
            "heart_healthy_pick": 2,
            "heart_healthy_reason": (
                "Uses lean protein, vegetables, and moderate sodium."
            ),
        }
        client = FakeClient(parsed)

        with patch(
            "food.pantry_advisor.get_client",
            return_value=client,
        ):
            goal_context = build_pantry_goal_context(
                daily_totals={"calories": 700},
                saved_goal={
                    "calorie_target_low": 1800,
                    "calorie_target_high": 1950,
                    "calculation_date": "2026-08-28",
                },
            )
            result = generate_pantry_meal_ideas(
                pantry_items=pantry_items(),
                meal_type="lunch",
                daily_totals={"calories": 700, "protein_g": 35},
                goal_context=goal_context,
            )

        self.assertEqual(len(result), 3)
        self.assertTrue(client.closed)
        prompt = client.models.kwargs["contents"]
        self.assertIn('"calories": 700', prompt)
        self.assertIn('"protein_g": 35', prompt)
        self.assertIn('"saved_target_low": 1800.0', prompt)
        self.assertIn("at or below\n   500 calories", prompt)
        self.assertIn("Select exactly one", prompt)
        self.assertFalse(result[0]["heart_healthy_pick"])
        self.assertTrue(result[1]["heart_healthy_pick"])
        self.assertIn("lean protein", result[1]["heart_healthy_reason"])
        self.assertFalse(result[2]["heart_healthy_pick"])
        self.assertIn(
            "bring today's logged total to about 1150",
            result[0]["goal_fit"],
        )
        self.assertIn(
            "No linked Pantry nutrition",
            result[0]["nutrition_basis"],
        )

    def test_builds_goal_context_without_recalculating_target(self) -> None:
        context = build_pantry_goal_context(
            daily_totals={"calories": 825},
            saved_goal={
                "calorie_target_low": 1800,
                "calorie_target_high": 1950,
                "calculation_date": "2026-08-20",
            },
            missing_calorie_items=2,
        )

        self.assertEqual(context["remaining_to_low"], 975)
        self.assertEqual(context["remaining_to_high"], 1125)
        self.assertEqual(context["status"], "below")
        self.assertEqual(context["calculation_date"], "2026-08-20")
        self.assertEqual(context["missing_calorie_items"], 2)

    def test_goal_fit_uses_exact_saved_range_math(self) -> None:
        context = build_pantry_goal_context(
            daily_totals={"calories": 1450},
            saved_goal={
                "calorie_target_low": 1800,
                "calorie_target_high": 1950,
            },
        )

        within = pantry_goal_fit_text(400, context)
        above = pantry_goal_fit_text(600, context)

        self.assertIn("1850, within the saved 1800-1950 range", within)
        self.assertIn("2050, around 100 above", above)

    def test_goal_context_is_optional(self) -> None:
        self.assertIsNone(
            build_pantry_goal_context(
                daily_totals={"calories": 500},
                saved_goal=None,
            )
        )
        self.assertIsNone(pantry_goal_fit_text(450, None))

    def test_nutrition_basis_reports_linked_pantry_coverage(self) -> None:
        meal = idea("Linked meal")
        meal["ingredients"].append(
            {
                "name": "black olives",
                "amount": "1 serving",
                "source": "pantry",
            }
        )
        items = [
            {
                "display_name": "Chicken breast",
                "nutrition_version_id": 8,
                "calories": 120,
            },
            {"display_name": "black olives"},
        ]

        result = pantry_nutrition_basis_text(
            meal,
            pantry_items=items,
        )

        self.assertIn("1 of 2 Pantry ingredients", result)

    def test_rejects_missing_heart_healthy_pick(self) -> None:
        ideas = [idea("Idea A"), idea("Idea B"), idea("Idea C")]

        with self.assertRaisesRegex(ValueError, "Exactly one"):
            validate_pantry_meal_ideas(
                ideas,
                pantry_items=pantry_items(),
                meal_type="lunch",
            )

    def test_rejects_multiple_heart_healthy_picks(self) -> None:
        ideas = [
            idea("Idea A", heart_healthy_pick=True),
            idea("Idea B", heart_healthy_pick=True),
            idea("Idea C"),
        ]

        with self.assertRaisesRegex(ValueError, "Exactly one"):
            validate_pantry_meal_ideas(
                ideas,
                pantry_items=pantry_items(),
                meal_type="dinner",
            )

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
