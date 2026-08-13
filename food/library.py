from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any

from food.database import (
    DATABASE_PATH,
    build_search_key,
    current_timestamp,
    get_connection,
    initialize_database,
    save_food_alias,
)

REVERIFY_USE_COUNT = 20
REVERIFY_MIN_DAYS = 30
REVERIFY_MAX_DAYS = 180


def clean_optional_text(value: str | None) -> str | None:
    """Trim optional text and convert blank strings to None."""
    if value is None:
        return None

    cleaned = value.strip()
    return cleaned or None


def validate_verification_status(status: str) -> str:
    """Validate a Food verification status."""
    allowed = {"verified", "estimated", "unverified"}

    if status not in allowed:
        raise ValueError(
            "verification_status must be verified, estimated, or unverified."
        )

    return status


def find_food(
    *,
    canonical_name: str,
    serving_description: str,
    brand: str | None = None,
    restaurant: str | None = None,
) -> dict[str, Any] | None:
    """Find an existing Food using its identifying fields."""
    initialize_database()

    name = canonical_name.strip()
    serving = serving_description.strip()
    cleaned_brand = clean_optional_text(brand)
    cleaned_restaurant = clean_optional_text(restaurant)

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM foods
            WHERE lower(canonical_name) = lower(?)
              AND lower(serving_description) = lower(?)
              AND (
                    brand = ?
                    OR (brand IS NULL AND ? IS NULL)
              )
              AND (
                    restaurant = ?
                    OR (restaurant IS NULL AND ? IS NULL)
              )
            ORDER BY food_id
            LIMIT 1
            """,
            (
                name,
                serving,
                cleaned_brand,
                cleaned_brand,
                cleaned_restaurant,
                cleaned_restaurant,
            ),
        ).fetchone()

    return dict(row) if row else None


def get_food(food_id: int) -> dict[str, Any] | None:
    """Return one Food record by ID."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM foods
            WHERE food_id = ?
            """,
            (food_id,),
        ).fetchone()

    return dict(row) if row else None


def get_active_nutrition(
    food_id: int,
) -> dict[str, Any] | None:
    """Return the active Nutrition Version for one Food."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT nutrition_versions.*
            FROM foods
            JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id =
                 foods.active_nutrition_version_id
            WHERE foods.food_id = ?
            """,
            (food_id,),
        ).fetchone()

    return dict(row) if row else None


def add_food_with_nutrition(
    *,
    canonical_name: str,
    serving_description: str,
    serving_amount: float,
    serving_unit: str,
    verification_status: str,
    verification_source: str,
    calories: float | None,
    protein_g: float | None = None,
    carbohydrates_g: float | None = None,
    fat_g: float | None = None,
    fiber_g: float | None = None,
    sugar_g: float | None = None,
    sodium_mg: float | None = None,
    brand: str | None = None,
    restaurant: str | None = None,
    food_type: str = "food",
    source_item_id: str | None = None,
    source_url: str | None = None,
) -> dict[str, Any]:
    """
    Add a Food and its first Nutrition Version.

    If the Food already exists, return the existing Food without creating
    a duplicate.
    """
    initialize_database()

    if not canonical_name.strip():
        raise ValueError("canonical_name is required.")

    if not serving_description.strip():
        raise ValueError("serving_description is required.")

    if float(serving_amount) <= 0:
        raise ValueError("serving_amount must be greater than zero.")

    if not serving_unit.strip():
        raise ValueError("serving_unit is required.")

    allowed_food_types = {"food", "drink", "meal", "recipe"}

    if food_type not in allowed_food_types:
        raise ValueError("Invalid food_type.")

    status = validate_verification_status(verification_status)

    if status == "verified" and not verification_source.strip():
        raise ValueError(
            "verification_source is required for verified Foods."
        )

    search_key = build_search_key(
        canonical_name=canonical_name,
        serving_description=serving_description,
        brand=brand,
        restaurant=restaurant,
    )

    existing = find_food(
        canonical_name=canonical_name,
        serving_description=serving_description,
        brand=brand,
        restaurant=restaurant,
    )

    if existing:
        return {
            "created": False,
            "food": existing,
            "nutrition": get_active_nutrition(existing["food_id"]),
        }

    timestamp = current_timestamp()
    next_verification_due = (
        datetime.now().astimezone() + timedelta(days=REVERIFY_MAX_DAYS)
    ).isoformat(timespec="seconds")

    with get_connection(DATABASE_PATH) as connection:
        food_cursor = connection.execute(
            """
            INSERT INTO foods (
                search_key,
                canonical_name,
                brand,
                restaurant,
                food_type,
                serving_description,
                serving_amount,
                serving_unit,
                verification_status,
                verification_source,
                source_item_id,
                source_url,
                last_verified_at,
                uses_since_verification,
                next_verification_due,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 0, ?, ?, ?)
            """,
            (
                search_key,
                canonical_name.strip(),
                clean_optional_text(brand),
                clean_optional_text(restaurant),
                food_type,
                serving_description.strip(),
                float(serving_amount),
                serving_unit.strip(),
                status,
                verification_source.strip(),
                clean_optional_text(source_item_id),
                clean_optional_text(source_url),
                timestamp,
                next_verification_due,
                timestamp,
                timestamp,
            ),
        )

        food_id = food_cursor.lastrowid

        nutrition_cursor = connection.execute(
            """
            INSERT INTO nutrition_versions (
                food_id,
                version_number,
                calories,
                protein_g,
                carbohydrates_g,
                fat_g,
                fiber_g,
                sugar_g,
                sodium_mg,
                serving_amount,
                serving_unit,
                verification_status,
                verification_source,
                source_item_id,
                source_url,
                verified_at,
                created_at
            )
            VALUES (
                ?, 1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?
            )
            """,
            (
                food_id,
                calories,
                protein_g,
                carbohydrates_g,
                fat_g,
                fiber_g,
                sugar_g,
                sodium_mg,
                float(serving_amount),
                serving_unit.strip(),
                status,
                verification_source.strip(),
                clean_optional_text(source_item_id),
                clean_optional_text(source_url),
                timestamp,
                timestamp,
            ),
        )

        nutrition_version_id = nutrition_cursor.lastrowid

        connection.execute(
            """
            UPDATE foods
            SET active_nutrition_version_id = ?,
                updated_at = ?
            WHERE food_id = ?
            """,
            (
                nutrition_version_id,
                timestamp,
                food_id,
            ),
        )

        connection.commit()

    aliases = {
        canonical_name.strip(),
    }

    cleaned_brand = clean_optional_text(brand)
    cleaned_restaurant = clean_optional_text(restaurant)

    if cleaned_brand:
        aliases.add(
            f"{cleaned_brand} {canonical_name.strip()}"
        )

    if cleaned_restaurant:
        aliases.add(
            f"{cleaned_restaurant} {canonical_name.strip()}"
        )

    for alias in aliases:
        save_food_alias(
            food_id=food_id,
            alias_text=alias,
        )

    return {
        "created": True,
        "food": get_food(food_id),
        "nutrition": get_active_nutrition(food_id),
    }


def list_user_saved_foods() -> list[dict[str, Any]]:
    """List foods whose nutrition was entered by the user."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT
                foods.*,
                nutrition_versions.version_number,
                nutrition_versions.calories,
                nutrition_versions.protein_g,
                nutrition_versions.carbohydrates_g,
                nutrition_versions.fat_g,
                nutrition_versions.fiber_g,
                nutrition_versions.sugar_g,
                nutrition_versions.sodium_mg
            FROM foods
            JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id =
                 foods.active_nutrition_version_id
            WHERE foods.verification_source IN (
                'user_package_label',
                'user_entered'
            )
            ORDER BY lower(foods.canonical_name), foods.food_id
            """
        ).fetchall()

    return [dict(row) for row in rows]


def add_user_nutrition_version(
    *,
    food_id: int,
    calories: float,
    protein_g: float,
    carbohydrates_g: float,
    fat_g: float,
    fiber_g: float,
    sugar_g: float,
    sodium_mg: float,
) -> dict[str, Any]:
    """
    Create a new active nutrition version for future logs.

    Existing ledger entries retain their saved nutrition snapshots.
    """
    initialize_database()
    timestamp = current_timestamp()

    nutrient_values = {
        "calories": calories,
        "protein_g": protein_g,
        "carbohydrates_g": carbohydrates_g,
        "fat_g": fat_g,
        "fiber_g": fiber_g,
        "sugar_g": sugar_g,
        "sodium_mg": sodium_mg,
    }

    normalized = {}

    for field, value in nutrient_values.items():
        number = float(value)

        if number < 0:
            raise ValueError(f"{field} cannot be negative.")

        normalized[field] = number

    with get_connection(DATABASE_PATH) as connection:
        food = connection.execute(
            """
            SELECT *
            FROM foods
            WHERE food_id = ?
            """,
            (int(food_id),),
        ).fetchone()

        if food is None:
            raise ValueError(f"Food not found: {food_id}")

        version_row = connection.execute(
            """
            SELECT COALESCE(MAX(version_number), 0) AS version
            FROM nutrition_versions
            WHERE food_id = ?
            """,
            (int(food_id),),
        ).fetchone()

        next_version = int(version_row["version"]) + 1

        cursor = connection.execute(
            """
            INSERT INTO nutrition_versions (
                food_id,
                version_number,
                calories,
                protein_g,
                carbohydrates_g,
                fat_g,
                fiber_g,
                sugar_g,
                sodium_mg,
                serving_amount,
                serving_unit,
                verification_status,
                verification_source,
                source_item_id,
                source_url,
                verified_at,
                created_at
            )
            VALUES (
                ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?,
                'verified', 'user_entered', NULL, NULL, ?, ?
            )
            """,
            (
                int(food_id),
                next_version,
                normalized["calories"],
                normalized["protein_g"],
                normalized["carbohydrates_g"],
                normalized["fat_g"],
                normalized["fiber_g"],
                normalized["sugar_g"],
                normalized["sodium_mg"],
                float(food["serving_amount"]),
                str(food["serving_unit"]),
                timestamp,
                timestamp,
            ),
        )

        connection.execute(
            """
            UPDATE foods
            SET active_nutrition_version_id = ?,
                verification_status = 'verified',
                verification_source = 'user_entered',
                last_verified_at = ?,
                uses_since_verification = 0,
                updated_at = ?
            WHERE food_id = ?
            """,
            (
                cursor.lastrowid,
                timestamp,
                timestamp,
                int(food_id),
            ),
        )

        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM nutrition_versions
            WHERE nutrition_version_id = ?
            """,
            (cursor.lastrowid,),
        ).fetchone()

    return dict(row)


