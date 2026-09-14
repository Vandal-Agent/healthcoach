from __future__ import annotations

import unittest
from datetime import date
from unittest.mock import Mock, patch

with patch("logging.basicConfig"):
    import app

app.CHAT_ID = None


def tracker_row(
    day: str,
    *,
    sleep: str = "",
    weight: str = "",
) -> list[str]:
    return [
        f"{day} 08:00 AM",
        "",
        "",
        "",
        sleep,
        "",
        weight,
        "",
        "",
        "",
    ]


class HealthHistoryTests(unittest.TestCase):
    def test_read_only_recent_rows_never_migrates_or_creates_sheet(
        self,
    ) -> None:
        row = tracker_row("09/09/2026", weight="229")
        worksheet = Mock()
        worksheet.get_all_values.return_value = [app.HEADERS, row]
        spreadsheet = Mock()
        spreadsheet.worksheet.return_value = worksheet
        client = Mock()
        client.open.return_value = spreadsheet

        with (
            patch.object(app, "get_gspread_client", return_value=client),
            patch.object(app, "ensure_health_tracker_schema") as migrate,
        ):
            rows = app.get_recent_rows_read_only(
                date(2026, 9, 9),
                days_back=6,
            )

        self.assertEqual(rows, [row])
        migrate.assert_not_called()
        spreadsheet.add_worksheet.assert_not_called()

    def test_read_only_recent_rows_propagates_sheet_read_failure(
        self,
    ) -> None:
        worksheet = Mock()
        worksheet.get_all_values.side_effect = RuntimeError("read failed")
        spreadsheet = Mock()
        spreadsheet.worksheet.return_value = worksheet
        client = Mock()
        client.open.return_value = spreadsheet

        with patch.object(
            app,
            "get_gspread_client",
            return_value=client,
        ):
            with self.assertRaisesRegex(RuntimeError, "read failed"):
                app.get_recent_rows_read_only(
                    date(2026, 9, 9),
                    days_back=6,
                )

    def test_completed_day_averages_exclude_today_and_count_recorded_days(
        self,
    ) -> None:
        report = app.build_completed_day_report_data(
            reference_date=date(2026, 9, 10),
            days=7,
            rows=[
                [
                    "09/08/2026 08:00 AM",
                    "8000",
                    "2400",
                    "500",
                    "7:30",
                    "",
                    "230",
                    "",
                    "1900",
                    "90",
                    "30",
                ],
                [
                    "09/09/2026 08:00 AM",
                    "10000",
                    "2600",
                    "700",
                    "",
                    "",
                    "228",
                    "",
                    "2100",
                    "110",
                    "50",
                ],
                [
                    "09/10/2026 08:00 AM",
                    "50000",
                    "9000",
                    "8000",
                    "12",
                    "",
                    "100",
                    "",
                    "8000",
                    "500",
                    "300",
                ],
            ],
        )

        self.assertEqual(report["start_date"], date(2026, 9, 3))
        self.assertEqual(report["end_date"], date(2026, 9, 9))
        self.assertEqual(report["averages"]["weight"], 229.0)
        self.assertEqual(report["recorded_days"]["weight"], 2)
        self.assertEqual(report["averages"]["steps"], 9000.0)
        self.assertEqual(report["recorded_days"]["steps"], 2)
        self.assertEqual(report["averages"]["sleep_hours"], 7.5)
        self.assertEqual(report["recorded_days"]["sleep_hours"], 1)
        self.assertEqual(report["averages"]["exercise_minutes"], 40.0)
        self.assertEqual(report["recorded_days"]["exercise_minutes"], 2)
        self.assertEqual(report["averages"]["dietary_cals"], 2000.0)
        self.assertEqual(report["recorded_days"]["dietary_cals"], 2)
        self.assertEqual(report["averages"]["total_burn"], 2500.0)
        self.assertEqual(report["recorded_days"]["total_burn"], 2)
        self.assertEqual(report["averages"]["active_calories"], 600.0)
        self.assertEqual(report["recorded_days"]["active_calories"], 2)
        self.assertEqual(report["averages"]["protein"], 100.0)
        self.assertEqual(report["recorded_days"]["protein"], 2)

    def test_completed_day_report_uses_latest_valid_value_per_day(
        self,
    ) -> None:
        report = app.build_completed_day_report_data(
            reference_date=date(2026, 9, 10),
            days=7,
            rows=[
                [
                    "09/09/2026 09:00 AM",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "229",
                    "",
                    "",
                    "",
                    "",
                ],
                [
                    "09/09/2026 07:00 AM",
                    "8000",
                    "2400",
                    "500",
                    "7",
                    "",
                    "230",
                    "",
                    "1900",
                    "90",
                    "30",
                ],
            ],
        )

        self.assertEqual(report["averages"]["weight"], 229.0)
        self.assertEqual(report["averages"]["steps"], 8000.0)
        self.assertEqual(report["recorded_days"]["weight"], 1)
        self.assertEqual(report["recorded_days"]["steps"], 1)

    def test_completed_day_report_ignores_invalid_but_counts_zero(
        self,
    ) -> None:
        report = app.build_completed_day_report_data(
            reference_date=date(2026, 9, 10),
            days=7,
            rows=[
                [
                    "09/08/2026 08:00 AM",
                    "invalid",
                    "invalid",
                    "invalid",
                    "",
                    "",
                    "",
                    "",
                    "invalid",
                    "invalid",
                ],
                [
                    "09/09/2026 08:00 AM",
                    "0",
                    "0",
                    "0",
                    "",
                    "",
                    "",
                    "",
                    "0",
                    "0",
                ],
            ],
        )

        for metric in (
            "steps",
            "total_burn",
            "active_calories",
            "dietary_cals",
            "protein",
        ):
            with self.subTest(metric=metric):
                self.assertEqual(report["averages"][metric], 0.0)
                self.assertEqual(report["recorded_days"][metric], 1)

    def test_completed_day_report_rejects_nonfinite_and_impossible_values(
        self,
    ) -> None:
        report = app.build_completed_day_report_data(
            reference_date=date(2026, 9, 10),
            days=7,
            rows=[
                [
                    "09/09/2026 08:00 AM",
                    "-1",
                    "-2400",
                    "-500",
                    "nan",
                    "",
                    "nan",
                    "",
                    "-1900",
                    "inf",
                    "-30",
                ],
            ],
        )

        for metric in app.COMPLETED_DAY_REPORT_METRICS:
            with self.subTest(metric=metric):
                self.assertIsNone(report["averages"][metric])
                self.assertEqual(report["recorded_days"][metric], 0)

    def test_formats_completed_day_averages_with_coverage(self) -> None:
        message = app.format_completed_day_report({
            "period_days": 7,
            "start_date": date(2026, 9, 3),
            "end_date": date(2026, 9, 9),
            "averages": {
                "weight": 229.0,
                "steps": 9000.0,
                "exercise_minutes": 40.0,
                "sleep_hours": 7.5,
                "dietary_cals": 2000.0,
                "total_burn": 2500.0,
                "active_calories": 600.0,
                "protein": None,
            },
            "recorded_days": {
                "weight": 2,
                "steps": 2,
                "exercise_minutes": 2,
                "sleep_hours": 1,
                "dietary_cals": 2,
                "total_burn": 2,
                "active_calories": 2,
                "protein": 0,
            },
        })

        self.assertIn("7-Day Averages", message)
        self.assertIn("Completed days: Sep 3–Sep 9, 2026", message)
        self.assertIn(
            "Weight: 229 lb average (2 of 7 days recorded)",
            message,
        )
        self.assertIn(
            "Steps: 9,000 average (2 of 7 days recorded)",
            message,
        )
        self.assertIn(
            "Sleep: 7 hr 30 min average (1 of 7 days recorded)",
            message,
        )
        self.assertIn(
            "Protein: No data recorded (0 of 7 days recorded)",
            message,
        )
        self.assertIn("Today is excluded.", message)

    def test_weight_history_lists_latest_weight_once_per_completed_day(
        self,
    ) -> None:
        report = app.build_completed_day_report_data(
            reference_date=date(2026, 9, 10),
            days=30,
            rows=[
                tracker_row("09/08/2026", weight="231"),
                [
                    "09/09/2026 09:00 AM",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "229",
                ],
                [
                    "09/09/2026 07:00 AM",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "230",
                ],
                tracker_row("09/10/2026", weight="100"),
            ],
        )

        message = app.format_weight_history(report)

        self.assertIn(
            "Weight History - Previous 30 Completed Days",
            message,
        )
        self.assertLess(
            message.index("Sep 8: 231 lb"),
            message.index("Sep 9: 229 lb"),
        )
        self.assertNotIn("Sep 9: 230 lb", message)
        self.assertNotIn("Sep 10: 100 lb", message)
        self.assertIn("Weight recorded: 2 of 30 days", message)
        self.assertIn("Today is excluded.", message)

    def test_completed_day_report_fetches_only_completed_date_window(
        self,
    ) -> None:
        with (
            patch.object(
                app,
                "get_recent_rows",
                side_effect=AssertionError("write-capable reader called"),
            ),
            patch.object(
                app,
                "get_recent_rows_read_only",
                return_value=[tracker_row("09/09/2026", weight="229")],
            ) as get_rows,
        ):
            message = app.get_formatted_completed_day_report(
                reference_date=date(2026, 9, 10),
                days=7,
            )

        self.assertIn("7-Day Averages", message)
        self.assertIn("Weight: 229 lb average", message)
        get_rows.assert_called_once_with(
            date(2026, 9, 9),
            days_back=6,
        )

    def test_reports_menu_contains_completed_day_reports(self) -> None:
        message = app.healthcoach_reports_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("5. 7-day averages", message)
        self.assertIn("6. 30-day averages", message)
        self.assertIn("7. Weight history", message)
        self.assertIn("8. Back", message)
        self.assertIn(
            ["7-day averages", "30-day averages"],
            keyboard["keyboard"],
        )
        self.assertIn(["Weight history"], keyboard["keyboard"])

    def test_reports_menu_routes_to_completed_day_averages(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "reports",
            "known_data": {},
        }
        for reply, expected_days in (("5", 7), ("30-day averages", 30)):
            with self.subTest(reply=reply):
                with (
                    patch.object(
                        app,
                        "get_active_conversation",
                        return_value=conversation,
                    ),
                    patch.object(
                        app,
                        "get_formatted_completed_day_report",
                        return_value=f"{expected_days}-Day Averages",
                    ) as get_report,
                    patch.object(app, "update_conversation"),
                    patch.object(app, "send_telegram_msg") as send,
                ):
                    app.process_telegram_update({
                        "message": {
                            "chat": {"id": 123},
                            "text": reply,
                        }
                    })

                self.assertTrue(get_report.called)
                self.assertEqual(
                    get_report.call_args.kwargs["days"],
                    expected_days,
                )
                self.assertIn(
                    f"{expected_days}-Day Averages",
                    send.call_args.args[0],
                )

    def test_weight_history_fetches_30_completed_days(self) -> None:
        with (
            patch.object(
                app,
                "get_recent_rows",
                side_effect=AssertionError("write-capable reader called"),
            ),
            patch.object(
                app,
                "get_recent_rows_read_only",
                return_value=[tracker_row("09/09/2026", weight="229")],
            ) as get_rows,
        ):
            message = app.get_formatted_weight_history(
                reference_date=date(2026, 9, 10),
            )

        self.assertIn("Sep 9: 229 lb", message)
        get_rows.assert_called_once_with(
            date(2026, 9, 9),
            days_back=29,
        )

    def test_reports_menu_routes_to_weight_history(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "reports",
            "known_data": {},
        }
        for reply in ("7", "Weight history"):
            with self.subTest(reply=reply):
                with (
                    patch.object(
                        app,
                        "get_active_conversation",
                        return_value=conversation,
                    ),
                    patch.object(
                        app,
                        "get_formatted_weight_history",
                        return_value="Weight History - Previous 30",
                    ) as get_history,
                    patch.object(app, "send_telegram_msg") as send,
                ):
                    app.process_telegram_update({
                        "message": {
                            "chat": {"id": 123},
                            "text": reply,
                        }
                    })

                self.assertTrue(get_history.called)
                self.assertIn(
                    "Weight History - Previous 30",
                    send.call_args.args[0],
                )

    def test_completed_day_report_outputs_have_navigation_buttons(
        self,
    ) -> None:
        for message in (
            "7-Day Averages\n\nReply Back or Cancel.",
            "30-Day Averages\n\nReply Back or Cancel.",
            "Weight History - Previous 30 Completed Days\n\n"
            "Reply Back or Cancel.",
        ):
            with self.subTest(message=message.splitlines()[0]):
                keyboard = app.menu_reply_markup(message)
                self.assertIsNotNone(keyboard)
                self.assertIn(
                    ["Back", "Cancel"],
                    keyboard["keyboard"],
                )

    def test_builds_complete_range_with_missing_days(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 16),
            days=7,
            rows=[
                tracker_row(
                    "08/15/2026",
                    sleep="6.0",
                    weight="234.0",
                ),
                tracker_row(
                    "08/16/2026",
                    sleep="7.0",
                    weight="233.0",
                ),
            ],
        )

        self.assertEqual(len(history["days"]), 7)
        self.assertEqual(history["days"][0]["date"], date(2026, 8, 10))
        self.assertIsNone(history["days"][0]["weight"])
        self.assertEqual(history["average_weight"], 233.5)
        self.assertEqual(history["weight_change"], -1.0)
        self.assertEqual(history["weight_entries"], 2)
        self.assertEqual(history["average_sleep"], 6.5)
        self.assertEqual(history["sleep_entries"], 2)

    def test_history_message_shows_daily_values_and_summary(self) -> None:
        history = app.build_health_history_data(
            reference_date=date(2026, 8, 16),
            days=7,
            rows=[
                tracker_row(
                    "08/16/2026",
                    sleep="7.5",
                    weight="233.5",
                ),
            ],
        )

        message = app.format_health_history(history)

        self.assertIn("Health History - Last 7 Days", message)
        self.assertIn(
            "Sun Aug 16: weight 233.5 lb; sleep 7.5 h",
            message,
        )
        self.assertIn("not recorded", message)
        self.assertIn("Weight recorded: 1/7 days", message)
        self.assertIn("Sleep recorded: 1/7 days", message)

    def test_rejects_unsupported_history_period(self) -> None:
        with self.assertRaisesRegex(ValueError, "7, 14, or 30"):
            app.build_health_history_data(
                reference_date=date(2026, 8, 16),
                days=10,
                rows=[],
            )

    def test_health_menu_contains_history_action(self) -> None:
        message = app.healthcoach_health_menu_text()
        keyboard = app.menu_reply_markup(message)

        self.assertIn("4. Health history", message)
        self.assertIn(
            ["Record weight", "Health history"],
            keyboard["keyboard"],
        )

    def test_health_menu_routes_to_history_menu(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "health",
            "known_data": {},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Health history",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "health_history",
        )
        self.assertIn(
            "Last 7 days",
            send.call_args.args[0],
        )

    def test_history_menu_loads_selected_period(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "health_history",
            "known_data": {},
        }
        with (
            patch.object(
                app,
                "get_active_conversation",
                return_value=conversation,
            ),
            patch.object(
                app,
                "get_formatted_health_history",
                return_value="Health History - Last 14 Days",
            ) as get_history,
            patch.object(app, "send_telegram_msg") as send,
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "14 days",
                }
            })

        self.assertEqual(
            get_history.call_args.kwargs["days"],
            14,
        )
        self.assertIn(
            "Last 14 Days",
            send.call_args.args[0],
        )


if __name__ == "__main__":
    unittest.main()
