from __future__ import annotations

import unittest
from unittest.mock import patch

from food.restaurant_advisor import (
    RestaurantAdvice,
    RestaurantCandidate,
    recommend_restaurant_entrees,
)


def candidate(
    *,
    name: str = "Grilled Chicken Plate",
    url: str = "https://restaurant.example/menu",
    title: str = "Restaurant Menu",
    status: str = "official",
    calories: float | None = 520,
    protein: float | None = 42,
    heart_healthy_pick: bool = False,
    heart_healthy_reason: str | None = None,
) -> RestaurantCandidate:
    return RestaurantCandidate(
        item_name=name,
        calories=calories,
        protein_g=protein,
        nutrition_status=status,
        recommendation_reason="Protein-forward grilled entrée.",
        heart_healthy_pick=heart_healthy_pick,
        heart_healthy_reason=heart_healthy_reason,
        source_title=title,
        source_url=url,
    )


class RestaurantAdvisorTests(unittest.TestCase):
    def run_advisor(
        self,
        structured: RestaurantAdvice,
        *,
        citations=None,
    ):
        cited = citations or [
            {
                "title": "Restaurant Menu",
                "url": "https://restaurant.example/menu",
            }
        ]
        with (
            patch(
                "food.restaurant_advisor.get_client"
            ) as client_patch,
            patch(
                "food.restaurant_advisor.run_restaurant_menu_search",
                return_value=("cited report", cited),
            ) as search_patch,
            patch(
                "food.restaurant_advisor.structure_restaurant_advice",
                return_value=structured,
            ) as structure_patch,
        ):
            result = recommend_restaurant_entrees(
                "Example Restaurant in Redding, California"
            )

        client = client_patch.return_value
        search_patch.assert_called_once_with(
            "Example Restaurant in Redding, California",
            client=client,
        )
        self.assertIs(
            structure_patch.call_args.kwargs["client"],
            client,
        )
        client.close.assert_called_once_with()
        return result

    def test_keeps_candidate_with_matching_citation(self) -> None:
        structured = RestaurantAdvice(
            found=True,
            restaurant_display_name="Example Restaurant",
            candidates=[candidate()],
        )

        result = self.run_advisor(structured)

        self.assertTrue(result["found"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(
            result["candidates"][0]["item_name"],
            "Grilled Chicken Plate",
        )
        self.assertEqual(
            result["candidates"][0]["nutrition_status"],
            "official",
        )

    def test_rejects_candidate_without_matching_citation(self) -> None:
        structured = RestaurantAdvice(
            found=True,
            restaurant_display_name="Example Restaurant",
            candidates=[
                candidate(url="https://untrusted.example/item")
            ],
        )

        result = self.run_advisor(structured)

        self.assertFalse(result["found"])
        self.assertEqual(result["candidates"], [])
        self.assertIn(
            "Removed recommendations without valid cited support",
            result["notes"][-1],
        )

    def test_unpublished_nutrition_must_remain_null(self) -> None:
        valid = candidate(
            name="Vegetable and Chicken Bowl",
            status="not_published",
            calories=None,
            protein=None,
        )
        invalid = candidate(
            name="Invented Nutrition Bowl",
            status="not_published",
            calories=450,
            protein=35,
        )
        structured = RestaurantAdvice(
            found=True,
            restaurant_display_name="Example Restaurant",
            candidates=[valid, invalid],
        )

        result = self.run_advisor(structured)

        self.assertTrue(result["found"])
        self.assertEqual(len(result["candidates"]), 1)
        self.assertEqual(
            result["candidates"][0]["item_name"],
            "Vegetable and Chicken Bowl",
        )

    def test_preserves_one_explained_heart_healthy_pick(self) -> None:
        structured = RestaurantAdvice(
            found=True,
            restaurant_display_name="Example Restaurant",
            candidates=[
                candidate(
                    name="Grilled Fish and Vegetable Plate",
                    heart_healthy_pick=True,
                    heart_healthy_reason=(
                        "Grilled fish with visible vegetables; sodium "
                        "was not published."
                    ),
                ),
                candidate(name="Chicken Bowl"),
            ],
        )

        result = self.run_advisor(structured)

        self.assertTrue(
            result["candidates"][0]["heart_healthy_pick"]
        )
        self.assertIn(
            "visible vegetables",
            result["candidates"][0]["heart_healthy_reason"],
        )
        self.assertFalse(
            result["candidates"][1]["heart_healthy_pick"]
        )

    def test_removes_ambiguous_multiple_picks(self) -> None:
        structured = RestaurantAdvice(
            found=True,
            restaurant_display_name="Example Restaurant",
            candidates=[
                candidate(
                    name="Fish Plate",
                    heart_healthy_pick=True,
                    heart_healthy_reason="Fish and vegetables.",
                ),
                candidate(
                    name="Bean Bowl",
                    heart_healthy_pick=True,
                    heart_healthy_reason="Beans and vegetables.",
                ),
            ],
        )

        result = self.run_advisor(structured)

        self.assertFalse(
            any(
                item["heart_healthy_pick"]
                for item in result["candidates"]
            )
        )
        self.assertIn(
            "Removed an ambiguous Heart-Healthy Pick",
            result["notes"][-1],
        )

    def test_removes_unexplained_pick(self) -> None:
        structured = RestaurantAdvice(
            found=True,
            restaurant_display_name="Example Restaurant",
            candidates=[
                candidate(
                    heart_healthy_pick=True,
                    heart_healthy_reason=None,
                )
            ],
        )

        result = self.run_advisor(structured)

        self.assertFalse(
            result["candidates"][0]["heart_healthy_pick"]
        )
        self.assertIsNone(
            result["candidates"][0]["heart_healthy_reason"]
        )


if __name__ == "__main__":
    unittest.main()
