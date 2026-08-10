from __future__ import annotations

import argparse
import json
import re
from typing import Any

from food.nutrition_lookup import lookup_verified_nutrition
from food.usda_provider import (
    lookup_usda_branded_nutrition,
    lookup_usda_nutrition,
)


def normalize_text(value: str | None) -> str:
    """Normalize text for identifiers and comparisons."""
    if value is None:
        return ""

    cleaned = value.strip().lower()
    cleaned = cleaned.replace("’", "").replace("'", "")
    cleaned = cleaned.replace("®", "").replace("™", "")
    cleaned = re.sub(r"[^a-z0-9]+", "-", cleaned)

    return cleaned.strip("-")


def clean_canonical_name(
    *,
    component_name: str,
    restaurant: str | None,
) -> str:
    """
    Remove trademark marks and a duplicated restaurant prefix.

    This only cleans verified source text. It does not invent a name.
    """
    cleaned = component_name.replace("®", "").replace("™", "").strip()

    if restaurant:
        prefix_pattern = re.compile(
            rf"^{re.escape(restaurant)}\s+",
            flags=re.IGNORECASE,
        )
        cleaned = prefix_pattern.sub("", cleaned).strip()

    if not cleaned:
        raise ValueError(
            "Verified nutrition component had no usable food name."
        )

    return cleaned


def parse_serving_description(
    serving_description: str,
) -> tuple[float, str]:
    """
    Parse a verified serving such as '1 sandwich' or '1 serving'.

    The provider refuses the item if the official serving cannot be
    represented without guessing.
    """
    cleaned = serving_description.strip()

    match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s+(.+)",
        cleaned,
    )

    if match is None:
        raise ValueError(
            "Verified serving description could not be parsed: "
            f"{serving_description!r}"
        )

    amount = float(match.group(1))
    unit = match.group(2).strip()

    if amount <= 0 or not unit:
        raise ValueError(
            "Verified serving description was invalid."
        )

    return amount, unit


