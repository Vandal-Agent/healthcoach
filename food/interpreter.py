from __future__ import annotations

import json
import os
import re
from typing import Literal

from google import genai
from google.genai import types
from pydantic import BaseModel, Field, field_validator


MODEL_NAME = "gemini-3.5-flash-lite"


class FoodInterpretation(BaseModel):
    """Structured interpretation of one food-logging message."""

    is_food_logging_request: bool

    restaurant: str | None = None
    brand: str | None = None
    food_name: str | None = None
    size: str | None = None

    quantity: float | None = None

    @field_validator("quantity")
    @classmethod
    def validate_quantity(
        cls,
        value: float | None,
    ) -> float | None:
        """Require a positive quantity when one is provided."""
        if value is not None and value <= 0:
            raise ValueError("quantity must be greater than zero.")

        return value

    quantity_description: str | None = None

    meal_category: Literal[
        "before breakfast",
        "breakfast",
        "morning snack",
        "school snack",
        "lunch",
        "afternoon snack",
        "dinner",
        "dessert",
    ] | None = None

    drink: str | None = None

    is_combo_meal: bool = False
    combo_entree: str | None = None
    combo_side: str | None = None
    combo_side_size: str | None = None
    combo_drink: str | None = None
    combo_drink_size: str | None = None

    missing_fields: list[str] = Field(
        default_factory=list
    )

    assumptions: list[str] = Field(
        default_factory=list
    )

    confidence: float = Field(
        ge=0,
        le=1,
    )

    clarification_question: str | None = None



GENERIC_RESTAURANT_FOODS = {
    "burger",
    "burgers",
    "burrito",
    "burritos",
    "fries",
    "french fries",
    "pizza",
    "sandwich",
    "sandwiches",
    "salad",
    "salads",
    "taco",
    "tacos",
}


SIGNATURE_FOOD_RESTAURANTS = {
    "big mac": "McDonald's",
    "quarter pounder": "McDonald's",
    "quarter pounder with cheese": "McDonald's",
    "mcchicken": "McDonald's",
    "mcnuggets": "McDonald's",
    "egg mcmuffin": "McDonald's",
    "crunchwrap supreme": "Taco Bell",
    "doritos locos tacos": "Taco Bell",
    "whopper": "Burger King",
    "double double": "In-N-Out",
    "animal style fries": "In-N-Out",
}


def normalize_signature_food(value: str | None) -> str:
    """Normalize a food name for signature-item matching."""
    if value is None:
        return ""

    cleaned = value.strip().lower()
    cleaned = cleaned.replace("’", "'")
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)

    return cleaned.strip()


