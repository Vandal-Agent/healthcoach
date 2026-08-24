from __future__ import annotations

from typing import Any

from google.genai import types
from pydantic import BaseModel, Field

from food.nutrition_lookup import MODEL_NAME, get_client


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_RECIPE_TEXT_LENGTH = 20_000


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
    yield_servings: float | None = Field(
        default=None,
        gt=0,
        le=100,
    )
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
2. Never calculate, estimate, or return nutrition information.
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
"""


def _decode_recipe_response(response: Any) -> dict[str, Any]:
    if response.parsed is not None:
        draft = (
            response.parsed
            if isinstance(response.parsed, ImportedRecipeDraft)
            else ImportedRecipeDraft.model_validate(response.parsed)
        )
    elif response.text:
        draft = ImportedRecipeDraft.model_validate_json(response.text)
    else:
        raise RuntimeError("Gemini returned no recipe draft.")

    result = draft.model_dump()
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

    response = get_client().models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=(
                            RECIPE_EXTRACTION_RULES
                            + "\n\nRecipe text:\n"
                            + cleaned
                        )
                    )
                ],
            )
        ],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ImportedRecipeDraft,
        ),
    )
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
    response = get_client().models.generate_content(
        model=MODEL_NAME,
        contents=[
            types.Content(
                role="user",
                parts=[
                    types.Part.from_text(
                        text=(
                            RECIPE_EXTRACTION_RULES
                            + "\n\nUser context:\n"
                            + (context or "None")
                        )
                    ),
                    types.Part.from_bytes(
                        data=image_bytes,
                        mime_type=mime_type,
                    ),
                ],
            )
        ],
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=ImportedRecipeDraft,
        ),
    )
    return _decode_recipe_response(response)
