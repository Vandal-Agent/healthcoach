from __future__ import annotations

import json
import re
from typing import Any, Literal

from google.genai import types
from pydantic import BaseModel, Field

from food.nutrition_lookup import MODEL_NAME, get_client


MEAL_CALORIE_LIMITS = {
    "lunch": 500.0,
    "dinner": 600.0,
}
MAX_ADDITIONAL_INGREDIENTS = 2


class PantryMealIngredient(BaseModel):
    name: str
    amount: str
    source: Literal["pantry", "additional"]


class PantryMealIdea(BaseModel):
    name: str
    summary: str
    ingredients: list[PantryMealIngredient] = Field(
        min_length=1,
        max_length=14,
    )
    preparation_steps: list[str] = Field(
        min_length=1,
        max_length=8,
    )
    calories: float = Field(ge=0)
    protein_g: float = Field(ge=0)
    carbohydrates_g: float = Field(ge=0)
    fat_g: float = Field(ge=0)
    fiber_g: float = Field(ge=0)
    sugar_g: float = Field(ge=0)
    sodium_mg: float = Field(ge=0)
    daily_fit: str
    estimate_notes: str


class PantryMealIdeaSet(BaseModel):
    ideas: list[PantryMealIdea] = Field(
        min_length=3,
        max_length=3,
    )
    heart_healthy_pick: int = Field(ge=1, le=3)
    heart_healthy_reason: str = Field(
        min_length=1,
        max_length=400,
    )


def normalize_pantry_name(value: str | None) -> str:
    return re.sub(
        r"[^a-z0-9]+",
        " ",
        str(value or "").lower(),
    ).strip()


