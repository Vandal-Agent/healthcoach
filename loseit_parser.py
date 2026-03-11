import csv
from collections import defaultdict

CSV_PATH = "/home/vandal/bots/healthcoach/data/latest_loseit.csv"


def safe_float(value):
    try:
        if value in ("", None):
            return 0.0
        return float(value)
    except (ValueError, TypeError):
        return 0.0


def parse_loseit_csv(csv_path=CSV_PATH):
    foods = []
    meals = defaultdict(list)

    totals = {
        "calories": 0.0,
        "protein": 0.0,
        "carbs": 0.0,
        "fat": 0.0,
        "fiber": 0.0,
        "sugar": 0.0,
        "sodium": 0.0,
    }

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            food = {
                "date": row.get("Date", "").strip(),
                "name": row.get("Name", "").strip(),
                "meal": row.get("Type", "").strip(),
                "quantity": row.get("Quantity", "").strip(),
                "units": row.get("Units", "").strip(),
                "calories": safe_float(row.get("Calories")),
                "protein": safe_float(row.get("Protein (g)")),
                "carbs": safe_float(row.get("Carbohydrates (g)")),
                "fat": safe_float(row.get("Fat (g)")),
                "fiber": safe_float(row.get("Fiber (g)")),
                "sugar": safe_float(row.get("Sugars (g)")),
                "sodium": safe_float(row.get("Sodium (mg)")),
            }

            foods.append(food)
            meals[food["meal"]].append(food)

            totals["calories"] += food["calories"]
            totals["protein"] += food["protein"]
            totals["carbs"] += food["carbs"]
            totals["fat"] += food["fat"]
            totals["fiber"] += food["fiber"]
            totals["sugar"] += food["sugar"]
            totals["sodium"] += food["sodium"]

    top_calorie_foods = sorted(foods, key=lambda x: x["calories"], reverse=True)[:5]

    meal_totals = {}
    for meal_name, items in meals.items():
        meal_totals[meal_name] = {
            "calories": sum(i["calories"] for i in items),
            "protein": sum(i["protein"] for i in items),
            "carbs": sum(i["carbs"] for i in items),
            "fat": sum(i["fat"] for i in items),
            "fiber": sum(i["fiber"] for i in items),
            "sugar": sum(i["sugar"] for i in items),
            "items": items,
        }

    return {
        "totals": totals,
        "meal_totals": meal_totals,
        "top_calorie_foods": top_calorie_foods,
        "foods": foods,
    }
