from __future__ import annotations

import json
from typing import Any

from food.database import (
    DATABASE_PATH,
    current_timestamp,
    get_connection,
    initialize_database,
)
from food.library import add_food_with_nutrition


NUTRIENT_FIELDS = (
    "calories",
    "protein_g",
    "carbohydrates_g",
    "fat_g",
    "fiber_g",
    "sugar_g",
    "sodium_mg",
)


def normalize_meal_type(value: str) -> str:
    meal_type = str(value or "").strip().lower()
    if meal_type not in {"lunch", "dinner"}:
        raise ValueError("meal_type must be lunch or dinner.")
    return meal_type


def validate_recipe_idea(idea: dict[str, Any]) -> dict[str, Any]:
    name = str(idea.get("name") or "").strip()
    if not name:
        raise ValueError("Recipe name is required.")

    ingredients = list(idea.get("ingredients") or [])
    if not ingredients:
        raise ValueError("At least one recipe ingredient is required.")

    cleaned_ingredients = []
    for ingredient in ingredients:
        ingredient_name = str(
            ingredient.get("name") or ""
        ).strip()
        amount = str(
            ingredient.get("amount") or ""
        ).strip()
        source = str(
            ingredient.get("source") or "additional"
        ).strip().lower()

        if not ingredient_name or not amount:
            raise ValueError(
                "Every recipe ingredient needs a name and amount."
            )
        if source not in {"pantry", "additional"}:
            raise ValueError("Invalid recipe ingredient source.")

        cleaned_ingredients.append({
            "name": ingredient_name,
            "amount": amount,
            "source": source,
        })

    steps = [
        str(step).strip()
        for step in idea.get("preparation_steps") or []
        if str(step).strip()
    ]
    if not steps:
        raise ValueError("At least one preparation step is required.")

    nutrition = {}
    for field in NUTRIENT_FIELDS:
        value = float(idea.get(field) or 0)
        if value < 0:
            raise ValueError(f"{field} cannot be negative.")
        nutrition[field] = value

    if nutrition["calories"] <= 0:
        raise ValueError("Recipe calories must be greater than zero.")

    return {
        "name": name,
        "summary": str(idea.get("summary") or "").strip(),
        "ingredients": cleaned_ingredients,
        "preparation_steps": steps,
        "estimate_notes": str(
            idea.get("estimate_notes") or ""
        ).strip(),
        **nutrition,
    }


def decode_saved_recipe(row: Any) -> dict[str, Any]:
    result = dict(row)
    try:
        result["ingredients"] = json.loads(
            result.pop("ingredients_json")
        )
        result["preparation_steps"] = json.loads(
            result.pop("preparation_steps_json")
        )
    except (TypeError, ValueError, json.JSONDecodeError) as exc:
        raise RuntimeError("Saved Recipe data is unreadable.") from exc
    return result


def recipe_select_sql() -> str:
    return """
        SELECT
            saved_recipes.saved_recipe_id,
            saved_recipes.food_id,
            saved_recipes.meal_type,
            saved_recipes.summary,
            saved_recipes.ingredients_json,
            saved_recipes.preparation_steps_json,
            saved_recipes.estimate_notes,
            saved_recipes.created_at,
            saved_recipes.updated_at,
            foods.canonical_name,
            foods.serving_description,
            foods.verification_status,
            foods.verification_source,
            nutrition_versions.nutrition_version_id,
            nutrition_versions.version_number,
            nutrition_versions.calories,
            nutrition_versions.protein_g,
            nutrition_versions.carbohydrates_g,
            nutrition_versions.fat_g,
            nutrition_versions.fiber_g,
            nutrition_versions.sugar_g,
            nutrition_versions.sodium_mg
        FROM saved_recipes
        JOIN foods
          ON foods.food_id = saved_recipes.food_id
        JOIN nutrition_versions
          ON nutrition_versions.nutrition_version_id =
             foods.active_nutrition_version_id
    """


def save_pantry_meal_idea(
    idea: dict[str, Any],
    *,
    meal_type: str,
) -> dict[str, Any]:
    """Save one Pantry idea without logging it as eaten."""
    initialize_database()
    cleaned = validate_recipe_idea(idea)
    normalized_meal = normalize_meal_type(meal_type)

    created_food = add_food_with_nutrition(
        canonical_name=cleaned["name"],
        serving_description="1 saved recipe serving",
        serving_amount=1.0,
        serving_unit="serving",
        verification_status="estimated",
        verification_source="pantry_meal_idea",
        calories=cleaned["calories"],
        protein_g=cleaned["protein_g"],
        carbohydrates_g=cleaned["carbohydrates_g"],
        fat_g=cleaned["fat_g"],
        fiber_g=cleaned["fiber_g"],
        sugar_g=cleaned["sugar_g"],
        sodium_mg=cleaned["sodium_mg"],
        food_type="recipe",
    )
    food = dict(created_food["food"])
    if food.get("food_type") != "recipe":
        raise ValueError(
            "A non-recipe Food already uses this recipe identity."
        )

    food_id = int(food["food_id"])
    timestamp = current_timestamp()

    with get_connection(DATABASE_PATH) as connection:
        existing = connection.execute(
            """
            SELECT saved_recipe_id
            FROM saved_recipes
            WHERE food_id = ?
            LIMIT 1
            """,
            (food_id,),
        ).fetchone()

        if existing is None:
            cursor = connection.execute(
                """
                INSERT INTO saved_recipes (
                    food_id,
                    meal_type,
                    summary,
                    ingredients_json,
                    preparation_steps_json,
                    estimate_notes,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    food_id,
                    normalized_meal,
                    cleaned["summary"],
                    json.dumps(cleaned["ingredients"]),
                    json.dumps(cleaned["preparation_steps"]),
                    cleaned["estimate_notes"],
                    timestamp,
                    timestamp,
                ),
            )
            saved_recipe_id = int(cursor.lastrowid)
            was_created = True
        else:
            saved_recipe_id = int(existing["saved_recipe_id"])
            was_created = False

        connection.commit()

    return {
        "created": was_created,
        "recipe": get_saved_recipe(saved_recipe_id),
    }


def get_saved_recipe(saved_recipe_id: int) -> dict[str, Any] | None:
    initialize_database()
    with get_connection(DATABASE_PATH) as connection:
        row = connection.execute(
            recipe_select_sql()
            + """
            WHERE saved_recipes.saved_recipe_id = ?
            LIMIT 1
            """,
            (int(saved_recipe_id),),
        ).fetchone()
    return decode_saved_recipe(row) if row else None


def list_saved_recipes() -> list[dict[str, Any]]:
    initialize_database()
    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            recipe_select_sql()
            + """
            ORDER BY
                lower(foods.canonical_name),
                saved_recipes.saved_recipe_id
            """
        ).fetchall()
    return [decode_saved_recipe(row) for row in rows]
