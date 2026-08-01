from __future__ import annotations

from datetime import date
from typing import Any

from food.database import (
    DATABASE_PATH,
    current_timestamp,
    get_connection,
    initialize_database,
)
from food.library import (
    get_active_nutrition,
    get_food,
    increment_food_usage,
)

TELEGRAM_SOURCES = {
    "telegram_ai",
    "telegram_manual",
}

ALLOWED_SOURCES = {
    "telegram_ai",
    "telegram_manual",
    "loseit",
    "barcode",
    "recipe",
    "manual",
}


def normalize_date(value: date | str) -> str:
    """Return a YYYY-MM-DD date string."""
    if isinstance(value, date):
        return value.isoformat()

    return date.fromisoformat(value).isoformat()


def normalize_meal_category(value: str) -> str:
    """Normalize the meal name used by the Food ledger."""
    cleaned = value.strip().lower()

    aliases = {
        "before breakfast": "before breakfast",
        "breakfast": "breakfast",
        "school snack": "school snack",
        "morning snack": "school snack",
        "lunch": "lunch",
        "afternoon snack": "afternoon snack",
        "dinner": "dinner",
        "dessert": "dessert",
        "snack": "afternoon snack",
    }

    if cleaned not in aliases:
        raise ValueError(f"Unsupported meal category: {value}")

    return aliases[cleaned]


def validate_logging_source(value: str) -> str:
    """Validate and return a Food logging source."""
    cleaned = value.strip().lower()

    if cleaned not in ALLOWED_SOURCES:
        raise ValueError(f"Unsupported logging source: {value}")

    return cleaned


def source_group(source: str) -> str:
    """Return the meal-level source group used for conflict checking."""
    if source in TELEGRAM_SOURCES:
        return "telegram"

    return source


