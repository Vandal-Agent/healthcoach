from __future__ import annotations

from datetime import date, datetime, timedelta
import unittest
from unittest.mock import MagicMock, patch

with patch("logging.basicConfig"):
    import app
    import health_insights

app.CHAT_ID = None


class FixedDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = datetime(2026, 8, 29, 10, 0)
        if tz is not None:
            return tz.localize(value)
        return value


def weekly_records(reference_date: date) -> list[dict]:
    records = []
    for days_ago in range(14, 0, -1):
        recent = days_ago <= 7
        records.append({
            "date": (reference_date - timedelta(days=days_ago)).isoformat(),
            "steps": 8000 if recent else 7000,
            "total_burn": 2500 if recent else 2300,
            "sleep_hours": 7.0,
            "resting_heart_rate": 60,
            "hrv": 30,
            "exercise_minutes": 30 if recent else 15,
            "weight": 230 if recent else 231,
            "cardio_fitness": 30.0 + ((14 - days_ago) / 10),
            "walking_heart_rate": 78 - ((14 - days_ago) / 10),
            "blood_pressure_systolic": 110 if recent else None,
            "blood_pressure_diastolic": 72 if recent else None,
        })
    records.append({
        "date": reference_date.isoformat(),
        "steps": 99999,
        "total_burn": 9999,
        "exercise_minutes": 999,
    })
    return records


class WeeklyHealthInsightTests(unittest.TestCase):
    def test_evidence_compares_completed_weeks_and_excludes_today(self):
        reference_date = date(2026, 8, 29)
        evidence = health_insights.build_weekly_health_evidence(
            records=weekly_records(reference_date),
            reference_date=reference_date,
        )

        statements = " ".join(
            fact["statement"] for fact in evidence["facts"]
        )
        self.assertIn("Average sleep was 7 h", statements)
        self.assertIn("preceding week", statements)
        self.assertIn("Apple Exercise Minutes totaled 210", statements)
        self.assertNotIn("99,999", statements)
        self.assertEqual(evidence["period_start"], "2026-08-22")
        self.assertEqual(evidence["period_end"], "2026-08-28")
        self.assertTrue(evidence["safety"]["today_is_excluded"])

    def test_missing_days_are_reported_and_not_converted_to_zero(self):
        reference_date = date(2026, 8, 29)
        evidence = health_insights.build_weekly_health_evidence(
            records=[{
                "date": "2026-08-28",
                "sleep_hours": 6.5,
                "steps": None,
            }],
            reference_date=reference_date,
        )

        statements = " ".join(
            fact["statement"] for fact in evidence["facts"]
        )
        self.assertIn("sleep 1/7", statements)
        self.assertIn("steps 0/7", statements)
        self.assertNotIn("Average steps", statements)

    def test_no_records_produce_no_weekly_facts(self):
        evidence = health_insights.build_weekly_health_evidence(
            records=[],
            reference_date=date(2026, 8, 29),
        )
        self.assertEqual(evidence["facts"], [])

    def test_formatter_uses_exact_evidence_and_completed_date_range(self):
        evidence = health_insights.build_weekly_health_evidence(
            records=weekly_records(date(2026, 8, 29)),
            reference_date=date(2026, 8, 29),
        )
        narrative = health_insights.fallback_weekly_health_narrative(
            evidence
        )

        message = health_insights.format_weekly_health_insight(
            evidence,
            narrative,
            personalized=False,
        )

        self.assertIn("Weekly Health Insight", message)
        self.assertIn("Aug 22–Aug 28, 2026", message)
        self.assertIn("What your completed-week data shows", message)
        self.assertIn("One focus for the coming week", message)
        self.assertIn("does not diagnose", message)
        self.assertIn("recorded less average daily movement", message)
        self.assertIn("Steps and total burn moved together", message)
        self.assertLessEqual(len(message), 4096)

    def test_generated_wording_must_cite_computed_weekly_facts(self):
        evidence = health_insights.build_weekly_health_evidence(
            records=weekly_records(date(2026, 8, 29)),
            reference_date=date(2026, 8, 29),
        )
        fact_id = evidence["facts"][0]["id"]
        narrative = health_insights.WeeklyHealthNarrative(
            summary="Your completed-week patterns offer a useful comparison.",
            observations=[
                health_insights.GroundedHealthObservation(
                    fact_ids=[fact_id],
                    interpretation=(
                        "This personal pattern is worth watching alongside "
                        "how you feel during the coming week."
                    ),
                )
            ],
            health_connection=(
                "Repeated recovery and activity patterns may relate to "
                "energy and consistency."
            ),
            practical_focus=(
                "Choose one manageable routine tied to this pattern."
            ),
            data_limit=(
                "Recorded averages do not establish a cause or medical "
                "conclusion."
            ),
        )
        client = MagicMock()
        client.models.generate_content.return_value.parsed = narrative
        client.models.generate_content.return_value.text = None

        result = health_insights.generate_weekly_health_narrative(
            evidence,
            client=client,
        )

        self.assertEqual(result.observations[0].fact_ids, [fact_id])
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn(fact_id, prompt)
        self.assertIn("Use only this computed evidence", prompt)

    def test_app_uses_grounded_fallback_when_gemini_is_unavailable(self):
        records = weekly_records(date(2026, 8, 29))
        with (
            patch.object(app, "get_recent_rows", return_value=[]),
            patch.object(
                app,
                "build_daily_health_records",
                return_value=records,
            ),
            patch.object(
                app,
                "generate_weekly_health_narrative",
                side_effect=RuntimeError("model unavailable"),
            ),
        ):
            message = app.get_weekly_health_insight_message(
                reference_date=date(2026, 8, 29),
            )

        self.assertIn("Weekly Health Insight", message)
        self.assertIn("calculated evidence directly", message)
        self.assertIn("recorded less average daily movement", message)

    def test_health_menu_routes_to_weekly_insight(self):
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
            patch.object(
                app,
                "get_weekly_health_insight_message",
                return_value="Weekly Health Insight\nPersonalized result",
            ) as get_insight,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
            patch.object(app, "datetime", FixedDateTime),
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Weekly health insight",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "health_weekly_insight",
        )
        get_insight.assert_called_once_with(
            reference_date=date(2026, 8, 29),
        )
        self.assertIn("last completed week", send.call_args_list[0].args[0])
        self.assertIn("Personalized result", send.call_args_list[1].args[0])
        self.assertIn(
            "6. Weekly health insight",
            app.healthcoach_health_menu_text(),
        )
        self.assertIn("7. Back", app.healthcoach_health_menu_text())

    def test_back_from_weekly_insight_returns_to_health_menu(self):
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "health_weekly_insight",
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
            patch.object(app, "datetime", FixedDateTime),
        ):
            app.process_telegram_update({
                "message": {"chat": {"id": 123}, "text": "Back"}
            })

        self.assertEqual(update.call_args.kwargs["current_step"], "health")
        self.assertIn("Health Menu", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
