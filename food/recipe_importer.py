from __future__ import annotations

import json
import re
from typing import Any

from google.genai import types
from pydantic import BaseModel, Field

from food.nutrition_lookup import MODEL_NAME, get_client


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_RECIPE_TEXT_LENGTH = 20_000


def suggest_generic_ingredient_name(
    ingredient_name: str,
    *,
    brand: str | None = None,
) -> str | None:
    """Suggest a visibly less-specific produce name for user approval."""
    if str(brand or "").strip():
        return None

    tokens = re.findall(r"[a-z0-9]+", str(ingredient_name or "").lower())
    singular = {
        "onions": "onion",
        "peppers": "pepper",
        "apples": "apple",
        "grapes": "grape",
        "potatoes": "potato",
        "cabbages": "cabbage",
    }
    tokens = [singular.get(token, token) for token in tokens]
    produce_nouns = {
        "onion",
        "pepper",
        "apple",
        "grape",
        "potato",
        "cabbage",
    }
    if not (set(tokens) & produce_nouns):
        return None

    simplified = [
        token
        for token in tokens
        if token not in {"white", "yellow", "red", "green", "orange"}
    ]
    suggestion = " ".join(simplified).strip()
    original = " ".join(tokens).strip()
    if not suggestion or suggestion == original:
        return None
    return suggestion


class ImportedRecipeIngredient(BaseModel):
    ingredient_name: str
    amount_description: str
    brand: str | None = None
    optional: bool = False
    trace_only: bool = False


class ImportedRecipeDraft(BaseModel):
    readable: bool
    recipe_name: str | None = None
    meal_type: str | None = None
    # Gemini's response-schema adapter rejects Pydantic's
    # exclusiveMinimum keyword, so validate this range after parsing.
    yield_servings: float | None = None
    claimed_calories_per_serving: float | None = None
    claimed_protein_g_per_serving: float | None = None
    summary: str = ""
    ingredients: list[ImportedRecipeIngredient] = Field(
        default_factory=list,
        max_length=50,
    )
    preparation_steps: list[str] = Field(
        default_factory=list,
        max_length=30,
    )
    notes: list[str] = Field(default_factory=list)


RECIPE_EXTRACTION_RULES = """
Extract a recipe draft for HealthCoach.

Strict rules:
1. Transcribe only recipe information supported by the supplied text or photo.
2. Never calculate or estimate nutrition. Extract claimed_calories_per_serving
   and claimed_protein_g_per_serving only when the source explicitly states
   those values for one finished serving; otherwise return null. These claims
   are comparison-only and are never used to calculate recipe nutrition.
3. Keep each ingredient amount exactly as written, including fractions and units.
4. ingredient_name must exclude the numeric amount but retain meaningful form,
   such as raw, cooked, drained, shredded, or boneless skinless.
5. Put a brand in brand only when it is explicitly printed or written.
6. Set yield_servings to null when the recipe yield is not supplied.
7. Set meal_type to lunch or dinner only when clearly stated; otherwise null.
8. Preserve preparation order. Do not invent missing cooking times,
   temperatures, or food-safety instructions.
9. Mark optional=true only when the source explicitly says optional.
10. Mark trace_only=true only for a spice, dried herb, or garnish used in a
    small culinary amount whose nutrition may reasonably be excluded after
    explicit user confirmation. Never mark oils, sauces, cheese, meat,
    grains, produce, sweeteners, or other substantial foods as trace-only.
11. Set readable=false when a reliable ingredient list cannot be extracted.
12. Return this exact JSON shape and no other nutrition fields:
    {"readable": true, "recipe_name": null, "meal_type": null,
    "yield_servings": null, "claimed_calories_per_serving": null,
    "claimed_protein_g_per_serving": null, "summary": "", "ingredients":
    [{"ingredient_name": "", "amount_description": "", "brand": null,
    "optional": false, "trace_only": false}],
    "preparation_steps": [], "notes": []}
"""


