from __future__ import annotations

import json
import re
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
from food.pantry import list_pantry_items
from food.resolver import is_trusted_saved_food, resolve_food


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
        if source not in {"pantry", "additional", "saved_food"}:
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
            saved_recipes.yield_servings,
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


def _save_recipe_idea(
    idea: dict[str, Any],
    *,
    meal_type: str,
    yield_servings: float,
    verification_source: str,
    linked_ingredients: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Save one recipe and optional version-linked ingredients."""
    initialize_database()
    cleaned = validate_recipe_idea(idea)
    normalized_meal = normalize_meal_type(meal_type)
    numeric_yield = float(yield_servings)
    if numeric_yield <= 0 or numeric_yield > 100:
        raise ValueError(
            "Recipe yield must be greater than 0 and no more than 100."
        )

    created_food = add_food_with_nutrition(
        canonical_name=cleaned["name"],
        serving_description="1 saved recipe serving",
        serving_amount=1.0,
        serving_unit="serving",
        verification_status="estimated",
        verification_source=verification_source,
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

    if not created_food["created"]:
        with get_connection(DATABASE_PATH) as connection:
            existing_recipe = connection.execute(
                """
                SELECT saved_recipe_id
                FROM saved_recipes
                WHERE food_id = ?
                LIMIT 1
                """,
                (food_id,),
            ).fetchone()

        if existing_recipe is None:
            add_user_nutrition_version(
                food_id=food_id,
                verification_status="estimated",
                verification_source=verification_source,
                calories=cleaned["calories"],
                protein_g=cleaned["protein_g"],
                carbohydrates_g=cleaned["carbohydrates_g"],
                fat_g=cleaned["fat_g"],
                fiber_g=cleaned["fiber_g"],
                sugar_g=cleaned["sugar_g"],
                sodium_mg=cleaned["sodium_mg"],
            )

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
                    yield_servings,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                    numeric_yield,
                    timestamp,
                    timestamp,
                ),
            )
            saved_recipe_id = int(cursor.lastrowid)
            was_created = True

            for position, ingredient in enumerate(
                linked_ingredients or [],
                start=1,
            ):
                connection.execute(
                    """
                    INSERT INTO saved_recipe_ingredients (
                        saved_recipe_id,
                        position,
                        food_id,
                        nutrition_version_id,
                        amount_description,
                        serving_multiplier,
                        created_at,
                        updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        saved_recipe_id,
                        position,
                        int(ingredient["food_id"]),
                        int(ingredient["nutrition_version_id"]),
                        str(ingredient["amount_description"]).strip(),
                        float(ingredient["serving_multiplier"]),
                        timestamp,
                        timestamp,
                    ),
                )
        else:
            saved_recipe_id = int(existing["saved_recipe_id"])
            was_created = False

        connection.commit()

    return {
        "created": was_created,
        "recipe": get_saved_recipe(saved_recipe_id),
    }


def save_pantry_meal_idea(
    idea: dict[str, Any],
    *,
    meal_type: str,
) -> dict[str, Any]:
    """Save one Pantry idea without logging it as eaten."""
    return _save_recipe_idea(
        idea,
        meal_type=meal_type,
        yield_servings=1.0,
        verification_source="pantry_meal_idea",
    )


def _parse_amount_number(value: str) -> float:
    cleaned = str(value or "").strip()
    mixed = re.fullmatch(r"(\d+)\s+(\d+)\/(\d+)", cleaned)
    if mixed:
        denominator = int(mixed.group(3))
        if denominator == 0:
            raise ValueError("Ingredient amount cannot divide by zero.")
        return float(mixed.group(1)) + (
            float(mixed.group(2)) / denominator
        )

    fraction = re.fullmatch(r"(\d+)\/(\d+)", cleaned)
    if fraction:
        denominator = int(fraction.group(2))
        if denominator == 0:
            raise ValueError("Ingredient amount cannot divide by zero.")
        return float(fraction.group(1)) / denominator

    return float(cleaned)


def _normalize_amount_unit(value: str) -> str:
    unit = re.sub(r"\s+", " ", str(value or "").strip().lower())
    aliases = {
        "servings": "serving",
        "g": "gram",
        "grams": "gram",
        "grm": "gram",
        "oz": "ounce",
        "ounces": "ounce",
        "fl oz": "fluid ounce",
        "floz": "fluid ounce",
        "fluid ounces": "fluid ounce",
    }
    unit = aliases.get(unit, unit)
    if unit.endswith("s") and len(unit) > 1:
        unit = unit[:-1]
    return unit


