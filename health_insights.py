from __future__ import annotations

import json
import re
from datetime import date, datetime, timedelta
from statistics import mean
from typing import Any, Literal

from google.genai import types
from pydantic import BaseModel, Field

from food.nutrition_lookup import MODEL_NAME, get_client


MIN_PERSONAL_BASELINE_READINGS = 5
MIN_WEEKLY_READINGS = 4


class GroundedHealthObservation(BaseModel):
    fact_ids: list[str] = Field(min_length=1, max_length=3)
    interpretation: str = Field(min_length=1, max_length=420)


class DailyHealthNarrative(BaseModel):
    summary: str = Field(min_length=1, max_length=420)
    observations: list[GroundedHealthObservation] = Field(
        min_length=1,
        max_length=4,
    )
    health_connection: str = Field(min_length=1, max_length=650)
    practical_focus: str = Field(min_length=1, max_length=360)
    data_limit: str = Field(min_length=1, max_length=320)


class WeeklyHealthNarrative(DailyHealthNarrative):
    """Grounded narrative fields for one completed-week comparison."""


METRIC_DETAILS = {
    "sleep_hours": ("Sleep", "h", 0.5),
    "resting_heart_rate": ("Resting heart rate", "bpm", 3.0),
    "hrv": ("HRV", "ms", 5.0),
}


def _number(value: float) -> str:
    rounded = round(float(value), 1)
    if rounded.is_integer():
        return str(int(rounded))
    return f"{rounded:.1f}"


def _record_date(record: dict[str, Any]) -> date | None:
    value = record.get("date")
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    try:
        return date.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None


def _valid_values(
    records: list[dict[str, Any]],
    metric: str,
) -> list[float]:
    values = []
    for record in records:
        value = record.get(metric)
        if value is None:
            continue
        try:
            values.append(float(value))
        except (TypeError, ValueError):
            continue
    return values


def _period_records(
    records_by_date: dict[date, dict[str, Any]],
    start_date: date,
    end_date: date,
) -> list[dict[str, Any]]:
    return [
        records_by_date[current_date]
        for current_date in sorted(records_by_date)
        if start_date <= current_date <= end_date
    ]