def _normalize_recipe_payload(value: Any) -> dict[str, Any]:
    """Normalize harmless JSON key variants without inventing amounts."""
    if isinstance(value, str):
        payload = json.loads(value)
    elif isinstance(value, dict):
        payload = dict(value)
    else:
        raise ValueError("Gemini returned an invalid recipe object.")

    normalized_ingredients = []
    for raw_item in payload.get("ingredients") or []:
        if not isinstance(raw_item, dict):
            continue
        item = dict(raw_item)
        ingredient_name = str(
            item.get("ingredient_name") or item.get("name") or ""
        ).strip()
        if not ingredient_name:
            continue

        amount_description = str(
            item.get("amount_description") or ""
        ).strip()
        if not amount_description:
            amount = item.get("amount")
            unit = str(item.get("unit") or "").strip()
            amount_text = (
                str(amount).strip()
                if amount is not None and str(amount).strip()
                else ""
            )
            amount_description = " ".join(
                part for part in (amount_text, unit) if part
            )
        if not amount_description:
            amount_description = "amount not specified"

        normalized_ingredients.append({
            "ingredient_name": ingredient_name,
            "amount_description": amount_description,
            "brand": item.get("brand"),
            "optional": bool(item.get("optional", False)),
            "trace_only": bool(item.get("trace_only", False)),
        })

    return {
        "readable": bool(payload.get("readable", normalized_ingredients)),
        "recipe_name": payload.get("recipe_name") or payload.get("name"),
        "meal_type": payload.get("meal_type"),
        "yield_servings": (
            payload.get("yield_servings")
            if payload.get("yield_servings") is not None
            else payload.get("servings")
        ),
        "claimed_calories_per_serving": payload.get(
            "claimed_calories_per_serving"
        ),
        "claimed_protein_g_per_serving": payload.get(
            "claimed_protein_g_per_serving"
        ),
        "summary": str(payload.get("summary") or ""),
        "ingredients": normalized_ingredients,
        "preparation_steps": list(
            payload.get("preparation_steps")
            or payload.get("steps")
            or []
        ),
        "notes": list(payload.get("notes") or []),
    }


def _decode_recipe_response(response: Any) -> dict[str, Any]:
    if response.parsed is not None:
        if isinstance(response.parsed, ImportedRecipeDraft):
            draft = response.parsed
        else:
            draft = ImportedRecipeDraft.model_validate(
                _normalize_recipe_payload(response.parsed)
            )
    elif response.text:
        draft = ImportedRecipeDraft.model_validate(
            _normalize_recipe_payload(response.text)
        )
    else:
        raise RuntimeError("Gemini returned no recipe draft.")

    result = draft.model_dump()
    yield_servings = result.get("yield_servings")
    if yield_servings is not None and not (
        0 < float(yield_servings) <= 100
    ):
        result["yield_servings"] = None
        result.setdefault("notes", []).append(
            "The supplied recipe yield was outside the supported range."
        )
    for field in (
        "claimed_calories_per_serving",
        "claimed_protein_g_per_serving",
    ):
        value = result.get(field)
        if value is not None and float(value) < 0:
            result[field] = None
            result.setdefault("notes", []).append(
                "A negative source nutrition claim was ignored."
            )
    if result.get("readable") and not result.get("ingredients"):
        result["readable"] = False
        result.setdefault("notes", []).append(
            "No recipe ingredients were found."
        )
    return result


def parse_recipe_text(recipe_text: str) -> dict[str, Any]:
    """Extract a structured recipe draft from pasted text."""
    cleaned = str(recipe_text or "").strip()
    if not cleaned:
        raise ValueError("Recipe text is required.")
    if len(cleaned) > MAX_RECIPE_TEXT_LENGTH:
        raise ValueError("That recipe is too long to import safely.")

    client = get_client()
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=(
                RECIPE_EXTRACTION_RULES
                + "\n\nReturn one JSON object using exactly the recipe "
                "draft fields described above.\n\nRecipe text:\n"
                + cleaned
            ),
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )
    finally:
        client.close()
    return _decode_recipe_response(response)


def parse_recipe_photo(
    image_bytes: bytes,
    *,
    mime_type: str,
    user_context: str | None = None,
) -> dict[str, Any]:
    """Extract a structured recipe draft from a recipe image."""
    if not image_bytes:
        raise ValueError("Recipe photo is required.")
    if mime_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError("Unsupported recipe photo type.")

    context = str(user_context or "").strip()
    client = get_client()
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                (
                    RECIPE_EXTRACTION_RULES
                    + "\n\nReturn one JSON object using exactly the recipe "
                    "draft fields described above.\n\nUser context:\n"
                    + (context or "None")
                ),
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type=mime_type,
                ),
            ],
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
            ),
        )
    finally:
        client.close()
    return _decode_recipe_response(response)
