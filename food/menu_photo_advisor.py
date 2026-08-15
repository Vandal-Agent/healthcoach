from __future__ import annotations

from typing import Any

import requests
from google.genai import types
from pydantic import BaseModel, Field

from food.nutrition_lookup import MODEL_NAME, get_client


ALLOWED_IMAGE_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_PHOTO_BYTES = 10 * 1024 * 1024


class MenuPhotoCandidate(BaseModel):
    item_name: str
    printed_price: str | None = None
    printed_calories: float | None = None
    visible_details: str
    recommendation_reason: str


class MenuPhotoAnalysis(BaseModel):
    readable: bool
    restaurant_name: str | None = None
    candidates: list[MenuPhotoCandidate] = Field(
        default_factory=list,
        max_length=3,
    )
    notes: list[str] = Field(default_factory=list)


def download_telegram_photo(
    *,
    telegram_token: str,
    file_id: str,
) -> tuple[bytes, str]:
    if not telegram_token:
        raise ValueError("telegram_token is required.")

    metadata_response = requests.get(
        f"https://api.telegram.org/bot{telegram_token}/getFile",
        params={"file_id": file_id},
        timeout=20,
    )
    metadata_response.raise_for_status()
    payload = metadata_response.json()
    file_path = (
        payload.get("result", {}).get("file_path")
        if payload.get("ok")
        else None
    )
    if not file_path:
        raise RuntimeError("Telegram did not return a photo file path.")

    photo_response = requests.get(
        f"https://api.telegram.org/file/bot{telegram_token}/{file_path}",
        timeout=30,
    )
    photo_response.raise_for_status()
    image_bytes = photo_response.content

    if not image_bytes:
        raise RuntimeError("Telegram returned an empty photo.")
    if len(image_bytes) > MAX_PHOTO_BYTES:
        raise ValueError("The menu photo is too large to analyze.")

    mime_type = (
        photo_response.headers.get("Content-Type", "")
        .split(";", 1)[0]
        .strip()
        .lower()
    )
    if mime_type not in ALLOWED_IMAGE_TYPES:
        suffix = file_path.lower().rsplit(".", 1)[-1]
        mime_type = {
            "jpg": "image/jpeg",
            "jpeg": "image/jpeg",
            "png": "image/png",
            "webp": "image/webp",
        }.get(suffix, "")

    if mime_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError("Unsupported menu photo type.")

    return image_bytes, mime_type