def increment_food_usage(
    food_id: int,
) -> dict[str, Any]:
    """Increment the number of uses since verification."""
    initialize_database()
    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        existing = connection.execute(
            """
            SELECT *
            FROM foods
            WHERE food_id = ?
            """,
            (food_id,),
        ).fetchone()

        if existing is None:
            raise ValueError(f"Food not found: {food_id}")

        connection.execute(
            """
            UPDATE foods
            SET uses_since_verification =
                    uses_since_verification + 1,
                updated_at = ?
            WHERE food_id = ?
            """,
            (
                timestamp,
                food_id,
            ),
        )

        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM foods
            WHERE food_id = ?
            """,
            (food_id,),
        ).fetchone()

    return dict(row)


def food_needs_reverification(
    food: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Return True when a Food should be checked against its source again."""
    reference_time = now or datetime.now().astimezone()
    last_verified_at = food.get("last_verified_at")

    if not last_verified_at:
        return True

    try:
        verified_time = datetime.fromisoformat(last_verified_at)
    except (TypeError, ValueError):
        return True

    age = reference_time - verified_time
    uses = int(food.get("uses_since_verification") or 0)

    frequently_used_and_old_enough = (
        uses >= REVERIFY_USE_COUNT
        and age >= timedelta(days=REVERIFY_MIN_DAYS)
    )

    maximum_age_reached = age >= timedelta(days=REVERIFY_MAX_DAYS)

    return frequently_used_and_old_enough or maximum_age_reached


def list_foods_due_for_reverification() -> list[dict[str, Any]]:
    """Return saved Foods that are due for source verification."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM foods
            WHERE verification_status = 'verified'
            ORDER BY last_verified_at, food_id
            """
        ).fetchall()

    foods = [dict(row) for row in rows]

    return [
        food
        for food in foods
        if food_needs_reverification(food)
    ]