def _whole_item_unit_tokens(value: str) -> set[str]:
    """Return meaningful tokens from a whole-item serving description."""
    cleaned = re.sub(r"\([^)]*\)", " ", str(value or "").lower())
    cleaned = re.sub(r"[^a-z0-9]+", " ", cleaned).strip()
    tokens = {
        token[:-1] if token.endswith("s") and len(token) > 1 else token
        for token in cleaned.split()
    }
    return tokens - {
        "white",
        "yellow",
        "red",
        "green",
        "fresh",
        "raw",
        "whole",
    }


def _whole_item_units_equivalent(
    *,
    requested_unit: str,
    base_unit: str,
) -> bool:
    """Recognize a requested whole item contained in a verified serving label."""
    requested_tokens = _whole_item_unit_tokens(requested_unit)
    base_tokens = _whole_item_unit_tokens(base_unit)
    if not requested_tokens or not base_tokens:
        return False

    measurement_tokens = {
        "serving",
        "gram",
        "ounce",
        "fluid",
        "cup",
        "teaspoon",
        "tablespoon",
        "tsp",
        "tbsp",
        "milliliter",
        "liter",
        "pound",
        "lb",
        "kg",
        "ml",
    }
    if (requested_tokens | base_tokens) & measurement_tokens:
        return False

    size_tokens = {"small", "medium", "large", "extra", "jumbo"}
    requested_sizes = requested_tokens & size_tokens
    base_sizes = base_tokens & size_tokens
    if requested_sizes != base_sizes:
        return False
    if not (base_tokens - size_tokens):
        return False

    return base_tokens.issubset(requested_tokens)


def ingredient_serving_multiplier(
    *,
    amount_description: str,
    serving_amount: float,
    serving_unit: str,
) -> float:
    """Convert an explicit ingredient amount into saved-food servings."""
    match = re.fullmatch(
        r"\s*(\d+\s+\d+/\d+|\d+/\d+|\d+(?:\.\d+)?)"
        r"(?:\s+|\s*)(.*?)\s*",
        str(amount_description or ""),
    )
    if match is None:
        raise ValueError(
            "Use an amount such as 1 serving, 40 g, 3 oz, or 2 slices."
        )

    amount = _parse_amount_number(match.group(1))
    if amount <= 0:
        raise ValueError("Ingredient amount must be greater than zero.")

    base_amount = float(serving_amount)
    if base_amount <= 0:
        raise ValueError("The Saved Food serving amount is invalid.")

    requested_unit = _normalize_amount_unit(match.group(2) or "serving")
    base_unit = _normalize_amount_unit(serving_unit)

    if requested_unit == "serving":
        return amount

    if requested_unit == base_unit:
        return amount / base_amount

    if _whole_item_units_equivalent(
        requested_unit=requested_unit,
        base_unit=base_unit,
    ):
        return amount / base_amount

    weight_units = {"gram", "ounce"}
    if requested_unit in weight_units and base_unit in weight_units:
        amount_grams = (
            amount * 28.349523125
            if requested_unit == "ounce"
            else amount
        )
        base_grams = (
            base_amount * 28.349523125
            if base_unit == "ounce"
            else base_amount
        )
        return amount_grams / base_grams

    raise ValueError(
        "That amount cannot be converted safely to this Saved Food's "
        f"serving ({serving_amount:g} {serving_unit})."
    )


