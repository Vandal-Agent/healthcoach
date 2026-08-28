from __future__ import annotations

import argparse
import json
import re
from typing import Any
from urllib.parse import urlparse

from food.database import (
    DATABASE_PATH,
    build_search_key,
    get_connection,
    initialize_database,
    normalize_key_part,
)
from food.nutrition_lookup import is_trusted_nutrition_source


def is_trusted_saved_food(
    food: dict[str, Any] | None,
) -> bool:
    """Return whether a saved Food still has an approved source."""
    if food is None:
        return False

    verification_status = str(
        food.get("verification_status") or ""
    ).strip().lower()

    verification_source = str(
        food.get("verification_source") or ""
    ).strip()

    if verification_status != "verified":
        return False

    if not verification_source:
        return False

    if verification_source in {
        "user_package_label",
        "user_entered",
    }:
        return True

    source_url = str(food.get("source_url") or "").strip()
    if source_url:
        hostname = str(urlparse(source_url).hostname or "").strip()
        if hostname and is_trusted_nutrition_source(hostname):
            return True

    return is_trusted_nutrition_source(
        verification_source
    )


def clean_optional_text(value: str | None) -> str | None:
    """Trim optional text and convert blank strings to None."""
    if value is None:
        return None

    cleaned = value.strip()
    return cleaned or None


