from __future__ import annotations

from typing import Any

from food.database import (
    DATABASE_PATH,
    current_timestamp,
    get_connection,
    initialize_database,
    normalize_key_part,
)
from food.resolver import normalized_food_tokens


def _candidate_foods() -> list[dict[str, Any]]:
    """Return active non-recipe Foods eligible for duplicate review."""
    initialize_database()
    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT
                foods.food_id,
                foods.canonical_name,
                foods.brand,
                foods.restaurant,
                foods.food_type,
                foods.serving_description,
                foods.verification_status,
                foods.verification_source,
                nutrition_versions.version_number,
                nutrition_versions.calories,
                nutrition_versions.protein_g,
                nutrition_versions.carbohydrates_g,
                nutrition_versions.fat_g,
                nutrition_versions.fiber_g,
                nutrition_versions.sugar_g,
                nutrition_versions.sodium_mg,
                (
                    SELECT GROUP_CONCAT(food_aliases.alias_text, ' | ')
                    FROM food_aliases
                    WHERE food_aliases.food_id = foods.food_id
                ) AS aliases,
                (
                    SELECT COUNT(*)
                    FROM pantry_items
                    WHERE pantry_items.food_id = foods.food_id
                ) AS pantry_count,
                (
                    SELECT COUNT(*)
                    FROM food_favorites
                    WHERE food_favorites.food_id = foods.food_id
                ) AS favorite_count,
                (
                    SELECT COUNT(*)
                    FROM barcode_mappings
                    WHERE barcode_mappings.food_id = foods.food_id
                ) AS barcode_count,
                (
                    SELECT COUNT(*)
                    FROM food_entries
                    WHERE food_entries.food_id = foods.food_id
                ) AS log_count,
                (
                    SELECT COUNT(*)
                    FROM saved_recipe_ingredients
                    WHERE saved_recipe_ingredients.food_id = foods.food_id
                ) AS recipe_ingredient_count
            FROM foods
            LEFT JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id =
                 foods.active_nutrition_version_id
            WHERE foods.verification_status = 'verified'
              AND foods.food_type != 'recipe'
              AND NOT EXISTS (
                    SELECT 1
                    FROM saved_recipes
                    WHERE saved_recipes.food_id = foods.food_id
              )
              AND NOT EXISTS (
                    SELECT 1
                    FROM food_consolidations
                    WHERE food_consolidations.duplicate_food_id = foods.food_id
              )
            ORDER BY lower(foods.canonical_name), foods.food_id
            """
        ).fetchall()
    return [dict(row) for row in rows]


def _identity_variants(food: dict[str, Any]) -> set[frozenset[str]]:
    """Build conservative name identities without inferring food meaning."""
    name = str(food.get("canonical_name") or "")
    brand = str(food.get("brand") or "")
    aliases = str(food.get("aliases") or "")
    variants: set[frozenset[str]] = set()

    for value in (name, " ".join(part for part in (brand, name) if part)):
        tokens = normalized_food_tokens(value)
        if tokens:
            variants.add(frozenset(tokens))

    for alias in aliases.split(" | "):
        tokens = normalized_food_tokens(alias)
        if tokens:
            variants.add(frozenset(tokens))

    return variants


def _possible_duplicate_rank(
    first: dict[str, Any],
    second: dict[str, Any],
) -> tuple[Any, ...] | None:
    """Return a deterministic rank for a conservative possible match."""
    if str(first.get("food_type")) != str(second.get("food_type")):
        return None

    first_restaurant = normalize_key_part(first.get("restaurant"))
    second_restaurant = normalize_key_part(second.get("restaurant"))
    if first_restaurant or second_restaurant:
        if first_restaurant != second_restaurant:
            return None

    first_brand = normalize_key_part(first.get("brand"))
    second_brand = normalize_key_part(second.get("brand"))
    if first_brand and second_brand and first_brand != second_brand:
        return None

    shared_variants = _identity_variants(first) & _identity_variants(second)
    if not shared_variants:
        return None

    first_name = normalize_key_part(first.get("canonical_name"))
    second_name = normalize_key_part(second.get("canonical_name"))
    exact_name = first_name == second_name
    same_brand = first_brand == second_brand
    total_uses = sum(
        int(food.get(field) or 0)
        for food in (first, second)
        for field in (
            "pantry_count",
            "favorite_count",
            "barcode_count",
            "log_count",
            "recipe_ingredient_count",
        )
    )
    shortest_identity = min(len(value) for value in shared_variants)

    return (
        0 if exact_name else 1,
        0 if same_brand else 1,
        -shortest_identity,
        -total_uses,
        min(int(first["food_id"]), int(second["food_id"])),
        max(int(first["food_id"]), int(second["food_id"])),
    )


def list_possible_food_duplicates(
    *,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """List undismissed possible duplicate pairs for explicit review."""
    if int(limit) < 1:
        raise ValueError("limit must be positive.")

    foods = _candidate_foods()
    initialize_database()
    with get_connection(DATABASE_PATH) as connection:
        reviewed = {
            (int(row["first_food_id"]), int(row["second_food_id"]))
            for row in connection.execute(
                """
                SELECT first_food_id, second_food_id
                FROM food_duplicate_reviews
                WHERE decision = 'keep_separate'
                """
            ).fetchall()
        }

    ranked: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    for first_index, first in enumerate(foods):
        for second in foods[first_index + 1:]:
            pair_key = (
                min(int(first["food_id"]), int(second["food_id"])),
                max(int(first["food_id"]), int(second["food_id"])),
            )
            if pair_key in reviewed:
                continue
            rank = _possible_duplicate_rank(first, second)
            if rank is None:
                continue
            ranked.append((rank, {"first": first, "second": second}))

    ranked.sort(key=lambda item: item[0])
    return [pair for _, pair in ranked[: int(limit)]]


def mark_foods_keep_separate(
    first_food_id: int,
    second_food_id: int,
) -> dict[str, Any]:
    """Persist a user's decision that two active Foods are distinct."""
    first_id, second_id = sorted((int(first_food_id), int(second_food_id)))
    if first_id == second_id:
        raise ValueError("Choose two different Food records.")
    initialize_database()
    timestamp = current_timestamp()
    with get_connection(DATABASE_PATH) as connection:
        count = connection.execute(
            """
            SELECT COUNT(*) AS count
            FROM foods
            WHERE food_id IN (?, ?)
            """,
            (first_id, second_id),
        ).fetchone()
        if int(count["count"] or 0) != 2:
            raise ValueError("One of those Food records no longer exists.")
        connection.execute(
            """
            INSERT INTO food_duplicate_reviews (
                first_food_id,
                second_food_id,
                decision,
                created_at,
                updated_at
            )
            VALUES (?, ?, 'keep_separate', ?, ?)
            ON CONFLICT(first_food_id, second_food_id)
            DO UPDATE SET
                decision = excluded.decision,
                updated_at = excluded.updated_at
            """,
            (first_id, second_id, timestamp, timestamp),
        )
        connection.commit()
    return {
        "first_food_id": first_id,
        "second_food_id": second_id,
        "decision": "keep_separate",
    }


