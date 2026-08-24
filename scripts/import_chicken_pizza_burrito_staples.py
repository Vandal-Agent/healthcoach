#!/usr/bin/env python3
"""Add confirmed chicken-pizza-burrito staples to Saved Foods and Pantry."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from food.library import (
    add_food_with_nutrition,
    find_food,
    get_active_nutrition,
)
from food.pantry import add_pantry_item


NUTRIENT_FIELDS = (
    "calories",
    "protein_g",
    "carbohydrates_g",
    "fat_g",
    "fiber_g",
    "sugar_g",
    "sodium_mg",
)


STAPLES: tuple[dict[str, Any], ...] = (
    {
        "canonical_name": "Chicken Breast, Boneless Skinless, Raw",
        "brand": None,
        "serving_description": "100 g",
        "serving_amount": 100.0,
        "serving_unit": "g",
        "calories": 120.0,
        "protein_g": 22.5,
        "carbohydrates_g": 0.0,
        "fat_g": 2.62,
        "fiber_g": 0.0,
        "sugar_g": 0.0,
        "sodium_mg": 45.0,
        "verification_source": "fdc.nal.usda.gov",
        "source_item_id": "fdc-171077",
        "source_url": (
            "https://fdc.nal.usda.gov/fdc-app.html#/food-details/"
            "171077/nutrients"
        ),
    },
    {
        "canonical_name": "Kirkland Light Tasting Olive Oil",
        "brand": "Kirkland Signature",
        "serving_description": "1 tbsp (3 tsp)",
        "serving_amount": 3.0,
        "serving_unit": "tsp",
        "calories": 120.0,
        "protein_g": 0.0,
        "carbohydrates_g": 0.0,
        "fat_g": 14.0,
        "fiber_g": 0.0,
        "sugar_g": 0.0,
        "sodium_mg": 0.0,
        "verification_source": "user_entered",
        "source_item_id": "kirkland-olive-oil-label",
        "source_url": (
            "https://www.costco.com/p/-/kirkland-signature-100-spanish-"
            "extra-virgin-olive-oil-3-l/100638063"
        ),
    },
    {
        "canonical_name": "Hormel Original Pepperoni",
        "brand": "Hormel",
        "serving_description": "30 g",
        "serving_amount": 30.0,
        "serving_unit": "g",
        "calories": 150.0,
        "protein_g": 5.0,
        "carbohydrates_g": 0.0,
        "fat_g": 14.0,
        "fiber_g": 0.0,
        "sugar_g": 0.0,
        "sodium_mg": 520.0,
        "verification_source": "user_package_label",
        "source_item_id": "upc-037600762007",
        "source_url": (
            "https://nutrition.hormelfoods.com/product-info/"
            "00037600100229"
        ),
    },
    {
        "canonical_name": "Kroger Pizza Sauce",
        "brand": "Kroger",
        "serving_description": "63 g",
        "serving_amount": 63.0,
        "serving_unit": "g",
        "calories": 30.0,
        "protein_g": 1.0,
        "carbohydrates_g": 5.0,
        "fat_g": 1.0,
        "fiber_g": 1.0,
        "sugar_g": 3.0,
        "sodium_mg": 200.0,
        "verification_source": "user_package_label",
        "source_item_id": "kroger-pizza-sauce-label",
        "source_url": None,
    },
    {
        "canonical_name": "Kraft 100% Grated Parmesan Cheese",
        "brand": "Kraft",
        "serving_description": "2 tsp (5 g)",
        "serving_amount": 5.0,
        "serving_unit": "g",
        "calories": 20.0,
        "protein_g": 2.0,
        "carbohydrates_g": 0.0,
        "fat_g": 1.5,
        "fiber_g": 0.0,
        "sugar_g": 0.0,
        "sodium_mg": 80.0,
        "verification_source": "user_entered",
        "source_item_id": "upc-021000615315",
        "source_url": (
            "https://tools.myfooddata.com/nutrition-facts/2617650/wt1/"
        ),
    },
    {
        "canonical_name": "Garlic, Raw",
        "brand": None,
        "serving_description": "1 clove (3 g)",
        "serving_amount": 1.0,
        "serving_unit": "clove",
        "calories": 4.47,
        "protein_g": 0.191,
        "carbohydrates_g": 0.992,
        "fat_g": 0.015,
        "fiber_g": 0.063,
        "sugar_g": 0.03,
        "sodium_mg": 0.51,
        "verification_source": "fdc.nal.usda.gov",
        "source_item_id": "usda-garlic-raw",
        "source_url": "https://fdc.nal.usda.gov/",
    },
    {
        "canonical_name": "Great Value Cream Cheese",
        "brand": "Great Value",
        "serving_description": "2 tbsp (28 g)",
        "serving_amount": 28.0,
        "serving_unit": "g",
        "calories": 100.0,
        "protein_g": 2.0,
        "carbohydrates_g": 2.0,
        "fat_g": 9.0,
        "fiber_g": 0.0,
        "sugar_g": 1.0,
        "sodium_mg": 100.0,
        "verification_source": "user_entered",
        "source_item_id": "upc-078742370477",
        "source_url": (
            "https://tools.myfooddata.com/nutrition-facts/373484/wt1/"
        ),
    },
    {
        "canonical_name": "Mission Flour Tortilla, Soft Taco",
        "brand": "Mission",
        "serving_description": "1 tortilla",
        "serving_amount": 1.0,
        "serving_unit": "tortilla",
        "calories": 140.0,
        "protein_g": 4.0,
        "carbohydrates_g": 24.0,
        "fat_g": 3.0,
        "fiber_g": 1.0,
        "sugar_g": 2.0,
        "sodium_mg": 410.0,
        "verification_source": "user_package_label",
        "source_item_id": "mission-soft-taco-label",
        "source_url": None,
    },
    {
        "canonical_name": "Sargento Reduced Fat Mozzarella",
        "brand": "Sargento",
        "serving_description": "1/4 cup (28 g)",
        "serving_amount": 28.0,
        "serving_unit": "g",
        "calories": 70.0,
        "protein_g": 8.0,
        "carbohydrates_g": 1.0,
        "fat_g": 4.5,
        "fiber_g": 0.0,
        "sugar_g": 0.0,
        "sodium_mg": 210.0,
        "verification_source": "user_entered",
        "source_item_id": "upc-046100800829",
        "source_url": (
            "https://www.sargento.com/our-cheese/shredded-cheese/"
            "sargento-shredded-reduced-fat-mozzarella-natural-cheese"
        ),
    },
    {
        "canonical_name": "Kirkland Mexican Style Blend",
        "brand": "Kirkland Signature",
        "serving_description": "1/3 cup (28 g)",
        "serving_amount": 28.0,
        "serving_unit": "g",
        "calories": 110.0,
        "protein_g": 7.0,
        "carbohydrates_g": 1.0,
        "fat_g": 9.0,
        "fiber_g": 0.0,
        "sugar_g": 0.0,
        "sodium_mg": 170.0,
        "verification_source": "user_package_label",
        "source_item_id": "kirkland-mexican-blend-label",
        "source_url": None,
    },
)


def _assert_existing_matches(food: dict[str, Any], expected: dict[str, Any]) -> None:
    nutrition = get_active_nutrition(int(food["food_id"]))
    if nutrition is None:
        raise RuntimeError(
            f"Stopped safely: {food['canonical_name']} has no active nutrition."
        )

    mismatches = []
    for field in NUTRIENT_FIELDS:
        actual = nutrition.get(field)
        expected_value = expected[field]
        if actual is None or abs(float(actual) - float(expected_value)) > 0.01:
            mismatches.append(field)

    if mismatches:
        raise RuntimeError(
            "Stopped safely: existing nutrition conflicts for "
            f"{food['canonical_name']}: {', '.join(mismatches)}."
        )


def import_staples() -> dict[str, int]:
    """Preflight, save, and link all confirmed staples to My Pantry."""
    for item in STAPLES:
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
    pantry_created_count = 0

    for item in STAPLES:
        result = add_food_with_nutrition(
            restaurant=None,
            food_type="food",
            verification_status="verified",
            **item,
        )
        food = result["food"]
        if result.get("created"):
            created_count += 1
        else:
            reused_count += 1

        pantry_result = add_pantry_item(
            display_name=str(food["canonical_name"]),
            food_id=int(food["food_id"]),
            source="saved_food",
        )
        if pantry_result.pop("created"):
            pantry_created_count += 1

    return {
        "foods_created": created_count,
        "foods_reused": reused_count,
        "pantry_items_created": pantry_created_count,
        "pantry_items_ready": len(STAPLES),
    }


def main() -> None:
    result = import_staples()
    print(
        "Chicken pizza burrito staples are ready: "
        f"{result['foods_created']} Saved Foods created, "
        f"{result['foods_reused']} reused, "
        f"{result['pantry_items_created']} Pantry items added, "
        f"{result['pantry_items_ready']} nutrition-ready."
    )


if __name__ == "__main__":
    main()
