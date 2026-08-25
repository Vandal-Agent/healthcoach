import unittest

from food import usda_provider


class UsdaProviderTests(unittest.TestCase):
    def test_onion_plural_normalizes_for_generic_matching(self) -> None:
        self.assertEqual(usda_provider.normalized_tokens("onions"), {"onion"})

    def test_multiword_size_matches_one_exact_portion(self) -> None:
        medium = {
            "amount": 1.0,
            "modifier": "medium",
            "portionDescription": "",
            "gramWeight": 110.0,
        }
        record = {
            "foodPortions": [
                medium,
                {
                    "amount": 1.0,
                    "modifier": 'slice, medium (1/8" thick)',
                    "portionDescription": "",
                    "gramWeight": 14.0,
                },
                {
                    "amount": 1.0,
                    "modifier": "large",
                    "portionDescription": "",
                    "gramWeight": 150.0,
                },
            ]
        }

        self.assertEqual(
            usda_provider.find_portion(record=record, size="1 medium"),
            medium,
        )

    def test_ambiguous_multiword_portion_is_rejected(self) -> None:
        record = {
            "foodPortions": [
                {
                    "amount": 1.0,
                    "modifier": "cup chopped",
                    "portionDescription": "",
                    "gramWeight": 160.0,
                },
                {
                    "amount": 1.0,
                    "modifier": "cup sliced",
                    "portionDescription": "",
                    "gramWeight": 115.0,
                },
            ]
        }

        self.assertIsNone(
            usda_provider.find_portion(record=record, size="1 cup")
        )


if __name__ == "__main__":
    unittest.main()