def get_meal_sources(
    *,
    entry_date: date | str,
    meal_category: str,
) -> list[str]:
    """Return the distinct logging sources already used for one meal."""
    initialize_database()

    normalized_date = normalize_date(entry_date)
    normalized_meal = normalize_meal_category(meal_category)

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT DISTINCT logging_source
            FROM food_entries
            WHERE entry_date = ?
              AND meal_category = ?
            ORDER BY logging_source
            """,
            (
                normalized_date,
                normalized_meal,
            ),
        ).fetchall()

    return [row["logging_source"] for row in rows]


def meal_has_source_conflict(
    *,
    entry_date: date | str,
    meal_category: str,
    proposed_source: str,
) -> dict[str, Any]:
    """Check the one-source-per-meal rule."""
    source = validate_logging_source(proposed_source)
    existing_sources = get_meal_sources(
        entry_date=entry_date,
        meal_category=meal_category,
    )

    proposed_group = source_group(source)
    existing_groups = {
        source_group(existing)
        for existing in existing_sources
    }

    conflict = bool(
        existing_groups
        and proposed_group not in existing_groups
    )

    return {
        "conflict": conflict,
        "existing_sources": existing_sources,
        "proposed_source": source,
    }


def scale_value(
    value: float | None,
    quantity: float,
) -> float | None:
    """Scale an optional nutrient value by quantity."""
    if value is None:
        return None

    return round(float(value) * quantity, 3)


def add_food_entry(
    *,
    entry_date: date | str,
    meal_category: str,
    food_id: int,
    quantity: float,
    logging_source: str,
    original_text: str | None = None,
    quantity_is_estimated: bool = False,
    user_confirmed: bool = True,
) -> dict[str, Any]:
    """Add a confirmed Food entry while enforcing meal-source rules."""
    initialize_database()

    normalized_date = normalize_date(entry_date)
    normalized_meal = normalize_meal_category(meal_category)
    source = validate_logging_source(logging_source)
    numeric_quantity = float(quantity)

    if numeric_quantity <= 0:
        raise ValueError("quantity must be greater than zero.")

    food = get_food(food_id)

    if food is None:
        raise ValueError(f"Food not found: {food_id}")

    nutrition = get_active_nutrition(food_id)

    if nutrition is None:
        raise ValueError(
            "This Food has no active Nutrition Version."
        )

    conflict = meal_has_source_conflict(
        entry_date=normalized_date,
        meal_category=normalized_meal,
        proposed_source=source,
    )

    if conflict["conflict"]:
        existing = ", ".join(conflict["existing_sources"])

        raise ValueError(
            f"{normalized_meal.title()} on {normalized_date} "
            f"already contains entries from: {existing}. "
            "One source per meal is allowed."
        )

    timestamp = current_timestamp()

    values = {
        "calories": scale_value(
            nutrition["calories"],
            numeric_quantity,
        ),
        "protein_g": scale_value(
            nutrition["protein_g"],
            numeric_quantity,
        ),
        "carbohydrates_g": scale_value(
            nutrition["carbohydrates_g"],
            numeric_quantity,
        ),
        "fat_g": scale_value(
            nutrition["fat_g"],
            numeric_quantity,
        ),
        "fiber_g": scale_value(
            nutrition["fiber_g"],
            numeric_quantity,
        ),
        "sugar_g": scale_value(
            nutrition["sugar_g"],
            numeric_quantity,
        ),
        "sodium_mg": scale_value(
            nutrition["sodium_mg"],
            numeric_quantity,
        ),
    }

    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            INSERT INTO food_entries (
                entry_date,
                meal_category,
                food_id,
                nutrition_version_id,
                quantity,
                original_text,
                logging_source,
                quantity_is_estimated,
                user_confirmed,
                calories,
                protein_g,
                carbohydrates_g,
                fat_g,
                fiber_g,
                sugar_g,
                sodium_mg,
                created_at,
                updated_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                normalized_date,
                normalized_meal,
                food_id,
                nutrition["nutrition_version_id"],
                numeric_quantity,
                original_text,
                source,
                1 if quantity_is_estimated else 0,
                1 if user_confirmed else 0,
                values["calories"],
                values["protein_g"],
                values["carbohydrates_g"],
                values["fat_g"],
                values["fiber_g"],
                values["sugar_g"],
                values["sodium_mg"],
                timestamp,
                timestamp,
            ),
        )

        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM food_entries
            WHERE food_entry_id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()

    increment_food_usage(food_id)

    return dict(row)


def list_food_entries(
    *,
    entry_date: date | str,
    meal_category: str | None = None,
) -> list[dict[str, Any]]:
    """Return Food entries for one date."""
    initialize_database()

    normalized_date = normalize_date(entry_date)

    query = """
        SELECT
            food_entries.*,
            foods.canonical_name,
            foods.brand,
            foods.restaurant,
            foods.serving_description
        FROM food_entries
        JOIN foods
          ON foods.food_id = food_entries.food_id
        WHERE food_entries.entry_date = ?
    """
    parameters: list[Any] = [normalized_date]

    if meal_category is not None:
        query += " AND food_entries.meal_category = ?"
        parameters.append(
            normalize_meal_category(meal_category)
        )

    query += """
        ORDER BY
            food_entries.meal_category,
            food_entries.food_entry_id
    """

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            query,
            parameters,
        ).fetchall()

    return [dict(row) for row in rows]


def get_daily_totals(
    entry_date: date | str,
) -> dict[str, float]:
    """Return summed nutrition for one date."""
    initialize_database()

    normalized_date = normalize_date(entry_date)

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT
                COALESCE(SUM(calories), 0) AS calories,
                COALESCE(SUM(protein_g), 0) AS protein_g,
                COALESCE(SUM(carbohydrates_g), 0)
                    AS carbohydrates_g,
                COALESCE(SUM(fat_g), 0) AS fat_g,
                COALESCE(SUM(fiber_g), 0) AS fiber_g,
                COALESCE(SUM(sugar_g), 0) AS sugar_g,
                COALESCE(SUM(sodium_mg), 0) AS sodium_mg
            FROM food_entries
            WHERE entry_date = ?
            """,
            (normalized_date,),
        ).fetchone()

    return {
        key: round(float(row[key] or 0), 3)
        for key in row.keys()
    }


def delete_food_entry(food_entry_id: int) -> bool:
    """Delete one Food entry."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        deleted = connection.execute(
            """
            DELETE FROM food_entries
            WHERE food_entry_id = ?
            """,
            (food_entry_id,),
        ).rowcount

        connection.commit()

    return bool(deleted)