def list_recipe_ingredient_foods() -> list[dict[str, Any]]:
    """List trusted, non-recipe Foods available to Recipe Builder."""
    initialize_database()
    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT
                foods.*,
                nutrition_versions.nutrition_version_id,
                nutrition_versions.version_number,
                nutrition_versions.calories,
                nutrition_versions.protein_g,
                nutrition_versions.carbohydrates_g,
                nutrition_versions.fat_g,
                nutrition_versions.fiber_g,
                nutrition_versions.sugar_g,
                nutrition_versions.sodium_mg
            FROM foods
            JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id =
                 foods.active_nutrition_version_id
            WHERE foods.food_type != 'recipe'
            ORDER BY lower(foods.canonical_name), foods.food_id
            """
        ).fetchall()
    return [
        dict(row)
        for row in rows
        if is_trusted_saved_food(dict(row))
    ]


def list_recipe_pantry_foods() -> list[dict[str, Any]]:
    """List Pantry items and whether each is ready for Recipe Builder."""
    results: list[dict[str, Any]] = []

    for pantry_item in list_pantry_items():
        item = dict(pantry_item)
        missing_nutrients = [
            field
            for field in NUTRIENT_FIELDS
            if item.get(field) is None
        ]
        nutrition_ready = bool(
            item.get("food_id") is not None
            and item.get("food_type") != "recipe"
            and is_trusted_saved_food(item)
            and not missing_nutrients
        )
        item["missing_nutrients"] = missing_nutrients
        item["nutrition_ready"] = nutrition_ready
        results.append(item)

    return results


def find_recipe_ingredient_food(name: str) -> dict[str, Any] | None:
    """Resolve one trusted Saved Food for Recipe Builder."""
    resolution = resolve_food(
        food_name=str(name or "").strip(),
        serving_description="standard",
        brand=None,
        restaurant=None,
    )
    if not resolution.get("found"):
        return None

    food = dict(resolution.get("food") or {})
    nutrition = dict(resolution.get("nutrition") or {})
    if food.get("food_type") == "recipe":
        return None
    return {**food, **nutrition}


def prepare_recipe_ingredient(
    *,
    food_id: int,
    amount_description: str,
    nutrition_version_id: int | None = None,
) -> dict[str, Any]:
    """Freeze one Saved Food version and calculate its contribution."""
    food = None
    if nutrition_version_id is None:
        food = next(
            (
                item
                for item in list_recipe_ingredient_foods()
                if int(item["food_id"]) == int(food_id)
            ),
            None,
        )
    else:
        initialize_database()
        with get_connection(DATABASE_PATH) as connection:
            row = connection.execute(
                """
                SELECT
                    foods.*,
                    nutrition_versions.nutrition_version_id,
                    nutrition_versions.version_number,
                    nutrition_versions.calories,
                    nutrition_versions.protein_g,
                    nutrition_versions.carbohydrates_g,
                    nutrition_versions.fat_g,
                    nutrition_versions.fiber_g,
                    nutrition_versions.sugar_g,
                    nutrition_versions.sodium_mg,
                    nutrition_versions.serving_amount AS version_serving_amount,
                    nutrition_versions.serving_unit AS version_serving_unit
                FROM foods
                JOIN nutrition_versions
                  ON nutrition_versions.food_id = foods.food_id
                WHERE foods.food_id = ?
                  AND nutrition_versions.nutrition_version_id = ?
                LIMIT 1
                """,
                (int(food_id), int(nutrition_version_id)),
            ).fetchone()
        if row is not None:
            food = dict(row)
            food["serving_amount"] = food.pop("version_serving_amount")
            food["serving_unit"] = food.pop("version_serving_unit")
            if not is_trusted_saved_food(food):
                food = None
    if food is None:
        raise ValueError("That trusted Saved Food is not available.")

    missing = [
        field
        for field in NUTRIENT_FIELDS
        if food.get(field) is None
    ]
    if missing:
        raise ValueError(
            "This Saved Food is missing: "
            + ", ".join(field.replace("_g", "").replace("_mg", "") for field in missing)
            + ". Complete its nutrition before using it in a recipe."
        )

    multiplier = ingredient_serving_multiplier(
        amount_description=amount_description,
        serving_amount=float(food["serving_amount"]),
        serving_unit=str(food["serving_unit"]),
    )
    contribution = {
        field: round(float(food[field]) * multiplier, 3)
        for field in NUTRIENT_FIELDS
    }
    return {
        "food_id": int(food["food_id"]),
        "nutrition_version_id": int(food["nutrition_version_id"]),
        "name": str(food["canonical_name"]),
        "amount_description": str(amount_description).strip(),
        "serving_multiplier": multiplier,
        "serving_description": str(food["serving_description"]),
        "nutrition": contribution,
    }


def calculate_recipe_nutrition(
    ingredients: list[dict[str, Any]],
    *,
    yield_servings: float,
) -> dict[str, dict[str, float]]:
    """Calculate recipe totals and per-serving nutrition."""
    if not ingredients:
        raise ValueError("At least one recipe ingredient is required.")
    numeric_yield = float(yield_servings)
    if numeric_yield <= 0 or numeric_yield > 100:
        raise ValueError(
            "Recipe yield must be greater than 0 and no more than 100."
        )

    totals = {field: 0.0 for field in NUTRIENT_FIELDS}
    for ingredient in ingredients:
        nutrition = dict(ingredient.get("nutrition") or {})
        for field in NUTRIENT_FIELDS:
            if nutrition.get(field) is None:
                raise ValueError(
                    f"Ingredient nutrition is missing {field}."
                )
            totals[field] += float(nutrition[field])

    totals = {field: round(value, 3) for field, value in totals.items()}
    per_serving = {
        field: round(value / numeric_yield, 3)
        for field, value in totals.items()
    }
    return {"total": totals, "per_serving": per_serving}


def validate_linked_recipe_ingredients(
    ingredients: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Reload exact ingredient versions so draft values cannot drift."""
    initialize_database()
    if not ingredients:
        raise ValueError("At least one recipe ingredient is required.")

    validated = []
    with get_connection(DATABASE_PATH) as connection:
        for ingredient in ingredients:
            row = connection.execute(
                """
                SELECT
                    foods.food_id,
                    foods.canonical_name,
                    foods.food_type,
                    foods.verification_status,
                    foods.verification_source,
                    nutrition_versions.nutrition_version_id,
                    nutrition_versions.serving_amount,
                    nutrition_versions.serving_unit,
                    nutrition_versions.calories,
                    nutrition_versions.protein_g,
                    nutrition_versions.carbohydrates_g,
                    nutrition_versions.fat_g,
                    nutrition_versions.fiber_g,
                    nutrition_versions.sugar_g,
                    nutrition_versions.sodium_mg
                FROM foods
                JOIN nutrition_versions
                  ON nutrition_versions.food_id = foods.food_id
                WHERE foods.food_id = ?
                  AND nutrition_versions.nutrition_version_id = ?
                LIMIT 1
                """,
                (
                    int(ingredient["food_id"]),
                    int(ingredient["nutrition_version_id"]),
                ),
            ).fetchone()
            if row is None:
                raise ValueError(
                    "A selected ingredient nutrition version is unavailable."
                )

            food = dict(row)
            if food.get("food_type") == "recipe" or not is_trusted_saved_food(
                food
            ):
                raise ValueError(
                    "Recipe ingredients must be trusted Saved Foods."
                )

            missing = [
                field
                for field in NUTRIENT_FIELDS
                if food.get(field) is None
            ]
            if missing:
                raise ValueError(
                    "A selected ingredient has incomplete nutrition."
                )

            amount_description = str(
                ingredient.get("amount_description") or ""
            ).strip()
            multiplier = ingredient_serving_multiplier(
                amount_description=amount_description,
                serving_amount=float(food["serving_amount"]),
                serving_unit=str(food["serving_unit"]),
            )
            validated.append({
                "food_id": int(food["food_id"]),
                "nutrition_version_id": int(
                    food["nutrition_version_id"]
                ),
                "name": str(food["canonical_name"]),
                "amount_description": amount_description,
                "serving_multiplier": multiplier,
                "nutrition": {
                    field: round(float(food[field]) * multiplier, 3)
                    for field in NUTRIENT_FIELDS
                },
            })
    return validated