def clean_interpretation_missing_fields(
    interpretation: FoodInterpretation,
) -> FoodInterpretation:
    """
    Remove missing fields that already have values and generate one
    focused clarification question.
    """
    if interpretation.is_combo_meal:
        if (
            interpretation.combo_entree
            and not interpretation.food_name
        ):
            interpretation.food_name = (
                f"{interpretation.combo_entree} meal"
            )

        if not interpretation.combo_side:
            interpretation.combo_side = "french fries"

        if not interpretation.combo_side_size:
            interpretation.combo_side_size = interpretation.size

        if not interpretation.combo_drink:
            interpretation.combo_drink = interpretation.drink

        if not interpretation.combo_drink_size:
            interpretation.combo_drink_size = interpretation.size

    is_standalone_drink = (
        interpretation.drink not in (None, "")
        and interpretation.food_name in (None, "")
    )

    if is_standalone_drink:
        interpretation.food_name = interpretation.drink

    normalized_drink = normalize_signature_food(
        interpretation.drink
    )

    is_generic_drink = normalized_drink in {
        "drink",
        "beverage",
    }

    field_values = {
        "restaurant": interpretation.restaurant,
        "brand": interpretation.brand,
        "food_name": interpretation.food_name,
        "size": interpretation.size,
        "quantity": interpretation.quantity,
        "quantity_description": interpretation.quantity_description,
        "meal_category": interpretation.meal_category,
        "drink": interpretation.drink,
    }

    cleaned_missing: list[str] = []

    for field in interpretation.missing_fields:
        if field_values.get(field) not in (None, ""):
            continue

        if field not in cleaned_missing:
            cleaned_missing.append(field)

    normalized_food = normalize_signature_food(
        interpretation.food_name
    )

    combo_suffixes = (
        " meal",
        " combo",
        " value meal",
    )

    matched_combo_suffix = next(
        (
            suffix
            for suffix in combo_suffixes
            if normalized_food.endswith(suffix)
        ),
        None,
    )

    if matched_combo_suffix:
        entree_name = normalized_food[
            : -len(matched_combo_suffix)
        ].strip()

        if entree_name:
            interpretation.is_combo_meal = True
            interpretation.combo_entree = entree_name
            interpretation.combo_side = "french fries"
            interpretation.combo_side_size = interpretation.size
            interpretation.combo_drink = interpretation.drink
            interpretation.combo_drink_size = interpretation.size

    is_generic_restaurant_food = (
        bool(interpretation.restaurant)
        and normalized_food in GENERIC_RESTAURANT_FOODS
    )

    if (
        is_generic_restaurant_food
        and "food_name_detail" not in cleaned_missing
    ):
        cleaned_missing.append("food_name_detail")

    is_french_fries = normalized_food in {
        "fries",
        "french fries",
        "fry",
    }

    if is_french_fries:
        required_fries_fields = (
            "restaurant",
            "size",
            "quantity",
        )

        for field in required_fries_fields:
            if (
                field_values.get(field) in (None, "")
                and field not in cleaned_missing
            ):
                cleaned_missing.append(field)

    if interpretation.is_combo_meal:
        if (
            interpretation.size in (None, "")
            and "size" not in cleaned_missing
        ):
            cleaned_missing.append("size")

        if (
            interpretation.combo_drink in (None, "")
            and interpretation.drink in (None, "")
            and "drink" not in cleaned_missing
        ):
            cleaned_missing.append("drink")

    if is_standalone_drink:
        required_drink_fields = []

        if is_generic_drink:
            required_drink_fields.append("drink_detail")

        required_drink_fields.extend(
            [
                "size",
                "quantity",
            ]
        )

        for field in required_drink_fields:
            if (
                field_values.get(field) in (None, "")
                and field not in cleaned_missing
            ):
                cleaned_missing.append(field)

    is_branded_packaged_food = (
        bool(interpretation.brand)
        and not interpretation.restaurant
        and not interpretation.is_combo_meal
    )

    if is_branded_packaged_food:
        # A bare count such as "1" is ambiguous for packaged food.
        # Require an amount with meaning: 1 serving, 28 g, 1 oz,
        # a handful, etc.
        if interpretation.quantity_description in (None, ""):
            cleaned_missing = [
                field
                for field in cleaned_missing
                if field != "quantity"
            ]

            if "quantity_description" not in cleaned_missing:
                cleaned_missing.append("quantity_description")

    if (
        interpretation.is_food_logging_request
        and interpretation.food_name
        and not interpretation.meal_category
        and "meal_category" not in cleaned_missing
    ):
        cleaned_missing.append("meal_category")

    clarification_priority = (
        "restaurant",
        "brand",
        "size",
        "quantity",
        "quantity_description",
        "drink_detail",
        "drink",
        "meal_category",
        "food_name_detail",
        "food_name",
    )

    cleaned_missing = [
        field
        for field in clarification_priority
        if field in cleaned_missing
    ]

    interpretation.missing_fields = cleaned_missing

    if cleaned_missing == ["meal_category"]:
        interpretation.clarification_question = (
            "Which meal was this for?"
        )
    elif not cleaned_missing:
        interpretation.clarification_question = None
    elif cleaned_missing:
        first_field = cleaned_missing[0]

        questions = {
            "restaurant": "What restaurant was this from?",
            "brand": "What brand was this?",
            "food_name": "What food did you have?",
            "size": "What size was it?",
            "quantity": "How many did you have?",
            "quantity_description": (
                "How much did you have? For example: "
                "1 serving, 28 g, 1 oz, or a handful."
            ),
            "drink": "What drink did you have?",
            "drink_detail": "What drink did you have?",
            "food_name_detail": (
                "Which exact menu item did you have?"
            ),
        }

        interpretation.clarification_question = questions.get(
            first_field,
            "What detail should I add?",
        )

    return interpretation


