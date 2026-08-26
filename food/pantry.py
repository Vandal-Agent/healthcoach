from __future__ import annotations

import re
import sqlite3
from typing import Any

from food.database import (
    DATABASE_PATH,
    current_timestamp,
    get_connection,
    initialize_database,
    normalize_key_part,
)


PANTRY_SOURCES = {"manual", "barcode", "saved_food", "shelf_photo"}
MAX_PANTRY_ITEMS_PER_MESSAGE = 30
PANTRY_STORAGE_AREAS = {
    "unsorted": "Unsorted",
    "pantry_shelf": "Pantry shelf",
    "refrigerator": "Refrigerator",
    "freezer": "Freezer",
    "counter_produce": "Counter/produce",
    "other": "Other",
}
PANTRY_FOOD_CATEGORIES = {
    "unsorted": "Unsorted",
    "produce": "Produce",
    "protein": "Protein",
    "dairy": "Dairy",
    "grains": "Grains",
    "canned_jarred": "Canned/jarred",
    "snacks": "Snacks",
    "condiments": "Condiments",
    "baking_spices": "Baking/spices",
    "drinks": "Drinks",
    "other": "Other",
}


def validate_pantry_storage_area(value: str | None) -> str:
    """Validate one persistent Pantry storage-area key."""
    cleaned = str(value or "unsorted").strip().lower()
    if cleaned not in PANTRY_STORAGE_AREAS:
        raise ValueError("Unknown Pantry storage area.")
    return cleaned


def validate_pantry_food_category(value: str | None) -> str:
    """Validate one persistent Pantry food-category key."""
    cleaned = str(value or "unsorted").strip().lower()
    if cleaned not in PANTRY_FOOD_CATEGORIES:
        raise ValueError("Unknown Pantry food category.")
    return cleaned


def clean_pantry_name(value: str) -> str:
    """Return a safe Pantry display name."""
    cleaned = re.sub(r"\s+", " ", str(value or "")).strip(" ,;:-")

    if not cleaned:
        raise ValueError("Pantry item name is required.")
    if len(cleaned) > 120:
        raise ValueError("Pantry item names must be 120 characters or less.")

    return cleaned


def pantry_name_key(value: str) -> str:
    """Return a stable case-insensitive Pantry identity."""
    cleaned = clean_pantry_name(value)
    key = normalize_key_part(cleaned)
    key = "_".join(
        part
        for part in key.split("_")
        if part != "and"
    )

    if not key:
        raise ValueError("Pantry item name must contain letters or numbers.")

    return key


def parse_pantry_item_list(value: str) -> list[str]:
    """Parse a comma, semicolon, or line-separated Pantry list."""
    text = str(value or "").strip()
    if not text:
        return []

    pieces = re.split(r"[,;\n\r]+", text)
    results: list[str] = []
    seen: set[str] = set()

    for piece in pieces:
        piece = re.sub(
            r"^\s*(?:[-*•]|\d+[.)])\s*",
            "",
            piece,
        )
        if not piece.strip():
            continue

        name = clean_pantry_name(piece)
        key = pantry_name_key(name)
        if key in seen:
            continue

        seen.add(key)
        results.append(name)

    if len(results) > MAX_PANTRY_ITEMS_PER_MESSAGE:
        raise ValueError(
            "Add no more than 30 Pantry items at a time."
        )

    return results