def list_saved_recipe_ingredients(
    saved_recipe_id: int,
) -> list[dict[str, Any]]:
    """Return the immutable ingredient-version links for one recipe."""
    initialize_database()
    with get_connection(DATABASE_PATH) as connection:
        rows = connection.execute(
            """
            SELECT
                saved_recipe_ingredients.*,
                foods.canonical_name,
                nutrition_versions.version_number
            FROM saved_recipe_ingredients
            JOIN foods
              ON foods.food_id = saved_recipe_ingredients.food_id
            JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id =
                 saved_recipe_ingredients.nutrition_version_id
            WHERE saved_recipe_ingredients.saved_recipe_id = ?
            ORDER BY saved_recipe_ingredients.position
            """,
            (int(saved_recipe_id),),
        ).fetchall()
    return [dict(row) for row in rows]


def create_saved_recipe_from_ingredients(
    *,
    name: str,
    meal_type: str,
    yield_servings: float,
    ingredients: list[dict[str, Any]],
    preparation_steps: list[str],
    summary: str = "",
    excluded_ingredients: list[str] | None = None,
) -> dict[str, Any]:
    """Create a reproducible Saved Recipe from frozen ingredient versions."""
    validated_ingredients = validate_linked_recipe_ingredients(ingredients)
    calculated = calculate_recipe_nutrition(
        validated_ingredients,
        yield_servings=yield_servings,
    )
    display_ingredients = [
        {
            "name": str(ingredient["name"]),
            "amount": str(ingredient["amount_description"]),
            "source": "saved_food",
        }
        for ingredient in validated_ingredients
    ]
    estimate_notes = (
        "Calculated from the exact Saved Food nutrition versions "
        "listed when this recipe was created."
    )
    excluded = [
        str(item).strip()
        for item in excluded_ingredients or []
        if str(item).strip()
    ]
    if excluded:
        estimate_notes += (
            " Nutrition excludes these user-approved optional or "
            "trace ingredients: " + ", ".join(excluded) + "."
        )

    idea = {
        "name": str(name).strip(),
        "summary": str(summary or "").strip(),
        "ingredients": display_ingredients,
        "preparation_steps": preparation_steps,
        "estimate_notes": estimate_notes,
        "heart_healthy_pick": False,
        "heart_healthy_reason": "",
        **calculated["per_serving"],
    }
    result = _save_recipe_idea(
        idea,
        meal_type=meal_type,
        yield_servings=float(yield_servings),
        verification_source="recipe_builder",
        linked_ingredients=validated_ingredients,
    )
    return {
        **result,
        "total_nutrition": calculated["total"],
        "per_serving_nutrition": calculated["per_serving"],
    }


