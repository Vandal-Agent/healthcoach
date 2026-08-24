#!/usr/bin/env python3
"""Create the confirmed 10-serving Chicken Pizza Burritos recipe."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from food.library import add_food_with_nutrition, find_food
from food.recipes import (
    create_saved_recipe_from_ingredients,
    list_saved_recipes,
    prepare_recipe_ingredient,
    update_saved_recipe,
)
from scripts.import_chicken_pizza_burrito_staples import (
    STAPLES,
    import_staples,
)


RECIPE_NAME = "Chicken Pizza Burritos"
RECIPE_YIELD = 10.0

RECIPE_AMOUNTS = {
    "Chicken Breast, Boneless Skinless, Raw": "1700 g",
    "Kirkland Light Tasting Olive Oil": "1 tsp",
    "Hormel Original Pepperoni": "200 g",
    "Kroger Pizza Sauce": "350 g",
    "Kraft 100% Grated Parmesan Cheese": "80 g",
    "Garlic, Raw": "5 cloves",
    "Great Value Cream Cheese": "200 g",
    "Mission Flour Tortilla, Soft Taco": "10 tortillas",
    "Sargento Reduced Fat Mozzarella": "250 g",
    "Kirkland Mexican Style Blend": "200 g",
    "Table Salt": "1 tsp",
}

DISPLAY_INGREDIENTS = [
    {
        "name": "boneless, skinless chicken breast, raw",
        "amount": "1.7 kg",
        "source": "saved_food",
    },
    {"name": "table salt", "amount": "1 tsp", "source": "saved_food"},
    {"name": "garlic powder", "amount": "1 tsp", "source": "additional"},
    {"name": "onion powder", "amount": "1 tsp", "source": "additional"},
    {"name": "smoked paprika", "amount": "1 tsp", "source": "additional"},
    {
        "name": "Kirkland Light Tasting Olive Oil",
        "amount": "1 tsp",
        "source": "saved_food",
    },
    {
        "name": "Hormel Original Pepperoni",
        "amount": "200 g",
        "source": "saved_food",
    },
    {
        "name": "Kroger Pizza Sauce",
        "amount": "350 g",
        "source": "saved_food",
    },
    {
        "name": "Kraft 100% Grated Parmesan Cheese",
        "amount": "80 g",
        "source": "saved_food",
    },
    {"name": "garlic, diced", "amount": "5 cloves", "source": "saved_food"},
    {
        "name": "Great Value Cream Cheese",
        "amount": "200 g",
        "source": "saved_food",
    },
    {
        "name": "Italian herb seasoning mix",
        "amount": "1.5 tsp",
        "source": "additional",
    },
    {
        "name": "Mission Flour Tortillas, Soft Taco",
        "amount": "10 tortillas",
        "source": "saved_food",
    },
    {
        "name": "Sargento Reduced Fat Mozzarella",
        "amount": "250 g total (25 g per burrito)",
        "source": "saved_food",
    },
    {
        "name": "Kirkland Mexican Style Blend",
        "amount": "200 g total (20 g per burrito)",
        "source": "saved_food",
    },
    {
        "name": "fresh parsley, optional garnish",
        "amount": "as desired",
        "source": "additional",
    },
]

PREPARATION_STEPS = [
    (
        "Cut the chicken into small pieces. Season it with the salt, "
        "garlic powder, onion powder, smoked paprika, and Italian herbs."
    ),
    (
        "Heat the olive oil in a large skillet. Cook the chicken until it "
        "reaches 165°F in the thickest pieces."
    ),
    (
        "Add the diced garlic, pepperoni, pizza sauce, Parmesan, and cream "
        "cheese. Stir over low heat until evenly combined and hot."
    ),
    (
        "Divide the filling evenly among 10 tortillas. Add 25 g of reduced-"
        "fat mozzarella inside each tortilla and roll into burritos."
    ),
    (
        "Place the burritos seam-side down, distribute 20 g of Kirkland "
        "Mexican Style Blend over the outside of each burrito, and heat "
        "until the tortillas are warm and the cheese has melted."
    ),
    "Garnish with fresh parsley if desired and serve.",
]


def _staple_food_ids() -> dict[str, int]:
    results: dict[str, int] = {}
    for item in STAPLES:
        food = find_food(
            canonical_name=item["canonical_name"],
            serving_description=item["serving_description"],
            brand=item["brand"],
            restaurant=None,
        )
        if food is None:
            raise RuntimeError(
                f"Stopped safely: missing Saved Food {item['canonical_name']}."
            )
        results[item["canonical_name"]] = int(food["food_id"])
    return results


def _add_or_reuse_table_salt() -> int:
    result = add_food_with_nutrition(
        canonical_name="Table Salt",
        serving_description="1 tsp (6 g)",
        serving_amount=1.0,
        serving_unit="tsp",
        brand=None,
        restaurant=None,
        food_type="food",
        verification_status="verified",
        verification_source="fdc.nal.usda.gov",
        source_item_id="usda-table-salt",
        source_url="https://fdc.nal.usda.gov/",
        calories=0.0,
        protein_g=0.0,
        carbohydrates_g=0.0,
        fat_g=0.0,
        fiber_g=0.0,
        sugar_g=0.0,
        sodium_mg=2325.0,
    )
    nutrition = result.get("nutrition") or {}
    if (
        float(nutrition.get("calories") or 0) != 0
        or abs(float(nutrition.get("sodium_mg") or 0) - 2325.0) > 0.01
    ):
        raise RuntimeError(
            "Stopped safely: existing Table Salt nutrition conflicts."
        )
    return int(result["food"]["food_id"])


def create_recipe() -> dict[str, Any]:
    """Create the recipe once without replacing any existing recipe."""
    existing = next(
        (
            recipe
            for recipe in list_saved_recipes()
            if str(recipe.get("canonical_name") or "").strip().lower()
            == RECIPE_NAME.lower()
        ),
        None,
    )
    if existing is not None:
        return {"created": False, "recipe": existing}

    import_staples()
    food_ids = _staple_food_ids()
    food_ids["Table Salt"] = _add_or_reuse_table_salt()

    linked_ingredients = [
        prepare_recipe_ingredient(
            food_id=food_ids[name],
            amount_description=amount,
        )
        for name, amount in RECIPE_AMOUNTS.items()
    ]
    result = create_saved_recipe_from_ingredients(
        name=RECIPE_NAME,
        meal_type="dinner",
        yield_servings=RECIPE_YIELD,
        ingredients=linked_ingredients,
        summary=(
            "Ten high-protein chicken pizza burritos with pepperoni, pizza "
            "sauce, cream cheese, Parmesan, mozzarella, and Mexican blend."
        ),
        preparation_steps=PREPARATION_STEPS,
    )
    recipe = result["recipe"]
    recipe = update_saved_recipe(
        int(recipe["saved_recipe_id"]),
        ingredients=DISPLAY_INGREDIENTS,
        estimate_notes=(
            "Nutrition is calculated from the exact saved versions of all "
            "major ingredients and generic USDA table salt. Trace nutrition "
            "from garlic powder, onion powder, smoked paprika, Italian herbs, "
            "and optional parsley is not included."
        ),
    )
    return {
        **result,
        "created": True,
        "recipe": recipe,
    }


def main() -> None:
    result = create_recipe()
    recipe = result["recipe"]
    if not result.get("created"):
        print(
            f"Stopped safely: {RECIPE_NAME} already exists and was not "
            "overwritten."
        )
        return

    print(f"CREATED: {recipe['canonical_name']}")
    print(f"Yield: {recipe['yield_servings']:g} burritos")
    print("Per burrito:")
    print(f"- Calories: {recipe['calories']:.0f}")
    print(f"- Protein: {recipe['protein_g']:.1f} g")
    print(f"- Carbohydrates: {recipe['carbohydrates_g']:.1f} g")
    print(f"- Fat: {recipe['fat_g']:.1f} g")
    print(f"- Fiber: {recipe['fiber_g']:.1f} g")
    print(f"- Sugar: {recipe['sugar_g']:.1f} g")
    print(f"- Sodium: {recipe['sodium_mg']:.0f} mg")
    print("Nothing was logged as eaten.")


if __name__ == "__main__":
    main()