def analyze_menu_photo(
    image_bytes: bytes,
    *,
    mime_type: str,
    user_context: str | None = None,
) -> dict[str, Any]:
    if not image_bytes:
        raise ValueError("image_bytes is required.")
    if mime_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError("Unsupported menu photo type.")

    context = (user_context or "").strip()
    prompt = f"""
Analyze this restaurant menu photo for HealthCoach.

User context:
{context or "None"}

Rules:
1. Use only information visibly supported by the photo.
2. Recommend no more than three entrees.
3. Prioritize lean protein, vegetables, beans, and balanced plates.
4. Prefer grilled, roasted, or baked choices when supported.
5. Treat fried foods, creamy sauces, and large portions cautiously.
6. Never invent ingredients, calories, protein, or serving sizes.
7. printed_calories must be null unless visibly printed.
8. Copy item names closely enough to order them.
9. Set readable=false when the photo is too blurry or incomplete.
10. Mention when nutrition is not printed.
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
                response_schema=MenuPhotoAnalysis,
            ),
        )
    finally:
        client.close()

    if response.parsed is not None:
        result = (
            response.parsed
            if isinstance(response.parsed, MenuPhotoAnalysis)
            else MenuPhotoAnalysis.model_validate(response.parsed)
        )
    elif response.text:
        result = MenuPhotoAnalysis.model_validate_json(response.text)
    else:
        raise RuntimeError("Gemini returned no menu-photo analysis.")

    if not result.readable:
        result.candidates = []
    return result.model_dump()


def format_menu_photo_analysis(result: dict[str, Any]) -> str:
    candidates = list(result.get("candidates") or [])
    if not result.get("readable") or not candidates:
        return (
            "I couldn't read enough of that menu to make reliable "
            "choices.\n\nTry taking a closer photo with the item "
            "names and descriptions in focus."
        )

    lines = [
        "Menu Photo Recommendations",
        "",
        str(result.get("restaurant_name") or "Menu photo"),
        "",
    ]

    for index, candidate in enumerate(candidates[:3], start=1):
        lines.append(f"{index}. {candidate['item_name']}")
        calories = candidate.get("printed_calories")
        lines.append(
            f"Printed calories: {float(calories):g}"
            if calories is not None
            else "Calories: not printed"
        )
        if candidate.get("printed_price"):
            lines.append(f"Price: {candidate['printed_price']}")
        if candidate.get("visible_details"):
            lines.append(f"Visible details: {candidate['visible_details']}")
        lines.append(
            f"Why: {candidate.get('recommendation_reason') or ''}"
        )
        lines.append("")

    lines.append(
        "Nothing has been logged. These recommendations use only "
        "what is visible in the picture."
    )
    return "\n".join(lines).strip()


class BarcodePhotoRead(BaseModel):
    readable: bool
    barcode: str | None = None
    notes: list[str] = Field(default_factory=list)


def read_barcode_photo(
    image_bytes: bytes,
    *,
    mime_type: str,
) -> dict[str, Any]:
    """
    Read the printed digits beneath a product barcode.

    The extracted number is accepted only when it passes the
    barcode length and checksum validation used by the USDA lookup.
    """
    if not image_bytes:
        raise ValueError("image_bytes is required.")
    if mime_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError("Unsupported barcode photo type.")

    prompt = """
Read the product barcode in this photo for HealthCoach.

Rules:
1. Read the digits printed directly beneath the barcode bars.
2. Preserve every digit, including small digits at either edge.
3. Preserve leading zeroes.
4. Return only one barcode.
5. Do not guess digits that are blurry, hidden, or cut off.
6. Set readable=false and barcode=null if the complete number
   cannot be read confidently.
7. The expected code is GTIN-8, UPC-A, EAN-13, or GTIN-14.
8. Briefly explain any readability problem in notes.
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
                response_schema=BarcodePhotoRead,
            ),
        )
    finally:
        client.close()

    if response.parsed is not None:
        result = (
            response.parsed
            if isinstance(response.parsed, BarcodePhotoRead)
            else BarcodePhotoRead.model_validate(response.parsed)
        )
    elif response.text:
        result = BarcodePhotoRead.model_validate_json(
            response.text
        )
    else:
        raise RuntimeError(
            "Gemini returned no barcode-photo result."
        )

    if not result.readable or not result.barcode:
        result.readable = False
        result.barcode = None
        return result.model_dump()

    from food.usda_provider import normalize_barcode

    try:
        result.barcode = normalize_barcode(result.barcode)
    except ValueError:
        result.readable = False
        result.barcode = None
        result.notes.append(
            "The visible number did not pass barcode validation."
        )

    return result.model_dump()


class FoodPhotoEstimate(BaseModel):
    readable: bool
    dish_name: str | None = None
    visible_components: list[str] = Field(default_factory=list)

    calories_low: float | None = Field(default=None, ge=0)
    calories_high: float | None = Field(default=None, ge=0)

    protein_low: float | None = Field(default=None, ge=0)
    protein_high: float | None = Field(default=None, ge=0)

    carbohydrates_low: float | None = Field(default=None, ge=0)
    carbohydrates_high: float | None = Field(default=None, ge=0)

    fat_low: float | None = Field(default=None, ge=0)
    fat_high: float | None = Field(default=None, ge=0)

    portion_assumptions: list[str] = Field(default_factory=list)
    uncertainty_notes: list[str] = Field(default_factory=list)


