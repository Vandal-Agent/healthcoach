from __future__ import annotations

from typing import Any

from food.database import (
    DATABASE_PATH,
    get_connection,
    initialize_database,
)
from food.resolver import normalized_food_tokens


def _all_food_locations() -> list[dict[str, Any]]:
    """Return Food records with their user-facing locations."""
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
                foods.source_url,
                foods.last_verified_at,
                foods.uses_since_verification,
                foods.next_verification_due,
                foods.active_nutrition_version_id,
                nutrition_versions.version_number,
                nutrition_versions.calories,
                nutrition_versions.protein_g,
                nutrition_versions.carbohydrates_g,
                nutrition_versions.fat_g,
                nutrition_versions.fiber_g,
                nutrition_versions.sugar_g,
                nutrition_versions.sodium_mg,
                (
                    SELECT COUNT(*)
                    FROM nutrition_versions AS version_history
                    WHERE version_history.food_id = foods.food_id
                ) AS nutrition_version_count,
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
                    SELECT GROUP_CONCAT(
                        pantry_items.display_name || ' (' ||
                        REPLACE(pantry_items.storage_area, '_', ' ') || ')',
                        ' | '
                    )
                    FROM pantry_items
                    WHERE pantry_items.food_id = foods.food_id
                ) AS pantry_locations,
                EXISTS (
                    SELECT 1
                    FROM saved_recipes
                    WHERE saved_recipes.food_id = foods.food_id
                ) AS is_saved_recipe,
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
                       OR food_entries.food_id IN (
                            SELECT food_consolidations.duplicate_food_id
                            FROM food_consolidations
                            WHERE food_consolidations.primary_food_id =
                                  foods.food_id
                       )
                ) AS log_count,
                (
                    SELECT MAX(food_entries.entry_date)
                    FROM food_entries
                    WHERE food_entries.food_id = foods.food_id
                       OR food_entries.food_id IN (
                            SELECT food_consolidations.duplicate_food_id
                            FROM food_consolidations
                            WHERE food_consolidations.primary_food_id =
                                  foods.food_id
                       )
                ) AS last_logged_date,
                (
                    SELECT COUNT(*)
                    FROM saved_recipe_ingredients
                    WHERE saved_recipe_ingredients.food_id = foods.food_id
                       OR saved_recipe_ingredients.food_id IN (
                            SELECT food_consolidations.duplicate_food_id
                            FROM food_consolidations
                            WHERE food_consolidations.primary_food_id =
                                  foods.food_id
                       )
                ) AS recipe_ingredient_count
            FROM foods
            LEFT JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id =
                 foods.active_nutrition_version_id
            WHERE NOT EXISTS (
                SELECT 1
                FROM food_consolidations
                WHERE food_consolidations.duplicate_food_id = foods.food_id
            )
            ORDER BY lower(foods.canonical_name), foods.food_id
            """
        ).fetchall()

    results = []
    for row in rows:
        item = dict(row)
        item["is_entered_food"] = str(
            item.get("verification_source") or ""
        ) in {"user_entered", "user_package_label"}
        results.append(item)
    return results


def list_food_locations() -> list[dict[str, Any]]:
    """List active Food Library records with their current uses."""
    return _all_food_locations()


def _search_rank(food: dict[str, Any], query: str) -> tuple | None:
    """Return a deterministic conservative search rank or no match."""
    requested_tokens = normalized_food_tokens(query)
    if not requested_tokens:
        return None

    canonical = str(food.get("canonical_name") or "")
    brand = str(food.get("brand") or "")
    aliases = str(food.get("aliases") or "")
    combined = " ".join(part for part in (canonical, brand, aliases) if part)
    candidate_tokens = normalized_food_tokens(combined)
    canonical_tokens = normalized_food_tokens(canonical)
    if not candidate_tokens:
        return None

    query_text = " ".join(str(query).lower().split())
    canonical_text = " ".join(canonical.lower().split())
    combined_text = " ".join(combined.lower().split())
    overlap = requested_tokens & candidate_tokens

    exact_name = requested_tokens == canonical_tokens
    exact_combined = requested_tokens == candidate_tokens
    requested_is_contained = requested_tokens.issubset(candidate_tokens)
    candidate_name_is_contained = canonical_tokens.issubset(requested_tokens)
    text_match = query_text in combined_text

    if not (
        exact_name
        or exact_combined
        or requested_is_contained
        or candidate_name_is_contained
        or text_match
    ):
        return None

    return (
        0 if exact_name else 1 if exact_combined else 2,
        0 if query_text == canonical_text else 1,
        0 if requested_is_contained else 1,
        -len(overlap),
        len(candidate_tokens - requested_tokens),
        canonical_text,
        int(food.get("food_id") or 0),
    )


def search_food_locations(
    query: str,
    *,
    limit: int = 12,
) -> list[dict[str, Any]]:
    """Search every Food record and explain where each result is used."""
    cleaned = str(query or "").strip()
    if len(cleaned) < 2:
        raise ValueError("Search for at least two characters.")
    if limit < 1:
        raise ValueError("limit must be positive.")

    ranked = []
    for food in list_food_locations():
        rank = _search_rank(food, cleaned)
        if rank is not None:
            ranked.append((rank, food))

    ranked.sort(key=lambda item: item[0])
    return [food for _, food in ranked[: int(limit)]]


def get_food_location(food_id: int) -> dict[str, Any] | None:
    """Return one Food record with its current locations."""
    return next(
        (
            food
            for food in list_food_locations()
            if int(food["food_id"]) == int(food_id)
        ),
        None,
    )
