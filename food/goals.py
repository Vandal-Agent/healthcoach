from __future__ import annotations

import math
from datetime import date
from pathlib import Path
from typing import Any

from food.database import (
    DATABASE_PATH,
    current_timestamp,
    get_connection,
    initialize_database,
)


MINIMUM_DAILY_CALORIES = 1500.0
MAXIMUM_DAILY_DEFICIT = 1000.0
MAXIMUM_WEEKLY_LOSS = 2.0
MINIMUM_BURN_DAYS = 3
CALORIE_RANGE_WIDTH = 150.0


def normalize_date(value: date | str, *, field_name: str) -> str:
    if isinstance(value, date):
        return value.isoformat()

    normalized = str(value or "").strip()
    try:
        return date.fromisoformat(normalized).isoformat()
    except ValueError as error:
        raise ValueError(f"{field_name} must be a valid date.") from error


def round_to_50(value: float) -> float:
    """Round positive calorie values to the nearest 50."""
    return float(math.floor(float(value) / 50.0 + 0.5) * 50)


def calculate_weight_goal(
    *,
    current_date: date,
    current_weight: float,
    target_weight: float,
    target_date: date,
    average_daily_burn: float,
    burn_days: int,
) -> dict[str, Any]:
    """Calculate one explicitly requested, safety-capped goal update."""
    current = float(current_weight)
    target = float(target_weight)
    average_burn = float(average_daily_burn)
    valid_burn_days = int(burn_days)

    if current <= 0 or target <= 0:
        raise ValueError("Weights must be greater than zero.")
    if average_burn <= 0:
        raise ValueError("Average daily burn must be greater than zero.")
    if valid_burn_days < MINIMUM_BURN_DAYS:
        raise ValueError(
            "At least three completed days of burn data are required."
        )

    days_remaining = (target_date - current_date).days
    if days_remaining < 0:
        raise ValueError("The goal date has already passed.")

    pounds_remaining = max(current - target, 0.0)
    if pounds_remaining > 0 and days_remaining == 0:
        required_weekly_loss = float("inf")
        required_daily_deficit = float("inf")
    elif pounds_remaining == 0:
        required_weekly_loss = 0.0
        required_daily_deficit = 0.0
    else:
        required_weekly_loss = (
            pounds_remaining * 7.0 / days_remaining
        )
        required_daily_deficit = (
            pounds_remaining * 3500.0 / days_remaining
        )

    floor_capacity = max(
        average_burn - MINIMUM_DAILY_CALORIES,
        0.0,
    )
    safe_deficit_capacity = min(
        MAXIMUM_DAILY_DEFICIT,
        floor_capacity,
    )

    safely_reachable = (
        required_weekly_loss <= MAXIMUM_WEEKLY_LOSS
        and required_daily_deficit <= MAXIMUM_DAILY_DEFICIT
        and required_daily_deficit <= floor_capacity
    )

    planned_daily_deficit = (
        required_daily_deficit
        if safely_reachable
        else safe_deficit_capacity
    )
    planned_daily_deficit = max(planned_daily_deficit, 0.0)

    target_center = max(
        average_burn - planned_daily_deficit,
        MINIMUM_DAILY_CALORIES,
    )
    calorie_target_low = max(
        MINIMUM_DAILY_CALORIES,
        round_to_50(target_center - CALORIE_RANGE_WIDTH / 2.0),
    )
    calorie_target_high = calorie_target_low + CALORIE_RANGE_WIDTH

    projected_daily_deficit = max(
        average_burn
        - (calorie_target_low + calorie_target_high) / 2.0,
        0.0,
    )
    projected_loss = (
        projected_daily_deficit * days_remaining / 3500.0
    )
    projected_weight = max(current - projected_loss, 1.0)

    limiting_reasons = []
    if required_weekly_loss > MAXIMUM_WEEKLY_LOSS:
        limiting_reasons.append("more than 2 lb per week")
    if required_daily_deficit > MAXIMUM_DAILY_DEFICIT:
        limiting_reasons.append("more than a 1,000 calorie daily deficit")
    if required_daily_deficit > floor_capacity:
        limiting_reasons.append("the 1,500 calorie safety floor")

    return {
        "calculation_date": current_date.isoformat(),
        "current_weight": round(current, 3),
        "average_daily_burn": round(average_burn, 3),
        "burn_days": valid_burn_days,
        "days_remaining": days_remaining,
        "required_weekly_loss": (
            None
            if math.isinf(required_weekly_loss)
            else round(required_weekly_loss, 4)
        ),
        "required_daily_deficit": (
            None
            if math.isinf(required_daily_deficit)
            else round(required_daily_deficit, 3)
        ),
        "planned_daily_deficit": round(planned_daily_deficit, 3),
        "calorie_target_low": calorie_target_low,
        "calorie_target_high": calorie_target_high,
        "safely_reachable": safely_reachable,
        "projected_weight": round(projected_weight, 3),
        "limiting_reason": ", ".join(limiting_reasons) or None,
    }


