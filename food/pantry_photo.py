from __future__ import annotations

from typing import Any

from google.genai import types
from pydantic import BaseModel, Field

from food.menu_photo_advisor import ALLOWED_IMAGE_TYPES
from food.nutrition_lookup import MODEL_NAME, get_client
from food.pantry import clean_pantry_name, pantry_name_key


MAX_PANTRY_PHOTO_ITEMS = 30
MAX_PANTRY_PHOTO_SESSION_ITEMS = 30


class PantryPhotoItem(BaseModel):
    display_name: str


class PantryPhotoAnalysis(BaseModel):
    readable: bool
    items: list[PantryPhotoItem] = Field(
        default_factory=list,
        max_length=MAX_PANTRY_PHOTO_ITEMS,
    )
    notes: list[str] = Field(default_factory=list)


def merge_pantry_photo_names(
    existing: list[str],
    detected: list[str],
) -> list[str]:
    """Combine photo results by Pantry identity without tracking quantity."""
    combined: list[str] = []
    seen: set[str] = set()

    for raw_name in [*existing, *detected]:
        try:
            name = clean_pantry_name(raw_name)
            key = pantry_name_key(name)
        except ValueError:
            continue
        if key in seen:
            continue
        if len(combined) >= MAX_PANTRY_PHOTO_SESSION_ITEMS:
            break
        seen.add(key)
        combined.append(name)

    return combined


def analyze_pantry_photo(
    image_bytes: bytes,
    *,
    mime_type: str,
    user_context: str | None = None,
) -> dict[str, Any]:
    """Identify only clearly visible foods for presence-only Pantry review."""
    if not image_bytes:
        raise ValueError("image_bytes is required.")
    if mime_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError("Unsupported Pantry photo type.")

    context = str(user_context or "").strip()
    prompt = f"""
Inspect this pantry, cupboard, refrigerator, freezer, or food-shelf photo for
HealthCoach.

User context:
{context or "None"}

Strict rules:
1. Return only food or beverage items clearly supported by the visible photo.
2. For a packaged item, use the readable brand and product name when visible.
   Do not invent a brand, flavor, variety, size, or package detail.
3. A generic name is allowed only when the food type itself is visually clear,
   such as bananas, onions, potatoes, or canned black beans with a readable
   product label.
4. Omit every item whose identity is uncertain, obstructed, or too blurry.
5. Do not read or estimate nutrition, serving sizes, barcodes, quantities, or
   how many packages are present.
6. List an apparent item only once even when multiple identical packages are
   visible. HealthCoach does not track Pantry quantity.
7. Exclude dishes, appliances, storage containers, medicines, supplements,
   cleaning products, and non-food objects.
8. Return no more than {MAX_PANTRY_PHOTO_ITEMS} distinct items from this photo.
9. Set readable=false when no food can be identified reliably.
10. Notes may describe blur or blocked labels, but must not suggest or invent
    additional Pantry items.
11. Nothing will be saved automatically; the user will review every name.
"""

    client = get_client()
    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=[
                prompt,
                types.Part.from_bytes(
                    data=image_bytes,
                    mime_type=mime_type,
                ),
            ],
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=PantryPhotoAnalysis,
            ),
        )
    finally:
        client.close()

    if response.parsed is not None:
        result = (
            response.parsed
            if isinstance(response.parsed, PantryPhotoAnalysis)
            else PantryPhotoAnalysis.model_validate(response.parsed)
        )
    elif response.text:
        result = PantryPhotoAnalysis.model_validate_json(response.text)
    else:
        raise RuntimeError("Gemini returned no Pantry-photo analysis.")

    dumped = result.model_dump()
    if not dumped.get("readable"):
        dumped["items"] = []
        return dumped

    detected = [
        str(item.get("display_name") or "")
        for item in dumped.get("items") or []
    ]
    names = merge_pantry_photo_names([], detected)
    dumped["items"] = [{"display_name": name} for name in names]
    if not names:
        dumped["readable"] = False
        dumped.setdefault("notes", []).append(
            "No clearly identifiable food items were returned."
        )
    return dumped