def apply_signature_restaurant_inference(
    interpretation: FoodInterpretation,
) -> FoodInterpretation:
    """
    Add a restaurant only when a recognized signature item is present.

    This deterministic catalog does not guess from generic foods.
    """
    if interpretation.restaurant:
        return interpretation

    normalized_food = normalize_signature_food(
        interpretation.food_name
    )

    if not normalized_food:
        return interpretation

    restaurant = None

    for signature, signature_restaurant in (
        SIGNATURE_FOOD_RESTAURANTS.items()
    ):
        if (
            normalized_food == signature
            or normalized_food.startswith(f"{signature} ")
            or normalized_food.endswith(f" {signature}")
        ):
            restaurant = signature_restaurant
            break

    if restaurant is None:
        return interpretation

    interpretation.restaurant = restaurant

    interpretation.missing_fields = [
        field
        for field in interpretation.missing_fields
        if field != "restaurant"
    ]

    if interpretation.missing_fields == ["meal_category"]:
        interpretation.clarification_question = (
            "Which meal was this for?"
        )
    elif not interpretation.missing_fields:
        interpretation.clarification_question = None

    assumption = (
        f"Inferred restaurant as {restaurant} from the "
        f"signature menu item {interpretation.food_name}."
    )

    if assumption not in interpretation.assumptions:
        interpretation.assumptions.append(assumption)

    return interpretation


SYSTEM_INSTRUCTION = """
You interpret food-logging messages for HealthCoach.

Your job is only to identify what the user said.

You may extract:
- restaurant
- brand
- food name
- size
- quantity
- quantity description
- meal category
- drink
- missing details
- assumptions
- confidence
- one clarification question

Strict rules:

1. Never provide calories, protein, carbohydrates, fat, fiber,
   sugar, sodium, or any other nutrition values.

2. Never invent a restaurant, brand, menu item, serving size,
   quantity, or meal category.

3. If information is missing or unclear, list it in missing_fields
   and write one concise clarification_question.

4. The user's stable preference is:
   when the user says soda without specifying regular or diet,
   interpret it as diet soda and record this in assumptions.

5. Approximate descriptions such as "a handful" are allowed.
   Preserve the phrase in quantity_description.
   Do not convert it to grams, ounces, or another measurement.

6. Use only information contained in the user's message or the
   stated diet-soda preference.

7. If the message is not a food-logging request, set
   is_food_logging_request to false.

8. Confidence means confidence in the interpretation, not confidence
   in nutrition information.
"""


def get_client() -> genai.Client:
    """Create a Gemini client using the HealthCoach API key."""
    api_key = os.getenv("HEALTH_GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "HEALTH_GEMINI_API_KEY is not configured."
        )

    return genai.Client(api_key=api_key)


def interpret_food_message(
    text: str,
) -> FoodInterpretation:
    """Interpret one natural-language food message."""
    cleaned = text.strip()

    if not cleaned:
        raise ValueError("Food message cannot be blank.")

    client = get_client()

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=cleaned,
        config=types.GenerateContentConfig(
            system_instruction=SYSTEM_INSTRUCTION,
            temperature=0,
            response_mime_type="application/json",
            response_schema=FoodInterpretation,
        ),
    )

    if response.parsed is not None:
        if isinstance(response.parsed, FoodInterpretation):
            interpretation = response.parsed
        else:
            interpretation = FoodInterpretation.model_validate(
                response.parsed
            )

        interpretation = apply_signature_restaurant_inference(
            interpretation
        )

        return clean_interpretation_missing_fields(
            interpretation
        )

    if not response.text:
        raise RuntimeError(
            "Gemini returned no interpretable response."
        )

    interpretation = FoodInterpretation.model_validate_json(
        response.text
    )

    interpretation = apply_signature_restaurant_inference(
        interpretation
    )

    return clean_interpretation_missing_fields(
        interpretation
    )


def main() -> None:
    """Run a command-line interpretation test."""
    import sys

    if len(sys.argv) < 2:
        raise SystemExit(
            'Usage: python3 -m food.interpreter '
            '"I had a medium Big Mac meal for lunch"'
        )

    interpretation = interpret_food_message(
        " ".join(sys.argv[1:])
    )

    print(
        json.dumps(
            interpretation.model_dump(),
            indent=2,
        )
    )


if __name__ == "__main__":
    main()