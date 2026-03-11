from loseit_parser import parse_loseit_csv


def build_food_coaching():
    data = parse_loseit_csv()
    totals = data["totals"]
    meals = data["meal_totals"]
    top_foods = data["top_calorie_foods"]

    notes = []

    calories = totals["calories"]
    protein = totals["protein"]
    fiber = totals["fiber"]
    sugar = totals["sugar"]
    sodium = totals["sodium"]

    if protein < 100:
        notes.append("Protein was low for the day. Try to build in a stronger protein anchor earlier.")
    elif protein >= 130:
        notes.append("Protein was solid for the day, which is a real positive.")

    if fiber < 25:
        notes.append("Fiber was a little low. More fruit, vegetables, beans, or higher-fiber wraps could help.")
    else:
        notes.append("Fiber intake was decent.")

    if sugar > 60:
        notes.append("Sugar was fairly high. Check whether sweets early in the day are making hunger harder to manage later.")

    if sodium > 3500:
        notes.append("Sodium was high. That may affect scale weight and water retention the next morning.")

    breakfast = meals.get("Breakfast", {})
    breakfast_protein = breakfast.get("protein", 0)
    breakfast_sugar = breakfast.get("sugar", 0)

    if breakfast:
        if breakfast_protein < 20:
            notes.append("Breakfast was low in protein. A better protein start could help with fullness.")
        if breakfast_sugar > 20:
            notes.append("Breakfast was sugar-heavy. Pair sweets with more protein or reduce them on tighter days.")

    top_lines = []
    for food in top_foods[:3]:
        top_lines.append(f"{food['name']} ({food['calories']:.0f} cal)")

    message = []
    message.append("Food coaching")
    message.append(f"Calories: {calories:.0f}")
    message.append(f"Protein: {protein:.0f}g")
    message.append(f"Fiber: {fiber:.0f}g")
    message.append(f"Sugar: {sugar:.0f}g")
    message.append("")

    if top_lines:
        message.append("Top calorie foods:")
        for line in top_lines:
            message.append(f"- {line}")
        message.append("")

    message.append("Coaching notes:")
    for note in notes[:4]:
        message.append(f"- {note}")

    return "\n".join(message)