def is_food_photo_request(caption: str | None) -> bool:
    normalized = " ".join(
        str(caption or "").strip().lower().split()
    )

    phrases = {
        "estimate this meal",
        "estimate this food",
        "estimate my meal",
        "estimate my food",
        "food photo",
        "meal photo",
        "actual food",
        "actual meal",
        "this is what i ate",
        "this is what i'm eating",
        "my plate",
    }

    return any(phrase in normalized for phrase in phrases)


def analyze_food_photo(
    image_bytes: bytes,
    *,
    mime_type: str,
    user_context: str | None = None,
) -> dict[str, Any]:
    if not image_bytes:
        raise ValueError("image_bytes is required.")

    if mime_type not in ALLOWED_IMAGE_TYPES:
        raise ValueError("Unsupported food photo type.")

    context = str(user_context or "").strip()

    prompt = f"""
Analyze this photograph of an actual meal for HealthCoach.

User context:
{context or "None"}

Return an honest nutrition estimate based on visible food and
reasonable portion-size assumptions.

Rules:
1. Identify only food that is visible or stated by the user.
2. Estimate ranges for calories, protein, carbohydrates, and fat.
3. The low value must not exceed the high value.
4. Use realistic portion estimates rather than false precision.
5. Account for likely cooking oil only as uncertainty unless visible
   or stated.
6. Never claim an exact restaurant recipe or exact ingredient weight.
7. Clearly identify hidden oil, sauce, dressing, cheese, and portion
   size as uncertainty when applicable.
8. Do not assume the user ate the entire portion unless stated.
9. Set readable=false if the food cannot be assessed reliably.
10. This is estimation only. Nothing will be logged automatically.
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
                response_schema=FoodPhotoEstimate,
            ),
        )
    finally:
        client.close()

    if response.parsed is not None:
        result = (
            response.parsed
            if isinstance(response.parsed, FoodPhotoEstimate)
            else FoodPhotoEstimate.model_validate(
                response.parsed
            )
        )
    elif response.text:
        result = FoodPhotoEstimate.model_validate_json(
            response.text
        )
    else:
        raise RuntimeError(
            "Gemini returned no food-photo estimate."
        )

    if not result.readable:
        return result.model_dump()

    ranges = [
        ("calories", result.calories_low, result.calories_high),
        ("protein", result.protein_low, result.protein_high),
        (
            "carbohydrates",
            result.carbohydrates_low,
            result.carbohydrates_high,
        ),
        ("fat", result.fat_low, result.fat_high),
    ]

    for name, low, high in ranges:
        if low is None or high is None:
            raise ValueError(
                f"Food-photo estimate omitted the {name} range."
            )
        if low > high:
            raise ValueError(
                f"Food-photo estimate returned an invalid {name} range."
            )

    return result.model_dump()


def format_food_photo_estimate(
    result: dict[str, Any],
) -> str:
    if not result.get("readable"):
        return (
            "I couldn't see the meal clearly enough to estimate it.\n\n"
            "Try another photo showing the entire plate in good light."
        )

    def format_range(
        label: str,
        low_key: str,
        high_key: str,
        unit: str,
    ) -> str:
        low = float(result[low_key])
        high = float(result[high_key])
        return f"{label}: {low:g}-{high:g}{unit}"

    lines = [
        "Estimated Meal Nutrition",
        "",
        str(result.get("dish_name") or "Meal photo"),
        "",
        format_range(
            "Calories",
            "calories_low",
            "calories_high",
            "",
        ),
        format_range(
            "Protein",
            "protein_low",
            "protein_high",
            " g",
        ),
        format_range(
            "Carbohydrates",
            "carbohydrates_low",
            "carbohydrates_high",
            " g",
        ),
        format_range(
            "Fat",
            "fat_low",
            "fat_high",
            " g",
        ),
    ]

    components = list(
        result.get("visible_components") or []
    )
    if components:
        lines.extend(
            [
                "",
                "Visible components:",
                *[f"- {item}" for item in components],
            ]
        )

    assumptions = list(
        result.get("portion_assumptions") or []
    )
    if assumptions:
        lines.extend(
            [
                "",
                "Portion assumptions:",
                *[f"- {item}" for item in assumptions],
            ]
        )

    uncertainty = list(
        result.get("uncertainty_notes") or []
    )
    if uncertainty:
        lines.extend(
            [
                "",
                "Main uncertainty:",
                *[f"- {item}" for item in uncertainty],
            ]
        )

    lines.extend(
        [
            "",
            "This is a visual estimate, not verified nutrition.",
            "Nothing has been logged.",
        ]
    )

    return "\n".join(lines).strip()


def refine_food_photo_estimate(
    initial_result: dict[str, Any],
    *,
    user_details: str,
) -> dict[str, Any]:
    details = str(user_details or "").strip()

    if not details:
        return dict(initial_result)

    prompt = f"""
