from __future__ import annotations

from typing import Any

from food.database import (
    DATABASE_PATH,
    current_timestamp,
    get_connection,
    initialize_database,
)
from food.pantry import (
    add_pantry_item,
    clean_pantry_name,
    pantry_name_key,
    parse_pantry_item_list,
)


SHOPPING_SOURCES = {"manual", "pantry_swap"}


def add_shopping_item(
    *,
    display_name: str,
    source: str = "manual",
    source_note: str | None = None,
) -> dict[str, Any]:
    """Add one persistent Shopping List item without duplicating it."""
    initialize_database()
    cleaned_name = clean_pantry_name(display_name)
    normalized_name = pantry_name_key(cleaned_name)
    cleaned_source = str(source or "").strip().lower()
    if cleaned_source not in SHOPPING_SOURCES:
        raise ValueError("source must be manual or pantry_swap.")

    cleaned_note = str(source_note or "").strip() or None
    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        existing = connection.execute(
            """
            SELECT shopping_list_item_id
            FROM shopping_list_items
            WHERE normalized_name = ?
            """,
            (normalized_name,),
        ).fetchone()

        connection.execute(
            """
            INSERT INTO shopping_list_items (
                display_name,
                normalized_name,
                source,
                source_note,
                created_at,
                updated_at
            )
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT(normalized_name)
            DO UPDATE SET
                display_name = excluded.display_name,
                source = excluded.source,
                source_note = COALESCE(
                    excluded.source_note,
                    shopping_list_items.source_note
                ),
                updated_at = excluded.updated_at
            """,
            (
                cleaned_name,
                normalized_name,
                cleaned_source,
                cleaned_note,
                timestamp,
                timestamp,
            ),
        )
        connection.commit()
        row = connection.execute(
            """
            SELECT *
            FROM shopping_list_items
            WHERE normalized_name = ?
            """,
            (normalized_name,),
        ).fetchone()

    result = dict(row)
    result["created"] = existing is None
    return result


def add_shopping_items(names: list[str]) -> dict[str, list[dict[str, Any]]]:
    """Add several manually entered Shopping List items."""
    created: list[dict[str, Any]] = []
    existing: list[dict[str, Any]] = []

    for name in names:
        item = add_shopping_item(display_name=name, source="manual")
        if item.pop("created"):
            created.append(item)
        else:
            existing.append(item)

    return {"created": created, "existing": existing}


def parse_shopping_item_list(value: str) -> list[str]:
    """Use the same safe natural list format as My Pantry."""
    return parse_pantry_item_list(value)


def list_shopping_items() -> list[dict[str, Any]]:
    """List current Shopping List items alphabetically."""
    initialize_database()
    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT *
            FROM shopping_list_items
            ORDER BY lower(display_name), shopping_list_item_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def get_shopping_item(shopping_list_item_id: int) -> dict[str, Any] | None:
    initialize_database()
    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT *
            FROM shopping_list_items
            WHERE shopping_list_item_id = ?
            """,
            (int(shopping_list_item_id),),
        ).fetchone()
    return dict(row) if row is not None else None


def remove_shopping_item(shopping_list_item_id: int) -> bool:
    initialize_database()
    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute(
            """
            DELETE FROM shopping_list_items
            WHERE shopping_list_item_id = ?
            """,
            (int(shopping_list_item_id),),
        )
        connection.commit()
    return cursor.rowcount == 1


def clear_shopping_list() -> int:
    initialize_database()
    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute("DELETE FROM shopping_list_items")
        connection.commit()
    return int(cursor.rowcount)


def mark_shopping_item_purchased(
    shopping_list_item_id: int,
) -> dict[str, Any]:
    """Move one purchased item into My Pantry without risking loss."""
    item = get_shopping_item(shopping_list_item_id)
    if item is None:
        raise ValueError("Shopping List item was not found.")

    pantry_result = add_pantry_item(
        display_name=item["display_name"],
        source="manual",
    )
    removed = remove_shopping_item(shopping_list_item_id)
    if not removed:
        raise RuntimeError(
            "The purchased item reached My Pantry but could not be "
            "removed from the Shopping List."
        )

    return {
        "shopping_item": item,
        "pantry_item": pantry_result,
    }