def build_daily_health_evidence(
    *,
    records: list[dict[str, Any]],
    reference_date: date,
    now_hour: int,
) -> dict[str, Any]:
    """Build numerical facts before any language model is consulted."""
    records_by_date: dict[date, dict[str, Any]] = {}
    for record in records:
        record_date = _record_date(record)
        if record_date is None or record_date > reference_date:
            continue
        records_by_date[record_date] = {
            **record,
            "date": record_date.isoformat(),
        }

    today = records_by_date.get(reference_date, {})
    prior_14 = _period_records(
        records_by_date,
        reference_date - timedelta(days=14),
        reference_date - timedelta(days=1),
    )
    recent_7 = _period_records(
        records_by_date,
        reference_date - timedelta(days=7),
        reference_date - timedelta(days=1),
    )
    previous_7 = _period_records(
        records_by_date,
        reference_date - timedelta(days=14),
        reference_date - timedelta(days=8),
    )
    prior_30 = _period_records(
        records_by_date,
        reference_date - timedelta(days=29),
        reference_date,
    )

    facts: list[dict[str, Any]] = []

    def add_fact(
        category: Literal[
            "recovery",
            "activity",
            "weight",
            "heart",
            "blood_pressure",
            "data_quality",
        ],
        statement: str,
        *,
        metrics: list[str],
        priority: int,
        relationship: str | None = None,
    ) -> None:
        facts.append({
            "id": f"F{len(facts) + 1}",
            "category": category,
            "statement": statement,
            "metrics": metrics,
            "priority": int(priority),
            "relationship": relationship,
        })

    for metric, (label, unit, notable_difference) in (
        METRIC_DETAILS.items()
    ):
        today_value = today.get(metric)
        if today_value is None:
            continue
        baseline_values = _valid_values(prior_14, metric)
        if len(baseline_values) >= MIN_PERSONAL_BASELINE_READINGS:
            baseline = mean(baseline_values)
            difference = float(today_value) - baseline
            if difference >= notable_difference:
                relationship = "above personal baseline"
            elif difference <= -notable_difference:
                relationship = "below personal baseline"
            else:
                relationship = "near personal baseline"
            add_fact(
                "recovery",
                f"Today's {label.lower()} is {_number(today_value)} "
                f"{unit}; the prior fourteen-day personal average is "
                f"{_number(baseline)} {unit} from "
                f"{len(baseline_values)} readings, a difference of "
                f"{float(difference):+.1f} {unit}.",
                metrics=[metric],
                priority=(1 if relationship != "near personal baseline" else 3),
                relationship=relationship,
            )
        else:
            add_fact(
                "data_quality",
                f"Today's {label.lower()} is {_number(today_value)} "
                f"{unit}, but only {len(baseline_values)} prior readings "
                "are available, so no personal-baseline comparison was "
                "made.",
                metrics=[metric],
                priority=5,
                relationship="insufficient baseline",
            )

    yesterday = records_by_date.get(
        reference_date - timedelta(days=1),
        {},
    )
    yesterday_parts = []
    if yesterday.get("steps") is not None:
        yesterday_parts.append(
            f"{int(round(float(yesterday['steps']))):,} steps"
        )
    if yesterday.get("exercise_minutes") is not None:
        yesterday_parts.append(
            f"{_number(yesterday['exercise_minutes'])} Apple Exercise "
            "Minutes"
        )
    if yesterday.get("total_burn") is not None:
        yesterday_parts.append(
            f"{_number(yesterday['total_burn'])} calories of total burn"
        )
    if yesterday_parts:
        add_fact(
            "activity",
            "Yesterday's completed record shows "
            + ", ".join(yesterday_parts)
            + ".",
            metrics=["steps", "exercise_minutes", "total_burn"],
            priority=3,
            relationship="completed day",
        )

    recent_exercise = _valid_values(recent_7, "exercise_minutes")
    if recent_exercise:
        add_fact(
            "activity",
            "Apple Exercise Minutes total "
            f"{_number(sum(recent_exercise))} across "
            f"{len(recent_exercise)} of the last seven completed days.",
            metrics=["exercise_minutes"],
            priority=(2 if len(recent_exercise) >= 5 else 4),
            relationship="seven-day total",
        )

    recent_steps = _valid_values(recent_7, "steps")
    if len(recent_steps) >= MIN_WEEKLY_READINGS:
        add_fact(
            "activity",
            "Steps averaged "
            f"{int(round(mean(recent_steps))):,} across "
            f"{len(recent_steps)} of the last seven completed days.",
            metrics=["steps"],
            priority=4,
            relationship="seven-day average",
        )

    recent_sleep = _valid_values(recent_7, "sleep_hours")
    previous_sleep = _valid_values(previous_7, "sleep_hours")
    if (
        len(recent_sleep) >= MIN_WEEKLY_READINGS
        and len(previous_sleep) >= MIN_WEEKLY_READINGS
    ):
        recent_average = mean(recent_sleep)
        previous_average = mean(previous_sleep)
        difference = recent_average - previous_average
        add_fact(
            "recovery",
            "Average sleep over the last seven completed days is "
            f"{_number(recent_average)} h, compared with "
            f"{_number(previous_average)} h in the preceding seven-day "
            f"period, a difference of {float(difference):+.1f} h.",
            metrics=["sleep_hours"],
            priority=(2 if abs(difference) >= 0.4 else 4),
            relationship=(
                "recent average higher"
                if difference >= 0.4
                else "recent average lower"
                if difference <= -0.4
                else "recent average similar"
            ),
        )

    recent_weight = _valid_values(recent_7, "weight")
    previous_weight = _valid_values(previous_7, "weight")
    if (
        len(recent_weight) >= MIN_WEEKLY_READINGS
        and len(previous_weight) >= MIN_WEEKLY_READINGS
    ):
        recent_average = mean(recent_weight)
        previous_average = mean(previous_weight)
        difference = recent_average - previous_average
        add_fact(
            "weight",
            "Seven-day average weight is "
            f"{_number(recent_average)} lb, compared with "
            f"{_number(previous_average)} lb in the preceding seven-day "
            f"period, a difference of {float(difference):+.1f} lb.",
            metrics=["weight"],
            priority=2,
            relationship=(
                "average increased"
                if difference > 0.2
                else "average decreased"
                if difference < -0.2
                else "average stable"
            ),
        )

    for metric, label, unit in (
        ("cardio_fitness", "Cardio Fitness", "mL/kg/min"),
        ("walking_heart_rate", "walking heart rate", "bpm"),
    ):
        values = _valid_values(prior_30, metric)
        if len(values) < 3:
            continue
        change = values[-1] - values[0]
        add_fact(
            "heart",
            f"Latest recorded {label} is {_number(values[-1])} {unit}; "
            f"the first of {len(values)} recorded days in this thirty-day "
            f"window was {_number(values[0])} {unit}, a recorded change "
            f"of {float(change):+.1f} {unit}.",
            metrics=[metric],
            priority=(3 if abs(change) >= 0.5 else 5),
            relationship="thirty-day recorded change",
        )

    recent_blood_pressure_period = _period_records(
        records_by_date,
        reference_date - timedelta(days=6),
        reference_date,
    )
    blood_pressure_records = [
        record
        for record in recent_blood_pressure_period
        if (
            record.get("blood_pressure_systolic") is not None
            and record.get("blood_pressure_diastolic") is not None
        )
    ]
    if blood_pressure_records:
        systolic = _valid_values(
            blood_pressure_records,
            "blood_pressure_systolic",
        )
        diastolic = _valid_values(
            blood_pressure_records,
            "blood_pressure_diastolic",
        )
        latest = blood_pressure_records[-1]
        add_fact(
            "blood_pressure",
            "The latest recorded blood pressure is "
            f"{_number(latest['blood_pressure_systolic'])}/"
            f"{_number(latest['blood_pressure_diastolic'])} mmHg. "
            f"The available seven-day average is {_number(mean(systolic))}/"
            f"{_number(mean(diastolic))} mmHg from "
            f"{len(blood_pressure_records)} reading"
            f"{'s' if len(blood_pressure_records) != 1 else ''}.",
            metrics=[
                "blood_pressure_systolic",
                "blood_pressure_diastolic",
            ],
            priority=3,
            relationship="recorded blood pressure context",
        )

    today_activity = {
        metric: today.get(metric)
        for metric in ("steps", "exercise_minutes", "total_burn")
        if today.get(metric) is not None
    }
    day_phase = (
        "morning"
        if int(now_hour) < 12
        else "afternoon"
        if int(now_hour) < 19
        else "evening"
    )
    if today_activity:
        activity_labels = {
            "steps": "steps",
            "exercise_minutes": "Apple Exercise Minutes",
            "total_burn": "total burn",
        }
        labels = [
            activity_labels[metric]
            for metric in today_activity
        ]
        if len(labels) == 1:
            label_text = labels[0]
        else:
            label_text = ", ".join(labels[:-1]) + " and " + labels[-1]
        if day_phase == "evening":
            current_parts = []
            if "steps" in today_activity:
                current_parts.append(
                    f"{int(round(float(today_activity['steps']))):,} steps"
                )
            if "exercise_minutes" in today_activity:
                current_parts.append(
                    f"{_number(today_activity['exercise_minutes'])} Apple "
                    "Exercise Minutes"
                )
            if "total_burn" in today_activity:
                current_parts.append(
                    f"{_number(today_activity['total_burn'])} calories of "
                    "total burn"
                )
            statement = (
                "Today's evening record so far shows "
                + ", ".join(current_parts)
                + ". The day is still in progress, so these were not "
                "treated as completed-day totals."
            )
        else:
            statement = (
                f"Today's {label_text} values are still partial because "
                f"this insight was requested in the {day_phase}; they were "
                "not treated as completed-day totals."
            )
        add_fact(
            "data_quality",
            statement,
            metrics=list(today_activity),
            priority=1,
            relationship="partial day",
        )

    coverage = {
        metric: len(_valid_values(prior_14, metric))
        for metric in (
            "sleep_hours",
            "resting_heart_rate",
            "hrv",
            "exercise_minutes",
            "steps",
            "weight",
            "cardio_fitness",
            "walking_heart_rate",
            "blood_pressure_systolic",
        )
    }

    return {
        "reference_date": reference_date.isoformat(),
        "day_phase": day_phase,
        "facts": sorted(
            facts,
            key=lambda fact: (fact["priority"], fact["id"]),
        ),
        "coverage_prior_fourteen_days": coverage,
        "safety": {
            "personal_baseline_minimum": MIN_PERSONAL_BASELINE_READINGS,
            "missing_values_are_not_zero": True,
            "today_activity_is_partial_before_evening": True,
            "single_readings_are_not_diagnoses": True,
        },
    }