def unsupported_result(
    *,
    notes: list[str] | None = None,
    clarification_question: str | None = None,
    missing_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Return the provider's standard unsupported response."""
    return {
        "found": False,
        "provider": "grounded_verified_lookup",
        "food": None,
        "nutrition": None,
        "verification": None,
        "missing_fields": missing_fields or [],
        "clarification_question": clarification_question,
        "notes": notes or [],
    }


def lookup_official_nutrition(
    *,
    restaurant: str | None,
    food_name: str,
    size: str | None = None,
    brand: str | None = None,
    drink: str | None = None,
) -> dict[str, Any]:
    """
    Retrieve verified nutrition through grounded web search.

    AI may locate and structure cited source data, but unsupported,
    incomplete, uncited, or ambiguous results are never accepted.
    """
    if brand and not restaurant:
        try:
            branded_usda_result = (
                lookup_usda_branded_nutrition(
                    brand=brand,
                    food_name=food_name,
                )
            )
        except Exception as error:
            branded_usda_result = unsupported_result(
                notes=[
                    "USDA branded-food lookup failed.",
                    str(error),
                ]
            )

        if branded_usda_result.get("found"):
            return branded_usda_result

    if not restaurant and not brand:
        try:
            generic_usda_result = lookup_usda_nutrition(
                restaurant=None,
                food_name=food_name,
                size=size,
                brand=None,
            )
        except Exception as error:
            generic_usda_result = unsupported_result(
                notes=[
                    "USDA generic-food lookup failed.",
                    str(error),
                ]
            )

        if generic_usda_result.get("found"):
            return generic_usda_result

    result = lookup_verified_nutrition(
        restaurant=restaurant,
        food_name=food_name,
        size=size,
        drink=drink,
    )

    components = result.get("components") or []
    missing_fields = result.get("missing_fields") or []
    clarification_question = result.get(
        "clarification_question"
    )
    notes = result.get("notes") or []

    if not components and restaurant:
        try:
            usda_result = lookup_usda_nutrition(
                restaurant=restaurant,
                food_name=food_name,
                size=size,
                brand=brand,
            )
        except Exception as error:
            usda_result = unsupported_result(
                notes=[
                    "USDA FoodData Central lookup failed.",
                    str(error),
                ]
            )

        if usda_result.get("found"):
            return usda_result

        usda_notes = usda_result.get("notes") or []

        return unsupported_result(
            notes=[
                *notes,
                *usda_notes,
            ],
            clarification_question=(
                clarification_question
                or usda_result.get(
                    "clarification_question"
                )
            ),
            missing_fields=(
                missing_fields
                or usda_result.get("missing_fields")
                or []
            ),
        )

    if not components and not restaurant and not brand:
        try:
            usda_result = lookup_usda_nutrition(
                restaurant=None,
                food_name=food_name,
                size=size,
                brand=None,
            )
        except Exception as error:
            usda_result = unsupported_result(
                notes=[
                    "USDA generic-food lookup failed.",
                    str(error),
                ]
            )

        if usda_result.get("found"):
            return usda_result

        return unsupported_result(
            notes=[
                *notes,
                *(usda_result.get("notes") or []),
            ],
            clarification_question=(
                clarification_question
                or usda_result.get("clarification_question")
            ),
            missing_fields=(
                missing_fields
                or usda_result.get("missing_fields")
                or []
            ),
        )

    if not components:
        return unsupported_result(
            notes=notes,
            clarification_question=clarification_question,
            missing_fields=missing_fields,
        )

    # app.py currently saves one normalized food at a time.
    # Multi-component meals will be handled by meal decomposition.
    if len(components) != 1:
        return unsupported_result(
            notes=[
                "Multiple verified components were returned, but "
                "single-food saving is required at this stage.",
                *notes,
            ],
            clarification_question=clarification_question,
            missing_fields=missing_fields,
        )

    # Do not silently save an incomplete combo or meal.
    if missing_fields:
        return unsupported_result(
            notes=notes,
            clarification_question=clarification_question,
            missing_fields=missing_fields,
        )

    component = components[0]

    source_title = str(
        component.get("source_title") or ""
    ).strip()
    source_url = str(
        component.get("source_url") or ""
    ).strip()
    component_name = str(
        component.get("name") or ""
    ).strip()
    serving_description = str(
        component.get("serving_description") or ""
    ).strip()

    if (
        not source_title
        or not source_url
        or not component_name
        or not serving_description
    ):
        return unsupported_result(
            notes=[
                "The verified result was missing required source "
                "or serving information.",
                *notes,
            ],
        )

    try:
        canonical_name = clean_canonical_name(
            component_name=component_name,
            restaurant=restaurant,
        )
        serving_amount, serving_unit = parse_serving_description(
            serving_description
        )
    except ValueError as error:
        return unsupported_result(
            notes=[str(error), *notes],
        )

    source_item_id = "-".join(
        part
        for part in (
            normalize_text(restaurant),
            normalize_text(canonical_name),
            normalize_text(serving_description),
        )
        if part
    )

    return {
        "found": True,
        "provider": "grounded_verified_lookup",
        "food": {
            "canonical_name": canonical_name,
            "restaurant": restaurant,
            "brand": brand,
            "food_type": "food",
            "serving_description": serving_description,
            "serving_amount": serving_amount,
            "serving_unit": serving_unit,
        },
        "nutrition": {
            "calories": component.get("calories"),
            "protein_g": component.get("protein_g"),
            "carbohydrates_g": component.get(
                "carbohydrates_g"
            ),
            "fat_g": component.get("fat_g"),
            "fiber_g": component.get("fiber_g"),
            "sugar_g": component.get("sugar_g"),
            "sodium_mg": component.get("sodium_mg"),
        },
        "verification": {
            "status": "verified",
            "source": source_title,
            "source_item_id": source_item_id or None,
            "source_url": source_url,
        },
        "missing_fields": [],
        "clarification_question": None,
        "notes": notes,
    }


def main() -> None:
    """Run a grounded nutrition provider test."""
    parser = argparse.ArgumentParser(
        description="Test grounded verified nutrition lookup."
    )

    parser.add_argument("food_name")
    parser.add_argument("--restaurant")
    parser.add_argument("--brand")
    parser.add_argument("--size")
    parser.add_argument("--drink")

    args = parser.parse_args()

    result = lookup_official_nutrition(
        restaurant=args.restaurant,
        food_name=args.food_name,
        size=args.size,
        brand=args.brand,
        drink=args.drink,
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
