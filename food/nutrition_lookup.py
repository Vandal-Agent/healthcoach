from __future__ import annotations

import json
import os
import re
from typing import Any

from google import genai
from google.genai import types
from pydantic import BaseModel, Field


MODEL_NAME = "gemini-3.6-flash"


RESTAURANT_OFFICIAL_DOMAINS = {
    "burger king": "burgerking.com",
    "mcdonalds": "mcdonalds.com",
    "mcdonald's": "mcdonalds.com",
    "taco bell": "tacobell.com",
    "wendys": "wendys.com",
    "wendy's": "wendys.com",
}


COMMON_MENU_ALIASES = {
    "fry": "French Fries",
    "fries": "French Fries",
    "french fry": "French Fries",
    "french fries": "French Fries",
    "large fry": "French Fries",
    "medium fry": "French Fries",
    "small fry": "French Fries",
    "soda": "Soft Drink",
    "soft drink": "Soft Drink",
    "pop": "Soft Drink",
    "diet soda": "Diet Soft Drink",
    "diet coke": "Diet Coke",
    "coke": "Coca-Cola",
}


def normalize_lookup_text(value: str | None) -> str:
    """Normalize user wording for deterministic menu matching."""
    if not value:
        return ""

    cleaned = value.strip().lower()
    cleaned = cleaned.replace("’", "'")
    cleaned = re.sub(r"[^a-z0-9']+", " ", cleaned)
    cleaned = re.sub(r"\s+", " ", cleaned)

    return cleaned.strip()


def normalize_menu_item(
    food_name: str,
    size: str | None,
) -> str:
    """
    Convert common user wording to official-style menu terminology.

    This changes search wording only. It never supplies nutrition or
    infers a different food.
    """
    normalized_food = normalize_lookup_text(food_name)
    normalized_size = normalize_lookup_text(size)

    candidates = []

    if normalized_size:
        candidates.append(
            f"{normalized_size} {normalized_food}".strip()
        )

    candidates.append(normalized_food)

    for candidate in candidates:
        if candidate in COMMON_MENU_ALIASES:
            return COMMON_MENU_ALIASES[candidate]

    return food_name.strip()


def official_restaurant_domain(
    restaurant: str | None,
) -> str | None:
    """Return the configured official U.S. domain for a restaurant."""
    normalized = normalize_lookup_text(restaurant)

    return RESTAURANT_OFFICIAL_DOMAINS.get(normalized)


TRUSTED_NUTRITION_DOMAINS = {
    "burgerking.com",
    "fdc.nal.usda.gov",
    "mcdonalds.com",
    "openfoodfacts.org",
    "pepsicoproductfacts.com",
    "tacobell.com",
    "usda.gov",
    "wendys.com",
    "world.openfoodfacts.org",
}


def normalize_source_domain(value: str) -> str:
    """Normalize a cited source title into a comparable domain."""
    domain = value.strip().lower()

    if domain.startswith("www."):
        domain = domain[4:]

    return domain.rstrip(".")


def is_trusted_nutrition_source(source_title: str) -> bool:
    """Return whether a citation belongs to an approved source."""
    domain = normalize_source_domain(source_title)

    return any(
        domain == trusted
        or domain.endswith("." + trusted)
        for trusted in TRUSTED_NUTRITION_DOMAINS
    )


class NutritionComponent(BaseModel):
    """One verified component of a food or meal."""

    name: str
    serving_description: str

    calories: float | None = None
    protein_g: float | None = None
    carbohydrates_g: float | None = None
    fat_g: float | None = None
    fiber_g: float | None = None
    sugar_g: float | None = None
    sodium_mg: float | None = None

    source_title: str
    source_url: str


class NutritionLookupResult(BaseModel):
    """Structured nutrition extracted from cited source text."""

    found: bool
    exact_match: bool
    components: list[NutritionComponent] = Field(
        default_factory=list
    )
    missing_fields: list[str] = Field(default_factory=list)
    clarification_question: str | None = None
    notes: list[str] = Field(default_factory=list)


def get_client() -> genai.Client:
    """Create a Gemini client using the HealthCoach key."""
    api_key = os.getenv("HEALTH_GEMINI_API_KEY")

    if not api_key:
        raise RuntimeError(
            "HEALTH_GEMINI_API_KEY is not configured."
        )

    return genai.Client(api_key=api_key)