def _narrative_text(narrative: DailyHealthNarrative) -> str:
    pieces = [
        narrative.summary,
        narrative.health_connection,
        narrative.practical_focus,
        narrative.data_limit,
    ]
    pieces.extend(
        observation.interpretation
        for observation in narrative.observations
    )
    return " ".join(pieces)


def validate_daily_health_narrative(
    narrative: DailyHealthNarrative,
    evidence: dict[str, Any],
) -> DailyHealthNarrative:
    """Reject ungrounded, numerical, diagnostic, or risky narratives."""
    valid_fact_ids = {
        str(fact["id"])
        for fact in evidence.get("facts") or []
    }
    if not valid_fact_ids:
        raise ValueError("No health evidence is available.")
    for observation in narrative.observations:
        if not set(observation.fact_ids).issubset(valid_fact_ids):
            raise ValueError("Daily insight cited an unknown evidence fact.")
    cited_fact_ids = [
        fact_id
        for observation in narrative.observations
        for fact_id in observation.fact_ids
    ]
    if len(cited_fact_ids) != len(set(cited_fact_ids)):
        raise ValueError("Daily insight cited the same evidence twice.")

    text = _narrative_text(narrative)
    if re.search(r"\d", text):
        raise ValueError(
            "Daily insight introduced numbers outside the evidence display."
        )
    forbidden = (
        "diagnos",
        "risk score",
        "hypertension",
        "heart attack",
        "stroke",
        "normal blood pressure",
        "healthy blood pressure",
        "unhealthy blood pressure",
        "blood pressure is normal",
        "blood pressure is elevated",
        "blood pressure is high",
        "blood pressure is low",
        "stage one",
        "stage two",
        "abnormal",
        "cardiovascular risk",
        "medical risk",
        "disease risk",
        "disease",
        "systemic strain",
        "nervous system is processing",
        "often indicates",
        "steady sleep",
        "consistent sleep",
        "sleep consistency",
        "rest schedule",
        "change your medication",
        "stop taking",
    )
    lowered = text.casefold()
    if any(term in lowered for term in forbidden):
        raise ValueError("Daily insight used prohibited medical language.")
    return narrative


