from __future__ import annotations

import json
from typing import Any

from food.database import (
    DATABASE_PATH,
    build_search_key,
    current_timestamp,
    get_connection,
    initialize_database,
    normalize_key_part,
)
from food.library import (
    add_food_with_nutrition,
    add_user_nutrition_version,
)


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

    heart_healthy_pick = bool(
        idea.get("heart_healthy_pick")
    )
    heart_healthy_reason = str(
        idea.get("heart_healthy_reason") or ""
    ).strip()
    if heart_healthy_pick and not heart_healthy_reason:
        raise ValueError(
            "A Heart-Healthy Pick requires an explanation."
        )
    if not heart_healthy_pick:
        heart_healthy_reason = ""

    return {
        "name": name,
        "summary": str(idea.get("summary") or "").strip(),
        "ingredients": cleaned_ingredients,
        "preparation_steps": steps,
        "estimate_notes": str(
            idea.get("estimate_notes") or ""
        ).strip(),
        "heart_healthy_pick": heart_healthy_pick,
        "heart_healthy_reason": heart_healthy_reason,
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
            saved_recipes.heart_healthy_pick,
            saved_recipes.heart_healthy_reason,
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
                    heart_healthy_pick,
                    heart_healthy_reason,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    food_id,
                    normalized_meal,
                    cleaned["summary"],
                    json.dumps(cleaned["ingredients"]),
                    json.dumps(cleaned["preparation_steps"]),
                    cleaned["estimate_notes"],
                    int(cleaned["heart_healthy_pick"]),
                    cleaned["heart_healthy_reason"],
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


def update_saved_recipe(
    saved_recipe_id: int,
    *,
    name: str | None = None,
    meal_type: str | None = None,
    summary: str | None = None,
    ingredients: list[dict[str, Any]] | None = None,
    preparation_steps: list[str] | None = None,
    estimate_notes: str | None = None,
) -> dict[str, Any]:
    """Update recipe instructions without changing logged history."""
    initialize_database()
    existing = get_saved_recipe(int(saved_recipe_id))
    if existing is None:
        raise ValueError("Saved Recipe was not found.")

    candidate = {
        "name": existing["canonical_name"] if name is None else name,
        "summary": existing["summary"] if summary is None else summary,
        "ingredients": (
            existing["ingredients"]
            if ingredients is None
            else ingredients
        ),
        "preparation_steps": (
            existing["preparation_steps"]
            if preparation_steps is None
            else preparation_steps
        ),
        "estimate_notes": (
            existing["estimate_notes"]
            if estimate_notes is None
            else estimate_notes
        ),
        "heart_healthy_pick": (
            False
            if ingredients is not None
            else bool(existing.get("heart_healthy_pick"))
        ),
        "heart_healthy_reason": (
            ""
            if ingredients is not None
            else str(existing.get("heart_healthy_reason") or "")
        ),
        **{
            field: existing.get(field)
            for field in NUTRIENT_FIELDS
        },
    }
    cleaned = validate_recipe_idea(candidate)
    normalized_meal = normalize_meal_type(
        existing["meal_type"] if meal_type is None else meal_type
    )
    timestamp = current_timestamp()
    food_id = int(existing["food_id"])

    with get_connection(DATABASE_PATH) as connection:
        food = connection.execute(
            "SELECT * FROM foods WHERE food_id = ?",
            (food_id,),
        ).fetchone()
        recipe = connection.execute(
            "SELECT saved_recipe_id FROM saved_recipes "
            "WHERE saved_recipe_id = ? AND food_id = ?",
            (int(saved_recipe_id), food_id),
        ).fetchone()
        if food is None or recipe is None:
            raise ValueError("Saved Recipe was not found.")

        new_search_key = build_search_key(
            canonical_name=cleaned["name"],
            serving_description=food["serving_description"],
            brand=food["brand"],
            restaurant=food["restaurant"],
        )
        conflict = connection.execute(
            "SELECT food_id FROM foods "
            "WHERE lower(search_key) = lower(?) AND food_id != ?",
            (new_search_key, food_id),
        ).fetchone()
        if conflict is not None:
            raise ValueError(
                "Another saved food or recipe already uses that name."
            )

        connection.execute(
            """
            UPDATE foods
            SET canonical_name = ?, search_key = ?, updated_at = ?
            WHERE food_id = ?
            """,
            (cleaned["name"], new_search_key, timestamp, food_id),
        )
        normalized_alias = normalize_key_part(cleaned["name"])
        connection.execute(
            """
            INSERT INTO food_aliases (
                food_id, alias_text, normalized_alias,
                created_at, updated_at
            )
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(food_id, normalized_alias)
            DO UPDATE SET
                alias_text = excluded.alias_text,
                updated_at = excluded.updated_at
            """,
            (
                food_id,
                cleaned["name"],
                normalized_alias,
                timestamp,
                timestamp,
            ),
        )
        connection.execute(
            """
            UPDATE saved_recipes
            SET meal_type = ?,
                summary = ?,
                ingredients_json = ?,
                preparation_steps_json = ?,
                estimate_notes = ?,
                heart_healthy_pick = ?,
                heart_healthy_reason = ?,
                updated_at = ?
            WHERE saved_recipe_id = ?
            """,
            (
                normalized_meal,
                cleaned["summary"],
                json.dumps(cleaned["ingredients"]),
                json.dumps(cleaned["preparation_steps"]),
                cleaned["estimate_notes"],
                int(cleaned["heart_healthy_pick"]),
                cleaned["heart_healthy_reason"],
                timestamp,
                int(saved_recipe_id),
            ),
        )
        connection.commit()

    updated = get_saved_recipe(int(saved_recipe_id))
    if updated is None:
        raise RuntimeError("Saved Recipe update could not be verified.")
    return updated


def update_saved_recipe_nutrition(
    saved_recipe_id: int,
    *,
    calories: float,
    protein_g: float,
    carbohydrates_g: float,
    fat_g: float,
    fiber_g: float,
    sugar_g: float,
    sodium_mg: float,
) -> dict[str, Any]:
    """Create a new estimated nutrition version for future logs."""
    recipe = get_saved_recipe(int(saved_recipe_id))
    if recipe is None:
        raise ValueError("Saved Recipe was not found.")

    add_user_nutrition_version(
        food_id=int(recipe["food_id"]),
        calories=calories,
        protein_g=protein_g,
        carbohydrates_g=carbohydrates_g,
        fat_g=fat_g,
        fiber_g=fiber_g,
        sugar_g=sugar_g,
        sodium_mg=sodium_mg,
        verification_status="estimated",
        verification_source="saved_recipe_edit",
    )
    with get_connection(DATABASE_PATH) as connection:
        connection.execute(
            """
            UPDATE saved_recipes
            SET heart_healthy_pick = 0,
                heart_healthy_reason = '',
                updated_at = ?
            WHERE saved_recipe_id = ?
            """,
            (current_timestamp(), int(saved_recipe_id)),
        )
        connection.commit()
    updated = get_saved_recipe(int(saved_recipe_id))
    if updated is None:
        raise RuntimeError("Saved Recipe nutrition could not be verified.")
    return updated


def delete_saved_recipe(saved_recipe_id: int) -> dict[str, Any]:
    """
    Remove a recipe from the library while preserving its Food history.
    """
    initialize_database()
    existing = get_saved_recipe(int(saved_recipe_id))
    if existing is None:
        raise ValueError("Saved Recipe was not found.")

    with get_connection(DATABASE_PATH) as connection:
        cursor = connection.execute(
            "DELETE FROM saved_recipes WHERE saved_recipe_id = ?",
            (int(saved_recipe_id),),
        )
        if cursor.rowcount != 1:
            raise RuntimeError("Saved Recipe was not deleted.")
        connection.commit()

    return existing