def extract_citations(
    interaction: Any,
) -> list[dict[str, str]]:
    """Return unique citations from a grounded interaction."""
    citations: list[dict[str, str]] = []
    seen_urls: set[str] = set()

    for step in interaction.steps or []:
        if getattr(step, "type", None) != "model_output":
            continue

        for block in getattr(step, "content", []) or []:
            for annotation in (
                getattr(block, "annotations", []) or []
            ):
                if getattr(annotation, "type", None) != "url_citation":
                    continue

                url = str(getattr(annotation, "url", "") or "")
                title = str(getattr(annotation, "title", "") or "")

                if not url or url in seen_urls:
                    continue

                seen_urls.add(url)
                citations.append(
                    {
                        "title": title,
                        "url": url,
                    }
                )

    return citations


def calculate_totals(
    components: list[NutritionComponent],
) -> dict[str, float | None]:
    """Calculate totals from verified components."""
    fields = (
        "calories",
        "protein_g",
        "carbohydrates_g",
        "fat_g",
        "fiber_g",
        "sugar_g",
        "sodium_mg",
    )

    totals: dict[str, float | None] = {}

    for field in fields:
        values = [
            getattr(component, field)
            for component in components
            if getattr(component, field) is not None
        ]

        totals[field] = (
            round(sum(float(value) for value in values), 3)
            if values
            else None
        )

    return totals


def run_grounded_search(
    *,
    restaurant: str | None,
    food_name: str,
    size: str | None,
    drink: str | None,
) -> tuple[str, list[dict[str, str]]]:
    """Search the web and return cited source text."""
    normalized_food_name = normalize_menu_item(
        food_name,
        size,
    )
    official_domain = official_restaurant_domain(
        restaurant
    )

    official_source_instruction = (
        f"Use the official U.S. website {official_domain} as the "
        "primary source. Search that domain specifically."
        if official_domain
        else (
            "Use an official manufacturer, restaurant, USDA, or "
            "Open Food Facts source."
        )
    )

    prompt = f"""
Find verified nutrition for this food request:

Restaurant: {restaurant or "not specified"}
User wording: {food_name}
Official menu search term: {normalized_food_name}
Requested size: {size or "not specified"}
Drink: {drink or "not specified"}
Official-source instruction: {official_source_instruction}

Rules:

1. For restaurant food, first search the specified official U.S.
   restaurant domain and match the official menu search term.
2. Do not substitute a different country, serving, product, or menu item.
3. If no exact official U.S. restaurant result exists, report that the
   item could not be verified.
4. For non-restaurant foods only, USDA FoodData Central or Open Food
   Facts may be used.
5. Do not use blogs, forums, social media, document-sharing sites,
   calorie-estimate sites, or crowdsourced restaurant entries.
6. Match only the food and drink items explicitly requested.
7. A sandwich or menu item must not be treated as a combo meal unless
   the user explicitly said meal, combo, value meal, or included sides.
8. Return separate components only when the request explicitly contains
   multiple foods or drinks.
9. Do not invent fries, sides, drinks, sizes, or meal components.
10. Do not estimate missing nutrients.
11. Clearly state when a nutrient is not listed.
12. Include only information supported by cited sources.
"""

    client = get_client()

    interaction = client.interactions.create(
        model=MODEL_NAME,
        input=prompt,
        tools=[{"type": "google_search"}],
    )

    source_text = interaction.output_text or ""
    citations = extract_citations(interaction)

    return source_text, citations


