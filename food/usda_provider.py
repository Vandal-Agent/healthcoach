from __future__ import annotations

import argparse
import json
import os
import re
from typing import Any

import requests


SEARCH_URL = (
    "https://api.nal.usda.gov/fdc/v1/foods/search"
)
FOOD_URL = (
    "https://api.nal.usda.gov/fdc/v1/food/{fdc_id}"
)
REQUEST_TIMEOUT = 30


NUTRIENT_NAMES = {
    "calories": {
        "Energy",
    },
    "protein_g": {
        "Protein",
    },
    "carbohydrates_g": {
        "Carbohydrate, by difference",
    },
    "fat_g": {
        "Total lipid (fat)",
    },
    "fiber_g": {
        "Fiber, total dietary",
    },
    "sugar_g": {
        "Sugars, total including NLEA",
        "Sugars, total",
    },
    "sodium_mg": {
        "Sodium, Na",
    },
}


def normalize_text(value: str | None) -> str:
    """Normalize text for strict comparisons."""
    if not value:
        return ""

    cleaned = value.lower().strip()
    cleaned = cleaned.replace("’", "'")
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)

    return cleaned.strip()


def normalized_tokens(value: str | None) -> set[str]:
    """Return meaningful normalized tokens."""
    replacements = {
        "fry": "fries",
        "fries": "fries",
        "burgers": "burger",
        "onions": "onion",
        "tacos": "taco",
        "sandwiches": "sandwich",
    }

    ignored = {
        "a",
        "an",
        "the",
        "serving",
        "standard",
    }

    tokens = set()

    for token in normalize_text(value).split():
        normalized = replacements.get(token, token)

        if normalized not in ignored:
            tokens.add(normalized)

    return tokens


def get_api_key() -> str:
    """Return the configured FoodData Central API key."""
    api_key = os.getenv("USDA_FDC_API_KEY")

    if not api_key:
        raise RuntimeError(
            "USDA_FDC_API_KEY is not configured."
        )

    return api_key


def unsupported_result(
    *,
    notes: list[str],
    missing_fields: list[str] | None = None,
) -> dict[str, Any]:
    """Return the standard unsupported response."""
    return {
        "found": False,
        "provider": "usda_fooddata_central",
        "food": None,
        "nutrition": None,
        "verification": None,
        "missing_fields": missing_fields or [],
        "clarification_question": None,
        "notes": notes,
    }


