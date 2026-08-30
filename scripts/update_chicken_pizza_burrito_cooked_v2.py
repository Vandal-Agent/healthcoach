#!/usr/bin/env python3
"""Update Chicken Pizza Burritos to the confirmed five-burrito batch."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from food.library import (
    add_food_with_nutrition,
    find_food,
    get_active_nutrition,
)
from food.recipes import (
    NUTRIENT_FIELDS,
    calculate_recipe_nutrition,
    list_saved_recipe_ingredients,
    list_saved_recipes,
    prepare_recipe_ingredient,
    update_saved_recipe,
    update_saved_recipe_from_ingredients,
)


RECIPE_NAME = "Chicken Pizza Burritos"
RECIPE_YIELD = 5.0

COOKED_CHICKEN = {
    "canonical_name": (
        "Chicken Breast, Meat Only, Cooked, Roasted"
    ),
    "serving_description": "100 g cooked",
    "serving_amount": 100.0,
    "serving_unit": "g",
    "calories": 165.0,
    "protein_g": 31.02,
    "carbohydrates_g": 0.0,
    "fat_g": 3.57,
    "fiber_g": 0.0,
    "sugar_g": 0.0,
    "sodium_mg": 74.0,
    "verification_source": "fdc.nal.usda.gov",
    "source_item_id": "fdc-171477",
    "source_url": (
        "https://fdc.nal.usda.gov/fdc-app.html#/food-details/"
        "171477/nutrients"
    ),
}

INGREDIENT_SPECS = (
    (
        COOKED_CHICKEN["canonical_name"],
        COOKED_CHICKEN["serving_description"],
        None,
        "15 oz",
    ),
    (
        "Kirkland Light Tasting Olive Oil",
        "1 tbsp (3 tsp)",
        "Kirkland Signature",
        "0.5 tsp",
    ),
    (
        "Hormel Original Pepperoni",
        "30 g",
        "Hormel",
        "75 g",
    ),
    (
        "Kroger Pizza Sauce",
        "63 g",
        "Kroger",
        "250 g",
    ),
    (
        "Kraft 100% Grated Parmesan Cheese",
        "2 tsp (5 g)",
        "Kraft",
        "20 g",
    ),
    (
        "Great Value Cream Cheese",
        "2 tbsp (28 g)",
        "Great Value",
        "50 g",
    ),
    (
        "Mission Flour Tortilla, Soft Taco",
        "1 tortilla",
        "Mission",
        "5 tortillas",
    ),
    (
        "Kirkland Mexican Style Blend",
        "1/3 cup (28 g)",
        "Kirkland Signature",
        "100 g",
    ),
    (
        "Table Salt",
        "1 tsp (6 g)",
        None,
        "0.5 tsp",
    ),
)

DISPLAY_INGREDIENTS = [
    {
        "name": "chicken breast, cooked weight",
        "amount": "15 oz",
        "source": "saved_food",
    },
    {"name": "table salt", "amount": "0.5 tsp", "source": "saved_food"},
    {"name": "garlic powder", "amount": "0.5 tsp", "source": "additional"},
    {"name": "onion powder", "amount": "0.5 tsp", "source": "additional"},
    {"name": "smoked paprika", "amount": "0.5 tsp", "source": "additional"},
    {
        "name": "Kirkland Light Tasting Olive Oil",
        "amount": "0.5 tsp",
        "source": "saved_food",
    },
    {
        "name": "Hormel Original Pepperoni",
        "amount": "75 g",
        "source": "saved_food",
    },
    {
        "name": "Kroger Pizza Sauce",
        "amount": "250 g",
        "source": "saved_food",
    },
    {
        "name": "Kraft 100% Grated Parmesan Cheese",
        "amount": "20 g",
        "source": "saved_food",
    },
    {
        "name": "Great Value Cream Cheese",
        "amount": "50 g",
        "source": "saved_food",
    },
    {
        "name": "Italian herb seasoning mix",
        "amount": "0.75 tsp",
        "source": "additional",
    },
    {
        "name": "Mission Flour Tortillas, Soft Taco",
        "amount": "5 tortillas",
        "source": "saved_food",
    },
    {
        "name": "Kirkland Mexican Style Blend",
        "amount": "100 g total (20 g per burrito)",
        "source": "saved_food",
    },
]

SUMMARY = (
    "Five chicken pizza burritos made with cooked-weight chicken, pepperoni, "
    "pizza sauce, cream cheese, Parmesan, and Mexican blend cheese."
)

PREPARATION_STEPS = [
    (
        "Season the cooked chicken with the salt, garlic powder, onion "
        "powder, smoked paprika, and Italian herbs."
    ),
    (
        "Warm the olive oil in a large skillet. Add the cooked chicken and "
        "heat gently."
    ),
    (
        "Add the pepperoni, pizza sauce, Parmesan, and cream cheese. Stir "
        "over low heat until evenly combined and hot."
    ),
    (
        "Divide the filling evenly among five tortillas, using 20 g of "
        "Mexican blend cheese for each burrito, then roll and heat through."
    ),
]

ESTIMATE_NOTES = (
    "Nutrition is calculated from 15 oz cooked USDA roasted chicken breast "
    "and the exact saved versions of the other major ingredients. The "
    "recipe makes five burritos and includes 0.5 tsp olive oil total. Trace "
    "nutrition from garlic powder, onion powder, smoked paprika, and Italian "
    "herbs is not included. No mozzarella is used."
)


def _find_recipe() -> dict[str, Any]:
    matches = [
        recipe
        for recipe in list_saved_recipes()
        if str(recipe.get("canonical_name") or "").strip().lower()
        == RECIPE_NAME.lower()
    ]
    if len(matches) != 1:
        raise RuntimeError(
            f"Stopped safely: expected one {RECIPE_NAME} recipe, "
            f"found {len(matches)}."
        )
    return matches[0]


def _add_or_reuse_cooked_chicken() -> dict[str, Any]:
    result = add_food_with_nutrition(
        brand=None,
        restaurant=None,
        food_type="food",
        verification_status="verified",
        **COOKED_CHICKEN,
    )
    food = result["food"]
    nutrition = get_active_nutrition(int(food["food_id"]))
    if nutrition is None:
        raise RuntimeError(
            "Stopped safely: cooked chicken has no active nutrition."
        )
    mismatches = [
        field
        for field in NUTRIENT_FIELDS
        if nutrition.get(field) is None
        or abs(
            float(nutrition[field]) - float(COOKED_CHICKEN[field])
        ) > 0.01
    ]
    if mismatches:
        raise RuntimeError(
            "Stopped safely: existing cooked chicken nutrition conflicts "
            "for " + ", ".join(mismatches) + "."
        )
    return food


def _prepare_ingredients() -> list[dict[str, Any]]:
    cooked_chicken = _add_or_reuse_cooked_chicken()
    ingredients = []
    for name, serving, brand, amount in INGREDIENT_SPECS:
        food = (
            cooked_chicken
            if name == COOKED_CHICKEN["canonical_name"]
            else find_food(
                canonical_name=name,
                serving_description=serving,
                brand=brand,
                restaurant=None,
            )
        )
        if food is None:
            raise RuntimeError(
                f"Stopped safely: missing Saved Food {name}."
            )
        ingredients.append(
            prepare_recipe_ingredient(
                food_id=int(food["food_id"]),
                amount_description=amount,
            )
        )
    return ingredients


def _links_match(
    saved_recipe_id: int,
    ingredients: list[dict[str, Any]],
) -> bool:
    links = list_saved_recipe_ingredients(saved_recipe_id)
    if len(links) != len(ingredients):
        return False
    return all(
        int(link["food_id"]) == int(ingredient["food_id"])
        and int(link["nutrition_version_id"])
        == int(ingredient["nutrition_version_id"])
        and str(link["amount_description"]) == str(
            ingredient["amount_description"]
        )
        for link, ingredient in zip(links, ingredients)
    )


def update_recipe() -> dict[str, Any]:
    """Create one versioned five-burrito recalculation, idempotently."""
    recipe = _find_recipe()
    saved_recipe_id = int(recipe["saved_recipe_id"])
    ingredients = _prepare_ingredients()
    calculated = calculate_recipe_nutrition(
        ingredients,
        yield_servings=RECIPE_YIELD,
    )
    nutrition_matches = all(
        abs(
            float(recipe.get(field) or 0)
            - float(calculated["per_serving"][field])
        ) <= 0.01
        for field in NUTRIENT_FIELDS
    )
    already_calculated = (
        abs(float(recipe.get("yield_servings") or 0) - RECIPE_YIELD)
        <= 0.001
        and nutrition_matches
        and _links_match(saved_recipe_id, ingredients)
    )

    if not already_calculated:
        result = update_saved_recipe_from_ingredients(
            saved_recipe_id,
            yield_servings=RECIPE_YIELD,
            ingredients=ingredients,
            preparation_steps=PREPARATION_STEPS,
            summary=SUMMARY,
        )
        recipe = result["recipe"]

    recipe = update_saved_recipe(
        saved_recipe_id,
        summary=SUMMARY,
        ingredients=DISPLAY_INGREDIENTS,
        preparation_steps=PREPARATION_STEPS,
        estimate_notes=ESTIMATE_NOTES,
    )
    return {
        "updated": not already_calculated,
        "recipe": recipe,
        "total_nutrition": calculated["total"],
        "per_serving_nutrition": calculated["per_serving"],
    }


def main() -> None:
    result = update_recipe()
    recipe = result["recipe"]
    action = "UPDATED" if result["updated"] else "ALREADY CURRENT"
    print(f"{action}: {recipe['canonical_name']}")
    print(f"Yield: {recipe['yield_servings']:g} burritos")
    print("Per burrito:")
    print(f"- Calories: {recipe['calories']:.0f}")
    print(f"- Protein: {recipe['protein_g']:.1f} g")
    print(f"- Carbohydrates: {recipe['carbohydrates_g']:.1f} g")
    print(f"- Fat: {recipe['fat_g']:.1f} g")
    print(f"- Fiber: {recipe['fiber_g']:.1f} g")
    print(f"- Sugar: {recipe['sugar_g']:.1f} g")
    print(f"- Sodium: {recipe['sodium_mg']:.0f} mg")
    print("Previously logged meals were not changed.")


if __name__ == "__main__":
    main()
