from __future__ import annotations

import re
from typing import Any

import requests

from food.usda_provider import (
    barcode_match_key,
    lookup_usda_barcode_nutrition,
    normalize_barcode,
)


OPEN_FOOD_FACTS_URL = (
    "https://world.openfoodfacts.org/api/v3/product/{barcode}"
)

OPEN_FOOD_FACTS_FIELDS = ",".join([
    "code",
    "product_name",
    "brands",
    "serving_size",
    "nutrition_data_per",
    "nutriments",
])

OPEN_FOOD_FACTS_USER_AGENT = (
    "HealthCoach/1.0 "
    "(personal nutrition logging application)"
)


def unsupported_result(
    *,
    notes: list[str],
    missing_fields: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "found": False,
        "provider": "open_food_facts",
        "food": None,
        "nutrition": None,
        "verification": None,
        "missing_fields": list(missing_fields or []),
        "clarification_question": None,
        "notes": notes,
    }


def safe_float(value: Any) -> float | None:
    if value in ("", None):
        return None

    try:
        number = float(value)
    except (TypeError, ValueError):
        return None

    if number < 0:
        return None

    return number


def serving_gram_weight(
    serving_description: str,
) -> float | None:
    text = str(serving_description or "").lower()

    matches = re.findall(
        r"(?<!m)([0-9]+(?:\.[0-9]+)?)\s*g\b",
        text,
    )

    if not matches:
        return None

    return float(matches[-1])


def extract_open_food_facts_nutrition(
    product: dict[str, Any],
) -> dict[str, float | None]:
    nutriments = dict(product.get("nutriments") or {})
    serving_description = str(
        product.get("serving_size") or ""
    ).strip()

    field_names = {
        "calories": "energy-kcal",
        "protein_g": "proteins",
        "carbohydrates_g": "carbohydrates",
        "fat_g": "fat",
        "fiber_g": "fiber",
        "sugar_g": "sugars",
        "sodium_mg": "sodium",
    }

    serving_values: dict[str, float | None] = {}

    for output_field, source_field in field_names.items():
        value = safe_float(
            nutriments.get(f"{source_field}_serving")
        )

        if (
            value is not None
            and output_field == "sodium_mg"
        ):
            value = round(value * 1000.0, 3)

        serving_values[output_field] = value

    if serving_values["calories"] is not None:
        return serving_values

    gram_weight = serving_gram_weight(
        serving_description
    )

    if gram_weight is None:
        return serving_values

    factor = gram_weight / 100.0
    scaled_values: dict[str, float | None] = {}

    for output_field, source_field in field_names.items():
        value = safe_float(
            nutriments.get(f"{source_field}_100g")
        )

        if value is None:
            scaled_values[output_field] = None
            continue

        scaled = value * factor

        if output_field == "sodium_mg":
            scaled *= 1000.0

        scaled_values[output_field] = round(
            scaled,
            3,
        )

    return scaled_values


def lookup_open_food_facts_barcode_nutrition(
    barcode: str | int,
) -> dict[str, Any]:
    normalized = normalize_barcode(barcode)

    response = requests.get(
        OPEN_FOOD_FACTS_URL.format(
            barcode=normalized
        ),
        params={
            "fields": OPEN_FOOD_FACTS_FIELDS,
        },
        headers={
            "User-Agent": OPEN_FOOD_FACTS_USER_AGENT,
        },
        timeout=20,
    )
    response.raise_for_status()

    data = response.json()
    product = dict(data.get("product") or {})

    if not product:
        return unsupported_result(
            notes=[
                "Open Food Facts did not contain this barcode."
            ]
        )

    returned_code = str(
        data.get("code")
        or product.get("code")
        or ""
    ).strip()

    try:
        returned_normalized = normalize_barcode(
            returned_code
        )
    except ValueError:
        return unsupported_result(
            notes=[
                "Open Food Facts returned an invalid barcode."
            ]
        )

    if (
        barcode_match_key(returned_normalized)
        != barcode_match_key(normalized)
    ):
        return unsupported_result(
            notes=[
                "Open Food Facts returned a different barcode."
            ]
        )

    product_name = str(
        product.get("product_name") or ""
    ).strip()
    serving_description = str(
        product.get("serving_size") or ""
    ).strip()

    if not product_name:
        return unsupported_result(
            notes=[
                "Open Food Facts found the barcode, but no "
                "product name was available."
            ],
            missing_fields=["product_name"],
        )

    if not serving_description:
        return unsupported_result(
            notes=[
                "Open Food Facts identified the product, but no "
                "usable serving size was available. Enter the "
                "package label instead."
            ],
            missing_fields=["serving_size"],
        )

    nutrition = extract_open_food_facts_nutrition(
        product
    )

    if nutrition["calories"] is None:
        return unsupported_result(
            notes=[
                "Open Food Facts identified the product, but no "
                "usable serving-level calories were available. "
                "Enter the package label instead."
            ],
            missing_fields=["calories"],
        )

    return {
        "found": True,
        "provider": "open_food_facts",
        "food": {
            "canonical_name": product_name,
            "restaurant": None,
            "brand": str(
                product.get("brands") or ""
            ).strip() or None,
            "food_type": "food",
            "serving_description": serving_description,
            "serving_amount": 1.0,
            "serving_unit": "serving",
        },
        "nutrition": nutrition,
        "verification": {
            "status": "community_data",
            "source": (
                "Open Food Facts community database"
            ),
            "source_item_id": normalized,
            "source_url": (
                "https://world.openfoodfacts.org/"
                f"product/{normalized}"
            ),
        },
        "missing_fields": [],
        "clarification_question": None,
        "notes": [
            "This nutrition is community-contributed. Compare "
            "it with the package label before saving.",
            f"Exact barcode match: {normalized}.",
            f"Serving: {serving_description}.",
        ],
    }


def lookup_barcode_nutrition(
    barcode: str | int,
) -> dict[str, Any]:
    normalized = normalize_barcode(barcode)

    usda_result = lookup_usda_barcode_nutrition(
        normalized
    )

    if usda_result.get("found"):
        return usda_result

    try:
        fallback_result = (
            lookup_open_food_facts_barcode_nutrition(
                normalized
            )
        )
    except Exception:
        fallback_result = unsupported_result(
            notes=[
                "The Open Food Facts fallback could not be "
                "reached."
            ]
        )

    if fallback_result.get("found"):
        return fallback_result

    notes = (
        list(usda_result.get("notes") or [])
        + list(fallback_result.get("notes") or [])
    )

    return {
        **fallback_result,
        "notes": notes,
    }
