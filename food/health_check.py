from __future__ import annotations

from datetime import datetime
from typing import Any

from food.finder import list_food_locations
from food.library import food_needs_reverification
from food.pantry import list_pantry_items
from food.resolver import is_trusted_saved_food


NUTRIENT_FIELDS = (
    ("calories", "calories"),
    ("protein_g", "protein"),
    ("carbohydrates_g", "carbohydrates"),
    ("fat_g", "fat"),
    ("fiber_g", "fiber"),
    ("sugar_g", "sugar"),
    ("sodium_mg", "sodium"),
)

USER_MAINTAINED_SOURCES = {
    "user_entered",
    "user_package_label",
}

INACTIVE_SOURCES = {
    "archived_user_food",
    "consolidated_food_record",
}


def missing_nutrition_fields(food: dict[str, Any]) -> list[str]:
    """Return nutrition fields that are genuinely absent, preserving zeroes."""
    return [
        label
        for field, label in NUTRIENT_FIELDS
        if food.get(field) is None
    ]


def food_source_needs_review(food: dict[str, Any]) -> bool:
    """Identify non-recipe Foods whose active source is not trusted."""
    if str(food.get("food_type") or "food") == "recipe":
        return False
    if str(food.get("verification_source") or "") in INACTIVE_SOURCES:
        return False
    return not is_trusted_saved_food(food)


def food_source_is_due(
    food: dict[str, Any],
    *,
    now: datetime | None = None,
) -> bool:
    """Identify trusted provider Foods whose source is due for a recheck."""
    if str(food.get("food_type") or "food") == "recipe":
        return False
    source = str(food.get("verification_source") or "").strip()
    if source in USER_MAINTAINED_SOURCES | INACTIVE_SOURCES:
        return False
    if not is_trusted_saved_food(food):
        return False
    return food_needs_reverification(food, now=now)


def pantry_item_needs_nutrition(item: dict[str, Any]) -> bool:
    """Return whether a Pantry item lacks usable linked calories."""
    return (
        item.get("food_id") is None
        or item.get("nutrition_version_id") is None
        or item.get("calories") is None
    )


def pantry_item_needs_organization(item: dict[str, Any]) -> bool:
    """Return whether Pantry storage or food type is still unsorted."""
    return (
        str(item.get("storage_area") or "unsorted") == "unsorted"
        or str(item.get("food_category") or "unsorted") == "unsorted"
    )


def build_food_library_health_check(
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Build a read-only Food Library and Pantry maintenance report."""
    foods = [
        food
        for food in list_food_locations()
        if str(food.get("verification_source") or "")
        not in INACTIVE_SOURCES
    ]
    pantry_items = list_pantry_items()

    nutrition_gaps = []
    for food in foods:
        missing = missing_nutrition_fields(food)
        if missing:
            nutrition_gaps.append({**food, "health_check_reason": missing})

    source_review = [
        food for food in foods if food_source_needs_review(food)
    ]
    source_rechecks = [
        food for food in foods if food_source_is_due(food, now=now)
    ]
    pantry_nutrition = [
        item for item in pantry_items if pantry_item_needs_nutrition(item)
    ]
    pantry_organization = [
        item
        for item in pantry_items
        if pantry_item_needs_organization(item)
    ]

    preserved_versions = sum(
        max(0, int(food.get("nutrition_version_count") or 0) - 1)
        for food in foods
    )

    return {
        "food_count": len(foods),
        "pantry_count": len(pantry_items),
        "pantry_nutrition": pantry_nutrition,
        "pantry_organization": pantry_organization,
        "nutrition_gaps": nutrition_gaps,
        "source_review": source_review,
        "source_rechecks": source_rechecks,
        "preserved_versions": preserved_versions,
    }


def health_check_food_items(
    report: dict[str, Any],
    category: str,
) -> list[dict[str, Any]]:
    """Return one stable Food category from a generated health check."""
    allowed = {"nutrition_gaps", "source_review", "source_rechecks"}
    if category not in allowed:
        raise ValueError("Unknown Food Library health-check category.")
    return list(report.get(category) or [])


def health_check_food_reason(food: dict[str, Any], category: str) -> str:
    """Explain why one Food appears in a health-check category."""
    if category == "nutrition_gaps":
        missing = list(food.get("health_check_reason") or [])
        return "Missing: " + ", ".join(missing)
    if category == "source_review":
        status = str(food.get("verification_status") or "unknown")
        source = str(food.get("verification_source") or "not recorded")
        return f"Status: {status}; source: {source}"
    if category == "source_rechecks":
        verified = str(food.get("last_verified_at") or "").strip()
        return f"Last checked: {verified[:10] if verified else 'not recorded'}"
    raise ValueError("Unknown Food Library health-check category.")
