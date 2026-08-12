import ast
import re
import unittest
from datetime import datetime
from pathlib import Path


def load_functions(*names):
    tree = ast.parse(Path("app.py").read_text())
    selected = [
        node
        for node in tree.body
        if isinstance(node, ast.FunctionDef)
        and node.name in names
    ]
    module = ast.fix_missing_locations(
        ast.Module(body=selected, type_ignores=[])
    )
    namespace = {
        "re": re,
        "datetime": datetime,
    }
    exec(compile(module, "app.py", "exec"), namespace)
    return namespace


class HealthMetricParserTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.namespace = load_functions(
            "parse_sleep",
            "extract_sleep_value_from_text",
            "extract_weight_value_from_text",
        )
        cls.sleep = staticmethod(
            cls.namespace["extract_sleep_value_from_text"]
        )
        cls.weight = staticmethod(
            cls.namespace["extract_weight_value_from_text"]
        )

    def test_natural_sleep_hours(self):
        self.assertEqual(self.sleep("I slept 7 hours"), 7.0)
        self.assertEqual(
            self.sleep("I got 6 and a half hours"),
            6.5,
        )
        self.assertEqual(
            self.sleep("I slept 7 hours and 30 minutes"),
            7.5,
        )

    def test_colon_sleep_duration(self):
        self.assertEqual(
            self.sleep("Record my sleep as 7:15"),
            7.25,
        )
        self.assertEqual(
            self.sleep("7:15", allow_bare=True),
            7.25,
        )

    def test_overnight_sleep_range(self):
        self.assertEqual(
            self.sleep(
                "I slept from 10:30 PM to 5:45 AM"
            ),
            7.25,
        )

    def test_invalid_sleep_is_rejected(self):
        self.assertIsNone(self.sleep("I did not sleep well"))
        self.assertIsNone(
            self.sleep("25", allow_bare=True)
        )

    def test_natural_weight(self):
        self.assertEqual(
            self.weight("I weighed 214.6 this morning"),
            214.6,
        )
        self.assertEqual(
            self.weight("Record my weight as 213"),
            213.0,
        )
        self.assertEqual(
            self.weight("212.4", allow_bare=True),
            212.4,
        )

    def test_implausible_weight_is_rejected(self):
        self.assertIsNone(
            self.weight("I weighed 12 pounds")
        )
        self.assertIsNone(
            self.weight("900", allow_bare=True)
        )


class FakeSheet:
    def __init__(self):
        self.updated = None
        self.appended = None

    def update(self, *, range_name, values):
        self.updated = (range_name, values)

    def append_row(self, row):
        self.appended = row


class FixedNow:
    def strftime(self, pattern):
        values = {
            "%m/%d/%Y": "08/12/2026",
            "%m/%d/%Y %I:%M %p": (
                "08/12/2026 08:15 AM"
            ),
        }
        return values[pattern]


class FakeDatetime:
    @staticmethod
    def now(_timezone):
        return FixedNow()


class WeightStorageTests(unittest.TestCase):
    def load_weight_function(self, existing_row):
        namespace = load_functions("set_today_weight")
        sheet = FakeSheet()

        namespace.update(
            {
                "datetime": FakeDatetime,
                "PACIFIC_TZ": object(),
                "get_current_sheet": lambda: sheet,
                "get_today_row_index_and_row": (
                    lambda _sheet, _date: (
                        (2, list(existing_row), [])
                        if existing_row is not None
                        else (None, None, [])
                    )
                ),
            }
        )
        return namespace["set_today_weight"], sheet

    def test_first_weight_creates_daily_value(self):
        setter, sheet = self.load_weight_function(None)
        success, message = setter(214.6)

        self.assertTrue(success)
        self.assertIn("Recorded", message)
        self.assertEqual(sheet.appended[6], 214.6)
        self.assertEqual(
            sheet.appended[0],
            "08/12/2026 08:15 AM",
        )

    def test_later_weight_corrects_same_daily_row(self):
        existing = [
            "08/12/2026 06:00 AM",
            "1000",
            "",
            "",
            "7.5",
            "",
            "216.0",
            "",
            "",
            "",
        ]
        setter, sheet = self.load_weight_function(existing)
        success, message = setter(214.6)

        self.assertTrue(success)
        self.assertIn("Corrected", message)
        self.assertEqual(sheet.updated[0], "A2:J2")
        self.assertEqual(sheet.updated[1][0][6], 214.6)
        self.assertEqual(
            sheet.updated[1][0][0],
            "08/12/2026 08:15 AM",
        )


if __name__ == "__main__":
    unittest.main()
