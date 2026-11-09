"""Preview or apply the agreed starter favorites without logging any food."""

import argparse
import sqlite3
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from food.database import DATABASE_PATH
from food.ledger import save_food_favorite
from food.library import add_food_with_nutrition


# Breakfast and the existing Dinner salad already have saved favorites.
STARTER_FAVORITES = {
    "lunch": (
        ("turkey sandwich", "1 serving"),
        ("popcorn", "18 g"),
        ("peanut butter and jelly sandwich", "1 serving"),
        ("Original beef jerky", "1 package (35 g)"),
        ("Cinnamon discs", "1 cinnamon disc"),
        ("Chippers", "1 bag"),
        ("Medium apple", "1 medium apple (182 g)"),
        ("Mozzarella cheese stick", "1 stick (28 g)"),
        ("Pretzel Pieces", "28 g"),
    ),
    "dinner": (("Tortilla Pizza", "1 tortilla pizza"),),
    "snack": (
        ("Pretzel Pieces", "28 g"),
        ("nut thins", "30 g"),
        ("bomb pop", "1 serving"),
        ("Cinnamon discs", "1 cinnamon disc"),
    ),
}

GENERIC_PORTIONS = {
    "Medium apple": {
        "serving_amount": 182,
        "calories": 95,
        "protein_g": 0.5,
        "source_url": "https://www.fda.gov/media/134471/download",
    },
    "Mozzarella cheese stick": {
        "serving_amount": 28,
        "calories": 80,
        "protein_g": 7,
        "source_url": "https://www.sargento.com/our-cheese/snack-cheese/sargento-natural-string-cheese-snacks",
    },
}


def food_identity(name, serving):
    return name.strip().casefold(), "".join(serving.casefold().split())


def find_starter_foods(database_path):
    """Look up exact names and servings from the supplied Food Library list."""
    connection = sqlite3.connect(database_path.resolve().as_uri() + "?mode=ro", uri=True)
    try:
        rows = connection.execute(
            """
            SELECT foods.food_id, foods.canonical_name, foods.serving_description
            FROM foods
            JOIN nutrition_versions
              ON nutrition_versions.nutrition_version_id = foods.active_nutrition_version_id
            WHERE nutrition_versions.calories IS NOT NULL
              AND NOT EXISTS (
                SELECT 1 FROM food_consolidations
                WHERE food_consolidations.duplicate_food_id = foods.food_id
              )
            """
        ).fetchall()
    finally:
        connection.close()
    matches = {}
    for food_id, name, serving in rows:
        matches.setdefault(food_identity(name, serving), []).append(food_id)
    return matches


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--apply", action="store_true", help="Save the previewed favorites")
    args = parser.parse_args()
    matches = find_starter_foods(DATABASE_PATH)
    plan = []
    unresolved = False
    for category, choices in STARTER_FAVORITES.items():
        for name, serving in choices:
            ids = matches.get(food_identity(name, serving), [])
            if len(ids) == 1:
                plan.append((category, name, serving, ids[0]))
                status = f"use food ID {ids[0]}"
            elif not ids and name in GENERIC_PORTIONS:
                plan.append((category, name, serving, None))
                status = "create estimated nutrition for the standard portion"
            else:
                unresolved = True
                status = "review needed: " + ("multiple exact matches" if ids else "no exact match")
            print(f"{category.title()}: {name}, {serving} — {status}")

    if unresolved:
        print("Resolve the listed matches before applying the starter favorites.")
        return 1
    if not args.apply:
        print("Preview only. Re-run with --apply after reviewing the matches.")
        return 0

    for category, name, serving, food_id in reversed(plan):
        if food_id is None:
            result = add_food_with_nutrition(
                canonical_name=name,
                serving_description=serving,
                serving_unit="g",
                verification_status="estimated",
                verification_source="quick_log_reference_estimate",
                **GENERIC_PORTIONS[name],
            )
            food_id = result["food"]["food_id"]
        save_food_favorite(food_id=food_id, quantity=1, meal_category=category)
    print(f"Saved {len(plan)} starter favorites.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
