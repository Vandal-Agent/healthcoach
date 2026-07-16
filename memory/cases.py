from __future__ import annotations

import json
from datetime import date, datetime
from typing import Any

from memory.database import (
    DATABASE_PATH,
    LOGIC_VERSION,
    current_timestamp,
    get_connection,
    initialize_database,
)


def normalize_date(value: date | str) -> str:
    """Return a YYYY-MM-DD date string."""
    if isinstance(value, date):
        return value.isoformat()

    parsed = datetime.strptime(value, "%Y-%m-%d")
    return parsed.date().isoformat()


def validate_confidence(value: float, field_name: str) -> float:
    """Validate that a confidence value is between 0.0 and 1.0."""
    numeric_value = float(value)

    if not 0.0 <= numeric_value <= 1.0:
        raise ValueError(f"{field_name} must be between 0.0 and 1.0.")

    return numeric_value


def get_recommendation(recommendation_code: str) -> dict[str, Any]:
    """Return one enabled recommendation by its stable code."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT
                recommendation_id,
                recommendation_code,
                case_type,
                recommendation_text,
                recommendation_reason,
                expected_metric,
                default_expected_threshold,
                priority_rank
            FROM recommendations
            WHERE recommendation_code = ?
              AND enabled = 1
            """,
            (recommendation_code,),
        ).fetchone()

    if row is None:
        raise ValueError(
            f"Enabled recommendation not found: {recommendation_code}"
        )

    return dict(row)


def find_open_duplicate(
    *,
    case_date: str,
    case_type: str,
    observation_code: str,
    recommendation_code: str,
) -> dict[str, Any] | None:
    """Find an existing open or evaluated Case for the same situation."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM cases
            WHERE case_date = ?
              AND case_type = ?
              AND observation_code = ?
              AND recommendation_code = ?
              AND status IN ('open', 'evaluated')
            ORDER BY case_id DESC
            LIMIT 1
            """,
            (
                case_date,
                case_type,
                observation_code,
                recommendation_code,
            ),
        ).fetchone()

    return dict(row) if row else None


