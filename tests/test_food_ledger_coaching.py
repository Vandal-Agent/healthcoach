import unittest

from loseit_coaching import (
    build_food_coaching,
    build_food_ledger_coaching_data,
)


class FoodLedgerCoachingTests(unittest.TestCase):
    def test_ledger_foods_drive_coaching_totals(self):
        entries = [
            {
                "canonical_name": "Protein Shake",
                "meal_category": "breakfast",
                "calories": 240,
                "protein_g": 34,
                "carbohydrates_g": 12,
                "fat_g": 4,
                "fiber_g": 3,
                "sugar_g": 5,
                "sodium_mg": 300,
            },
            {
                "canonical_name": "Turkey Bowl",
                "meal_category": "dinner",
                "calories": 568,
                "protein_g": 45.2,
                "carbohydrates_g": 69.2,
                "fat_g": 16.5,
                "fiber_g": 11,
                "sugar_g": 9.2,
                "sodium_mg": 1111,
            },
        ]
        totals = {
            "calories": 808,
            "protein_g": 79.2,
            "carbohydrates_g": 81.2,
            "fat_g": 20.5,
            "fiber_g": 14,
            "sugar_g": 14.2,
            "sodium_mg": 1411,
        }

        food_data = build_food_ledger_coaching_data(
            entries,
            totals,
        )
        message = build_food_coaching(
            total_burn=2600,
            steps=10000,
            food_data=food_data,
        )

        self.assertIn("Calories: 808", message)
        self.assertIn("Protein: 79g", message)
        self.assertIn("Fiber: 14g", message)
        self.assertIn("Estimated deficit: 1792", message)
        self.assertIn("Food entries: 2", message)
        self.assertIn("Turkey Bowl (568 cal)", message)

    def test_empty_ledger_does_not_claim_deficit(self):
        food_data = build_food_ledger_coaching_data(
            [],
            {
                "calories": 0,
                "protein_g": 0,
                "fiber_g": 0,
                "sugar_g": 0,
                "sodium_mg": 0,
            },
        )
        message = build_food_coaching(
            total_burn=2891,
            steps=11468,
            food_data=food_data,
        )

        self.assertIn(
            "Food log: no foods recorded for the day",
            message,
        )
        self.assertNotIn("Estimated deficit", message)
        self.assertNotIn("Protein: 0g", message)
        self.assertNotIn("Fiber: 0g", message)
        self.assertNotIn("Protein was low", message)

    def test_ledger_source_does_not_call_loseit_parser(self):
        food_data = build_food_ledger_coaching_data(
            [
                {
                    "canonical_name": "Apple",
                    "meal_category": "breakfast",
                    "calories": 100,
                    "protein_g": 1,
                    "fiber_g": 4,
                    "sugar_g": 19,
                    "sodium_mg": 0,
                }
            ],
            {
                "calories": 100,
                "protein_g": 1,
                "fiber_g": 4,
                "sugar_g": 19,
                "sodium_mg": 0,
            },
        )

        message = build_food_coaching(
            total_burn=2000,
            food_data=food_data,
        )

        self.assertIn("Calories: 100", message)
        self.assertIn("Food entries: 1", message)


if __name__ == "__main__":
    unittest.main()