def add_pantry_item(
    *,
    display_name: str,
    food_id: int | None = None,
    source: str = "manual",
    barcode_text: str | None = None,
    storage_area: str | None = None,
    food_category: str = "unsorted",
) -> dict[str, Any]:
    """Add or refresh one presence-only Pantry item."""
    initialize_database()
    cleaned_name = clean_pantry_name(display_name)
    normalized_name = pantry_name_key(cleaned_name)
    cleaned_source = str(source or "").strip().lower()

    if cleaned_source not in PANTRY_SOURCES:
        raise ValueError(
            "source must be manual, barcode, saved_food, or shelf_photo."
        )

    cleaned_storage_area = validate_pantry_storage_area(
        storage_area
        or ("pantry_shelf" if cleaned_source == "shelf_photo" else None)
    )
    cleaned_food_category = validate_pantry_food_category(food_category)

    cleaned_barcode = (
        str(barcode_text).strip()
        if barcode_text is not None
        else None
    ) or None
    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        if food_id is not None:
            food = connection.execute(
                "SELECT food_id FROM foods WHERE food_id = ?",
                (int(food_id),),
            ).fetchone()
            if food is None:
                raise ValueError(f"Food not found: {food_id}")

        existing = connection.execute(
            """
            SELECT pantry_item_id, display_name, normalized_name
            FROM pantry_items
            WHERE normalized_name = ?
            """,
            (normalized_name,),
        ).fetchone()

        if existing is None:
            candidates = connection.execute(
                """
                SELECT pantry_item_id, display_name, normalized_name
                FROM pantry_items
                """
            ).fetchall()
            existing = next(
                (
                    candidate
                    for candidate in candidates
                    if pantry_name_key(candidate["display_name"])
                    == normalized_name
                ),
                None,
            )

        if existing is not None:
            connection.execute(
                """
                UPDATE pantry_items
                SET
                    display_name = CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE display_name
                    END,
                    food_id = COALESCE(?, food_id),
                    source = CASE
                        WHEN ? IS NOT NULL THEN ?
                        ELSE source
                    END,
                    barcode_text = COALESCE(?, barcode_text),
                    updated_at = ?
                WHERE pantry_item_id = ?
                """,
                (
                    int(food_id) if food_id is not None else None,
                    cleaned_name,
                    int(food_id) if food_id is not None else None,
                    int(food_id) if food_id is not None else None,
                    cleaned_source,
                    cleaned_barcode,
                    timestamp,
                    int(existing["pantry_item_id"]),
                ),
            )
            connection.commit()
            row = connection.execute(
                """
                SELECT *
                FROM pantry_items
                WHERE pantry_item_id = ?
                """,
                (int(existing["pantry_item_id"]),),
            ).fetchone()
            result = dict(row)
            result["created"] = False
            return result

        connection.execute(
            """
            INSERT INTO pantry_items (
                display_name,
                normalized_name,
                food_id,
                source,
                barcode_text,
                storage_area,
                food_category,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_name)
            DO UPDATE SET
                display_name = CASE
                    WHEN excluded.food_id IS NOT NULL
                    THEN excluded.display_name
                    ELSE pantry_items.display_name
                END,
                food_id = COALESCE(
                    excluded.food_id,
                    pantry_items.food_id
                ),
                source = CASE
                    WHEN excluded.food_id IS NOT NULL
                    THEN excluded.source
                    ELSE pantry_items.source
                END,
                barcode_text = COALESCE(
                    excluded.barcode_text,
                    pantry_items.barcode_text
                ),
                updated_at = excluded.updated_at
            """,
            (
                cleaned_name,
                normalized_name,
                int(food_id) if food_id is not None else None,
                cleaned_source,
                cleaned_barcode,
                cleaned_storage_area,
                cleaned_food_category,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()

        row = connection.execute(
            """
            SELECT *
            FROM pantry_items
            WHERE normalized_name = ?
            """,
            (normalized_name,),
        ).fetchone()

    result = dict(row)
    result["created"] = True
    return result


def add_pantry_items(
    names: list[str],
    *,
    source: str = "manual",
) -> dict[str, list[dict[str, Any]]]:
    """Add multiple Pantry items and report new and existing rows."""
    created: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []

    for name in names:
        item = add_pantry_item(display_name=name, source=source)
        if item.pop("created"):
            created.append(item)
        else:
            existing.append(item)

    return {"created": created, "existing": existing}


def list_pantry_items() -> list[dict[str, Any]]:
    """List Pantry items with linked active nutrition when available."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT
                pantry_items.*,
                foods.canonical_name,
                foods.brand,
                foods.food_type,
                foods.serving_description,
                foods.serving_amount,
                foods.serving_unit,
                foods.verification_status,
                foods.verification_source,
                nutrition_versions.nutrition_version_id,
                nutrition_versions.version_number,
                nutrition_versions.calories,
                nutrition_versions.protein_g,
                nutrition_versions.carbohydrates_g,
                nutrition_versions.fat_g,
                nutrition_versions.fiber_g,
                nutrition_versions.sugar_g,
                nutrition_versions.sodium_mg
            FROM pantry_items
            LEFT JOIN foods
              ON foods.food_id = pantry_items.food_id
            LEFT JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id =
                 foods.active_nutrition_version_id
            ORDER BY
                lower(pantry_items.display_name),
                pantry_items.pantry_item_id
            """
        ).fetchall()

    return [dict(row) for row in rows]


def link_pantry_item_to_food(
    pantry_item_id: int,
    *,
    food_id: int,
    source: str | None = None,
    barcode_text: str | None = None,
) -> dict[str, Any]:
    """Link one Pantry item to a nutrition-ready Food record."""
    initialize_database()
    cleaned_source = (
        str(source).strip().lower()
        if source is not None
        else None
    )
    if cleaned_source is not None and cleaned_source not in PANTRY_SOURCES:
        raise ValueError(
            "source must be manual, barcode, saved_food, or shelf_photo."
        )
    cleaned_barcode = (
        str(barcode_text).strip()
        if barcode_text is not None
        else None
    ) or None

    with get_connection(DATABASE_PATH) as connection:
        pantry_row = connection.execute(
            "SELECT pantry_item_id FROM pantry_items WHERE pantry_item_id = ?",
            (int(pantry_item_id),),
        ).fetchone()
        if pantry_row is None:
            raise ValueError(f"Pantry item not found: {pantry_item_id}")

        food = connection.execute(
            """
            SELECT
                foods.food_id,
                foods.active_nutrition_version_id,
                nutrition_versions.calories
            FROM foods
            LEFT JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id =
                 foods.active_nutrition_version_id
            WHERE foods.food_id = ?
            """,
            (int(food_id),),
        ).fetchone()
        if (
            food is None
            or food["active_nutrition_version_id"] is None
            or food["calories"] is None
        ):
            raise ValueError(
                "That Saved Food does not have usable active nutrition."
            )

        connection.execute(
            """
            UPDATE pantry_items
            SET
                food_id = ?,
                source = COALESCE(?, source),
                barcode_text = COALESCE(?, barcode_text),
                updated_at = ?
            WHERE pantry_item_id = ?
            """,
            (
                int(food_id),
                cleaned_source,
                cleaned_barcode,
                current_timestamp(),
                int(pantry_item_id),
            ),
        )
        connection.commit()

    return next(
        item
        for item in list_pantry_items()
        if int(item["pantry_item_id"]) == int(pantry_item_id)
    )


def unlink_pantry_item_nutrition(
    pantry_item_id: int,
) -> dict[str, Any]:
    """Remove only one Pantry item's Food/nutrition association."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            UPDATE pantry_items
            SET
                food_id = NULL,
                source = 'manual',
                barcode_text = NULL,
                updated_at = ?
            WHERE pantry_item_id = ?
            """,
            (current_timestamp(), int(pantry_item_id)),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Pantry item not found: {pantry_item_id}")
        connection.commit()

    return next(
        item
        for item in list_pantry_items()
        if int(item["pantry_item_id"]) == int(pantry_item_id)
    )


def update_pantry_item_organization(
    pantry_item_id: int,
    *,
    storage_area: str,
    food_category: str,
) -> dict[str, Any]:
    """Update only the organizational labels for one Pantry item."""
    initialize_database()
    cleaned_storage_area = validate_pantry_storage_area(storage_area)
    cleaned_food_category = validate_pantry_food_category(food_category)

    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            UPDATE pantry_items
            SET
                storage_area = ?,
                food_category = ?,
                updated_at = ?
            WHERE pantry_item_id = ?
            """,
            (
                cleaned_storage_area,
                cleaned_food_category,
                current_timestamp(),
                int(pantry_item_id),
            ),
        )
        if cursor.rowcount != 1:
            raise ValueError(f"Pantry item not found: {pantry_item_id}")
        connection.commit()
        row = connection.execute(
            "SELECT * FROM pantry_items WHERE pantry_item_id = ?",
            (int(pantry_item_id),),
        ).fetchone()

    return dict(row)


def rename_pantry_item(
    pantry_item_id: int,
    *,
    display_name: str,
) -> dict[str, Any]:
    """Rename one Pantry item without changing its linked data."""
    initialize_database()
    cleaned_name = clean_pantry_name(display_name)
    normalized_name = pantry_name_key(cleaned_name)

    with get_connection(DATABASE_PATH) as connection:
        current = connection.execute(
            "SELECT * FROM pantry_items WHERE pantry_item_id = ?",
            (int(pantry_item_id),),
        ).fetchone()
        if current is None:
            raise ValueError(f"Pantry item not found: {pantry_item_id}")

        candidates = connection.execute(
            """
            SELECT pantry_item_id, display_name
            FROM pantry_items
            WHERE pantry_item_id != ?
            """,
            (int(pantry_item_id),),
        ).fetchall()
        if any(
            pantry_name_key(candidate["display_name"]) == normalized_name
            for candidate in candidates
        ):
            raise ValueError(
                "A Pantry item with that name already exists."
            )

        try:
            connection.execute(
                """
                UPDATE pantry_items
                SET
                    display_name = ?,
                    normalized_name = ?,
                    updated_at = ?
                WHERE pantry_item_id = ?
                """,
                (
                    cleaned_name,
                    normalized_name,
                    current_timestamp(),
                    int(pantry_item_id),
                ),
            )
            connection.commit()
        except sqlite3.IntegrityError as exc:
            raise ValueError(
                "A Pantry item with that name already exists."
            ) from exc

        row = connection.execute(
            "SELECT * FROM pantry_items WHERE pantry_item_id = ?",
            (int(pantry_item_id),),
        ).fetchone()

    return dict(row)


def remove_pantry_item(pantry_item_id: int) -> bool:
    """Remove one Pantry item by ID."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute(
            "DELETE FROM pantry_items WHERE pantry_item_id = ?",
            (int(pantry_item_id),),
        )
        connection.commit()

    return cursor.rowcount == 1


def clear_pantry() -> int:
    """Remove all Pantry items and return the number removed."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute("DELETE FROM pantry_items")
        connection.commit()

    return int(cursor.rowcount)