def search_foods(
    *,
    query: str,
    page_size: int = 20,
    data_types: list[str] | None = None,
) -> list[dict[str, Any]]:
    """Search FoodData Central."""
    response = requests.post(
        SEARCH_URL,
        params={"api_key": get_api_key()},
        json={
            "query": query,
            "pageSize": page_size,
            **(
                {"dataType": data_types}
                if data_types
                else {}
            ),
        },
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    return response.json().get("foods") or []


def get_food_record(
    fdc_id: int,
) -> dict[str, Any]:
    """Retrieve one complete FoodData Central record."""
    response = requests.get(
        FOOD_URL.format(fdc_id=fdc_id),
        params={"api_key": get_api_key()},
        timeout=REQUEST_TIMEOUT,
    )

    response.raise_for_status()

    return response.json()


def select_exact_branded_food(
    *,
    foods: list[dict[str, Any]],
    brand: str,
    food_name: str,
) -> dict[str, Any] | None:
    """
    Select one unambiguous branded packaged-food result.

    The requested brand must match the USDA brand name or brand owner,
    and every requested food token must appear in the product
    description. Ambiguous matches are rejected.
    """
    requested_brand_tokens = normalized_tokens(brand)
    requested_food_tokens = normalized_tokens(food_name)

    candidates = []

    for food in foods:
        if str(food.get("dataType") or "") != "Branded":
            continue

        description = str(
            food.get("description") or ""
        )

        brand_name = str(
            food.get("brandName") or ""
        )

        brand_owner = str(
            food.get("brandOwner") or ""
        )

        description_tokens = normalized_tokens(
            description
        )

        brand_tokens = (
            normalized_tokens(brand_name)
            | normalized_tokens(brand_owner)
        )

        if not requested_brand_tokens.issubset(
            brand_tokens
        ):
            continue

        if not requested_food_tokens.issubset(
            description_tokens
        ):
            continue

        extra_tokens = (
            description_tokens
            - requested_food_tokens
            - requested_brand_tokens
        )

        ignored_variant_tokens = {
            "of",
            "hanover",
            "inc",
            "oz",
            "ounce",
            "ounces",
            "g",
            "gram",
            "grams",
            "grm",
            "count",
            "pack",
        }

        meaningful_extra_tokens = {
            token
            for token in extra_tokens
            if (
                token not in ignored_variant_tokens
                and not token.isdigit()
            )
        }

        candidates.append(
            {
                "food": food,
                "extra_count": len(extra_tokens),
                "variant_tokens": meaningful_extra_tokens,
            }
        )

    if not candidates:
        return None

    variant_signatures = {
        frozenset(candidate["variant_tokens"])
        for candidate in candidates
        if candidate["variant_tokens"]
    }

    if len(variant_signatures) > 1:
        return None

    smallest_extra_count = min(
        candidate["extra_count"]
        for candidate in candidates
    )

    best = [
        candidate["food"]
        for candidate in candidates
        if candidate["extra_count"] == smallest_extra_count
    ]

    if len(best) != 1:
        return None

    return best[0]


def extract_label_nutrients(
    record: dict[str, Any],
) -> dict[str, float | None]:
    """Extract nutrition values from a USDA Branded package label."""
    label = record.get("labelNutrients") or {}

    mapping = {
        "calories": "calories",
        "protein_g": "protein",
        "carbohydrates_g": "carbohydrates",
        "fat_g": "fat",
        "fiber_g": "fiber",
        "sugar_g": "sugars",
        "sodium_mg": "sodium",
    }

    results: dict[str, float | None] = {}

    for output_field, label_field in mapping.items():
        item = label.get(label_field) or {}
        value = item.get("value")

        results[output_field] = (
            float(value)
            if value is not None
            else None
        )

    return results


def normalize_barcode(value: str | int) -> str:
    """
    Normalize and validate a GTIN-8, UPC-A, EAN-13, or GTIN-14.
    """
    cleaned = re.sub(
        r"[\s-]+",
        "",
        str(value or "").strip(),
    )

    if not cleaned or not cleaned.isdigit():
        raise ValueError(
            "Barcode must contain only digits, spaces, or hyphens."
        )

    if len(cleaned) not in {8, 12, 13, 14}:
        raise ValueError(
            "Barcode must contain 8, 12, 13, or 14 digits."
        )

    digits = [int(character) for character in cleaned]
    supplied_check_digit = digits[-1]
    body = list(reversed(digits[:-1]))

    weighted_total = sum(
        digit * (3 if index % 2 == 0 else 1)
        for index, digit in enumerate(body)
    )

    expected_check_digit = (
        10 - (weighted_total % 10)
    ) % 10

    if supplied_check_digit != expected_check_digit:
        raise ValueError(
            "Barcode check digit is invalid."
        )

    return cleaned


def barcode_match_key(value: str | int) -> str | None:
    """
    Return a 14-digit comparison key without validating checksum.

    USDA may represent the same product as UPC-A, EAN-13, or GTIN-14
    with additional leading zeroes.
    """
    cleaned = re.sub(
        r"[\s-]+",
        "",
        str(value or "").strip(),
    )

    if (
        not cleaned
        or not cleaned.isdigit()
        or len(cleaned) > 14
    ):
        return None

    return cleaned.zfill(14)


def select_exact_barcode_food(
    *,
    foods: list[dict[str, Any]],
    barcode: str,
) -> dict[str, Any] | None:
    """Select the newest exact USDA Branded GTIN/UPC match."""
    requested_key = barcode_match_key(barcode)

    if requested_key is None:
        return None

    matches = []

    for food in foods:
        if str(food.get("dataType") or "") != "Branded":
            continue

        candidate_key = barcode_match_key(
            food.get("gtinUpc") or ""
        )

        if candidate_key == requested_key:
            matches.append(food)

    if not matches:
        return None

    return max(
        matches,
        key=lambda food: int(food.get("fdcId") or 0),
    )


def lookup_usda_barcode_nutrition(
    barcode: str | int,
) -> dict[str, Any]:
    """
    Retrieve one exact packaged product using its GTIN/UPC.

    The barcode must pass its standard check-digit validation.
    Nutrition comes directly from the USDA Branded package label.
    """
    try:
        normalized = normalize_barcode(barcode)
    except ValueError as error:
        return unsupported_result(
            notes=[str(error)],
            missing_fields=["valid_barcode"],
        )

    foods = search_foods(
        query=normalized,
        page_size=50,
        data_types=["Branded"],
    )

    selected = select_exact_barcode_food(
        foods=foods,
        barcode=normalized,
    )

    if selected is None:
        return unsupported_result(
            notes=[
                "USDA did not return an exact Branded GTIN/UPC "
                "match for this barcode."
            ]
        )

    fdc_id = int(selected["fdcId"])
    record = get_food_record(fdc_id)

    record_key = barcode_match_key(
        record.get("gtinUpc") or ""
    )
    requested_key = barcode_match_key(normalized)

    if record_key != requested_key:
        return unsupported_result(
            notes=[
                "The USDA detail record did not retain the "
                "requested GTIN/UPC."
            ]
        )

    serving_size = (
        record.get("servingSize")
        if record.get("servingSize") is not None
        else selected.get("servingSize")
    )
    serving_unit = str(
        record.get("servingSizeUnit")
        or selected.get("servingSizeUnit")
        or ""
    ).strip()
    household_serving = str(
        record.get("householdServingFullText")
        or selected.get("householdServingFullText")
        or ""
    ).strip()

    if serving_size is None or not serving_unit:
        return unsupported_result(
            notes=[
                "USDA found the barcode, but no verified package "
                "serving size was available."
            ],
            missing_fields=["serving_size"],
        )

    nutrition = extract_label_nutrients(record)
    used_per_100g_fallback = False

    if nutrition["calories"] is None:
        normalized_unit = serving_unit.strip().lower()

        if normalized_unit not in {
            "g",
            "gram",
            "grams",
            "grm",
        }:
            return unsupported_result(
                notes=[
                    "USDA found the barcode, but complete label "
                    "nutrition and a gram serving were unavailable."
                ]
            )

        per_100g = extract_nutrients_per_100g(record)

        if per_100g["calories"] is None:
            return unsupported_result(
                notes=[
                    "USDA found the barcode, but the record did "
                    "not include usable calorie data."
                ]
            )

        nutrition = scale_nutrients(
            per_100g=per_100g,
            gram_weight=float(serving_size),
        )
        used_per_100g_fallback = True

    brand_name = str(
        record.get("brandName")
        or record.get("brandOwner")
        or ""
    ).strip() or None

    canonical_name = str(
        record.get("description")
        or "Packaged food"
    ).strip()

    serving_amount = float(serving_size)
    normalized_serving_unit = (
        "g"
        if serving_unit.strip().lower()
        in {"g", "gram", "grams", "grm"}
        else serving_unit
    )

    serving_description = (
        f"{household_serving} ({serving_amount:g} "
        f"{normalized_serving_unit})"
        if household_serving
        else f"{serving_amount:g} {normalized_serving_unit}"
    )

    return {
        "found": True,
        "provider": "usda_fooddata_central",
        "food": {
            "canonical_name": canonical_name,
            "restaurant": None,
            "brand": brand_name,
            "food_type": "food",
            "serving_description": serving_description,
            "serving_amount": serving_amount,
            "serving_unit": normalized_serving_unit,
        },
        "nutrition": nutrition,
        "verification": {
            "status": "verified",
            "source": "fdc.nal.usda.gov",
            "source_item_id": f"fdc-{fdc_id}",
            "source_url": (
                "https://fdc.nal.usda.gov/fdc-app.html"
                f"#/food-details/{fdc_id}/nutrients"
            ),
        },
        "missing_fields": [],
        "clarification_question": None,
        "notes": [
            (
                "Nutrition was scaled from USDA verified per-100 g "
                "values using the exact barcode record's package "
                "serving weight."
                if used_per_100g_fallback
                else "Nutrition came directly from the USDA "
                "Branded package label."
            ),
            f"Exact barcode match: {normalized}.",
            f"Verified serving: {serving_description}.",
        ],
    }


def lookup_usda_branded_nutrition(
    *,
    brand: str,
    food_name: str,
) -> dict[str, Any]:
    """
    Retrieve an exact branded packaged-food record from USDA.

    Nutrition comes directly from USDA labelNutrients and the
    package serving size. No scaling or estimation is performed.
    """
    query = " ".join(
        part
        for part in (
            brand,
            food_name,
        )
        if part
    )

    foods = search_foods(
        query=query,
        page_size=25,
        data_types=["Branded"],
    )

    selected = select_exact_branded_food(
        foods=foods,
        brand=brand,
        food_name=food_name,
    )

    if selected is None:
        return unsupported_result(
            notes=[
                "USDA did not return one unambiguous branded "
                "food match."
            ]
        )

    fdc_id = int(selected["fdcId"])
    record = get_food_record(fdc_id)

    serving_size = record.get("servingSize")
    serving_unit = str(
        record.get("servingSizeUnit") or ""
    ).strip()

    if serving_size is None or not serving_unit:
        return unsupported_result(
            notes=[
                "USDA found the branded product, but no verified "
                "package serving size was available."
            ],
            missing_fields=["serving_size"],
        )

    nutrition = extract_label_nutrients(
        record
    )

    if nutrition["calories"] is None:
        return unsupported_result(
            notes=[
                "USDA found the branded product, but the package "
                "label did not include calories."
            ]
        )

    brand_name = str(
        record.get("brandName")
        or record.get("brandOwner")
        or brand
    ).strip()

    canonical_name = str(
        record.get("description") or food_name
    ).strip()

    serving_amount = float(serving_size)

    serving_description = (
        f"{serving_amount:g} {serving_unit}"
    )

    return {
        "found": True,
        "provider": "usda_fooddata_central",
        "food": {
            "canonical_name": canonical_name,
            "restaurant": None,
            "brand": brand_name,
            "food_type": "food",
            "serving_description": serving_description,
            "serving_amount": serving_amount,
            "serving_unit": serving_unit,
        },
        "nutrition": nutrition,
        "verification": {
            "status": "verified",
            "source": "fdc.nal.usda.gov",
            "source_item_id": f"fdc-{fdc_id}",
            "source_url": (
                f"https://fdc.nal.usda.gov/fdc-app.html"
                f"#/food-details/{fdc_id}/nutrients"
            ),
        },
        "missing_fields": [],
        "clarification_question": None,
        "notes": [
            "Nutrition came directly from the USDA Branded "
            f"package label for a {serving_description} serving."
        ],
    }


def select_exact_food(
    *,
    foods: list[dict[str, Any]],
    restaurant: str | None,
    food_name: str,
) -> dict[str, Any] | None:
    """
    Select one unambiguous restaurant-food result.

    Restaurant and food-name tokens must all appear in the USDA
    description. Tied results are rejected.
    """
    requested_food_tokens = normalized_tokens(food_name)
    restaurant_tokens = normalized_tokens(restaurant)

    candidates = []

    for food in foods:
        description = str(
            food.get("description") or ""
        )
        description_tokens = normalized_tokens(description)

        if not requested_food_tokens.issubset(
            description_tokens
        ):
            continue

        if (
            restaurant_tokens
            and not restaurant_tokens.issubset(
                description_tokens
            )
        ):
            continue

        normalized_description = normalize_text(
            description
        )
        normalized_request = normalize_text(
            food_name
        )

        variant_phrases = (
            ("with cheese", "cheese"),
            ("double", "double"),
            ("jr", "jr"),
            ("junior", "junior"),
        )

        has_unrequested_variant = any(
            phrase in normalized_description
            and request_term not in normalized_request
            for phrase, request_term in variant_phrases
        )

        if has_unrequested_variant:
            continue

        data_type = str(food.get("dataType") or "")

        score = 0

        if data_type == "SR Legacy":
            score += 3
        elif data_type == "Foundation":
            score += 2
        elif data_type == "Survey (FNDDS)":
            score += 1

        extra_tokens = (
            description_tokens
            - requested_food_tokens
            - restaurant_tokens
        )

        # USDA uses "NFS" to mean "not further specified."
        # Treat that marker as neutral for generic-food matching.
        if not restaurant_tokens:
            extra_tokens -= {"nfs", "raw"}

        if (
            "no cheese" in normalized_description
            and "cheese" not in normalized_request
        ):
            extra_tokens -= {"no", "cheese"}

        score -= len(extra_tokens) * 10

        candidates.append(
            {
                "food": food,
                "score": score,
            }
        )

    if not candidates:
        return None

    best_score = max(
        candidate["score"]
        for candidate in candidates
    )

    best = [
        candidate["food"]
        for candidate in candidates
        if candidate["score"] == best_score
    ]

    if len(best) != 1:
        return None

    return best[0]


def find_portion(
    *,
    record: dict[str, Any],
    size: str | None,
) -> dict[str, Any] | None:
    """Find one exact USDA portion for the requested size."""
    portions = [
        portion
        for portion in (record.get("foodPortions") or [])
        if portion.get("gramWeight") is not None
    ]

    normalized_size = normalize_text(size)

    if not normalized_size:
        if len(portions) == 1:
            return portions[0]

        ounce_matches = []

        for portion in portions:
            description = normalize_text(
                portion.get("portionDescription")
            )

            if description == "1 oz":
                ounce_matches.append(portion)

        if len(ounce_matches) == 1:
            return ounce_matches[0]

        return None

    candidates = []

    for portion in portions:
        portion_amount = portion.get("amount")
        amount_text = (
            f"{float(portion_amount):g}"
            if portion_amount is not None
            else ""
        )
        modifier = normalize_text(
            portion.get("modifier")
        )
        description = normalize_text(
            portion.get("portionDescription")
        )

        searchable = re.sub(
            r"\([^)]*\)",
            " ",
            f"{amount_text} {modifier} {description}",
        )
        searchable = normalize_text(searchable)

        requested_tokens = set(normalized_size.split())
        searchable_tokens = set(searchable.split())
        if not requested_tokens.issubset(searchable_tokens):
            continue

        candidates.append({
            "portion": portion,
            "extra_token_count": len(searchable_tokens - requested_tokens),
        })

    if not candidates:
        return None

    best_extra_count = min(
        candidate["extra_token_count"]
        for candidate in candidates
    )
    best = [
        candidate["portion"]
        for candidate in candidates
        if candidate["extra_token_count"] == best_extra_count
    ]
    if len(best) != 1:
        return None
    return best[0]


def extract_nutrients_per_100g(
    record: dict[str, Any],
) -> dict[str, float | None]:
    """Extract supported nutrient values from a USDA record."""
    results: dict[str, float | None] = {
        key: None
        for key in NUTRIENT_NAMES
    }

    for item in record.get("foodNutrients") or []:
        nutrient = item.get("nutrient") or {}
        name = nutrient.get("name")
        unit = str(
            nutrient.get("unitName") or ""
        ).lower()
        amount = item.get("amount")

        if amount is None:
            continue

        for output_field, accepted_names in (
            NUTRIENT_NAMES.items()
        ):
            if name not in accepted_names:
                continue

            if output_field == "calories" and unit != "kcal":
                continue

            results[output_field] = float(amount)
            break

    return results


def scale_nutrients(
    *,
    per_100g: dict[str, float | None],
    gram_weight: float,
) -> dict[str, float | None]:
    """Scale per-100 g nutrients to a verified portion weight."""
    factor = float(gram_weight) / 100.0

    return {
        key: (
            round(float(value) * factor, 3)
            if value is not None
            else None
        )
        for key, value in per_100g.items()
    }


def lookup_usda_nutrition(
    *,
    restaurant: str | None,
    food_name: str,
    size: str | None,
    brand: str | None = None,
) -> dict[str, Any]:
    """
    Retrieve exact nutrition from USDA FoodData Central.

    Restaurant foods require:
    - an unambiguous matching USDA record
    - an exact USDA portion matching the requested size
    - calories in the USDA nutrient data

    No portion weights or nutrition values are estimated.
    """
    del brand

    if not food_name.strip():
        raise ValueError("food_name is required.")

    query = " ".join(
        part
        for part in (
            restaurant,
            food_name,
            size,
        )
        if part
    )

    foods = search_foods(query=query)

    selected = select_exact_food(
        foods=foods,
        restaurant=restaurant,
        food_name=food_name,
    )

    if selected is None:
        return unsupported_result(
            notes=[
                "USDA did not return one unambiguous matching "
                "food record."
            ]
        )

    fdc_id = int(selected["fdcId"])
    record = get_food_record(fdc_id)

    portion = find_portion(
        record=record,
        size=size,
    )

    if portion is None:
        return unsupported_result(
            notes=[
                "USDA verified the food, but no exact portion "
                f"was available for size: {size or 'not specified'}."
            ],
            missing_fields=["size"],
        )

    gram_weight = float(portion["gramWeight"])

    per_100g = extract_nutrients_per_100g(
        record
    )

    if per_100g["calories"] is None:
        return unsupported_result(
            notes=[
                "USDA did not provide calories for the selected "
                "food record."
            ]
        )

    nutrition = scale_nutrients(
        per_100g=per_100g,
        gram_weight=gram_weight,
    )

    canonical_name = str(
        record.get("description") or food_name
    ).strip()

    size_label = normalize_text(size).title()

    if size_label:
        serving_label = f"{size_label} serving"
    else:
        portion_description = str(
            portion.get("portionDescription") or ""
        ).strip()

        if portion_description:
            serving_label = portion_description
        else:
            modifier = normalize_text(
                portion.get("modifier")
            )

            serving_label = (
                modifier.title()
                if modifier
                else "Item"
            )

    portion_match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s+(.+)",
        serving_label,
    )

    if portion_match:
        serving_amount = float(portion_match.group(1))
        serving_unit = portion_match.group(2).strip()
        serving_description = (
            f"{serving_label} ({gram_weight:g} g)"
        )
    else:
        serving_amount = 1.0
        serving_unit = serving_label
        serving_description = (
            f"1 {serving_label} ({gram_weight:g} g)"
        )

    return {
        "found": True,
        "provider": "usda_fooddata_central",
        "food": {
            "canonical_name": canonical_name,
            "restaurant": restaurant,
            "brand": None,
            "food_type": "food",
            "serving_description": serving_description,
            "serving_amount": serving_amount,
            "serving_unit": serving_unit,
        },
        "nutrition": nutrition,
        "verification": {
            "status": "verified",
            "source": "fdc.nal.usda.gov",
            "source_item_id": f"fdc-{fdc_id}",
            "source_url": (
                f"https://fdc.nal.usda.gov/fdc-app.html"
                f"#/food-details/{fdc_id}/nutrients"
            ),
        },
        "missing_fields": [],
        "clarification_question": None,
        "notes": [
            "Nutrition was scaled from USDA values per 100 g "
            f"using USDA's verified {gram_weight:g} g "
            f"{serving_label.lower()}."
        ],
    }


def main() -> None:
    """Run a USDA provider test."""
    parser = argparse.ArgumentParser(
        description="Test USDA FoodData Central nutrition lookup."
    )

    parser.add_argument("food_name")
    parser.add_argument("--restaurant")
    parser.add_argument("--brand")
    parser.add_argument("--size")

    args = parser.parse_args()

    result = lookup_usda_nutrition(
        restaurant=args.restaurant,
        food_name=args.food_name,
        size=args.size,
        brand=args.brand,
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