def generate_daily_health_narrative(
    evidence: dict[str, Any],
    *,
    client=None,
) -> DailyHealthNarrative:
    """Turn computed evidence into personalized, constrained language."""
    if not evidence.get("facts"):
        raise ValueError("No recorded health facts are available.")

    prompt = f"""
You are writing one personalized Daily Health Insight for HealthCoach.

Computed evidence:
{json.dumps(evidence, indent=2, sort_keys=True)}

The evidence was calculated before this request. Treat it as the complete and
only source of personal health facts.

Rules:
1. Select the two to four most useful facts, cite their exact fact IDs, and
   explain how the pattern may relate to recovery, energy, appetite,
   cardiovascular health, metabolic health, or consistency.
2. Make the response specific to relationships in this evidence. Do not give
   stock praise or generic comments that could apply to anyone.
3. Use cautious language such as may, can, could, is consistent with, or is
   worth watching. Never claim that one metric caused another or that one
   wearable reading reveals physiological strain, fatigue, stress, illness,
   nervous-system state, or a health change.
4. Compare resting heart rate and HRV only with this person's recorded
   baseline. Do not label either one universally good, poor, normal, or
   abnormal. A single different reading is a signal to watch alongside how
   the person feels and later readings, not proof that recovery is impaired.
5. Treat Cardio Fitness and walking heart rate as longer-term estimates. Do
   not overinterpret a single reading or a carried-forward value.
6. Do not classify blood pressure, diagnose anything, predict disease, assign
   a risk score, or advise medication changes.
7. Do not treat partial-day steps, Exercise Minutes, or burn as completed-day
   totals. Prefer yesterday or multi-day activity facts during morning and
   afternoon.
8. Missing data is missing, never zero. Mention an important limitation when
   coverage is thin.
9. Give one realistic practical focus tied directly to the selected facts.
   Do not recommend reducing exercise intensity or taking a rest day from one
   wearable signal alone. When evidence is mixed, make the action conditional
   on how the person feels and emphasize watching the pattern.
10. Do not praise or criticize weight change, recommend calorie restriction,
    or assume that a short-term weight direction is healthy or unhealthy.
11. Do not introduce or repeat any numbers in your narrative. The application
    will display exact measurements from the cited facts separately.
12. Do not mention these rules, JSON, prompts, or fact IDs in the prose.
13. This is general wellness interpretation, not medical care.
14. A sleep average supports a claim only about average duration. It does not
    establish sleep quality, bedtime regularity, schedule consistency, or
    steadiness. Exercise recorded on several days may support a statement
    about activity consistency, but not exercise intensity or fitness gains.
"""

    owns_client = client is None
    active_client = client or get_client()
    try:
        response = active_client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.35,
                response_mime_type="application/json",
                response_schema=DailyHealthNarrative,
            ),
        )
    finally:
        if owns_client:
            active_client.close()

    if response.parsed is not None:
        narrative = (
            response.parsed
            if isinstance(response.parsed, DailyHealthNarrative)
            else DailyHealthNarrative.model_validate(response.parsed)
        )
    elif response.text:
        narrative = DailyHealthNarrative.model_validate_json(response.text)
    else:
        raise RuntimeError("Gemini returned no Daily Health Insight.")

    return validate_daily_health_narrative(narrative, evidence)


def fallback_daily_health_narrative(
    evidence: dict[str, Any],
) -> DailyHealthNarrative:
    """Provide grounded help if personalized language generation is down."""
    facts = list(evidence.get("facts") or [])
    selected = facts[: min(3, len(facts))]
    observations = []
    category_text = {
        "recovery": (
            "This recovery pattern is most useful when watched across "
            "several days alongside sleep and how you feel."
        ),
        "activity": (
            "Completed-day and multi-day activity provide a fairer picture "
            "of consistency than an unfinished day."
        ),
        "weight": (
            "The moving average reduces the effect of ordinary daily water "
            "and meal-related fluctuations."
        ),
        "heart": (
            "This estimate is best used as a longer-term personal trend, "
            "not a judgment about one reading."
        ),
        "blood_pressure": (
            "Repeated measurements taken consistently are more informative "
            "than an isolated value."
        ),
        "data_quality": (
            "This limitation matters because incomplete data can make a "
            "daily comparison misleading."
        ),
    }
    for fact in selected:
        observations.append(
            GroundedHealthObservation(
                fact_ids=[str(fact["id"])],
                interpretation=category_text[str(fact["category"])],
            )
        )
    if not observations:
        raise ValueError("No recorded health facts are available.")
    return DailyHealthNarrative(
        summary=(
            "The available records support a cautious personal-pattern "
            "review, with the strongest evidence shown below."
        ),
        observations=observations,
        health_connection=(
            "Recovery, activity, sleep, and weight patterns can influence "
            "energy, appetite, consistency, and cardiovascular health, but "
            "the relationship is best judged across repeated days."
        ),
        practical_focus=(
            "Use the strongest pattern below to choose one manageable action "
            "today, then watch whether the multi-day trend changes."
        ),
        data_limit=(
            "This fallback uses only calculated records and avoids drawing a "
            "conclusion from missing or partial-day data."
        ),
    )