def structure_grounded_result(
    *,
    source_text: str,
    citations: list[dict[str, str]],
) -> NutritionLookupResult:
    """Convert cited source text into structured nutrition."""
    if not source_text or not citations:
        return NutritionLookupResult(
            found=False,
            exact_match=False,
            notes=[
                "No cited nutrition source was returned."
            ],
        )

    citation_text = json.dumps(citations, indent=2)

    prompt = f"""
Convert the cited nutrition report below into structured data.

STRICT RULES:

1. Use only the supplied report and citation list.
2. Do not use outside knowledge.
3. Do not estimate or infer missing nutrients.
4. Missing nutrient values must be null.
5. Every component source_url must exactly match one URL from the
   supplied citation list.
6. Every component source_title must match the corresponding title.
7. Set found=true when every food or drink explicitly requested by the
   user has been verified from the supplied official sources.
8. Do not add or require fries, sides, drinks, sizes, or other components
   that were not explicitly requested.
9. Treat a single sandwich or menu item as one food, not as a meal.
10. Only list missing_fields for items or details the user explicitly
    requested but that the supplied report could not verify.

CITATIONS:
{citation_text}

CITED REPORT:
{source_text}
"""

    client = get_client()

    response = client.models.generate_content(
        model=MODEL_NAME,
        contents=prompt,
        config=types.GenerateContentConfig(
            temperature=0,
            response_mime_type="application/json",
            response_schema=NutritionLookupResult,
        ),
    )

    if response.parsed is not None:
        if isinstance(response.parsed, NutritionLookupResult):
            return response.parsed

        return NutritionLookupResult.model_validate(
            response.parsed
        )

    if not response.text:
        raise RuntimeError(
            "Gemini returned no structured nutrition result."
        )

    return NutritionLookupResult.model_validate_json(
        response.text
    )


def lookup_verified_nutrition(
    *,
    restaurant: str | None,
    food_name: str,
    size: str | None,
    drink: str | None,
) -> dict[str, Any]:
    """Search, cite, structure, and validate nutrition."""
    if not food_name.strip():
        raise ValueError("food_name is required.")

    source_text, citations = run_grounded_search(
        restaurant=restaurant,
        food_name=food_name,
        size=size,
        drink=drink,
    )

    result = structure_grounded_result(
        source_text=source_text,
        citations=citations,
    )

    allowed_urls = {
        citation["url"]
        for citation in citations
    }

    invalid_components = [
        component.name
        for component in result.components
        if component.source_url not in allowed_urls
    ]

    if invalid_components:
        return {
            "found": False,
            "exact_match": False,
            "components": [],
            "totals": {},
            "citations": citations,
            "missing_fields": [],
            "clarification_question": None,
            "notes": [
                "Rejected because a component used an unapproved source URL.",
                "Invalid components: "
                + ", ".join(invalid_components),
            ],
        }

    untrusted_components = [
        component.name
        for component in result.components
        if not is_trusted_nutrition_source(
            component.source_title
        )
    ]

    if untrusted_components:
        return {
            "found": False,
            "exact_match": False,
            "components": [],
            "totals": {},
            "citations": citations,
            "missing_fields": [],
            "clarification_question": None,
            "notes": [
                "Rejected because one or more nutrition sources "
                "were not on the trusted-source allowlist.",
                "Invalid components: "
                + ", ".join(untrusted_components),
            ],
        }

    if restaurant:
        foreign_market_suffixes = (
            ".ca",
            ".co.uk",
            ".com.au",
            ".co.nz",
            ".ie",
        )

        foreign_market_components = [
            component.name
            for component in result.components
            if component.source_title.lower().strip().endswith(
                foreign_market_suffixes
            )
        ]

        if foreign_market_components:
            return {
                "found": False,
                "exact_match": False,
                "components": [],
                "totals": {},
                "citations": citations,
                "missing_fields": [],
                "clarification_question": None,
                "notes": [
                    "Rejected because the restaurant nutrition "
                    "source was for a non-US market.",
                    "Invalid components: "
                    + ", ".join(foreign_market_components),
                ],
            }

        normalized_report = source_text.lower()

        mixed_source_markers = (
            "based on usda",
            "usda fooddata central",
            "fooddata central entry",
        )

        if any(
            marker in normalized_report
            for marker in mixed_source_markers
        ):
            return {
                "found": False,
                "exact_match": False,
                "components": [],
                "totals": {},
                "citations": citations,
                "missing_fields": [],
                "clarification_question": None,
                "notes": [
                    "Rejected because the restaurant result mixed "
                    "restaurant labeling with USDA-derived nutrition.",
                    "An exact official US restaurant source is required.",
                ],
            }

    return {
        "found": result.found,
        "exact_match": result.exact_match,
        "components": [
            component.model_dump()
            for component in result.components
        ],
        "totals": calculate_totals(result.components),
        "citations": citations,
        "missing_fields": result.missing_fields,
        "clarification_question": result.clarification_question,
        "notes": result.notes,
    }


def main() -> None:
    """Run a command-line nutrition lookup test."""
    result = lookup_verified_nutrition(
        restaurant="McDonald's",
        food_name="Big Mac meal",
        size="medium",
        drink="diet soda",
    )

    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
