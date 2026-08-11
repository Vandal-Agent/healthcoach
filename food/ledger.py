from __future__ import annotations

from datetime import date, datetime
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


def find_recent_duplicate_entry(
    *,
    entry_date: date | str,
    meal_category: str,
    food_id: int,
    quantity: float,
    window_minutes: int = 5,
) -> dict[str, Any] | None:
    """Return a matching recently logged Food entry, if one exists."""
    initialize_database()

    normalized_date = normalize_date(entry_date)
    normalized_meal = normalize_meal_category(meal_category)
    numeric_quantity = float(quantity)

    if numeric_quantity <= 0:
        raise ValueError("quantity must be greater than zero.")

    if window_minutes <= 0:
        raise ValueError(
            "window_minutes must be greater than zero."
        )

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM food_entries
            WHERE entry_date = ?
              AND meal_category = ?
              AND food_id = ?
              AND ABS(quantity - ?) < 0.000001
            ORDER BY food_entry_id DESC
            """,
            (
                normalized_date,
                normalized_meal,
                int(food_id),
                numeric_quantity,
            ),
        ).fetchall()

    now = datetime.now().astimezone()

    for row in rows:
        created_at = row["created_at"]

        try:
            created_time = datetime.fromisoformat(
                str(created_at)
            )
        except (TypeError, ValueError):
            continue

        if created_time.tzinfo is None:
            created_time = created_time.astimezone()

        age_seconds = (
            now - created_time.astimezone()
        ).total_seconds()

        if 0 <= age_seconds <= window_minutes * 60:
            return dict(row)

    return None


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


def save_food_favorite_from_entry(
    food_entry_id: int,
) -> dict[str, Any]:
    """Save one logged Food entry's food, quantity, and meal as a favorite."""
    initialize_database()
    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        entry = connection.execute(
            """
            SELECT food_id, quantity, meal_category
            FROM food_entries
            WHERE food_entry_id = ?
            """,
            (int(food_entry_id),),
        ).fetchone()

        if entry is None:
            raise ValueError("Food entry not found.")

        connection.execute(
            """
            INSERT INTO food_favorites (
                food_id,
                quantity,
                meal_category,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (food_id, quantity, meal_category)
            DO UPDATE SET updated_at = excluded.updated_at
            """,
            (
                int(entry["food_id"]),
                float(entry["quantity"]),
                entry["meal_category"],
                timestamp,
                timestamp,
            ),
        )
        connection.commit()

        favorite = connection.execute(
            """
            SELECT food_favorites.*, foods.canonical_name,
                   foods.brand, foods.restaurant,
                   foods.serving_description
            FROM food_favorites
            JOIN foods
              ON foods.food_id = food_favorites.food_id
            WHERE food_favorites.food_id = ?
              AND ABS(food_favorites.quantity - ?) < 0.000001
              AND food_favorites.meal_category = ?
            """,
            (
                int(entry["food_id"]),
                float(entry["quantity"]),
                entry["meal_category"],
            ),
        ).fetchone()

    return dict(favorite)


def list_food_favorites() -> list[dict[str, Any]]:
    """Return saved Food favorites with current serving nutrition."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT food_favorites.*, foods.canonical_name,
                   foods.brand, foods.restaurant,
                   foods.serving_description,
                   nutrition_versions.calories,
                   nutrition_versions.protein_g
            FROM food_favorites
            JOIN foods
              ON foods.food_id = food_favorites.food_id
            LEFT JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id =
                 foods.active_nutrition_version_id
            ORDER BY food_favorites.updated_at DESC,
                     food_favorites.food_favorite_id DESC
            """
        ).fetchall()

    return [dict(row) for row in rows]


def delete_food_favorite(food_favorite_id: int) -> bool:
    """Delete one saved Food favorite."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        deleted = connection.execute(
            """
            DELETE FROM food_favorites
            WHERE food_favorite_id = ?
            """,
            (int(food_favorite_id),),
        ).rowcount
        connection.commit()

    return bool(deleted)


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


def update_food_entry(
    food_entry_id: int,
    *,
    quantity: float | None = None,
    meal_category: str | None = None,
) -> dict[str, Any] | None:
    """Update one entry's quantity or meal while preserving its snapshot."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM food_entries
            WHERE food_entry_id = ?
            """,
            (int(food_entry_id),),
        ).fetchone()

        if row is None:
            return None

        existing = dict(row)
        old_quantity = float(existing["quantity"])
        new_quantity = (
            old_quantity
            if quantity is None
            else float(quantity)
        )

        if new_quantity <= 0:
            raise ValueError("quantity must be greater than zero.")

        new_meal = (
            existing["meal_category"]
            if meal_category is None
            else normalize_meal_category(meal_category)
        )

        if new_meal != existing["meal_category"]:
            source_rows = connection.execute(
                """
                SELECT DISTINCT logging_source
                FROM food_entries
                WHERE entry_date = ?
                  AND meal_category = ?
                  AND food_entry_id != ?
                """,
                (
                    existing["entry_date"],
                    new_meal,
                    int(food_entry_id),
                ),
            ).fetchall()
            existing_groups = {
                source_group(source_row["logging_source"])
                for source_row in source_rows
            }

            entry_group = source_group(existing["logging_source"])
            if existing_groups - {entry_group}:
                raise ValueError(
                    f"{new_meal.title()} already contains food "
                    "from a different logging source."
                )

        ratio = new_quantity / old_quantity
        nutrient_columns = (
            "calories",
            "protein_g",
            "carbohydrates_g",
            "fat_g",
            "fiber_g",
            "sugar_g",
            "sodium_mg",
        )
        scaled = {
            column: (
                None
                if existing[column] is None
                else round(float(existing[column]) * ratio, 3)
            )
            for column in nutrient_columns
        }

        connection.execute(
            """
            UPDATE food_entries
            SET meal_category = ?,
                quantity = ?,
                calories = ?,
                protein_g = ?,
                carbohydrates_g = ?,
                fat_g = ?,
                fiber_g = ?,
                sugar_g = ?,
                sodium_mg = ?,
                updated_at = ?
            WHERE food_entry_id = ?
            """,
            (
                new_meal,
                new_quantity,
                scaled["calories"],
                scaled["protein_g"],
                scaled["carbohydrates_g"],
                scaled["fat_g"],
                scaled["fiber_g"],
                scaled["sugar_g"],
                scaled["sodium_mg"],
                current_timestamp(),
                int(food_entry_id),
            ),
        )
        connection.commit()

        updated = connection.execute(
            """
            SELECT *
            FROM food_entries
            WHERE food_entry_id = ?
            """,
            (int(food_entry_id),),
        ).fetchone()

    return dict(updated) if updated is not None else None


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