def format_daily_health_insight(
    evidence: dict[str, Any],
    narrative: DailyHealthNarrative,
    *,
    personalized: bool,
) -> str:
    """Combine exact evidence with its grounded natural-language meaning."""
    facts_by_id = {
        str(fact["id"]): fact
        for fact in evidence.get("facts") or []
    }
    reference_text = str(evidence.get("reference_date") or "")
    try:
        parsed_reference = date.fromisoformat(reference_text)
        reference_text = (
            f"{parsed_reference.strftime('%a %b ')}"
            f"{parsed_reference.day}, {parsed_reference.year}"
        )
    except ValueError:
        pass
    lines = [
        "Daily Health Insight",
        reference_text,
        "",
        narrative.summary,
        "",
        "What your data shows",
    ]
    shown_fact_ids: set[str] = set()
    for observation in narrative.observations:
        statements = []
        for fact_id in observation.fact_ids:
            if fact_id in shown_fact_ids or fact_id not in facts_by_id:
                continue
            shown_fact_ids.add(fact_id)
            statements.append(str(facts_by_id[fact_id]["statement"]))
        if not statements:
            continue
        lines.append("- " + " ".join(statements))
        lines.append("  Meaning: " + observation.interpretation)

    lines.extend([
        "",
        "How this may matter",
        narrative.health_connection,
        "",
        "One practical focus",
        narrative.practical_focus,
        "",
        "Keep in mind",
        narrative.data_limit,
        "",
        (
            "This insight compares your recorded personal patterns. It does "
            "not diagnose, classify cardiovascular risk, or replace medical "
            "care."
        ),
        *(
            []
            if personalized
            else [
                "Personalized wording was temporarily unavailable, so this "
                "version uses the calculated evidence directly."
            ]
        ),
        "",
        "Reply Refresh insight, Back, or Cancel.",
    ])
    message = "\n".join(lines)
    if len(message) > 4096:
        raise ValueError("Daily Health Insight exceeded Telegram's limit.")
    return message


