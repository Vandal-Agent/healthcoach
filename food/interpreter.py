from __future__ import annotations

import json
import os
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
        "school snack",
        "lunch",
        "afternoon snack",
        "dinner",
        "dessert",
    ] | None = None

    drink: str | None = None

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
            return response.parsed

        return FoodInterpretation.model_validate(
            response.parsed
        )

    if not response.text:
        raise RuntimeError(
            "Gemini returned no interpretable response."
        )

    return FoodInterpretation.model_validate_json(
        response.text
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