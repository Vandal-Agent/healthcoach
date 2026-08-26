from __future__ import annotations

import unittest
from unittest.mock import patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


class TelegramPollingTests(unittest.TestCase):
    def test_update_is_claimed_before_conversation_processing(self) -> None:
        events = []

        with (
            patch.object(
                app,
                "load_state",
                return_value={"telegram_update_offset": 10},
            ),
            patch.object(
                app,
                "save_state",
                side_effect=lambda state: events.append(
                    ("saved", state["telegram_update_offset"])
                ),
            ),
            patch.object(
                app,
                "process_telegram_update",
                side_effect=lambda update: events.append(
                    ("processed", update["update_id"])
                ),
            ),
        ):
            app.process_telegram_update_safely({
                "update_id": 12,
                "message": {"chat": {"id": 123}, "text": "1"},
            })

        self.assertEqual(events, [("saved", 13), ("processed", 12)])

    def test_already_claimed_update_is_not_processed_twice(self) -> None:
        with (
            patch.object(
                app,
                "load_state",
                return_value={"telegram_update_offset": 13},
            ),
            patch.object(app, "save_state") as save,
            patch.object(app, "process_telegram_update") as process,
        ):
            app.process_telegram_update_safely({
                "update_id": 12,
                "message": {"chat": {"id": 123}, "text": "1"},
            })

        save.assert_not_called()
        process.assert_not_called()

    def test_successful_update_advances_offset(self) -> None:
        with (
            patch.object(app, "process_telegram_update") as process,
            patch.object(
                app,
                "load_state",
                return_value={"telegram_update_offset": 10},
            ),
            patch.object(app, "save_state") as save,
        ):
            app.process_telegram_update_safely({
                "update_id": 12,
                "message": {"chat": {"id": 123}, "text": "/status"},
            })

        process.assert_called_once()
        self.assertEqual(
            save.call_args.args[0]["telegram_update_offset"],
            13,
        )

    def test_failed_update_is_reported_and_advances_offset(self) -> None:
        with (
            patch.object(
                app,
                "process_telegram_update",
                side_effect=RuntimeError("bad update"),
            ),
            patch.object(
                app,
                "load_state",
                return_value={"telegram_update_offset": 10},
            ),
            patch.object(app, "save_state") as save,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update_safely({
                "update_id": 12,
                "message": {
                    "chat": {"id": 123},
                    "text": "Afternoon snack",
                },
            })

        self.assertEqual(
            save.call_args.args[0]["telegram_update_offset"],
            13,
        )
        self.assertEqual(send.call_args.kwargs["chat_id"], 123)
        self.assertIn("was skipped", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