def build_weekly_health_evidence(
    *,
    records: list[dict[str, Any]],
    reference_date: date,
) -> dict[str, Any]:
    """Compare the last seven completed days with the seven before them."""
    records_by_date: dict[date, dict[str, Any]] = {}
    for record in records:
        record_date = _record_date(record)
        if record_date is None or record_date >= reference_date:
            continue
        records_by_date[record_date] = {
            **record,
            "date": record_date.isoformat(),
        }

    recent_start = reference_date - timedelta(days=7)
    recent_end = reference_date - timedelta(days=1)
    previous_start = reference_date - timedelta(days=14)
    previous_end = reference_date - timedelta(days=8)
    recent = _period_records(records_by_date, recent_start, recent_end)
    previous = _period_records(
        records_by_date,
        previous_start,
        previous_end,
    )
    prior_28 = _period_records(
        records_by_date,
        reference_date - timedelta(days=28),
        recent_end,
    )
    facts: list[dict[str, Any]] = []

    def add_fact(
        category: str,
        statement: str,
        *,
        metrics: list[str],
        priority: int,
        relationship: str,
    ) -> None:
        facts.append({
            "id": f"F{len(facts) + 1}",
            "category": category,
            "statement": statement,
            "metrics": metrics,
            "priority": int(priority),
            "relationship": relationship,
        })

    comparisons = (
        ("sleep_hours", "Average sleep", "h", "recovery", 0.4),
        (
            "resting_heart_rate",
            "Average resting heart rate",
            "bpm",
            "recovery",
            3.0,
        ),
        ("hrv", "Average HRV", "ms", "recovery", 5.0),
        ("steps", "Average steps", "steps", "activity", 750.0),
        (
            "total_burn",
            "Average total burn",
            "calories",
            "activity",
            150.0,
        ),
        ("weight", "Average weight", "lb", "weight", 0.5),
    )
    for metric, label, unit, category, notable_difference in comparisons:
        recent_values = _valid_values(recent, metric)
        previous_values = _valid_values(previous, metric)
        if (
            len(recent_values) < MIN_WEEKLY_READINGS
            or len(previous_values) < MIN_WEEKLY_READINGS
        ):
            continue
        recent_average = mean(recent_values)
        previous_average = mean(previous_values)
        difference = recent_average - previous_average
        relationship = (
            "recent average higher"
            if difference >= notable_difference
            else "recent average lower"
            if difference <= -notable_difference
            else "recent average similar"
        )
        if metric == "steps":
            recent_text = f"{int(round(recent_average)):,}"
            previous_text = f"{int(round(previous_average)):,}"
            difference_text = f"{difference:+,.0f}"
        else:
            recent_text = _number(recent_average)
            previous_text = _number(previous_average)
            difference_text = f"{difference:+.1f}"
        add_fact(
            category,
            f"{label} was {recent_text} {unit} across "
            f"{len(recent_values)} recorded days, compared with "
            f"{previous_text} {unit} across {len(previous_values)} "
            f"recorded days in the preceding week, a difference of "
            f"{difference_text} {unit}.",
            metrics=[metric],
            priority=(2 if relationship != "recent average similar" else 4),
            relationship=relationship,
        )

    recent_exercise = _valid_values(recent, "exercise_minutes")
    previous_exercise = _valid_values(previous, "exercise_minutes")
    if recent_exercise:
        statement = (
            "Apple Exercise Minutes totaled "
            f"{_number(sum(recent_exercise))} across "
            f"{len(recent_exercise)} recorded days in the completed week"
        )
        relationship = "completed-week activity"
        priority = 3
        if previous_exercise:
            difference = sum(recent_exercise) - sum(previous_exercise)
            statement += (
                ", compared with "
                f"{_number(sum(previous_exercise))} across "
                f"{len(previous_exercise)} recorded days in the preceding "
                f"week, a difference of {difference:+.1f} minutes"
            )
            relationship = (
                "recent total higher"
                if difference >= 30
                else "recent total lower"
                if difference <= -30
                else "recent total similar"
            )
            priority = 2 if abs(difference) >= 30 else 4
        add_fact(
            "activity",
            statement + ".",
            metrics=["exercise_minutes"],
            priority=priority,
            relationship=relationship,
        )

    for metric, label, unit in (
        ("cardio_fitness", "Cardio Fitness", "mL/kg/min"),
        ("walking_heart_rate", "walking heart rate", "bpm"),
    ):
        values = _valid_values(prior_28, metric)
        if len(values) < 3:
            continue
        change = values[-1] - values[0]
        add_fact(
            "heart",
            f"Latest recorded {label} was {_number(values[-1])} {unit}; "
            f"the first of {len(values)} recorded days in the prior "
            f"twenty-eight-day window was {_number(values[0])} {unit}, "
            f"a recorded change of {change:+.1f} {unit}.",
            metrics=[metric],
            priority=(3 if abs(change) >= 0.5 else 5),
            relationship="longer-term recorded change",
        )

    blood_pressure_records = [
        record
        for record in recent
        if (
            record.get("blood_pressure_systolic") is not None
            and record.get("blood_pressure_diastolic") is not None
        )
    ]
    if blood_pressure_records:
        systolic = _valid_values(
            blood_pressure_records,
            "blood_pressure_systolic",
        )
        diastolic = _valid_values(
            blood_pressure_records,
            "blood_pressure_diastolic",
        )
        add_fact(
            "blood_pressure",
            "Recorded blood pressure averaged "
            f"{_number(mean(systolic))}/{_number(mean(diastolic))} mmHg "
            f"from {len(blood_pressure_records)} reading"
            f"{'s' if len(blood_pressure_records) != 1 else ''} during "
            "the completed week.",
            metrics=[
                "blood_pressure_systolic",
                "blood_pressure_diastolic",
            ],
            priority=4,
            relationship="completed-week recorded context",
        )

    coverage_metrics = (
        ("sleep_hours", "sleep"),
        ("steps", "steps"),
        ("exercise_minutes", "Exercise Minutes"),
        ("resting_heart_rate", "resting heart rate"),
        ("hrv", "HRV"),
        ("weight", "weight"),
    )
    coverage = {
        metric: len(_valid_values(recent, metric))
        for metric, _label in coverage_metrics
    }
    thin = [
        f"{label} {coverage[metric]}/7"
        for metric, label in coverage_metrics
        if coverage[metric] < MIN_WEEKLY_READINGS
    ]
    if thin and (recent or previous):
        add_fact(
            "data_quality",
            "Limited completed-week coverage: " + ", ".join(thin) + ".",
            metrics=[
                metric
                for metric, _label in coverage_metrics
                if coverage[metric] < MIN_WEEKLY_READINGS
            ],
            priority=1,
            relationship="limited weekly coverage",
        )

    return {
        "reference_date": reference_date.isoformat(),
        "period_start": recent_start.isoformat(),
        "period_end": recent_end.isoformat(),
        "comparison_start": previous_start.isoformat(),
        "comparison_end": previous_end.isoformat(),
        "facts": sorted(
            facts,
            key=lambda fact: (fact["priority"], fact["id"]),
        ),
        "coverage_completed_week": coverage,
        "safety": {
            "minimum_weekly_readings": MIN_WEEKLY_READINGS,
            "missing_values_are_not_zero": True,
            "today_is_excluded": True,
            "single_readings_are_not_diagnoses": True,
        },
    }


