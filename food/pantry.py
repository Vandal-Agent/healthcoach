from __future__ import annotations

import re
from typing import Any

from food.database import (
    DATABASE_PATH,
    current_timestamp,
    get_connection,
    initialize_database,
    normalize_key_part,
)


PANTRY_SOURCES = {"manual", "barcode", "saved_food"}
MAX_PANTRY_ITEMS_PER_MESSAGE = 30


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
) -> dict[str, Any]:
    """Add or refresh one presence-only Pantry item."""
    initialize_database()
    cleaned_name = clean_pantry_name(display_name)
    normalized_name = pantry_name_key(cleaned_name)
    cleaned_source = str(source or "").strip().lower()

    if cleaned_source not in PANTRY_SOURCES:
        raise ValueError(
            "source must be manual, barcode, or saved_food."
        )

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
            SELECT pantry_item_id
            FROM pantry_items
            WHERE normalized_name = ?
            """,
            (normalized_name,),
        ).fetchone()

        connection.execute(
            """
            INSERT INTO pantry_items (
                display_name,
                normalized_name,
                food_id,
                source,
                barcode_text,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?, ?)
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
    result["created"] = existing is None
    return result


def add_pantry_items(names: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Add multiple manual Pantry items and report new and existing rows."""
    created: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []

    for name in names:
        item = add_pantry_item(display_name=name, source="manual")
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
                foods.serving_description,
                foods.verification_status,
                foods.verification_source,
                nutrition_versions.nutrition_version_id,
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
