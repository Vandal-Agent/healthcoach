from __future__ import annotations

import os
from datetime import date, datetime
from typing import Any

from memory.database import (
    DATABASE_PATH,
    get_connection,
    initialize_database,
)

DEFAULT_BASELINE_DAYS = 30
DEFAULT_MIN_EVALUATED_CASES = 25
DEFAULT_MIN_CONFIDENCE = 0.80


def parse_start_date(value: str | None) -> date | None:
    """Parse an optional YYYY-MM-DD start date."""
    if not value:
        return None

    return datetime.strptime(value, "%Y-%m-%d").date()


def get_learning_start_date() -> date | None:
    """Return the configured Memory collection start date."""
    return parse_start_date(
        os.getenv("HEALTH_MEMORY_START_DATE")
    )


def calculate_learning_status(
    *,
    today: date | None = None,
    baseline_days: int = DEFAULT_BASELINE_DAYS,
    min_evaluated_cases: int = DEFAULT_MIN_EVALUATED_CASES,
    min_confidence: float = DEFAULT_MIN_CONFIDENCE,
) -> dict[str, Any]:
    """Return the current HealthCoach learning phase and readiness."""
    initialize_database()

    current_date = today or date.today()
    start_date = get_learning_start_date()

    with get_connection(DATABASE_PATH) as connection:
        totals = connection.execute(
            """
            SELECT
                COUNT(*) AS total_cases,
                SUM(
                    CASE
                        WHEN status = 'closed' THEN 1
                        ELSE 0
                    END
                ) AS evaluated_cases
            FROM cases
            """
        ).fetchone()

    total_cases = int(totals["total_cases"] or 0)
    evaluated_cases = int(totals["evaluated_cases"] or 0)

    if start_date is None:
        days_collected = 0
        phase = "not_configured"
    else:
        days_collected = max(
            0,
            (current_date - start_date).days + 1,
        )

        if current_date < start_date:
            phase = "scheduled"
        elif days_collected < baseline_days:
            phase = "baseline"
        elif (
            evaluated_cases < min_evaluated_cases
            or total_cases == 0
        ):
            phase = "learning"
        else:
            phase = "adaptive_ready"

    case_progress = min(
        1.0,
        evaluated_cases / min_evaluated_cases,
    )

    day_progress = min(
        1.0,
        days_collected / baseline_days,
    )

    confidence = round(
        min(day_progress, case_progress),
        3,
    )

    adaptive_ready = (
        phase == "adaptive_ready"
        and confidence >= min_confidence
    )

    return {
        "phase": phase,
        "start_date": (
            start_date.isoformat()
            if start_date
            else None
        ),
        "days_collected": days_collected,
        "baseline_days_required": baseline_days,
        "total_cases": total_cases,
        "evaluated_cases": evaluated_cases,
        "minimum_evaluated_cases": min_evaluated_cases,
        "confidence": confidence,
        "minimum_confidence": min_confidence,
        "adaptive_coaching": adaptive_ready,
    }


def format_learning_status(status: dict[str, Any]) -> str:
    """Return a readable learning-status message."""
    phase_names = {
        "not_configured": "Not configured",
        "scheduled": "Scheduled",
        "baseline": "Baseline collection",
        "learning": "Learning",
        "adaptive_ready": "Adaptive ready",
    }

    phase = phase_names.get(
        status["phase"],
        status["phase"],
    )

    adaptive = (
        "ON"
        if status["adaptive_coaching"]
        else "OFF"
    )

    confidence_percent = round(
        status["confidence"] * 100
    )

    return (
        "HealthCoach Learning Status\n\n"
        f"Phase: {phase}\n"
        f"Start date: {status['start_date']}\n"
        f"Days collected: "
        f"{status['days_collected']} / "
        f"{status['baseline_days_required']}\n"
        f"Cases collected: {status['total_cases']}\n"
        f"Cases evaluated: "
        f"{status['evaluated_cases']} / "
        f"{status['minimum_evaluated_cases']}\n"
        f"Confidence: {confidence_percent}%\n"
        f"Adaptive coaching: {adaptive}"
    )


def main() -> None:
    """Print the current learning status."""
    status = calculate_learning_status()
    print(format_learning_status(status))


if __name__ == "__main__":
    main()