Refine a HealthCoach visual meal estimate using new information
provided by the user.

Initial visual estimate:
{initial_result}

New user details:
{details}

Rules:
1. Treat the user's explicit details as more reliable than visual
   guesses.
2. Return revised ranges for calories, protein, carbohydrates, and fat.
3. Keep uncertainty where portion weight, oil, sauce, or preparation
   remains unknown.
4. The low value must never exceed the high value.
5. Do not claim verified or exact nutrition.
6. Preserve useful visible components and update assumptions.
7. Set readable=true because this refines an existing readable result.
"""

    client = get_client()

    try:
        response = client.models.generate_content(
            model=MODEL_NAME,
            contents=prompt,
            config=types.GenerateContentConfig(
                temperature=0,
                response_mime_type="application/json",
                response_schema=FoodPhotoEstimate,
            ),
        )
    finally:
        client.close()

    if response.parsed is not None:
        result = (
            response.parsed
            if isinstance(response.parsed, FoodPhotoEstimate)
            else FoodPhotoEstimate.model_validate(
                response.parsed
            )
        )
    elif response.text:
        result = FoodPhotoEstimate.model_validate_json(
            response.text
        )
    else:
        raise RuntimeError(
            "Gemini returned no refined food-photo estimate."
        )

    ranges = [
        ("calories", result.calories_low, result.calories_high),
        ("protein", result.protein_low, result.protein_high),
        (
            "carbohydrates",
            result.carbohydrates_low,
            result.carbohydrates_high,
        ),
        ("fat", result.fat_low, result.fat_high),
    ]

    for name, low, high in ranges:
        if low is None or high is None:
            raise ValueError(
                f"Refined estimate omitted the {name} range."
            )

        if low > high:
            raise ValueError(
                f"Refined estimate returned an invalid {name} range."
            )

    return result.model_dump()


def midpoint_food_photo_nutrition(
    result: dict[str, Any],
    *,
    portion_fraction: float = 1.0,
) -> dict[str, float | None]:
    fraction = float(portion_fraction)

    if fraction <= 0 or fraction > 1:
        raise ValueError(
            "portion_fraction must be greater than zero and at most one."
        )

    def midpoint(low_key: str, high_key: str) -> float:
        low = float(result[low_key])
        high = float(result[high_key])
        return round(((low + high) / 2.0) * fraction, 2)

    return {
        "calories": midpoint(
            "calories_low",
            "calories_high",
        ),
        "protein_g": midpoint(
            "protein_low",
            "protein_high",
        ),
        "carbohydrates_g": midpoint(
            "carbohydrates_low",
            "carbohydrates_high",
        ),
        "fat_g": midpoint(
            "fat_low",
            "fat_high",
        ),
        "fiber_g": None,
        "sugar_g": None,
        "sodium_mg": None,
    }
