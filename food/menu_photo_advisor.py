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
