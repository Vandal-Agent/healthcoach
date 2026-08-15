from __future__ import annotations

import unittest
from unittest.mock import Mock, patch

from food.barcode_provider import (
    lookup_barcode_nutrition,
    lookup_open_food_facts_barcode_nutrition,
)


class BarcodeProviderTests(unittest.TestCase):
    @patch("food.barcode_provider.requests.get")
    def test_accepts_equivalent_gtin_with_serving_data(
        self,
        mock_get,
    ):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "code": "0034000052004",
            "product": {
                "product_name": "Unsweetened Cocoa",
                "brands": "Test Brand",
                "serving_size": "1 tbsp (5g)",
                "nutriments": {
                    "energy-kcal_serving": 10,
                    "proteins_serving": 1,
                    "carbohydrates_serving": 3,
                    "fat_serving": 0.5,
                    "fiber_serving": 2,
                    "sugars_serving": 0,
                    "sodium_serving": 0,
                },
            },
        }
        mock_get.return_value = response

        result = (
            lookup_open_food_facts_barcode_nutrition(
                "034000052004"
            )
        )

        self.assertTrue(result["found"])
        self.assertEqual(
            result["provider"],
            "open_food_facts",
        )
        self.assertEqual(
            result["food"]["serving_description"],
            "1 tbsp (5g)",
        )
        self.assertEqual(
            result["nutrition"]["calories"],
            10,
        )
        self.assertEqual(
            result["nutrition"]["fiber_g"],
            2,
        )

    @patch("food.barcode_provider.requests.get")
    def test_rejects_product_without_serving_size(
        self,
        mock_get,
    ):
        response = Mock()
        response.raise_for_status.return_value = None
        response.json.return_value = {
            "code": "0038024300264",
            "product": {
                "product_name": "Baking Spray",
                "brands": "Test Brand",
                "serving_size": None,
                "nutriments": {
                    "energy-kcal_100g": 0,
                    "proteins_100g": 0,
                    "carbohydrates_100g": 0,
                    "fat_100g": 0,
                },
            },
        }
        mock_get.return_value = response

        result = (
            lookup_open_food_facts_barcode_nutrition(
                "038024300264"
            )
        )

        self.assertFalse(result["found"])
        self.assertIn(
            "serving_size",
            result["missing_fields"],
        )

    @patch(
        "food.barcode_provider."
        "lookup_open_food_facts_barcode_nutrition"
    )
    @patch(
        "food.barcode_provider."
        "lookup_usda_barcode_nutrition"
    )
    def test_uses_secondary_provider_after_usda_miss(
        self,
        mock_usda,
        mock_open_food_facts,
    ):
        mock_usda.return_value = {
            "found": False,
            "notes": ["Not found in USDA."],
        }
        mock_open_food_facts.return_value = {
            "found": True,
            "provider": "open_food_facts",
        }

        result = lookup_barcode_nutrition(
            "034000052004"
        )

        self.assertTrue(result["found"])
        self.assertEqual(
            result["provider"],
            "open_food_facts",
        )
        mock_open_food_facts.assert_called_once_with(
            "034000052004"
        )


if __name__ == "__main__":
    unittest.main()