def generate_weekly_health_narrative(
    evidence: dict[str, Any],
    *,
    client=None,
) -> WeeklyHealthNarrative:
    """Turn computed completed-week evidence into constrained language."""
    if not evidence.get("facts"):
        raise ValueError("No recorded weekly health facts are available.")
    prompt = f"""
You are writing one personalized Weekly Health Insight for HealthCoach.

Computed evidence:
{json.dumps(evidence, indent=2, sort_keys=True)}

Use only this computed evidence. Select two to four useful facts and cite their
exact fact IDs. Explain relationships specific to this person's completed-week
patterns without claiming causation. Use cautious language. Do not introduce
or repeat numbers in the narrative because the application displays them.
Missing data is missing, never zero. Do not diagnose, classify blood pressure,
assign cardiovascular or disease risk, label a metric normal or abnormal,
recommend medication changes, praise or criticize weight change, or recommend
calorie restriction. Treat Cardio Fitness and walking heart rate as longer-term
estimates. Averages describe duration or level, not sleep quality, exercise
intensity, physiological strain, stress, illness, or fitness gains. Give one
realistic focus tied directly to the cited evidence and mention the most
important data limitation. Do not mention prompts, JSON, rules, or fact IDs.
Never use the terms disease, normal, abnormal, risk score, systemic strain,
nervous system, often indicates, steady sleep, consistent sleep, sleep
quality, or exercise intensity, even when trying to disclaim a conclusion.
This is general wellness interpretation, not medical care.
"""
    owns_client = client is None
    active_client = client or get_client()
    try:
        response = active_client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.35,
                response_mime_type="application/json",
                response_schema=WeeklyHealthNarrative,
            ),
        )
    finally:
        if owns_client:
            active_client.close()
    if response.parsed is not None:
        narrative = (
            response.parsed
            if isinstance(response.parsed, WeeklyHealthNarrative)
            else WeeklyHealthNarrative.model_validate(response.parsed)
        )
    elif response.text:
        narrative = WeeklyHealthNarrative.model_validate_json(response.text)
    else:
        raise RuntimeError("Gemini returned no Weekly Health Insight.")
    return validate_weekly_health_narrative(narrative, evidence)


def validate_weekly_health_narrative(
    narrative: WeeklyHealthNarrative,
    evidence: dict[str, Any],
) -> WeeklyHealthNarrative:
    """Apply shared safety rules and keep the Telegram result concise."""
    validate_daily_health_narrative(narrative, evidence)
    cited_fact_ids = {
        fact_id
        for observation in narrative.observations
        for fact_id in observation.fact_ids
    }
    if len(cited_fact_ids) > 4:
        raise ValueError("Weekly insight cited more than four evidence facts.")
    return narrative