def find_by_search_key(
    search_key: str,
) -> dict[str, Any] | None:
    """Find one Food using its stable search key."""
    initialize_database()

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT resolved_foods.*
            FROM foods AS matched_foods
            LEFT JOIN food_consolidations
              ON food_consolidations.duplicate_food_id =
                 matched_foods.food_id
            JOIN foods AS resolved_foods
              ON resolved_foods.food_id = COALESCE(
                    food_consolidations.primary_food_id,
                    matched_foods.food_id
              )
            WHERE lower(matched_foods.search_key) = lower(?)
            LIMIT 1
            """,
            (search_key.strip(),),
        ).fetchone()

    return dict(row) if row else None


def find_by_alias(
    *,
    alias_text: str,
    brand: str | None = None,
    restaurant: str | None = None,
) -> dict[str, Any] | None:
    """Find one Food using a saved alias."""
    initialize_database()

    normalized_alias = normalize_key_part(alias_text)

    if not normalized_alias:
        return None

    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            """
            SELECT foods.*
            FROM food_aliases
            JOIN foods
              ON foods.food_id = food_aliases.food_id
            WHERE food_aliases.normalized_alias = ?
              AND NOT EXISTS (
                    SELECT 1
                    FROM food_consolidations
                    WHERE food_consolidations.duplicate_food_id =
                          foods.food_id
              )
              AND (
                    ? IS NULL
                    OR lower(foods.brand) = lower(?)
              )
              AND (
                    ? IS NULL
                    OR lower(foods.restaurant) = lower(?)
              )
            ORDER BY foods.food_id
            LIMIT 1
            """,
            (
                normalized_alias,
                brand,
                brand,
                restaurant,
                restaurant,
            ),
        ).fetchone()

    return dict(row) if row else None


def normalized_food_tokens(value: str | None) -> set[str]:
    """
    Return meaningful deterministic food-name tokens.

    This normalizes obvious singular/plural wording but does not infer
    a different food.
    """
    if not value:
        return set()

    cleaned = value.lower()
    cleaned = cleaned.replace("’", "'")
    cleaned = re.sub(r"\b([a-z0-9]+)'s\b", r"\1", cleaned)
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)

    ignored = {
        "a",
        "an",
        "the",
        "world",
        "famous",
        "serving",
        "order",
        "regular",
        "standard",
    }

    replacements = {
        "fry": "fries",
        "fries": "fries",
        "sandwiches": "sandwich",
        "burgers": "burger",
        "tacos": "taco",
        "burritos": "burrito",
        "tortillas": "tortilla",
    }

    tokens = set()

    for token in cleaned.split():
        normalized = replacements.get(token, token)

        if (
            normalized == token
            and len(token) > 3
            and token.endswith("s")
            and not token.endswith(("ss", "us", "is"))
        ):
            normalized = token[:-1]

        if normalized in ignored:
            continue

        tokens.add(normalized)

    return tokens


def nutrient_completeness(row: dict[str, Any]) -> int:
    """Count verified nutrient fields present on one candidate."""
    nutrient_fields = (
        "calories",
        "protein_g",
        "carbohydrates_g",
        "fat_g",
        "fiber_g",
        "sugar_g",
        "sodium_mg",
    )

    return sum(
        1
        for field in nutrient_fields
        if row.get(field) is not None
    )


def find_unique_restaurant_food_match(
    *,
    food_name: str,
    serving_description: str,
    brand: str | None,
    restaurant: str | None,
) -> dict[str, Any] | None:
    """
    Find one unambiguous saved Food within the requested source context.

    Requirements:
    - same restaurant or brand when supplied
    - meaningful food-name token overlap
    - requested non-standard serving/size appears in the saved name
      or serving description
    - one unique best candidate by nutrient completeness
    """
    initialize_database()

    requested_tokens = normalized_food_tokens(food_name)

    if not requested_tokens:
        return None

    requested_serving = normalize_key_part(serving_description)
    serving_is_generic = requested_serving in {
        "",
        "standard",
        "regular",
        "one",
        "1",
    }

    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT
                foods.*,
                nutrition_versions.calories,
                nutrition_versions.protein_g,
                nutrition_versions.carbohydrates_g,
                nutrition_versions.fat_g,
                nutrition_versions.fiber_g,
                nutrition_versions.sugar_g,
                nutrition_versions.sodium_mg
            FROM foods
            LEFT JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id =
                 foods.active_nutrition_version_id
            WHERE (
                    ? IS NULL
                    OR lower(foods.brand) = lower(?)
                  )
              AND (
                    ? IS NULL
                    OR lower(foods.restaurant) = lower(?)
                  )
              AND NOT EXISTS (
                    SELECT 1
                    FROM food_consolidations
                    WHERE food_consolidations.duplicate_food_id =
                          foods.food_id
              )
            ORDER BY foods.food_id
            """,
            (
                brand,
                brand,
                restaurant,
                restaurant,
            ),
        ).fetchall()

    candidates: list[dict[str, Any]] = []

    for row in rows:
        candidate = dict(row)

        candidate_name_tokens = normalized_food_tokens(
            candidate.get("canonical_name")
        )

        candidate_tokens = normalized_food_tokens(
            " ".join(
                part
                for part in (
                    candidate.get("canonical_name"),
                    candidate.get("serving_description"),
                )
                if part
            )
        )

        if (
            len(requested_tokens) == 1
            and candidate_name_tokens != requested_tokens
        ):
            continue

        if not requested_tokens.issubset(candidate_tokens):
            continue

        if not serving_is_generic:
            searchable_serving = normalize_key_part(
                " ".join(
                    part
                    for part in (
                        candidate.get("canonical_name"),
                        candidate.get("serving_description"),
                    )
                    if part
                )
            )

            serving_tokens = {
                token
                for token in requested_serving.split("_")
                if token
            }
            candidate_serving_tokens = {
                token
                for token in searchable_serving.split("_")
                if token
            }

            if not serving_tokens.issubset(
                candidate_serving_tokens
            ):
                continue

        candidate["_completeness"] = nutrient_completeness(
            candidate
        )
        candidates.append(candidate)

    if not candidates:
        return None

    exact_name_candidates = [
        candidate
        for candidate in candidates
        if normalized_food_tokens(
            candidate.get("canonical_name")
        ) == requested_tokens
    ]

    if len(exact_name_candidates) == 1:
        best = exact_name_candidates[0]
        best.pop("_completeness", None)

        for nutrient_field in (
            "calories",
            "protein_g",
            "carbohydrates_g",
            "fat_g",
            "fiber_g",
            "sugar_g",
            "sodium_mg",
        ):
            best.pop(nutrient_field, None)

        return best

    best_score = max(
        candidate["_completeness"]
        for candidate in candidates
    )

    best_candidates = [
        candidate
        for candidate in candidates
        if candidate["_completeness"] == best_score
    ]

    if len(best_candidates) != 1:
        nutrient_fields = (
            "calories",
            "protein_g",
            "carbohydrates_g",
            "fat_g",
            "fiber_g",
            "sugar_g",
            "sodium_mg",
        )

        nutrition_signatures = {
            tuple(candidate.get(field) for field in nutrient_fields)
            for candidate in best_candidates
        }

        normalized_names = {
            frozenset(
                normalized_food_tokens(
                    candidate.get("canonical_name")
                )
            )
            for candidate in best_candidates
        }

        if (
            len(nutrition_signatures) == 1
            and len(normalized_names) == 1
        ):
            best_candidates.sort(
                key=lambda candidate: candidate["food_id"]
            )
        else:
            return None

    best = best_candidates[0]
    best.pop("_completeness", None)

    for nutrient_field in (
        "calories",
        "protein_g",
        "carbohydrates_g",
        "fat_g",
        "fiber_g",
        "sugar_g",
        "sodium_mg",
    ):
        best.pop(nutrient_field, None)

    return best