def create_case(
    *,
    case_date: date | str,
    case_type: str,
    priority: str,
    observation_code: str,
    observation: str,
    supporting_data: dict[str, Any],
    data_confidence: float,
    recommendation_code: str,
    expected_result: str,
    evaluation_due_at: str,
    tags: list[str] | None = None,
    meal_category: str | None = None,
    estimated_window_start: str | None = None,
    estimated_window_end: str | None = None,
    notes: str | None = None,
) -> dict[str, Any]:
    """
    Create one coaching Case.

    Returns:
        {
            "created": True or False,
            "case": case row as a dictionary
        }

    If a matching open or evaluated Case already exists for the same day,
    observation, and recommendation, the existing Case is returned instead
    of creating a duplicate.
    """
    normalized_case_date = normalize_date(case_date)

    if priority not in {"low", "medium", "high"}:
        raise ValueError("priority must be low, medium, or high.")

    confidence = validate_confidence(
        data_confidence,
        "data_confidence",
    )

    if not case_type.strip():
        raise ValueError("case_type is required.")

    if not observation_code.strip():
        raise ValueError("observation_code is required.")

    if not observation.strip():
        raise ValueError("observation is required.")

    if not expected_result.strip():
        raise ValueError("expected_result is required.")

    if not isinstance(supporting_data, dict):
        raise TypeError("supporting_data must be a dictionary.")

    datetime.fromisoformat(evaluation_due_at)

    recommendation = get_recommendation(recommendation_code)

    if recommendation["case_type"] != case_type:
        raise ValueError(
            "Recommendation case type does not match the Case type."
        )

    duplicate = find_open_duplicate(
        case_date=normalized_case_date,
        case_type=case_type,
        observation_code=observation_code,
        recommendation_code=recommendation_code,
    )

    if duplicate:
        return {
            "created": False,
            "case": duplicate,
        }

    timestamp = current_timestamp()

    tags_json = json.dumps(tags or [], separators=(",", ":"))
    supporting_data_json = json.dumps(
        supporting_data,
        separators=(",", ":"),
        sort_keys=True,
    )

    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            INSERT INTO cases (
                created_at,
                case_date,
                status,
                case_type,
                priority,
                tags_json,
                observation_code,
                observation,
                supporting_data_json,
                data_confidence,
                meal_category,
                estimated_window_start,
                estimated_window_end,
                recommendation_id,
                recommendation_code,
                recommendation_text,
                recommendation_reason,
                expected_result,
                expected_metric,
                expected_threshold,
                evaluation_due_at,
                followed_status,
                observation_source,
                recommendation_source,
                logic_version,
                created_by,
                notes,
                updated_at
            )
            VALUES (
                ?, ?, 'open', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                ?, ?, ?, 'unknown', 'rules', 'rules', ?, 'healthcoach', ?, ?
            )
            """,
            (
                timestamp,
                normalized_case_date,
                case_type,
                priority,
                tags_json,
                observation_code,
                observation,
                supporting_data_json,
                confidence,
                meal_category,
                estimated_window_start,
                estimated_window_end,
                recommendation["recommendation_id"],
                recommendation["recommendation_code"],
                recommendation["recommendation_text"],
                recommendation["recommendation_reason"],
                expected_result,
                recommendation["expected_metric"],
                recommendation["default_expected_threshold"],
                evaluation_due_at,
                LOGIC_VERSION,
                notes,
                timestamp,
            ),
        )

        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM cases
            WHERE case_id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()

    if row is None:
        raise RuntimeError("Case was inserted but could not be retrieved.")

    return {
        "created": True,
        "case": dict(row),
    }


def get_case(case_id: int) -> dict[str, Any] | None:
    """Return one Case by ID."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM cases
            WHERE case_id = ?
            """,
            (case_id,),
        ).fetchone()

    return dict(row) if row else None


def list_open_cases() -> list[dict[str, Any]]:
    """Return all open Cases ordered by evaluation time."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM cases
            WHERE status = 'open'
            ORDER BY evaluation_due_at, case_id
            """
        ).fetchall()

    return [dict(row) for row in rows]

def close_case(
    *,
    case_id: int,
    followed_status: str,
    follow_through_evidence: str | None,
    follow_through_confidence: float | None,
    actual_result: str,
    actual_value: float | None,
    successful: bool,
    outcome_confidence: float,
    evaluator_source: str = "rules",
    notes: str | None = None,
) -> dict[str, Any]:
    """Evaluate and close one open Case."""
    allowed_followed = {
        "yes",
        "no",
        "partial",
        "likely",
        "unknown",
    }
    allowed_sources = {
        "rules",
        "ai",
        "hybrid",
        "user",
    }

    if followed_status not in allowed_followed:
        raise ValueError("Invalid followed_status.")

    if evaluator_source not in allowed_sources:
        raise ValueError("Invalid evaluator_source.")

    if not actual_result.strip():
        raise ValueError("actual_result is required.")

    follow_confidence = None
    if follow_through_confidence is not None:
        follow_confidence = validate_confidence(
            follow_through_confidence,
            "follow_through_confidence",
        )

    result_confidence = validate_confidence(
        outcome_confidence,
        "outcome_confidence",
    )

    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        existing = connection.execute(
            """
            SELECT *
            FROM cases
            WHERE case_id = ?
            """,
            (case_id,),
        ).fetchone()

        if existing is None:
            raise ValueError(f"Case not found: {case_id}")

        if existing["status"] != "open":
            return {
                "updated": False,
                "case": dict(existing),
            }

        connection.execute(
            """
            UPDATE cases
            SET
                status = 'closed',
                followed_status = ?,
                follow_through_evidence = ?,
                follow_through_confidence = ?,
                actual_result = ?,
                actual_value = ?,
                successful = ?,
                outcome_confidence = ?,
                evaluated_at = ?,
                closed_at = ?,
                evaluator_source = ?,
                notes = COALESCE(?, notes),
                updated_at = ?
            WHERE case_id = ?
            """,
            (
                followed_status,
                follow_through_evidence,
                follow_confidence,
                actual_result,
                actual_value,
                1 if successful else 0,
                result_confidence,
                timestamp,
                timestamp,
                evaluator_source,
                notes,
                timestamp,
                case_id,
            ),
        )
        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM cases
            WHERE case_id = ?
            """,
            (case_id,),
        ).fetchone()

    if row is None:
        raise RuntimeError(
            "Case was updated but could not be retrieved."
        )

    return {
        "updated": True,
        "case": dict(row),
    }


def evaluate_missing_data_cases(
    *,
    case_date: date | str,
    nutrition_data_available: bool,
    dietary_cals: float,
    protein: float,
    latest_update: str | None,
) -> list[dict[str, Any]]:
    """Evaluate and close open missing-data Cases for one date."""
    normalized_case_date = normalize_date(case_date)

    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM cases
            WHERE status = 'open'
              AND case_type = 'missing_data'
              AND case_date = ?
            ORDER BY case_id
            """,
            (normalized_case_date,),
        ).fetchall()

    results: list[dict[str, Any]] = []

    for row in rows:
        case = dict(row)

        if nutrition_data_available:
            result = close_case(
                case_id=case["case_id"],
                followed_status="likely",
                follow_through_evidence=(
                    "Nutrition data appeared after the midday reminder."
                ),
                follow_through_confidence=0.85,
                actual_result=(
                    "Nutrition data was available by the evening check."
                ),
                actual_value=float(dietary_cals),
                successful=True,
                outcome_confidence=0.95,
                evaluator_source="rules",
                notes=(
                    f"Latest update: {latest_update}; "
                    f"protein: {float(protein):.1f}g"
                ),
            )
        else:
            result = close_case(
                case_id=case["case_id"],
                followed_status="unknown",
                follow_through_evidence=(
                    "Nutrition data was still unavailable at the evening check."
                ),
                follow_through_confidence=0.90,
                actual_result=(
                    "Nutrition data was still missing by the evening check."
                ),
                actual_value=None,
                successful=False,
                outcome_confidence=0.95,
                evaluator_source="rules",
                notes=f"Latest update: {latest_update}",
            )

        results.append(result)

    return results
