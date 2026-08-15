from __future__ import annotations

import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from food.menu_photo_advisor import read_nutrition_label_photo


class NutritionLabelPhotoTests(unittest.TestCase):
    def read_result(self, parsed):
        client = Mock()
        client.models.generate_content.return_value = (
            SimpleNamespace(parsed=parsed, text=None)
        )

        with (
            patch(
                "food.menu_photo_advisor.get_client",
                return_value=client,
            ),
            patch(
                "food.menu_photo_advisor.types.Part.from_bytes",
                return_value=object(),
            ),
        ):
            result = read_nutrition_label_photo(
                b"image",
                mime_type="image/jpeg",
            )

        client.close.assert_called_once_with()
        return result

    def test_reads_complete_printed_serving(self) -> None:
        result = self.read_result({
            "readable": True,
            "product_name": "Test Crackers",
            "brand": "Test Brand",
            "serving_description": "5 crackers (30 g)",
            "serving_amount": 30,
            "serving_unit": "g",
            "calories": 140,
            "protein_g": 3,
            "carbohydrates_g": 22,
            "fat_g": 5,
            "fiber_g": 1,
            "sugar_g": 2,
            "sodium_mg": 210,
            "notes": [],
        })

        self.assertTrue(result["readable"])
        self.assertEqual(result["calories"], 140)
        self.assertEqual(result["sodium_mg"], 210)

    def test_rejects_incomplete_label_as_readable(self) -> None:
        result = self.read_result({
            "readable": True,
            "serving_description": "1 cup (40 g)",
            "serving_amount": 40,
            "serving_unit": "g",
            "calories": 150,
            "protein_g": 4,
            "carbohydrates_g": 30,
            "fat_g": 2,
            "fiber_g": 3,
            "sugar_g": None,
            "sodium_mg": 180,
            "notes": [],
        })

        self.assertFalse(result["readable"])
        self.assertIn("sugar_g", result["notes"][-1])


if __name__ == "__main__":
    unittest.main()
