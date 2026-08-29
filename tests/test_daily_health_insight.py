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
        value = datetime(2026, 8, 28, 9, 0)
        if tz is not None:
            return tz.localize(value)
        return value


def personal_records(reference_date: date) -> list[dict]:
    records = []
    for days_ago in range(14, 0, -1):
        record_date = reference_date - timedelta(days=days_ago)
        records.append({
            "date": record_date.isoformat(),
            "steps": 7000 + days_ago,
            "total_burn": 2500,
            "sleep_hours": 7.0,
            "resting_heart_rate": 55,
            "hrv": 30,
            "exercise_minutes": 25,
            "weight": 231 + (days_ago / 20),
            "cardio_fitness": 30.0,
            "walking_heart_rate": 75,
        })
    records.append({
        "date": reference_date.isoformat(),
        "steps": 1200,
        "total_burn": 900,
        "sleep_hours": 6.0,
        "resting_heart_rate": 60,
        "hrv": 22,
        "exercise_minutes": 0,
        "weight": 230.8,
        "cardio_fitness": 30.2,
        "walking_heart_rate": 74,
        "blood_pressure_systolic": 112,
        "blood_pressure_diastolic": 76,
    })
    return records


class DailyHealthInsightTests(unittest.TestCase):
    def test_evidence_compares_today_with_personal_baselines(self) -> None:
        reference_date = date(2026, 8, 28)

        evidence = health_insights.build_daily_health_evidence(
            records=personal_records(reference_date),
            reference_date=reference_date,
            now_hour=9,
        )

        statements = " ".join(
            fact["statement"] for fact in evidence["facts"]
        )
        relationships = {
            fact.get("relationship") for fact in evidence["facts"]
        }
        self.assertIn("Today's sleep is 6 h", statements)
        self.assertIn("prior fourteen-day personal average", statements)
        self.assertIn("Yesterday's completed record", statements)
        self.assertIn("below personal baseline", relationships)
        self.assertIn("above personal baseline", relationships)
        self.assertEqual(evidence["day_phase"], "morning")

    def test_partial_day_activity_is_not_treated_as_completed(self) -> None:
        reference_date = date(2026, 8, 28)

        evidence = health_insights.build_daily_health_evidence(
            records=personal_records(reference_date),
            reference_date=reference_date,
            now_hour=14,
        )

        partial = [
            fact
            for fact in evidence["facts"]
            if fact.get("relationship") == "partial day"
        ]
        self.assertEqual(len(partial), 1)
        self.assertIn("not treated as completed-day totals", partial[0]["statement"])

    def test_missing_tracker_cells_remain_missing(self) -> None:
        row = [
            "08/28/2026 08:00 AM",
            "",
            "",
            "",
            "6.5",
            "54",
            "230.5",
            "28",
            "",
            "",
            "",
            "30.2",
            "74",
            "",
            "",
            "",
        ]

        record = app.build_daily_health_records([row])[0]

        self.assertIsNone(record["steps"])
        self.assertIsNone(record["total_burn"])
        self.assertIsNone(record["exercise_minutes"])
        self.assertEqual(record["sleep_hours"], 6.5)

    def test_personalized_narrative_must_cite_computed_facts(self) -> None:
        reference_date = date(2026, 8, 28)
        evidence = health_insights.build_daily_health_evidence(
            records=personal_records(reference_date),
            reference_date=reference_date,
            now_hour=9,
        )
        fact_id = evidence["facts"][0]["id"]
        parsed = health_insights.DailyHealthNarrative(
            summary="Your recovery signals differ from your recent pattern.",
            observations=[
                health_insights.GroundedHealthObservation(
                    fact_ids=[fact_id],
                    interpretation=(
                        "The combination may reflect a higher recovery load "
                        "today, so the repeated pattern matters more than one day."
                    ),
                )
            ],
            health_connection=(
                "Sleep and recovery consistency can influence energy, appetite, "
                "training readiness, and cardiovascular well-being."
            ),
            practical_focus=(
                "Favor a manageable day and protect tonight's sleep opportunity."
            ),
            data_limit=(
                "These wearable measurements can shift for temporary reasons and "
                "should be interpreted as a personal pattern."
            ),
        )
        client = MagicMock()
        client.models.generate_content.return_value.parsed = parsed
        client.models.generate_content.return_value.text = None

        result = health_insights.generate_daily_health_narrative(
            evidence,
            client=client,
        )

        self.assertEqual(result.observations[0].fact_ids, [fact_id])
        prompt = client.models.generate_content.call_args.kwargs["contents"]
        self.assertIn(fact_id, prompt)
        self.assertIn("only source of personal health facts", prompt)

    def test_narrative_rejects_unknown_fact_or_new_number(self) -> None:
        evidence = {
            "facts": [{"id": "F1", "statement": "Recorded fact."}]
        }
        unknown = health_insights.DailyHealthNarrative(
            summary="A personal pattern is available.",
            observations=[
                health_insights.GroundedHealthObservation(
                    fact_ids=["F9"],
                    interpretation="This pattern is worth watching.",
                )
            ],
            health_connection="Consistency may support recovery.",
            practical_focus="Choose one manageable recovery action.",
            data_limit="One pattern does not establish a cause.",
        )
        with self.assertRaisesRegex(ValueError, "unknown evidence"):
            health_insights.validate_daily_health_narrative(
                unknown,
                evidence,
            )

        numbered = unknown.model_copy(deep=True)
        numbered.observations[0].fact_ids = ["F1"]
        numbered.practical_focus = "Take a 20 minute walk."
        with self.assertRaisesRegex(ValueError, "introduced numbers"):
            health_insights.validate_daily_health_narrative(
                numbered,
                evidence,
            )

    def test_narrative_rejects_medical_classification_language(self) -> None:
        evidence = {
            "facts": [{"id": "F1", "statement": "Recorded fact."}]
        }
        narrative = health_insights.DailyHealthNarrative(
            summary="Your blood pressure is normal.",
            observations=[
                health_insights.GroundedHealthObservation(
                    fact_ids=["F1"],
                    interpretation="This pattern is worth watching.",
                )
            ],
            health_connection="Consistency may support recovery.",
            practical_focus="Choose one manageable recovery action.",
            data_limit="One pattern does not establish a cause.",
        )

        with self.assertRaisesRegex(ValueError, "medical language"):
            health_insights.validate_daily_health_narrative(
                narrative,
                evidence,
            )

    def test_narrative_rejects_unsupported_recovery_mechanism(self) -> None:
        evidence = {
            "facts": [{"id": "F1", "statement": "Recorded fact."}]
        }
        narrative = health_insights.DailyHealthNarrative(
            summary="Your recent pattern is worth watching.",
            observations=[
                health_insights.GroundedHealthObservation(
                    fact_ids=["F1"],
                    interpretation=(
                        "This often indicates that your body is experiencing "
                        "extra systemic strain."
                    ),
                )
            ],
            health_connection="The pattern may relate to recovery.",
            practical_focus="Compare the pattern with how you feel.",
            data_limit="One reading does not establish a cause.",
        )

        with self.assertRaisesRegex(ValueError, "medical language"):
            health_insights.validate_daily_health_narrative(
                narrative,
                evidence,
            )

    def test_narrative_rejects_sleep_consistency_from_average_only(self) -> None:
        evidence = {
            "facts": [{"id": "F1", "statement": "Average sleep increased."}]
        }
        narrative = health_insights.DailyHealthNarrative(
            summary="Your steady sleep pattern may support recovery.",
            observations=[
                health_insights.GroundedHealthObservation(
                    fact_ids=["F1"],
                    interpretation="Your sleep duration averaged higher.",
                )
            ],
            health_connection="Sleep duration may relate to daily energy.",
            practical_focus="Keep a reasonable sleep opportunity tonight.",
            data_limit="The average does not show sleep quality.",
        )

        with self.assertRaisesRegex(ValueError, "medical language"):
            health_insights.validate_daily_health_narrative(
                narrative,
                evidence,
            )

    def test_formatter_shows_exact_evidence_and_natural_meaning(self) -> None:
        reference_date = date(2026, 8, 28)
        evidence = health_insights.build_daily_health_evidence(
            records=personal_records(reference_date),
            reference_date=reference_date,
            now_hour=9,
        )
        narrative = health_insights.fallback_daily_health_narrative(evidence)

        message = health_insights.format_daily_health_insight(
            evidence,
            narrative,
            personalized=False,
        )

        self.assertIn("Daily Health Insight", message)
        self.assertIn("Fri Aug 28, 2026", message)
        self.assertIn("What your data shows", message)
        self.assertIn("Meaning:", message)
        self.assertIn("How this may matter", message)
        self.assertIn("One practical focus", message)
        self.assertIn("temporarily unavailable", message)
        self.assertLessEqual(len(message), 4096)

    def test_app_uses_grounded_fallback_if_gemini_is_unavailable(self) -> None:
        row = [
            "08/28/2026 08:00 AM",
            "1000",
            "800",
            "100",
            "6.5",
            "54",
            "230.5",
            "28",
            "",
            "",
            "0",
            "30.2",
            "74",
            "",
            "",
            "",
        ]
        with (
            patch.object(app, "get_recent_rows", return_value=[row]),
            patch.object(
                app,
                "generate_daily_health_narrative",
                side_effect=RuntimeError("model unavailable"),
            ),
        ):
            message = app.get_daily_health_insight_message(
                reference_date=date(2026, 8, 28),
                now=datetime(2026, 8, 28, 9, 0, tzinfo=app.PACIFIC_TZ),
            )

        self.assertIn("Daily Health Insight", message)
        self.assertIn("calculated evidence directly", message)
        self.assertIn("does not diagnose", message)

    def test_health_menu_routes_to_daily_insight(self) -> None:
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
                "get_daily_health_insight_message",
                return_value="Daily Health Insight\nPersonalized result",
            ) as get_insight,
            patch.object(app, "update_conversation") as update,
            patch.object(app, "send_telegram_msg") as send,
            patch.object(app, "datetime", FixedDateTime),
        ):
            app.process_telegram_update({
                "message": {
                    "chat": {"id": 123},
                    "text": "Daily health insight",
                }
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "health_daily_insight",
        )
        get_insight.assert_called_once_with(
            reference_date=date(2026, 8, 28),
        )
        self.assertIn("comparing", send.call_args_list[0].args[0])
        self.assertIn("Personalized result", send.call_args_list[1].args[0])

    def test_back_from_daily_insight_returns_to_health_menu(self) -> None:
        conversation = {
            "conversation_type": "healthcoach_menu",
            "current_step": "health_daily_insight",
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
                "message": {"chat": {"id": 123}, "text": "Back"}
            })

        self.assertEqual(
            update.call_args.kwargs["current_step"],
            "health",
        )
        self.assertIn("Health Menu", send.call_args.args[0])


if __name__ == "__main__":
    unittest.main()
