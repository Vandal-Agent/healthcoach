#!/usr/bin/env python3
"""Add three reusable beer defaults with fluid-ounce scaling."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from food.database import (
    DATABASE_PATH,
    get_connection,
    normalize_key_part,
    save_food_alias,
)
from food.library import (
    add_food_with_nutrition,
    find_food,
    get_active_nutrition,
)


NUTRIENT_FIELDS = (
    "calories",
    "protein_g",
    "carbohydrates_g",
    "fat_g",
    "fiber_g",
    "sugar_g",
    "sodium_mg",
)


BEER_DEFAULTS: tuple[dict[str, Any], ...] = (
    {
        "canonical_name": "Corona Extra Mexican Lager",
        "brand": "Corona",
        "serving_description": "1 fl oz",
        "serving_amount": 1.0,
        "serving_unit": "fl oz",
        "calories": 148.0 / 12.0,
        "protein_g": 1.2 / 12.0,
        "carbohydrates_g": 13.9 / 12.0,
        "fat_g": 0.0,
        "fiber_g": None,
        "sugar_g": None,
        "sodium_mg": None,
        "verification_source": "coronausa.com",
        "source_item_id": "corona-extra-12-fl-oz",
        "source_url": "https://www.coronausa.com/pages/corona-extra/1000",
        "aliases": (
            "Mexican lager",
            "Mexican lager beer",
            "Mexican logger",
            "Mexican logger beer",
        ),
    },
    {
        "canonical_name": "Sierra Nevada Pale Ale",
        "brand": "Sierra Nevada",
        "serving_description": "1 fl oz",
        "serving_amount": 1.0,
        "serving_unit": "fl oz",
        "calories": 175.0 / 12.0,
        "protein_g": 1.9 / 12.0,
        "carbohydrates_g": 14.3 / 12.0,
        "fat_g": None,
        "fiber_g": None,
        "sugar_g": None,
        "sodium_mg": None,
        "verification_source": "sierranevada.com",
        "source_item_id": "sierra-nevada-pale-ale-12-fl-oz",
        "source_url": "https://sierranevada.com/brews/pale-ale",
        "aliases": (
            "Pale ale",
            "Pale ale beer",
        ),
    },
    {
        "canonical_name": "Sierra Nevada Hop Hunter IPA",
        "brand": "Sierra Nevada",
        "serving_description": "1 fl oz",
        "serving_amount": 1.0,
        "serving_unit": "fl oz",
        "calories": 194.0 / 12.0,
        "protein_g": 2.2 / 12.0,
        "carbohydrates_g": 14.6 / 12.0,
        "fat_g": None,
        "fiber_g": None,
        "sugar_g": None,
        "sodium_mg": None,
        "verification_source": "sierranevada.com",
        "source_item_id": "sierra-nevada-hop-hunter-ipa-12-fl-oz",
        "source_url": "https://sierranevada.com/brews/hop-hunter-ipa",
        "aliases": (
            "IPA",
            "IPA beer",
            "India pale ale",
            "India pale ale beer",
        ),
    },
)


def _values_match(actual: Any, expected: Any) -> bool:
    if actual is None or expected is None:
        return actual is None and expected is None
    return abs(float(actual) - float(expected)) <= 0.01


def _assert_existing_matches(
    food: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    nutrition = get_active_nutrition(int(food["food_id"]))
    if nutrition is None:
        raise RuntimeError(
            f"Stopped safely: {food['canonical_name']} has no active nutrition."
        )

    mismatches = [
        field
        for field in NUTRIENT_FIELDS
        if not _values_match(nutrition.get(field), expected.get(field))
    ]
    for field in ("serving_amount", "serving_unit"):
        if field == "serving_amount":
            matches = _values_match(food.get(field), expected.get(field))
        else:
            matches = (
                str(food.get(field) or "").strip().lower()
                == str(expected.get(field) or "").strip().lower()
            )
        if not matches:
            mismatches.append(field)

    if mismatches:
        raise RuntimeError(
            "Stopped safely: existing nutrition conflicts for "
            f"{food['canonical_name']}: {', '.join(mismatches)}."
        )


def _alias_owner_ids(alias: str) -> set[int]:
    normalized = normalize_key_part(alias)
    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT food_aliases.food_id
            FROM food_aliases
            WHERE food_aliases.normalized_alias = ?
              AND NOT EXISTS (
                    SELECT 1
                    FROM food_consolidations
                    WHERE food_consolidations.duplicate_food_id =
                          food_aliases.food_id
              )
            """,
            (normalized,),
        ).fetchall()
    return {int(row["food_id"]) for row in rows}


def import_beer_defaults() -> dict[str, Any]:
    """Preflight and save all three beer defaults without logging them."""
    for item in BEER_DEFAULTS:
        existing = find_food(
            canonical_name=item["canonical_name"],
            serving_description=item["serving_description"],
            brand=item["brand"],
            restaurant=None,
        )
        if existing is not None:
            _assert_existing_matches(existing, item)

    created_count = 0
    reused_count = 0
    aliases_added = 0
    alias_conflicts: list[str] = []

    for item in BEER_DEFAULTS:
        food_values = {
            key: value
            for key, value in item.items()
            if key != "aliases"
        }
        result = add_food_with_nutrition(
            restaurant=None,
            food_type="drink",
            verification_status="verified",
            **food_values,
        )
        food_id = int(result["food"]["food_id"])
        if result.get("created"):
            created_count += 1
        else:
            reused_count += 1

        for alias in item["aliases"]:
            owners = _alias_owner_ids(str(alias))
            if owners and owners != {food_id}:
                alias_conflicts.append(str(alias))
                continue
            save_food_alias(food_id=food_id, alias_text=str(alias))
            aliases_added += 1

    return {
        "foods_created": created_count,
        "foods_reused": reused_count,
        "aliases_added": aliases_added,
        "alias_conflicts": alias_conflicts,
    }


def _rounded_calories(calories_per_ounce: float, ounces: float) -> int:
    return round(float(calories_per_ounce) * float(ounces))


def main() -> None:
    result = import_beer_defaults()
    print(
        "Beer defaults are ready: "
        f"{result['foods_created']} Saved Foods created, "
        f"{result['foods_reused']} reused, "
        f"{result['aliases_added']} search names ready."
    )
    for item in BEER_DEFAULTS:
        print(
            f"- {item['canonical_name']}: "
            f"1 oz {float(item['calories']):.2f} cal; "
            f"12 oz {_rounded_calories(item['calories'], 12)} cal; "
            f"16 oz {_rounded_calories(item['calories'], 16)} cal; "
            f"32 oz {_rounded_calories(item['calories'], 32)} cal"
        )
    conflicts = list(result["alias_conflicts"])
    if conflicts:
        print(
            "Existing search names were preserved for manual review: "
            + ", ".join(conflicts)
            + "."
        )
    print("Nothing was logged as eaten and My Pantry was not changed.")


if __name__ == "__main__":
    main()