def get_active_nutrition(
    food_id: int,
) -> dict[str, Any] | None:
    """Return active nutrition for one Food."""
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
            LIMIT 1
            """,
            (food_id,),
        ).fetchone()

    return dict(row) if row else None


def resolve_food(
    *,
    food_name: str,
    serving_description: str = "standard",
    brand: str | None = None,
    restaurant: str | None = None,
) -> dict[str, Any]:
    """
    Resolve one interpreted Food against the saved Food Library.

    Resolution order:
    1. Stable search key
    2. Saved alias
    3. Unique controlled restaurant/brand fallback
    4. Return missing
    """
    name = food_name.strip()
    serving = serving_description.strip() or "standard"
    cleaned_brand = clean_optional_text(brand)
    cleaned_restaurant = clean_optional_text(restaurant)

    if not name:
        raise ValueError("food_name is required.")

    search_key = build_search_key(
        canonical_name=name,
        serving_description=serving,
        brand=cleaned_brand,
        restaurant=cleaned_restaurant,
    )

    food = find_by_search_key(search_key)
    matched_by = "search_key"

    if food is not None and not is_trusted_saved_food(food):
        food = None

    if food is None:
        food = find_by_alias(
            alias_text=name,
            brand=cleaned_brand,
            restaurant=cleaned_restaurant,
        )
        matched_by = "alias"

        if food is not None and not is_trusted_saved_food(food):
            food = None

    if food is None:
        food = find_unique_restaurant_food_match(
            food_name=name,
            serving_description=serving,
            brand=cleaned_brand,
            restaurant=cleaned_restaurant,
        )
        matched_by = "controlled_fallback"

        if food is not None and not is_trusted_saved_food(food):
            food = None

    request = {
        "food_name": name,
        "serving_description": serving,
        "brand": cleaned_brand,
        "restaurant": cleaned_restaurant,
    }

    if food is None:
        return {
            "status": "missing",
            "found": False,
            "matched_by": None,
            "search_key": search_key,
            "request": request,
            "food": None,
            "nutrition": None,
        }

    return {
        "status": "found",
        "found": True,
        "matched_by": matched_by,
        "search_key": search_key,
        "request": request,
        "food": food,
        "nutrition": get_active_nutrition(food["food_id"]),
    }


def main() -> None:
    """Run a command-line Food Library resolution."""
    parser = argparse.ArgumentParser(
        description="Resolve a Food against the HealthCoach Food Library."
    )

    parser.add_argument("food_name")
    parser.add_argument(
        "--serving",
        default="standard",
    )
    parser.add_argument("--brand")
    parser.add_argument("--restaurant")

    args = parser.parse_args()

    result = resolve_food(
        food_name=args.food_name,
        serving_description=args.serving,
        brand=args.brand,
        restaurant=args.restaurant,
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
