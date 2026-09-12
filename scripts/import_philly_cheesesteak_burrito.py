#!/usr/bin/env python3
"""Create the confirmed ten-serving Philly Cheesesteak Burrito recipe."""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any


PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from food.library import add_food_with_nutrition
from food.recipes import (
    create_saved_recipe_from_ingredients,
    list_saved_recipes,
    prepare_recipe_ingredient,
    update_saved_recipe,
)


RECIPE_NAME = "Philly Cheesesteak Burrito"
RECIPE_YIELD = 10.0

# Package-label values supplied by the user, plus clearly identified official
# manufacturer values for the beef, cream cheese, and Worcestershire sauce.
FOODS = (
    {
        "canonical_name": "96/4 Lean Ground Beef, Raw",
        "brand": "Laura's Lean",
        "serving_description": "4 oz (112 g)",
        "serving_amount": 4.0,
        "serving_unit": "oz",
        "calories": 140.0, "protein_g": 23.0, "carbohydrates_g": 0.0,
        "fat_g": 4.5, "fiber_g": 0.0, "sugar_g": 0.0, "sodium_mg": 75.0,
        "verification_source": "lauraslean.com",
        "source_url": "https://www.lauraslean.com/products/all-natural/lean-ground-beef-96",
    },
    {
        "canonical_name": "Mission Flour Tortilla",
        "brand": "Mission",
        "serving_description": "1 tortilla (49 g)",
        "serving_amount": 1.0,
        "serving_unit": "tortilla",
        "calories": 140.0, "protein_g": 4.0, "carbohydrates_g": 24.0,
        "fat_g": 3.0, "fiber_g": 1.0, "sugar_g": 2.0, "sodium_mg": 410.0,
        "verification_source": "user_package_label",
        "source_url": None,
    },
    {
        "canonical_name": "Philadelphia Reduced Fat Cream Cheese",
        "brand": "Philadelphia",
        "serving_description": "2 tbsp (31 g)",
        "serving_amount": 31.0,
        "serving_unit": "g",
        "calories": 60.0, "protein_g": 3.0, "carbohydrates_g": 2.0,
        "fat_g": 5.0, "fiber_g": 0.0, "sugar_g": 2.0, "sodium_mg": 120.0,
        "verification_source": "kraftheinz.com",
        "source_url": "https://www.kraftheinz.com/philadelphia/products/00021000612178-reduced-fat-cream-cheese-spread-with-a-third-less-fat",
    },
    {
        "canonical_name": "Sargento Smoked Provolone",
        "brand": "Sargento",
        "serving_description": "1 slice (19 g)",
        "serving_amount": 1.0,
        "serving_unit": "slice",
        "calories": 70.0, "protein_g": 5.0, "carbohydrates_g": 0.0,
        "fat_g": 5.0, "fiber_g": 0.0, "sugar_g": 0.0, "sodium_mg": 130.0,
        "verification_source": "user_package_label",
        "source_url": None,
    },
    {
        "canonical_name": "Kraft 100% Grated Parmesan Cheese",
        "brand": "Kraft",
        "serving_description": "2 tsp (5 g)",
        "serving_amount": 5.0,
        "serving_unit": "g",
        "calories": 20.0, "protein_g": 2.0, "carbohydrates_g": 0.0,
        "fat_g": 1.5, "fiber_g": 0.0, "sugar_g": 0.0, "sodium_mg": 80.0,
        "verification_source": "user_package_label",
        "source_url": None,
    },
    {
        "canonical_name": "Great Value Nonfat Plain Greek Yogurt",
        "brand": "Great Value",
        "serving_description": "2/3 cup (170 g)",
        "serving_amount": 170.0,
        "serving_unit": "g",
        "calories": 100.0, "protein_g": 17.0, "carbohydrates_g": 7.0,
        "fat_g": 0.0, "fiber_g": 0.0, "sugar_g": 7.0, "sodium_mg": 50.0,
        "verification_source": "user_package_label",
        "source_url": None,
    },
    {
        "canonical_name": "Great Value Light Mayonnaise",
        "brand": "Great Value",
        "serving_description": "1 tbsp (15 g)",
        "serving_amount": 15.0,
        "serving_unit": "g",
        "calories": 35.0, "protein_g": 0.0, "carbohydrates_g": 1.0,
        "fat_g": 3.5, "fiber_g": 0.0, "sugar_g": 0.0, "sodium_mg": 110.0,
        "verification_source": "user_package_label",
        "source_url": None,
    },
    {
        "canonical_name": "Raw Bell Pepper",
        "brand": None,
        "serving_description": "100 g",
        "serving_amount": 100.0,
        "serving_unit": "g",
        "calories": 20.0, "protein_g": 0.86, "carbohydrates_g": 4.64,
        "fat_g": 0.17, "fiber_g": 1.7, "sugar_g": 2.4, "sodium_mg": 3.0,
        "verification_source": "USDA FoodData Central",
        "source_url": "https://fdc.nal.usda.gov/",
    },
    {
        "canonical_name": "Raw Onion",
        "brand": None,
        "serving_description": "100 g",
        "serving_amount": 100.0,
        "serving_unit": "g",
        "calories": 40.0, "protein_g": 1.1, "carbohydrates_g": 9.34,
        "fat_g": 0.1, "fiber_g": 1.7, "sugar_g": 4.24, "sodium_mg": 4.0,
        "verification_source": "USDA FoodData Central",
        "source_url": "https://fdc.nal.usda.gov/",
    },
    {
        "canonical_name": "Lea & Perrins Worcestershire Sauce",
        "brand": "Lea & Perrins",
        "serving_description": "1 tsp (5 mL)",
        "serving_amount": 5.0,
        "serving_unit": "g",
        "calories": 5.0, "protein_g": 0.0, "carbohydrates_g": 1.0,
        "fat_g": 0.0, "fiber_g": 0.0, "sugar_g": 1.0, "sodium_mg": 65.0,
        "verification_source": "kraftheinz.com",
        "source_url": "https://www.kraftheinz.com/lea-perrins/products/00051600002208-the-original-worcestershire-sauce",
    },
)