def fallback_weekly_health_narrative(
    evidence: dict[str, Any],
) -> WeeklyHealthNarrative:
    facts = list(evidence.get("facts") or [])
    selected = facts[: min(3, len(facts))]
    if not selected:
        raise ValueError("No recorded weekly health facts are available.")
    selected_metrics = {
        metric
        for fact in selected
        for metric in fact.get("metrics") or []
    }

    def interpretation(fact: dict[str, Any]) -> str:
        metrics = set(fact.get("metrics") or [])
        relationship = str(fact.get("relationship") or "")
        if "steps" in metrics:
            if relationship == "recent average lower":
                return (
                    "The completed week recorded less average daily movement "
                    "than the comparison week. Another completed week can "
                    "show whether this was temporary or a continuing pattern."
                )
            if relationship == "recent average higher":
                return (
                    "The completed week recorded more average daily movement "
                    "than the comparison week. Repeating the comparison can "
                    "show whether that change continues."
                )
            return (
                "Average daily movement was similar across the two completed "
                "weeks, which provides a useful routine baseline."
            )
        if "total_burn" in metrics:
            if relationship == "recent average lower":
                if "steps" in selected_metrics:
                    return (
                        "Recorded total burn was lower in the completed week. "
                        "Steps moved the same way, so the records describe a "
                        "lower-movement week without establishing one cause."
                    )
                return (
                    "Recorded total burn was lower in the completed week. "
                    "Movement records can provide useful context without "
                    "establishing what caused the change."
                )
            if relationship == "recent average higher":
                return (
                    "Recorded total burn was higher in the completed week. "
                    "Comparing it with movement helps describe the week, but "
                    "the records do not establish what caused the change."
                )
            return (
                "Recorded total burn was similar across the two weeks, so a "
                "longer pattern may be more useful than this comparison alone."
            )
        if "weight" in metrics:
            return (
                "Weekly averages reduce the effect of ordinary daily "
                "fluctuations. Another completed week is needed to judge "
                "whether this direction persists."
            )
        if "sleep_hours" in metrics:
            return (
                "This compares recorded sleep duration across completed "
                "weeks. Watching the next week alongside daytime energy can "
                "make the pattern more useful."
            )
        if "exercise_minutes" in metrics:
            return (
                "Recorded intentional movement changed across the two weeks. "
                "The total does not describe the type or difficulty of that "
                "activity."
            )
        if metrics.intersection({"resting_heart_rate", "hrv"}):
            return (
                "This personal recovery comparison is most useful across "
                "repeated weeks and alongside how you feel."
            )
        if metrics.intersection({"cardio_fitness", "walking_heart_rate"}):
            return (
                "This estimate is best treated as a longer-term personal "
                "trend rather than a judgment about one week."
            )
        if "blood_pressure_systolic" in metrics:
            return (
                "Repeated readings taken under similar conditions provide "
                "more context than an isolated weekly average."
            )
        return (
            "This coverage limitation matters because missing days can make "
            "a weekly comparison less representative."
        )

    observations = [
        GroundedHealthObservation(
            fact_ids=[str(fact["id"])],
            interpretation=interpretation(fact),
        )
        for fact in selected
    ]
    if {"steps", "total_burn"}.issubset(selected_metrics):
        summary = (
            "The clearest completed-week difference is in recorded movement "
            "and total burn"
            + (
                ", with the weight average adding context."
                if "weight" in selected_metrics
                else "."
            )
        )
        connection = (
            "Steps and total burn moved together across the two weeks. That "
            "combination may help explain how the overall routine changed, "
            "while the records still cannot establish cause."
        )
    elif selected_metrics.intersection(
        {"sleep_hours", "resting_heart_rate", "hrv"}
    ):
        summary = (
            "The completed-week comparison is most informative for recovery "
            "and activity patterns."
        )
        connection = (
            "Sleep duration and personal recovery measurements can be viewed "
            "together across repeated weeks to support decisions about daily "
            "routine and energy."
        )
    else:
        summary = (
            "The available completed-week records show a personal pattern "
            "worth comparing again after another week."
        )
        connection = (
            "Repeated completed-week comparisons can make changes in activity, "
            "recovery, and weight easier to distinguish from short-term noise."
        )

    activity_lower = any(
        (
            "steps" in set(fact.get("metrics") or [])
            and fact.get("relationship") == "recent average lower"
        )
        or (
            "exercise_minutes" in set(fact.get("metrics") or [])
            and fact.get("relationship") == "recent total lower"
        )
        for fact in selected
    )
    activity_higher = any(
        (
            "steps" in set(fact.get("metrics") or [])
            and fact.get("relationship") == "recent average higher"
        )
        or (
            "exercise_minutes" in set(fact.get("metrics") or [])
            and fact.get("relationship") == "recent total higher"
        )
        for fact in selected
    )
    sleep_lower = any(
        "sleep_hours" in set(fact.get("metrics") or [])
        and fact.get("relationship") == "recent average lower"
        for fact in selected
    )
    if activity_lower:
        practical_focus = (
            "Consider what changed your movement during the completed week. "
            "If the reduction was not intentional, choose one repeatable "
            "opportunity to add movement in the coming week."
        )
    elif activity_higher:
        practical_focus = (
            "Identify which routine change supported the added movement. "
            "Keep it only if it remains manageable in the coming week."
        )
    elif sleep_lower:
        practical_focus = (
            "Choose one realistic way to protect your sleep opportunity in "
            "the coming week, then compare the next completed-week average."
        )
    else:
        practical_focus = (
            "Keep the most relevant routine manageable for another week, then "
            "compare the same completed measures again."
        )

    coverage = evidence.get("coverage_completed_week") or {}
    limited = any(
        int(value) < MIN_WEEKLY_READINGS
        for value in coverage.values()
    )
    return WeeklyHealthNarrative(
        summary=summary,
        observations=observations,
        health_connection=connection,
        practical_focus=practical_focus,
        data_limit=(
            (
                "Some completed-week measures have limited coverage, so the "
                "comparison may not represent the full week. Missing days "
                "were not treated as zero."
            )
            if limited
            else (
                "This comparison uses recorded completed days. It cannot "
                "describe unrecorded context behind the changes."
            )
        ),
    )


def format_weekly_health_insight(
    evidence: dict[str, Any],
    narrative: WeeklyHealthNarrative,
    *,
    personalized: bool,
) -> str:
    facts_by_id = {
        str(fact["id"]): fact
        for fact in evidence.get("facts") or []
    }
    start = date.fromisoformat(str(evidence["period_start"]))
    end = date.fromisoformat(str(evidence["period_end"]))
    period_text = (
        f"{start.strftime('%b ')}{start.day}–"
        f"{end.strftime('%b ')}{end.day}, {end.year}"
    )
    lines = [
        "Weekly Health Insight",
        period_text,
        "",
        narrative.summary,
        "",
        "What your completed-week data shows",
    ]
    shown_fact_ids: set[str] = set()
    for observation in narrative.observations:
        statements = []
        for fact_id in observation.fact_ids:
            if fact_id in shown_fact_ids or fact_id not in facts_by_id:
                continue
            shown_fact_ids.add(fact_id)
            statements.append(str(facts_by_id[fact_id]["statement"]))
        if statements:
            lines.append("- " + " ".join(statements))
            lines.append("  Meaning: " + observation.interpretation)
    lines.extend([
        "",
        "How this may matter",
        narrative.health_connection,
        "",
        "One focus for the coming week",
        narrative.practical_focus,
        "",
        "Keep in mind",
        narrative.data_limit,
        "",
        (
            "This insight compares recorded personal patterns. It does not "
            "diagnose, classify cardiovascular risk, or replace medical care."
        ),
        *(
            []
            if personalized
            else [
                "Personalized wording was temporarily unavailable, so this "
                "version uses the calculated evidence directly."
            ]
        ),
        "",
        "Reply Refresh weekly insight, Back, or Cancel.",
    ])
    message = "\n".join(lines)
    if len(message) > 4096:
        raise ValueError("Weekly Health Insight exceeded Telegram's limit.")
    return message