def get_consolidation_target(food_id: int) -> int:
    """Return the active target for a Food, following durable redirects."""
    initialize_database()
    current_id = int(food_id)
    seen: set[int] = set()
    with get_connection(DATABASE_PATH) as connection:
        while current_id not in seen:
            seen.add(current_id)
            row = connection.execute(
                """
                SELECT primary_food_id
                FROM food_consolidations
                WHERE duplicate_food_id = ?
                """,
                (current_id,),
            ).fetchone()
            if row is None:
                return current_id
            current_id = int(row["primary_food_id"])
    raise RuntimeError("Food consolidation records contain a cycle.")


def consolidate_food_records(
    *,
    primary_food_id: int,
    duplicate_food_id: int,
) -> dict[str, Any]:
    """
    Consolidate future-facing links while retaining every historical record.

    Food entries, Nutrition Versions, and saved recipe ingredient snapshots
    stay attached to their original IDs for auditability.
    """
    primary_id = int(primary_food_id)
    duplicate_id = int(duplicate_food_id)
    if primary_id == duplicate_id:
        raise ValueError("Choose two different Food records.")
    initialize_database()
    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        foods = {
            int(row["food_id"]): dict(row)
            for row in connection.execute(
                """
                SELECT *
                FROM foods
                WHERE food_id IN (?, ?)
                """,
                (primary_id, duplicate_id),
            ).fetchall()
        }
        if set(foods) != {primary_id, duplicate_id}:
            raise ValueError("One of those Food records no longer exists.")
        if str(foods[primary_id]["verification_status"]) != "verified":
            raise ValueError("The primary Food must have active nutrition.")

        existing_redirect = connection.execute(
            """
            SELECT duplicate_food_id
            FROM food_consolidations
            WHERE duplicate_food_id IN (?, ?)
            LIMIT 1
            """,
            (primary_id, duplicate_id),
        ).fetchone()
        if existing_redirect is not None:
            raise ValueError("One of those Foods was already consolidated.")

        recipe = connection.execute(
            """
            SELECT saved_recipe_id
            FROM saved_recipes
            WHERE food_id IN (?, ?)
            LIMIT 1
            """,
            (primary_id, duplicate_id),
        ).fetchone()
        if recipe is not None:
            raise ValueError(
                "Saved Recipe records must be managed as recipes, not "
                "consolidated as Foods."
            )

        aliases = {
            str(foods[duplicate_id]["canonical_name"]),
            *(
                str(row["alias_text"])
                for row in connection.execute(
                    """
                    SELECT alias_text
                    FROM food_aliases
                    WHERE food_id = ?
                    """,
                    (duplicate_id,),
                ).fetchall()
            ),
        }
        duplicate_brand = str(foods[duplicate_id].get("brand") or "").strip()
        if duplicate_brand:
            aliases.add(
                f"{duplicate_brand} {foods[duplicate_id]['canonical_name']}"
            )
        for alias in aliases:
            normalized = normalize_key_part(alias)
            if not normalized:
                continue
            connection.execute(
                """
                INSERT OR IGNORE INTO food_aliases (
                    food_id,
                    alias_text,
                    normalized_alias,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (primary_id, alias, normalized, timestamp, timestamp),
            )

        moved = {}
        for table in ("barcode_mappings", "pantry_items"):
            cursor = connection.execute(
                f"UPDATE {table} SET food_id = ?, updated_at = ? "
                "WHERE food_id = ?",
                (primary_id, timestamp, duplicate_id),
            )
            moved[table] = int(cursor.rowcount)

        duplicate_favorites = connection.execute(
            """
            SELECT quantity, meal_category, created_at, updated_at
            FROM food_favorites
            WHERE food_id = ?
            """,
            (duplicate_id,),
        ).fetchall()
        for favorite in duplicate_favorites:
            connection.execute(
                """
                INSERT OR IGNORE INTO food_favorites (
                    food_id,
                    quantity,
                    meal_category,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    primary_id,
                    favorite["quantity"],
                    favorite["meal_category"],
                    favorite["created_at"],
                    timestamp,
                ),
            )
        connection.execute(
            "DELETE FROM food_favorites WHERE food_id = ?",
            (duplicate_id,),
        )
        moved["food_favorites"] = len(duplicate_favorites)

        duplicate_profiles = connection.execute(
            """
            SELECT phrase, estimated_amount, estimated_unit,
                   user_confirmed, created_at
            FROM portion_profiles
            WHERE food_id = ?
            """,
            (duplicate_id,),
        ).fetchall()
        for profile in duplicate_profiles:
            connection.execute(
                """
                INSERT OR IGNORE INTO portion_profiles (
                    phrase,
                    food_id,
                    estimated_amount,
                    estimated_unit,
                    user_confirmed,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    profile["phrase"],
                    primary_id,
                    profile["estimated_amount"],
                    profile["estimated_unit"],
                    profile["user_confirmed"],
                    profile["created_at"],
                    timestamp,
                ),
            )
        connection.execute(
            "DELETE FROM portion_profiles WHERE food_id = ?",
            (duplicate_id,),
        )
        moved["portion_profiles"] = len(duplicate_profiles)

        connection.execute(
            "DELETE FROM food_aliases WHERE food_id = ?",
            (duplicate_id,),
        )
        connection.execute(
            """
            DELETE FROM food_duplicate_reviews
            WHERE first_food_id = ? OR second_food_id = ?
            """,
            (duplicate_id, duplicate_id),
        )
        connection.execute(
            """
            INSERT INTO food_consolidations (
                duplicate_food_id,
                primary_food_id,
                created_at
            )
            VALUES (?, ?, ?)
            """,
            (duplicate_id, primary_id, timestamp),
        )
        connection.execute(
            """
            UPDATE foods
            SET verification_status = 'unverified',
                verification_source = 'consolidated_food_record',
                updated_at = ?
            WHERE food_id = ?
            """,
            (timestamp, duplicate_id),
        )
        connection.commit()

    return {
        "primary_food_id": primary_id,
        "duplicate_food_id": duplicate_id,
        "primary_name": str(foods[primary_id]["canonical_name"]),
        "duplicate_name": str(foods[duplicate_id]["canonical_name"]),
        "moved": moved,
        "historical_records_preserved": True,
    }
