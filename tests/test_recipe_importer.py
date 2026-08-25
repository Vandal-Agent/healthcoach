import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from food import recipe_importer


class RecipeImporterTests(unittest.TestCase):
    def test_gemini_schema_has_no_unsupported_exclusive_minimum(self) -> None:
        schema = recipe_importer.ImportedRecipeDraft.model_json_schema()

        self.assertNotIn(
            "exclusiveMinimum",
            schema["properties"]["yield_servings"],
        )

    def test_text_import_extracts_structure_without_nutrition(self) -> None:
        response = SimpleNamespace(
            parsed={
                "readable": True,
                "recipe_name": "Chicken Bowl",
                "meal_type": "dinner",
                "yield_servings": 4,
                "summary": "A simple bowl.",
                "ingredients": [
                    {
                        "ingredient_name": "chicken breast",
                        "amount_description": "16 oz",
                        "brand": None,
                        "optional": False,
                        "trace_only": False,
                    }
                ],
                "preparation_steps": ["Cook the chicken."],
                "notes": [],
            },
            text=None,
        )
        client = Mock()
        client.models.generate_content.return_value = response

        with patch.object(recipe_importer, "get_client", return_value=client):
            result = recipe_importer.parse_recipe_text(
                "Chicken Bowl makes 4 servings"
            )

        self.assertTrue(result["readable"])
        self.assertEqual(result["recipe_name"], "Chicken Bowl")
        self.assertEqual(result["yield_servings"], 4)
        self.assertNotIn("calories", result)
        client.close.assert_called_once()

    def test_client_closes_when_text_request_fails(self) -> None:
        client = Mock()
        client.models.generate_content.side_effect = RuntimeError("failed")

        with (
            patch.object(recipe_importer, "get_client", return_value=client),
            self.assertRaises(RuntimeError),
        ):
            recipe_importer.parse_recipe_text("Recipe text")

        client.close.assert_called_once()

    def test_photo_import_rejects_unsupported_type(self) -> None:
        with self.assertRaises(ValueError):
            recipe_importer.parse_recipe_photo(
                b"photo",
                mime_type="image/gif",
            )

    def test_readable_draft_without_ingredients_becomes_unreadable(self) -> None:
        response = SimpleNamespace(
            parsed={
                "readable": True,
                "recipe_name": "Empty Recipe",
                "meal_type": None,
                "yield_servings": None,
                "summary": "",
                "ingredients": [],
                "preparation_steps": [],
                "notes": [],
            },
            text=None,
        )
        client = Mock()
        client.models.generate_content.return_value = response

        with patch.object(recipe_importer, "get_client", return_value=client):
            result = recipe_importer.parse_recipe_text("Empty")

        self.assertFalse(result["readable"])
        self.assertIn("No recipe ingredients", result["notes"][0])

    def test_unsupported_yield_is_cleared_after_parsing(self) -> None:
        response = SimpleNamespace(
            parsed={
                "readable": True,
                "recipe_name": "Large Batch",
                "meal_type": "dinner",
                "yield_servings": 101,
                "summary": "",
                "ingredients": [{
                    "ingredient_name": "chicken",
                    "amount_description": "1 lb",
                    "brand": None,
                    "optional": False,
                    "trace_only": False,
                }],
                "preparation_steps": ["Cook."],
                "notes": [],
            },
            text=None,
        )
        client = Mock()
        client.models.generate_content.return_value = response

        with patch.object(recipe_importer, "get_client", return_value=client):
            result = recipe_importer.parse_recipe_text("Large recipe")

        self.assertIsNone(result["yield_servings"])
        self.assertIn("outside the supported range", result["notes"][0])


if __name__ == "__main__":
    unittest.main()