def update_saved_recipe_from_ingredients(
    saved_recipe_id: int,
    *,
    yield_servings: float,
    ingredients: list[dict[str, Any]],
    preparation_steps: list[str],
    summary: str,
) -> dict[str, Any]:
    """Recalculate a recipe while preserving previous ledger snapshots."""
    existing = get_saved_recipe(int(saved_recipe_id))
    if existing is None:
        raise ValueError("Saved Recipe was not found.")

    validated_ingredients = validate_linked_recipe_ingredients(ingredients)
    calculated = calculate_recipe_nutrition(
        validated_ingredients,
        yield_servings=yield_servings,
    )
    display_ingredients = [
        {
            "name": str(ingredient["name"]),
            "amount": str(ingredient["amount_description"]),
            "source": "saved_food",
        }
        for ingredient in validated_ingredients
    ]
    candidate = {
        "name": existing["canonical_name"],
        "summary": str(summary or "").strip(),
        "ingredients": display_ingredients,
        "preparation_steps": preparation_steps,
        "estimate_notes": (
            "Calculated from the exact Saved Food nutrition versions "
            "listed when this recipe was updated."
        ),
        "heart_healthy_pick": False,
        "heart_healthy_reason": "",
        **calculated["per_serving"],
    }
    cleaned = validate_recipe_idea(candidate)

    add_user_nutrition_version(
        food_id=int(existing["food_id"]),
        verification_status="estimated",
        verification_source="recipe_builder_recalculation",
        **calculated["per_serving"],
    )

    timestamp = current_timestamp()
    with get_connection(DATABASE_PATH) as connection:
        connection.execute(
            "DELETE FROM saved_recipe_ingredients "
            "WHERE saved_recipe_id = ?",
            (int(saved_recipe_id),),
        )
        for position, ingredient in enumerate(
            validated_ingredients,
            start=1,
        ):
            connection.execute(
                """
                INSERT INTO saved_recipe_ingredients (
                    saved_recipe_id,
                    position,
                    food_id,
                    nutrition_version_id,
                    amount_description,
                    serving_multiplier,
                    created_at,
                    updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    int(saved_recipe_id),
                    position,
                    int(ingredient["food_id"]),
                    int(ingredient["nutrition_version_id"]),
                    str(ingredient["amount_description"]),
                    float(ingredient["serving_multiplier"]),
                    timestamp,
                    timestamp,
                ),
            )
        connection.execute(
            """
            UPDATE saved_recipes
            SET ingredients_json = ?,
                preparation_steps_json = ?,
                summary = ?,
                estimate_notes = ?,
                yield_servings = ?,
                heart_healthy_pick = 0,
                heart_healthy_reason = '',
                updated_at = ?
            WHERE saved_recipe_id = ?
            """,
            (
                json.dumps(cleaned["ingredients"]),
                json.dumps(cleaned["preparation_steps"]),
                cleaned["summary"],
                cleaned["estimate_notes"],
                float(yield_servings),
                timestamp,
                int(saved_recipe_id),
            ),
        )
        connection.commit()

    updated = get_saved_recipe(int(saved_recipe_id))
    if updated is None:
        raise RuntimeError("Saved Recipe recalculation could not be verified.")
    return {
        "recipe": updated,
        "total_nutrition": calculated["total"],
        "per_serving_nutrition": calculated["per_serving"],
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
