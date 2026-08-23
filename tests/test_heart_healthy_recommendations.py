from __future__ import annotations

import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

from food.heart_guidance import (
    sanitize_optional_heart_healthy_picks,
)
from food.menu_photo_advisor import (
    MenuPhotoAnalysis,
    MenuPhotoCandidate,
    analyze_menu_photo,
    format_menu_photo_analysis,
)

app.CHAT_ID = None


def recommendation(
    name: str,
    *,
    selected: bool = False,
    reason: str | None = None,
) -> dict:
    return {
        "item_name": name,
        "nutrition_status": "official",
        "calories": 450,
        "protein_g": 35,
        "recommendation_reason": "Grilled entrée with vegetables.",
        "heart_healthy_pick": selected,
        "heart_healthy_reason": reason,
        "source_title": "Official Menu",
        "source_url": "https://restaurant.example/menu",
    }


def photo_candidate(
    name: str,
    *,
    selected: bool = False,
    reason: str | None = None,
) -> MenuPhotoCandidate:
    return MenuPhotoCandidate(
        item_name=name,
        printed_price="$15",
        printed_calories=None,
        visible_details="Grilled fish, beans, and vegetables",
        recommendation_reason="Visible grilled protein and vegetables.",
        heart_healthy_pick=selected,
        heart_healthy_reason=reason,
    )


class HeartHealthyRecommendationTests(unittest.TestCase):
    def test_shared_validator_allows_no_pick(self) -> None:
        candidates, status = sanitize_optional_heart_healthy_picks(
            [recommendation("Choice A")]
        )

        self.assertEqual(status, "none")
        self.assertFalse(candidates[0]["heart_healthy_pick"])

    def test_online_restaurant_formatter_labels_exact_pick(self) -> None:
        advice = {
            "found": True,
            "restaurant_display_name": "Example Restaurant",
            "candidates": [
                recommendation("Choice A"),
                recommendation(
                    "Grilled Fish Plate",
                    selected=True,
                    reason=(
                        "Grilled fish and vegetables are supported by "
                        "the cited menu; sodium was not published."
                    ),
                ),
            ],
        }

        message = app.format_restaurant_advice(advice)

        self.assertIn(
            "2. Grilled Fish Plate — Heart-Healthy Pick",
            message,
        )
        self.assertIn("Heart-healthy note: Grilled fish", message)
        self.assertNotIn(
            "1. Choice A — Heart-Healthy Pick",
            message,
        )
        self.assertIn("not a medical rating", message)

    def test_online_formatter_explains_when_no_pick_is_supported(self):
        message = app.format_restaurant_advice({
            "found": True,
            "restaurant_display_name": "Example Restaurant",
            "candidates": [recommendation("Choice A")],
        })

        self.assertIn(
            "No Heart-Healthy Pick was assigned",
            message,
        )

    def test_menu_photo_formatter_labels_exact_visible_pick(self):
        result = {
            "readable": True,
            "restaurant_name": "Photo Restaurant",
            "candidates": [
                photo_candidate("Choice A").model_dump(),
                photo_candidate(
                    "Grilled Fish Plate",
                    selected=True,
                    reason=(
                        "The photo lists grilled fish, beans, and "
                        "vegetables; sodium is not visible."
                    ),
                ).model_dump(),
            ],
        }

        message = format_menu_photo_analysis(result)

        self.assertIn(
            "2. Grilled Fish Plate — Heart-Healthy Pick",
            message,
        )
        self.assertIn("Heart-healthy note: The photo lists", message)
        self.assertIn("based only on visible menu details", message)

    def test_menu_photo_analysis_removes_multiple_model_picks(self):
        parsed = MenuPhotoAnalysis(
            readable=True,
            restaurant_name="Photo Restaurant",
            candidates=[
                photo_candidate(
                    "Fish Plate",
                    selected=True,
                    reason="Visible fish and vegetables.",
                ),
                photo_candidate(
                    "Bean Bowl",
                    selected=True,
                    reason="Visible beans and vegetables.",
                ),
            ],
        )
        with patch(
            "food.menu_photo_advisor.get_client"
        ) as client_patch:
            client = client_patch.return_value
            client.models.generate_content.return_value.parsed = parsed
            client.models.generate_content.return_value.text = None

            result = analyze_menu_photo(
                b"photo",
                mime_type="image/jpeg",
            )

        self.assertFalse(
            any(
                item["heart_healthy_pick"]
                for item in result["candidates"]
            )
        )
        self.assertIn(
            "ambiguous Heart-Healthy Pick",
            result["notes"][-1],
        )
        client.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
