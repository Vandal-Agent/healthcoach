from contextlib import ExitStack
from datetime import date, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest import TestCase
from unittest.mock import patch

import conversation_engine
from food import database, ledger, library
from food.interpreter import FoodInterpretation

with patch("logging.basicConfig"):
    import app


class LunchTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 11, 8, 12)
        return tz.localize(value) if tz else value


class QuickLogTests(TestCase):
    def setUp(self):
        stack = ExitStack()
        self.addCleanup(stack.close)
        root = Path(stack.enter_context(TemporaryDirectory()))
        for module in (database, ledger, library):
            stack.enter_context(patch.object(module, "DATABASE_PATH", root / "food.db"))
        database.initialize_database(root / "food.db")
        stack.enter_context(patch.object(
            conversation_engine, "CONVERSATION_FILE", root / "conversations.json"
        ))
        stack.enter_context(patch.object(app, "CHAT_ID", None))
        stack.enter_context(patch.object(app, "datetime", LunchTime))
        self.send = stack.enter_context(patch.object(app, "send_telegram_msg"))
        self.sync = stack.enter_context(patch.object(app, "sync_food_ledger_totals_to_sheet"))
        self.food = library.add_food_with_nutrition(
            canonical_name="Pretzel Pieces", serving_description="28 g",
            serving_amount=28, serving_unit="g", verification_status="verified",
            verification_source="user_label", calories=120, protein_g=2,
        )["food"]
        entry = ledger.add_food_entry(
            entry_date="2026-11-07", meal_category="afternoon snack",
            food_id=self.food["food_id"], quantity=1, logging_source="telegram_ai",
        )
        ledger.save_food_favorite_from_entry(entry["food_entry_id"])
        conversation_engine.start_conversation(
            chat_id=123, conversation_type="healthcoach_menu", current_step="favorites",
        )

    def reply(self, value):
        app.process_telegram_update({"message": {"chat": {"id": 123}, "text": value}})
        return self.send.call_args.args[0]

    def test_snack_shortcut_logs_at_lunch_and_returns_to_snack(self):
        self.assertIn("Breakfast", self.reply("Quick log"))
        self.assertIn("Pretzel Pieces", self.reply("Snack"))
        response = self.reply("Pretzel Pieces")
        self.assertIn("Logged", response)
        self.assertIn("Did you eat anything else", response)
        self.assertIn("Other food", response)
        entries = ledger.list_food_entries(entry_date="2026-11-08")
        self.assertEqual([(e["meal_category"], e["quantity"]) for e in entries], [("lunch", 1)])
        self.sync.assert_called_once_with(date(2026, 11, 8))

        self.reply("Change amount")
        self.reply("0.5")
        self.assertEqual(ledger.list_food_entries(entry_date="2026-11-08")[0]["quantity"], 0.5)
        self.reply("Save to Quick Log")
        self.reply("Lunch")
        self.assertEqual(
            {(f["meal_category"], f["quantity"]) for f in ledger.list_food_favorites()},
            {("afternoon snack", 1), ("lunch", 0.5)},
        )
        self.assertEqual(len(ledger.list_food_entries(entry_date="2026-11-08")), 1)
        self.reply("Undo")
        self.assertEqual(ledger.list_food_entries(entry_date="2026-11-08"), [])
        self.assertIn("Pretzel Pieces", self.send.call_args.args[0])

    def test_other_food_uses_normal_logging_and_returns_to_category(self):
        self.reply("Quick log")
        self.reply("Dinner")
        self.assertIn("What food did you have?", self.reply("Other food"))
        interpretation = FoodInterpretation(
            is_food_logging_request=True, food_name="Pretzel Pieces",
            quantity=1, missing_fields=[], assumptions=[], confidence=1,
        )
        with patch.object(app, "interpret_food_message", return_value=interpretation):
            self.reply("one serving of Pretzel Pieces")
            self.reply("Edit")
            self.reply("a serving of Pretzel Pieces")
        resolution = {
            "found": True, "food": self.food,
            "nutrition": library.get_active_nutrition(self.food["food_id"]),
        }
        with patch.object(app, "resolve_food", return_value=resolution):
            self.reply("Correct")
        response = self.reply("Log It")
        self.assertIn("Quick Log — Dinner", response)
        self.assertIn("Logged Pretzel Pieces under Lunch.", response)

    def test_normal_food_confirmation_offers_to_save_it(self):
        conversation_engine.start_conversation(
            chat_id=123, conversation_type="food_interpretation",
            current_step="nutrition_confirmation",
            known_data={
                "meal_category": "lunch",
                "_pending_components": [{
                    "food_id": self.food["food_id"], "canonical_name": "Pretzel Pieces",
                    "quantity": 2,
                }],
            },
        )
        self.assertIn("Save to Quick Log", self.reply("Log It"))
        self.assertIn("choose a category", self.reply("Save to Quick Log"))
        self.assertIn("Saved Pretzel Pieces to Breakfast", self.reply("Breakfast"))
        self.assertEqual(len(ledger.list_food_entries(entry_date="2026-11-08")), 1)
        self.assertIn(
            ("breakfast", 2),
            {(f["meal_category"], f["quantity"]) for f in ledger.list_food_favorites()},
        )