def pantry_item_prompt_data(
    pantry_items: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    results: list[dict[str, Any]] = []

    for item in pantry_items:
        name = str(
            item.get("display_name")
            or item.get("canonical_name")
            or ""
        ).strip()
        if not name:
            continue

        nutrition = {
            field: item.get(field)
            for field in (
                "calories",
                "protein_g",
                "carbohydrates_g",
                "fat_g",
                "fiber_g",
                "sugar_g",
                "sodium_mg",
            )
            if item.get(field) is not None
        }

        results.append(
            {
                "name": name,
                "brand": item.get("brand"),
                "serving_description": item.get(
                    "serving_description"
                ),
                "known_nutrition_per_serving": nutrition or None,
            }
        )

    return results


def validate_pantry_meal_ideas(
    ideas: list[dict[str, Any]],
    *,
    pantry_items: list[dict[str, Any]],
    meal_type: str,
) -> list[dict[str, Any]]:
    normalized_meal = str(meal_type or "").strip().lower()
    if normalized_meal not in MEAL_CALORIE_LIMITS:
        raise ValueError("meal_type must be lunch or dinner.")

    if len(ideas) != 3:
        raise ValueError("Exactly three Pantry meal ideas are required.")

    pantry_names = {
        normalize_pantry_name(
            item.get("display_name")
            or item.get("canonical_name")
        )
        for item in pantry_items
    }
    pantry_names.discard("")

    calorie_limit = MEAL_CALORIE_LIMITS[normalized_meal]
    idea_names: set[str] = set()
    heart_healthy_picks = [
        idea
        for idea in ideas
        if bool(idea.get("heart_healthy_pick"))
    ]

    if len(heart_healthy_picks) != 1:
        raise ValueError(
            "Exactly one Pantry idea must be the Heart-Healthy Pick."
        )
    if not str(
        heart_healthy_picks[0].get("heart_healthy_reason") or ""
    ).strip():
        raise ValueError(
            "The Heart-Healthy Pick must explain why it was selected."
        )

    for idea in ideas:
        idea_name = normalize_pantry_name(idea.get("name"))
        if not idea_name or idea_name in idea_names:
            raise ValueError("Pantry meal idea names must be distinct.")
        idea_names.add(idea_name)

        calories = float(idea.get("calories") or 0)
        if calories <= 0 or calories > calorie_limit:
            raise ValueError(
                f"A {normalized_meal} idea exceeded its calorie limit."
            )

        ingredients = list(idea.get("ingredients") or [])
        used_pantry = [
            ingredient
            for ingredient in ingredients
            if ingredient.get("source") == "pantry"
        ]
        additional = {
            normalize_pantry_name(ingredient.get("name"))
            for ingredient in ingredients
            if ingredient.get("source") == "additional"
        }
        additional.discard("")

        if not used_pantry:
            raise ValueError(
                "Every meal idea must use at least one Pantry item."
            )
        if len(additional) > MAX_ADDITIONAL_INGREDIENTS:
            raise ValueError(
                "A meal idea required more than two additional ingredients."
            )

        for ingredient in used_pantry:
            ingredient_name = normalize_pantry_name(
                ingredient.get("name")
            )
            if ingredient_name not in pantry_names:
                raise ValueError(
                    "A meal idea claimed an unavailable Pantry item."
                )

    return ideas


def generate_pantry_meal_ideas(
    *,
    pantry_items: list[dict[str, Any]],
    meal_type: str,
    daily_totals: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    normalized_meal = str(meal_type or "").strip().lower()
    if normalized_meal not in MEAL_CALORIE_LIMITS:
        raise ValueError("meal_type must be lunch or dinner.")

    pantry_data = pantry_item_prompt_data(pantry_items)
    if not pantry_data:
        raise ValueError("At least one Pantry item is required.")

    calorie_limit = MEAL_CALORIE_LIMITS[normalized_meal]
    totals = dict(daily_totals or {})

    prompt = f"""
Create exactly three healthy {normalized_meal} ideas for HealthCoach.

Available Pantry items:
{json.dumps(pantry_data, indent=2, sort_keys=True)}

Nutrition already logged today:
{json.dumps(totals, indent=2, sort_keys=True)}

Rules:
1. Each idea must be one realistic serving at or below
   {calorie_limit:g} calories.
2. Each idea must use at least one available Pantry item.
3. For Pantry ingredients, copy the Pantry item name exactly and set
   source to "pantry".
4. An idea may require no more than two unique ingredients that are not
   in the Pantry; label each one source "additional".
5. Salt, pepper, cooking spray, and water may be assumed and do not count
   as additional ingredients. Any other oil, sauce, or seasoning must be
   listed.
6. Pantry is presence-only. Do not claim a known on-hand quantity.
7. Give a practical amount for every ingredient and clear preparation
   steps.
8. Estimate calories, protein, carbohydrates, fat, fiber, sugar, and
   sodium for the complete serving. Use known saved-product nutrition
   when supplied; otherwise use reasonable standard-food estimates.
9. Use today's logged nutrition to explain why each idea fits. Favor
   protein and fiber when those appear weak and avoid worsening an
   already high calorie, sugar, fat, or sodium intake.
10. Keep the three ideas meaningfully different.
11. Select exactly one of the three as heart_healthy_pick. Base that
   selection on the overall meal pattern: favor vegetables, fruits,
   whole grains, beans and legumes, nuts and seeds, fish, skinless
   poultry or other lean unprocessed protein, and unsaturated plant
   fats. Prefer higher fiber and lower sodium, added sugar, saturated
   fat, and processed or fatty meat. Do not select it merely because it
   has the fewest calories.
12. Give a short heart_healthy_reason that names the specific strengths
   and any relevant limitation. This is a food-choice label only; never
   claim that the meal prevents disease or describe the user's personal
   heart risk.
13. These are estimates. Never describe them as verified nutrition.
"""

    client = get_client()
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0.2,
                response_mime_type="application/json",
                response_schema=PantryMealIdeaSet,
            ),
        )
    finally:
        client.close()

    if response.parsed is not None:
        result = (
            response.parsed
            if isinstance(response.parsed, PantryMealIdeaSet)
            else PantryMealIdeaSet.model_validate(response.parsed)
        )
    elif response.text:
        result = PantryMealIdeaSet.model_validate_json(response.text)
    else:
        raise RuntimeError("Gemini returned no Pantry meal ideas.")

    ideas = [idea.model_dump() for idea in result.ideas]
    selected_index = int(result.heart_healthy_pick) - 1
    for index, idea in enumerate(ideas):
        is_selected = index == selected_index
        idea["heart_healthy_pick"] = is_selected
        idea["heart_healthy_reason"] = (
            result.heart_healthy_reason if is_selected else None
        )
    return validate_pantry_meal_ideas(
        ideas,
        pantry_items=pantry_items,
        meal_type=normalized_meal,
    )


def scale_pantry_meal_nutrition(
    idea: dict[str, Any],
    *,
    servings: float,
) -> dict[str, float]:
    amount = float(servings)
    if amount <= 0:
        raise ValueError("servings must be greater than zero.")

    return {
        field: round(float(idea.get(field) or 0) * amount, 3)
        for field in (
            "calories",
            "protein_g",
            "carbohydrates_g",
            "fat_g",
            "fiber_g",
            "sugar_g",
            "sodium_mg",
        )
    }
