from __future__ import annotations

import unittest
from unittest.mock import patch

from food.usda_provider import (
    barcode_match_key,
    lookup_usda_barcode_nutrition,
    normalize_barcode,
    select_exact_barcode_food,
)


class BarcodeLookupTests(unittest.TestCase):
    def test_normalizes_valid_upc(self):
        self.assertEqual(
            normalize_barcode("036000-291452"),
            "036000291452",
        )

    def test_rejects_invalid_check_digit(self):
        with self.assertRaisesRegex(
            ValueError,
            "check digit",
        ):
            normalize_barcode("036000291453")

    def test_rejects_unsupported_length(self):
        with self.assertRaisesRegex(
            ValueError,
            "8, 12, 13, or 14",
        ):
            normalize_barcode("12345")

    def test_leading_zero_formats_share_match_key(self):
        self.assertEqual(
            barcode_match_key("036000291452"),
            barcode_match_key("0036000291452"),
        )

    def test_selects_newest_exact_branded_match(self):
        foods = [
            {
                "fdcId": 100,
                "dataType": "Branded",
                "gtinUpc": "036000291452",
            },
            {
                "fdcId": 200,
                "dataType": "Branded",
                "gtinUpc": "0036000291452",
            },
            {
                "fdcId": 300,
                "dataType": "Branded",
                "gtinUpc": "012345678905",
            },
        ]

        selected = select_exact_barcode_food(
            foods=foods,
            barcode="036000291452",
        )

        self.assertEqual(selected["fdcId"], 200)

    @patch("food.usda_provider.get_food_record")
    @patch("food.usda_provider.search_foods")
    def test_converts_exact_usda_label(
        self,
        search_mock,
        record_mock,
    ):
        search_mock.return_value = [
            {
                "fdcId": 987,
                "dataType": "Branded",
                "gtinUpc": "036000291452",
            }
        ]

        record_mock.return_value = {
            "fdcId": 987,
            "dataType": "Branded",
            "gtinUpc": "036000291452",
            "description": "TEST GRANOLA BAR",
            "brandName": "Example Brand",
            "servingSize": 40,
            "servingSizeUnit": "g",
            "labelNutrients": {
                "calories": {"value": 180},
                "protein": {"value": 6},
                "carbohydrates": {"value": 28},
                "fat": {"value": 6},
                "fiber": {"value": 4},
                "sugars": {"value": 8},
                "sodium": {"value": 120},
            },
        }

        result = lookup_usda_barcode_nutrition(
            "036000291452"
        )

        self.assertTrue(result["found"])
        self.assertEqual(
            result["food"]["canonical_name"],
            "TEST GRANOLA BAR",
        )
        self.assertEqual(
            result["food"]["serving_description"],
            "40 g",
        )
        self.assertEqual(
            result["nutrition"]["calories"],
            180.0,
        )
        self.assertEqual(
            result["nutrition"]["protein_g"],
            6.0,
        )
        self.assertEqual(
            result["verification"]["source_item_id"],
            "fdc-987",
        )

    @patch("food.usda_provider.search_foods")
    def test_rejects_nonmatching_usda_result(
        self,
        search_mock,
    ):
        search_mock.return_value = [
            {
                "fdcId": 987,
                "dataType": "Branded",
                "gtinUpc": "012345678905",
            }
        ]

        result = lookup_usda_barcode_nutrition(
            "036000291452"
        )

        self.assertFalse(result["found"])


    @patch("food.usda_provider.get_food_record")
    @patch("food.usda_provider.search_foods")
    def test_scales_verified_per_100g_when_label_is_incomplete(
        self,
        search_mock,
        record_mock,
    ):
        search_mock.return_value = [
            {
                "fdcId": 2628268,
                "dataType": "Branded",
                "gtinUpc": "819009020007",
                "servingSize": 0.8,
                "servingSizeUnit": "GRM",
                "householdServingFullText": "1/4 tsp",
            }
        ]

        record_mock.return_value = {
            "fdcId": 2628268,
            "dataType": "Branded",
            "gtinUpc": "819009020007",
            "description": "ORIGINAL MIXED-UP SALT",
            "brandName": "JANE'S KRAZY",
            "servingSize": None,
            "servingSizeUnit": "GRM",
            "householdServingFullText": "1/4 tsp",
            "labelNutrients": {},
            "foodNutrients": [
                {
                    "nutrient": {
                        "name": "Energy",
                        "unitName": "kcal",
                    },
                    "amount": 0,
                },
                {
                    "nutrient": {
                        "name": "Sodium, Na",
                        "unitName": "mg",
                    },
                    "amount": 32500,
                },
                {
                    "nutrient": {
                        "name": "Protein",
                        "unitName": "g",
                    },
                    "amount": 0,
                },
            ],
        }

        result = lookup_usda_barcode_nutrition(
            "819009020007"
        )

        self.assertTrue(result["found"])
        self.assertEqual(
            result["food"]["serving_description"],
            "1/4 tsp (0.8 g)",
        )
        self.assertEqual(
            result["nutrition"]["calories"],
            0.0,
        )
        self.assertEqual(
            result["nutrition"]["sodium_mg"],
            260.0,
        )
        self.assertTrue(
            any(
                "per-100 g" in note
                for note in result["notes"]
            )
        )


if __name__ == "__main__":
    unittest.main()
