from __future__ import annotations

import unittest
from unittest.mock import MagicMock, patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


class WebhookAuthenticationTests(unittest.TestCase):
    def test_configured_webhook_rejects_a_missing_token(self) -> None:
        with (
            patch.object(app, "WEBHOOK_TOKEN", "expected-secret"),
            patch.object(app, "get_current_sheet") as get_sheet,
        ):
            response = app.app.test_client().post(
                "/webhook",
                json={"steps": 1000},
            )

        self.assertEqual(response.status_code, 401)
        self.assertEqual(response.get_json(), {"status": "unauthorized"})
        get_sheet.assert_not_called()

    def test_configured_webhook_rejects_an_incorrect_token(self) -> None:
        with (
            patch.object(app, "WEBHOOK_TOKEN", "expected-secret"),
            patch.object(app, "get_current_sheet") as get_sheet,
        ):
            response = app.app.test_client().post(
                "/webhook",
                headers={app.WEBHOOK_TOKEN_HEADER: "wrong-secret"},
                json={"steps": 1000},
            )

        self.assertEqual(response.status_code, 401)
        get_sheet.assert_not_called()

    def test_configured_webhook_accepts_the_correct_token(self) -> None:
        sheet = MagicMock()
        with (
            patch.object(app, "WEBHOOK_TOKEN", "expected-secret"),
            patch.object(app, "get_current_sheet", return_value=sheet),
            patch.object(
                app,
                "get_today_row_index_and_row",
                return_value=(None, None, [app.HEADERS]),
            ),
            patch.object(app, "update_or_insert_today") as update,
        ):
            response = app.app.test_client().post(
                "/webhook",
                headers={
                    app.WEBHOOK_TOKEN_HEADER: "expected-secret",
                },
                json={"steps": 1000},
            )

        self.assertEqual(response.status_code, 200)
        update.assert_called_once()

    def test_unconfigured_webhook_remains_available_for_rollout(self) -> None:
        sheet = MagicMock()
        with (
            patch.object(app, "WEBHOOK_TOKEN", ""),
            patch.object(app, "get_current_sheet", return_value=sheet),
            patch.object(
                app,
                "get_today_row_index_and_row",
                return_value=(None, None, [app.HEADERS]),
            ),
            patch.object(app, "update_or_insert_today") as update,
        ):
            response = app.app.test_client().post(
                "/webhook",
                json={"steps": 1000},
            )

        self.assertEqual(response.status_code, 200)
        update.assert_called_once()


if __name__ == "__main__":
    unittest.main()
