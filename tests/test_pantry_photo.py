import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from food import pantry_photo


class PantryPhotoTests(unittest.TestCase):
    def test_analysis_keeps_only_distinct_clean_names(self) -> None:
        response = SimpleNamespace(
            parsed={
                "readable": True,
                "items": [
                    {"display_name": "Kroger Black Beans"},
                    {"display_name": "kroger black beans"},
                    {"display_name": "  Fresh onions  "},
                ],
                "notes": [],
            },
            text=None,
        )
        client = Mock()
        client.models.generate_content.return_value = response

        with patch.object(pantry_photo, "get_client", return_value=client):
            result = pantry_photo.analyze_pantry_photo(
                b"photo",
                mime_type="image/jpeg",
            )

        self.assertTrue(result["readable"])
        self.assertEqual(
            [item["display_name"] for item in result["items"]],
            ["Kroger Black Beans", "Fresh onions"],
        )
        client.close.assert_called_once()
        config = client.models.generate_content.call_args.kwargs["config"]
        self.assertEqual(config.temperature, 0)
        self.assertIs(config.response_schema, pantry_photo.PantryPhotoAnalysis)

    def test_unreadable_analysis_discards_returned_items(self) -> None:
        response = SimpleNamespace(
            parsed={
                "readable": False,
                "items": [{"display_name": "Invented item"}],
                "notes": ["Labels were blurry."],
            },
            text=None,
        )
        client = Mock()
        client.models.generate_content.return_value = response

        with patch.object(pantry_photo, "get_client", return_value=client):
            result = pantry_photo.analyze_pantry_photo(
                b"photo",
                mime_type="image/jpeg",
            )

        self.assertFalse(result["readable"])
        self.assertEqual(result["items"], [])

    def test_analysis_closes_client_when_request_fails(self) -> None:
        client = Mock()
        client.models.generate_content.side_effect = RuntimeError("failed")

        with (
            patch.object(pantry_photo, "get_client", return_value=client),
            self.assertRaises(RuntimeError),
        ):
            pantry_photo.analyze_pantry_photo(
                b"photo",
                mime_type="image/jpeg",
            )

        client.close.assert_called_once()

    def test_session_merge_deduplicates_and_preserves_order(self) -> None:
        merged = pantry_photo.merge_pantry_photo_names(
            ["Black beans", "Rice", "Cilantro Lime Rice"],
            ["black beans", "Pasta", "Cilantro & Lime Rice"],
        )

        self.assertEqual(
            merged,
            ["Black beans", "Rice", "Cilantro Lime Rice", "Pasta"],
        )

    def test_unsupported_photo_type_is_rejected(self) -> None:
        with self.assertRaisesRegex(ValueError, "Unsupported"):
            pantry_photo.analyze_pantry_photo(
                b"photo",
                mime_type="image/gif",
            )


if __name__ == "__main__":
    unittest.main()
