import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


class DirectMenuCommandTests(unittest.TestCase):
    def assert_direct_command(
        self,
        *,
        command: str,
        expected_step: str,
        expected_heading: str,
    ) -> None:
        with (
            patch.object(app, "start_conversation") as start,
            patch.object(app, "get_active_conversation") as get_active,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": command,
                }
            })

        start.assert_called_once_with(
            chat_id=123,
            conversation_type="healthcoach_menu",
            current_step=expected_step,
            known_data={},
            missing_fields=[],
            original_message=command,
        )
        get_active.assert_not_called()
        self.assertIn(expected_heading, send.call_args.args[0])

    def test_food_command_replaces_current_flow(self) -> None:
        self.assert_direct_command(
            command="/food",
            expected_step="food",
            expected_heading="Food Menu",
        )

    def test_health_command_replaces_current_flow(self) -> None:
        self.assert_direct_command(
            command="/health",
            expected_step="health",
            expected_heading="Health Menu",
        )

    def test_reports_command_replaces_current_flow(self) -> None:
        self.assert_direct_command(
            command="/reports",
            expected_step="reports",
            expected_heading="Reports Menu",
        )

    def test_direct_commands_are_case_insensitive(self) -> None:
        self.assert_direct_command(
            command="/FOOD",
            expected_step="food",
            expected_heading="Food Menu",
        )

    def test_help_lists_direct_commands(self) -> None:
        message = app.build_help_message()

        self.assertIn("/menu", message)
        self.assertIn("/food", message)
        self.assertIn("/health", message)
        self.assertIn("/reports", message)

    def test_main_menu_displays_direct_shortcuts(self) -> None:
        message = app.healthcoach_main_menu_text()

        self.assertIn("Direct shortcuts", message)
        self.assertIn("/food", message)
        self.assertIn("/health", message)
        self.assertIn("/reports", message)


if __name__ == "__main__":
    unittest.main()