def create_weight_goal(
    *,
    start_date: date | str,
    start_weight: float,
    target_weight: float,
    target_date: date | str,
    database_path: Path | None = None,
) -> dict[str, Any]:
    """Create the only active weight-loss goal."""
    database_path = database_path or DATABASE_PATH
    initialize_database(database_path)
    normalized_start = normalize_date(start_date, field_name="start_date")
    normalized_target = normalize_date(target_date, field_name="target_date")
    start = date.fromisoformat(normalized_start)
    target_day = date.fromisoformat(normalized_target)
    starting_weight = float(start_weight)
    desired_weight = float(target_weight)

    if starting_weight <= 0 or desired_weight <= 0:
        raise ValueError("Weights must be greater than zero.")
    if desired_weight >= starting_weight:
        raise ValueError(
            "The target weight must be below the starting weight."
        )
    if target_day <= start:
        raise ValueError("The goal date must be after the start date.")

    timestamp = current_timestamp()
    with get_connection(database_path) as connection:
        existing = connection.execute(
            "SELECT weight_goal_id FROM weight_goals WHERE status = 'active'"
        ).fetchone()
        if existing is not None:
            raise ValueError(
                "An active weight goal already exists. Edit or remove it first."
            )

        cursor = connection.execute(
            """
            INSERT INTO weight_goals (
                start_date, start_weight, target_weight, target_date,
                status, created_at, updated_at
            )
            VALUES (?, ?, ?, ?, 'active', ?, ?)
            """,
            (
                normalized_start,
                starting_weight,
                desired_weight,
                normalized_target,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM weight_goals WHERE weight_goal_id = ?",
            (int(cursor.lastrowid),),
        ).fetchone()
    return dict(row)


def get_active_weight_goal(
    *,
    database_path: Path | None = None,
) -> dict[str, Any] | None:
    database_path = database_path or DATABASE_PATH
    initialize_database(database_path)
    with get_connection(database_path) as connection:
        row = connection.execute(
            """
            SELECT * FROM weight_goals
            WHERE status = 'active'
            ORDER BY weight_goal_id DESC
            LIMIT 1
            """
        ).fetchone()
    return dict(row) if row is not None else None


def list_weight_goals(
    *,
    database_path: Path | None = None,
) -> list[dict[str, Any]]:
    database_path = database_path or DATABASE_PATH
    initialize_database(database_path)
    with get_connection(database_path) as connection:
        rows = connection.execute(
            """
            SELECT * FROM weight_goals
            ORDER BY weight_goal_id DESC
            """
        ).fetchall()
    return [dict(row) for row in rows]


def update_active_weight_goal(
    *,
    target_weight: float,
    target_date: date | str,
    database_path: Path | None = None,
) -> dict[str, Any]:
    database_path = database_path or DATABASE_PATH
    initialize_database(database_path)
    desired_weight = float(target_weight)
    normalized_target = normalize_date(target_date, field_name="target_date")
    timestamp = current_timestamp()

    with get_connection(database_path) as connection:
        goal = connection.execute(
            "SELECT * FROM weight_goals WHERE status = 'active'"
        ).fetchone()
        if goal is None:
            raise ValueError("No active weight goal exists.")
        if desired_weight <= 0 or desired_weight >= float(goal["start_weight"]):
            raise ValueError(
                "The target weight must be below the starting weight."
            )
        if date.fromisoformat(normalized_target) <= date.fromisoformat(
            str(goal["start_date"])
        ):
            raise ValueError("The goal date must be after the start date.")

        connection.execute(
            """
            UPDATE weight_goals
            SET target_weight = ?, target_date = ?, updated_at = ?
            WHERE weight_goal_id = ?
            """,
            (
                desired_weight,
                normalized_target,
                timestamp,
                int(goal["weight_goal_id"]),
            ),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM weight_goals WHERE weight_goal_id = ?",
            (int(goal["weight_goal_id"]),),
        ).fetchone()
    return dict(row)


def archive_active_weight_goal(
    *,
    database_path: Path | None = None,
) -> dict[str, Any]:
    database_path = database_path or DATABASE_PATH
    initialize_database(database_path)
    timestamp = current_timestamp()
    with get_connection(database_path) as connection:
        goal = connection.execute(
            "SELECT * FROM weight_goals WHERE status = 'active'"
        ).fetchone()
        if goal is None:
            raise ValueError("No active weight goal exists.")
        connection.execute(
            """
            UPDATE weight_goals
            SET status = 'archived', archived_at = ?, updated_at = ?
            WHERE weight_goal_id = ?
            """,
            (timestamp, timestamp, int(goal["weight_goal_id"])),
        )
        connection.commit()
        row = connection.execute(
            "SELECT * FROM weight_goals WHERE weight_goal_id = ?",
            (int(goal["weight_goal_id"]),),
        ).fetchone()
    return dict(row)


def save_weight_goal_calculation(
    weight_goal_id: int,
    calculation: dict[str, Any],
    *,
    database_path: Path | None = None,
) -> dict[str, Any]:
    database_path = database_path or DATABASE_PATH
    initialize_database(database_path)
    timestamp = current_timestamp()
    with get_connection(database_path) as connection:
        goal = connection.execute(
            """
            SELECT weight_goal_id FROM weight_goals
            WHERE weight_goal_id = ? AND status = 'active'
            """,
            (int(weight_goal_id),),
        ).fetchone()
        if goal is None:
            raise ValueError("The active weight goal was not found.")

        cursor = connection.execute(
            """
            INSERT INTO weight_goal_calculations (
                weight_goal_id, calculation_date, current_weight,
                average_daily_burn, burn_days, days_remaining,
                required_weekly_loss, required_daily_deficit,
                planned_daily_deficit, calorie_target_low,
                calorie_target_high, safely_reachable,
                projected_weight, limiting_reason, created_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                int(weight_goal_id),
                calculation["calculation_date"],
                calculation["current_weight"],
                calculation["average_daily_burn"],
                calculation["burn_days"],
                calculation["days_remaining"],
                calculation.get("required_weekly_loss"),
                calculation.get("required_daily_deficit"),
                calculation["planned_daily_deficit"],
                calculation["calorie_target_low"],
                calculation["calorie_target_high"],
                int(bool(calculation["safely_reachable"])),
                calculation["projected_weight"],
                calculation.get("limiting_reason"),
                timestamp,
            ),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT * FROM weight_goal_calculations
            WHERE weight_goal_calculation_id = ?
            """,
            (int(cursor.lastrowid),),
        ).fetchone()
    return dict(row)


def get_latest_weight_goal_calculation(
    weight_goal_id: int | None = None,
    *,
    database_path: Path | None = None,
) -> dict[str, Any] | None:
    database_path = database_path or DATABASE_PATH
    initialize_database(database_path)
    with get_connection(database_path) as connection:
        if weight_goal_id is None:
            row = connection.execute(
                """
                SELECT calculations.*
                FROM weight_goal_calculations AS calculations
                JOIN weight_goals AS goals
                  ON goals.weight_goal_id = calculations.weight_goal_id
                WHERE goals.status = 'active'
                ORDER BY calculations.weight_goal_calculation_id DESC
                LIMIT 1
                """
            ).fetchone()
        else:
            row = connection.execute(
                """
                SELECT * FROM weight_goal_calculations
                WHERE weight_goal_id = ?
                ORDER BY weight_goal_calculation_id DESC
                LIMIT 1
                """,
                (int(weight_goal_id),),
            ).fetchone()
    return dict(row) if row is not None else None