AMOUNTS = (
    "48 oz", "10 tortillas", "100 g", "10 slices", "30 g",
    "300 g", "60 g", "8 oz", "8 oz", "20 g",
)

PREPARATION = [
    "Brown the 96/4 ground beef with the chopped onion and bell pepper; season to taste and cook the beef to 160°F.",
    "Blend the Greek yogurt, light mayonnaise, Parmesan, Worcestershire sauce, seasonings, and a little water until smooth.",
    "Stir the reduced-fat cream cheese and five chopped provolone slices into the cooked beef mixture until melted.",
    "Divide the filling among ten tortillas. Top each with half a provolone slice and the prepared sauce, then roll tightly.",
    "Toast each burrito on both sides until golden and heated through.",
]


def create_recipe() -> dict[str, Any]:
    existing = next((r for r in list_saved_recipes() if str(r.get("canonical_name", "")).casefold() == RECIPE_NAME.casefold()), None)
    if existing:
        return {"created": False, "recipe": existing}

    food_ids = []
    for spec in FOODS:
        result = add_food_with_nutrition(
            restaurant=None, food_type="food", verification_status="verified",
            source_item_id=None, **spec,
        )
        food_ids.append(int(result["food"]["food_id"]))

    ingredients = [
        prepare_recipe_ingredient(food_id=food_id, amount_description=amount)
        for food_id, amount in zip(food_ids, AMOUNTS)
    ]
    result = create_saved_recipe_from_ingredients(
        name=RECIPE_NAME, meal_type="dinner", yield_servings=RECIPE_YIELD,
        ingredients=ingredients,
        summary="Ten Philly cheesesteak burritos with extra-lean beef, peppers, onions, creamy sauce, and provolone.",
        preparation_steps=PREPARATION,
    )
    recipe = update_saved_recipe(
        int(result["recipe"]["saved_recipe_id"]),
        ingredients=[
            {"name": spec["canonical_name"], "amount": amount, "source": "saved_food"}
            for spec, amount in zip(FOODS, AMOUNTS)
        ] + [
            {"name": "garlic powder, onion powder, salt, paprika, pepper, and water", "amount": "to taste", "source": "additional"}
        ],
        estimate_notes=(
            "Calculated from the uploaded package labels, Laura's Lean official 96/4 beef label, "
            "official Philadelphia reduced-fat cream cheese and Lea & Perrins labels, and USDA produce values. "
            "The uploaded Mission 140-calorie flour tortilla is used; unspecified seasonings and water are excluded."
        ),
    )
    return {**result, "created": True, "recipe": recipe}


def main() -> None:
    result = create_recipe()
    recipe = result["recipe"]
    action = "CREATED" if result["created"] else "ALREADY EXISTS"
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
    print("Nothing was logged as eaten.")


if __name__ == "__main__":
    main()
