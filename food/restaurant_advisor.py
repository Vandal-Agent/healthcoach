from __future__ import annotations

import json
from datetime import datetime
from typing import Any, Literal

from google.genai import types
from pydantic import BaseModel, Field, field_validator

from food.nutrition_lookup import (
    MODEL_NAME,
    extract_citations,
    get_client,
)


class RestaurantCandidate(BaseModel):
    """One menu-based entrée recommendation."""

    item_name: str
    calories: float | None = None
    protein_g: float | None = None
    nutrition_status: Literal["official", "not_published"]
    recommendation_reason: str
    source_title: str
    source_url: str

    @field_validator("calories", "protein_g")
    @classmethod
    def nonnegative_nutrition(
        cls,
        value: float | None,
    ) -> float | None:
        if value is not None and value < 0:
            raise ValueError("nutrition values cannot be negative")
        return value


class RestaurantAdvice(BaseModel):
    """Structured restaurant recommendation result."""

    found: bool
    restaurant_display_name: str
    candidates: list[RestaurantCandidate] = Field(
        default_factory=list,
        max_length=3,
    )
    notes: list[str] = Field(default_factory=list)


def run_restaurant_menu_search(
    restaurant_query: str,
    *,
    client: Any,
) -> tuple[str, list[dict[str, str]]]:
    """Search current menu sources and return cited source text."""
    prompt = f"""
Find the current menu and, when available, official U.S. nutrition
information for this restaurant request:

{restaurant_query}

Current server-local date and time:
{datetime.now().astimezone().strftime("%A, %Y-%m-%d %I:%M %p %Z")}

Select up to three currently offered ENTRÉES that are likely to be good
HealthCoach choices.

Selection priorities:
1. Protein-forward.
2. Moderate calories when official calories are published.
3. Prefer grilled, roasted, lean-protein, vegetable-rich, or otherwise
   balanced entrées.
4. Avoid automatically adding fries, chips, sugary drinks, desserts,
   appetizers, or combo upgrades.
5. Use an official restaurant menu and official restaurant nutrition
   source whenever available.
6. A local restaurant's own website or online menu may be used when it
   is the best available primary menu source.
7. Do not use delivery marketplaces, blogs, forums, social media, or
   crowdsourced calorie sites as nutrition sources.
8. Never estimate calories or protein.
9. If official nutrition is not published, still identify promising
   entrées from the cited menu, but state that nutrition is not
   published.
10. Do not claim an item is available unless a cited current menu source
    supports it.
11. Report menu item names exactly enough for the user to order them.
12. Respect the current local date and time. Exclude breakfast-only,
    lunch-only, late-night-only, or otherwise time-limited items unless
    they are normally available at that time or the user requested that
    meal.
13. If current availability is uncertain, prefer all-day menu items.
14. Keep the report concise and include why each item may be a good
    choice.
"""

    interaction = client.interactions.create(
        model=MODEL_NAME,
        input=prompt,
        tools=[{"type": "google_search"}],
    )
    return (
        interaction.output_text or "",
        extract_citations(interaction),
    )


def structure_restaurant_advice(
    *,
    restaurant_query: str,
    source_text: str,
    citations: list[dict[str, str]],
    client: Any,
) -> RestaurantAdvice:
    """Convert a cited menu report into validated recommendations."""
    if not source_text or not citations:
        return RestaurantAdvice(
            found=False,
            restaurant_display_name=restaurant_query.strip(),
            notes=["No cited current menu source was returned."],
        )

    prompt = f"""
Convert the cited restaurant menu report into structured recommendations.

Restaurant request:
{restaurant_query}

Rules:
1. Use only the supplied report and citations.
2. Return no more than three entrées.
3. Every source_url must exactly match a URL in CITATIONS.
4. Every source_title must match the corresponding citation title.
5. Set nutrition_status="official" only when the report says the
   calories/protein came from official restaurant nutrition.
6. For nutrition_status="not_published", calories and protein_g must be
   null.
7. Never estimate, infer, or calculate missing nutrition.
8. recommendation_reason must be short and based only on supported menu
   description or official nutrition.
9. Set found=false if no current entrée could be supported.
10. Do not include sides, drinks, desserts, or unavailable items.

CITATIONS:
{json.dumps(citations, indent=2)}

CITED REPORT:
{source_text}
"""

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=RestaurantAdvice,
        ),
    )

    if response.parsed is not None:
        if isinstance(response.parsed, RestaurantAdvice):
            return response.parsed
        return RestaurantAdvice.model_validate(response.parsed)

    if not response.text:
        raise RuntimeError(
            "Gemini returned no structured restaurant advice."
        )

    return RestaurantAdvice.model_validate_json(response.text)


def recommend_restaurant_entrees(
    restaurant_query: str,
) -> dict[str, Any]:
    """Return up to three cited restaurant entrée recommendations."""
    cleaned_query = restaurant_query.strip()
    if not cleaned_query:
        raise ValueError("restaurant_query is required.")

    client = get_client()
    try:
        source_text, citations = run_restaurant_menu_search(
            cleaned_query,
            client=client,
        )
        result = structure_restaurant_advice(
            restaurant_query=cleaned_query,
            source_text=source_text,
            citations=citations,
            client=client,
        )
    finally:
        client.close()

    allowed_citations = {
        citation["url"]: citation["title"]
        for citation in citations
        if citation.get("url")
    }
    valid_candidates: list[RestaurantCandidate] = []
    rejected_names: list[str] = []

    for candidate in result.candidates:
        expected_title = allowed_citations.get(candidate.source_url)
        valid_source = (
            expected_title is not None
            and candidate.source_title == expected_title
        )
        valid_nutrition = (
            candidate.nutrition_status == "official"
            or (
                candidate.calories is None
                and candidate.protein_g is None
            )
        )

        if valid_source and valid_nutrition:
            valid_candidates.append(candidate)
        else:
            rejected_names.append(candidate.item_name)

    notes = list(result.notes)
    if rejected_names:
        notes.append(
            "Removed recommendations without valid cited support: "
            + ", ".join(rejected_names)
        )

    return {
        "found": bool(result.found and valid_candidates),
        "restaurant_display_name": result.restaurant_display_name,
        "candidates": [
            candidate.model_dump()
            for candidate in valid_candidates[:3]
        ],
        "notes": notes,
        "citations": citations,
    }
