from loseit_parser import parse_loseit_csv


def _safe_number(value, default=0):
    try:
        if value is None:
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _safe_avg(values, default=None):
    clean = [float(v) for v in values if v is not None]
    return sum(clean) / len(clean) if clean else default


def _trend_delta(values):
    clean = [float(v) for v in values if v is not None]
    if len(clean) < 2:
        return None
    return clean[-1] - clean[0]


def _count_hits(values, threshold=None, comparator=">="):
    clean = [float(v) for v in values if v is not None]
    if threshold is None:
        return len(clean)

    count = 0
    for value in clean:
        if comparator == ">=" and value >= threshold:
            count += 1
        elif comparator == ">" and value > threshold:
            count += 1
        elif comparator == "<=" and value <= threshold:
            count += 1
        elif comparator == "<" and value < threshold:
            count += 1
    return count


def _build_deficit_note(deficit):
    if deficit is None:
        return None

    if deficit > 750:
        return f"Estimated calorie deficit was about {deficit:.0f}, which is a meaningful deficit."
    if deficit > 250:
        return f"Estimated calorie deficit was about {deficit:.0f}, which supports fat loss."
    if deficit >= -150:
        return "Calories looked close to maintenance."
    return f"Calories likely ran about {abs(deficit):.0f} over burn."


def _build_protein_note(protein):
    if protein < 100:
        return "Protein was low for the day. Try to build in a stronger protein anchor earlier."
    if protein >= 130:
        return "Protein was solid for the day, which is a real positive."
    return "Protein was decent, but there is still room to push it a little higher."


def _build_fiber_note(fiber):
    if fiber < 25:
        return "Fiber was a little low. More fruit, vegetables, beans, or higher-fiber wraps could help."
    return "Fiber intake was decent."


def _build_sugar_note(sugar):
    if sugar > 60:
        return "Sugar was fairly high. Check whether sweets early in the day are making hunger harder to manage later."
    return None


def _build_sodium_note(sodium):
    if sodium > 3500:
        return "Sodium was high. That may affect scale weight and water retention the next morning."
    return None


def _build_breakfast_notes(meals):
    notes = []

    breakfast = meals.get("Breakfast", {}) or {}
    if not breakfast:
        return notes

    breakfast_protein = _safe_number(breakfast.get("protein"))
    breakfast_sugar = _safe_number(breakfast.get("sugar"))

    if breakfast_protein < 20:
        notes.append("Breakfast was low in protein. A better protein start could help with fullness.")
    if breakfast_sugar > 20:
        notes.append("Breakfast was sugar-heavy. Pair sweets with more protein or reduce them on tighter days.")

    return notes


def _build_steps_note(steps):
    if steps is None or steps <= 0:
        return None

    if steps >= 12000:
        return "Step count was strong, which supports overall adherence and daily burn."
    if steps < 5000:
        return "Step count was light. A little more movement would help on lower-activity days."
    return None


def _build_weight_note(weight_today, recent_weight_avg, sodium, deficit):
    if (
        weight_today is None
        or recent_weight_avg is None
        or recent_weight_avg <= 0
    ):
        return None

    diff = weight_today - recent_weight_avg

    if diff > 1.0 and sodium > 3000:
        return "Weight is above your recent average, and high sodium may be part of that bump."

    if diff < -0.8 and deficit is not None and deficit > 250:
        return "Weight is below your recent average, which lines up with the calorie deficit."

    return None


def _get_top_food_lines(top_foods, limit=3):
    lines = []

    for food in (top_foods or [])[:limit]:
        name = food.get("name", "Unknown food")
        calories = _safe_number(food.get("calories"))
        lines.append(f"{name} ({calories:.0f} cal)")

    return lines


def build_food_coaching_data(
    total_burn=None,
    steps=None,
    weight_today=None,
    recent_weight_avg=None,
    sleep=None,
):
    data = parse_loseit_csv()
    totals = data.get("totals", {}) or {}
    meals = data.get("meal_totals", {}) or {}
    top_foods = data.get("top_calorie_foods", []) or []

    calories = _safe_number(totals.get("calories"))
    protein = _safe_number(totals.get("protein"))
    fiber = _safe_number(totals.get("fiber"))
    sugar = _safe_number(totals.get("sugar"))
    sodium = _safe_number(totals.get("sodium"))

    total_burn = _safe_number(total_burn, default=None)
    steps = _safe_number(steps, default=None)
    weight_today = _safe_number(weight_today, default=None)
    recent_weight_avg = _safe_number(recent_weight_avg, default=None)
    sleep = _safe_number(sleep, default=None)

    deficit = None
    if total_burn is not None and total_burn > 0:
        deficit = total_burn - calories

    notes = []
    flags = []

    deficit_note = _build_deficit_note(deficit)
    if deficit_note:
        notes.append(deficit_note)
        if deficit > 750:
            flags.append("large_deficit")
        elif deficit < -150:
            flags.append("surplus")

    protein_note = _build_protein_note(protein)
    if protein_note:
        notes.append(protein_note)
        if protein < 100:
            flags.append("low_protein")
        elif protein >= 130:
            flags.append("high_protein")

    fiber_note = _build_fiber_note(fiber)
    if fiber_note:
        notes.append(fiber_note)
        if fiber < 25:
            flags.append("low_fiber")

    sugar_note = _build_sugar_note(sugar)
    if sugar_note:
        notes.append(sugar_note)
        flags.append("high_sugar")

    sodium_note = _build_sodium_note(sodium)
    if sodium_note:
        notes.append(sodium_note)
        flags.append("high_sodium")

    breakfast_notes = _build_breakfast_notes(meals)
    notes.extend(breakfast_notes)
    if breakfast_notes:
        flags.append("breakfast_issue")

    steps_note = _build_steps_note(steps)
    if steps_note:
        notes.append(steps_note)
        if steps < 5000:
            flags.append("low_steps")
        elif steps >= 12000:
            flags.append("high_steps")

    weight_note = _build_weight_note(weight_today, recent_weight_avg, sodium, deficit)
    if weight_note:
        notes.append(weight_note)
        flags.append("weight_context")

    if sleep is not None and sleep > 0:
        if sleep < 6:
            notes.append("Sleep was short. That can make hunger and food decisions harder the next day.")
            flags.append("low_sleep")
        elif sleep >= 7.5:
            notes.append("Sleep looked solid, which supports recovery and appetite control.")
            flags.append("good_sleep")

    top_lines = _get_top_food_lines(top_foods, limit=3)

    summary = {
        "calories": calories,
        "protein": protein,
        "fiber": fiber,
        "sugar": sugar,
        "sodium": sodium,
        "burn": total_burn,
        "steps": steps,
        "weight_today": weight_today,
        "recent_weight_avg": recent_weight_avg,
        "sleep": sleep,
        "estimated_deficit": deficit,
    }

    return {
        "summary": summary,
        "top_food_lines": top_lines,
        "notes": notes,
        "flags": flags,
        "raw_parse_data": data,
    }


def format_food_coaching_message(coaching_data, max_notes=5):
    summary = coaching_data.get("summary", {}) or {}
    notes = coaching_data.get("notes", []) or []
    top_lines = coaching_data.get("top_food_lines", []) or []

    calories = summary.get("calories", 0)
    protein = summary.get("protein", 0)
    fiber = summary.get("fiber", 0)
    sugar = summary.get("sugar", 0)
    burn = summary.get("burn")
    steps = summary.get("steps")
    weight_today = summary.get("weight_today")
    recent_weight_avg = summary.get("recent_weight_avg")
    sleep = summary.get("sleep")
    deficit = summary.get("estimated_deficit")

    message = []
    message.append("Food coaching")
    message.append(f"Calories: {calories:.0f}")

    if burn is not None and burn > 0:
        message.append(f"Burn: {burn:.0f}")
        if deficit is not None:
            if deficit >= 0:
                message.append(f"Estimated deficit: {deficit:.0f}")
            else:
                message.append(f"Estimated surplus: {abs(deficit):.0f}")

    if steps is not None and steps > 0:
        message.append(f"Steps: {steps:.0f}")

    message.append(f"Protein: {protein:.0f}g")
    message.append(f"Fiber: {fiber:.0f}g")
    message.append(f"Sugar: {sugar:.0f}g")

    if sleep is not None and sleep > 0:
        message.append(f"Sleep: {sleep:.1f}h")

    if weight_today is not None and weight_today > 0:
        message.append(f"Weight today: {weight_today:.1f}")

    if recent_weight_avg is not None and recent_weight_avg > 0:
        message.append(f"Recent avg weight: {recent_weight_avg:.1f}")

    message.append("")

    if top_lines:
        message.append("Top calorie foods:")
        for line in top_lines:
            message.append(f"- {line}")
        message.append("")

    message.append("Coaching notes:")
    if notes:
        for note in notes[:max_notes]:
            message.append(f"- {note}")
    else:
        message.append("- No major coaching flags from food data.")

    return "\n".join(message)


def build_food_coaching(total_burn=None, steps=None, weight_today=None, recent_weight_avg=None, sleep=None):
    coaching_data = build_food_coaching_data(
        total_burn=total_burn,
        steps=steps,
        weight_today=weight_today,
        recent_weight_avg=recent_weight_avg,
        sleep=sleep,
    )
    return format_food_coaching_message(coaching_data)


def build_weekly_health_report(week_rows, title="Weekly health report"):
    """
    week_rows is expected to be a list of dicts like:
    {
        "date": "2026-03-19",
        "steps": 8000 or None,
        "total_cals": 3000 or None,
        "dietary_cals": 2100 or None,
        "protein": 125 or None,
        "sleep_hours": 6.8 or None,
        "weight": 224.1 or None,
    }

    Missing values must be passed as None, not zero.
    """

    if not week_rows:
        return f"{title}\nNo weekly data available."

    weights = [row.get("weight") for row in week_rows if row.get("weight") is not None]
    steps = [row.get("steps") for row in week_rows if row.get("steps") is not None]
    burns = [row.get("total_cals") for row in week_rows if row.get("total_cals") is not None]
    dietary = [row.get("dietary_cals") for row in week_rows if row.get("dietary_cals") is not None]
    protein = [row.get("protein") for row in week_rows if row.get("protein") is not None]
    sleep = [row.get("sleep_hours") for row in week_rows if row.get("sleep_hours") is not None]

    deficits = []
    for row in week_rows:
        burn = row.get("total_cals")
        food = row.get("dietary_cals")
        if burn is None or food is None:
            continue
        deficits.append(burn - food)

    avg_weight = _safe_avg(weights, None)
    avg_steps = _safe_avg(steps, None)
    avg_burn = _safe_avg(burns, None)
    avg_food = _safe_avg(dietary, None)
    avg_protein = _safe_avg(protein, None)
    avg_sleep = _safe_avg(sleep, None)
    avg_deficit = _safe_avg(deficits, None)

    weight_delta = _trend_delta(weights)
    steps_hit_days = _count_hits(steps, 8000, ">=")
    protein_hit_days = _count_hits(protein, 100, ">=")
    sleep_hit_days = _count_hits(sleep, 7, ">=")

    observations = []

    if weight_delta is not None:
        if weight_delta <= -0.8:
            observations.append(
                f"Weight trended down by {abs(weight_delta):.1f} lb across the week, which suggests the week was generally moving in the right direction."
            )
        elif weight_delta >= 0.8:
            observations.append(
                f"Weight trended up by {weight_delta:.1f} lb across the week. That may reflect higher intake, lower activity, water retention, or some combination."
            )
        else:
            observations.append(
                "Weight was fairly stable across the week."
            )

    if avg_deficit is not None:
        if avg_deficit >= 400:
            observations.append(
                f"Average calorie deficit on days with both burn and intake data was about {avg_deficit:.0f}, which is meaningful for fat loss."
            )
        elif avg_deficit >= 100:
            observations.append(
                f"Average calorie deficit on tracked days was about {avg_deficit:.0f}. That supports slower progress, but still points in a good direction."
            )
        else:
            observations.append(
                "Tracked calorie balance was close to maintenance on average."
            )

    if avg_protein is not None:
        if avg_protein < 90:
            observations.append(
                f"Protein averaged {avg_protein:.0f}g on logged days, which is likely too low for your current goals."
            )
        elif avg_protein >= 120:
            observations.append(
                f"Protein averaged {avg_protein:.0f}g on logged days, which is a real strength."
            )
        else:
            observations.append(
                f"Protein averaged {avg_protein:.0f}g on logged days. That is workable, but there is room to tighten it up."
            )

    if avg_sleep is not None:
        if avg_sleep < 6.5:
            observations.append(
                f"Sleep averaged {avg_sleep:.2f} hours on recorded days. That is low enough to affect hunger, recovery, and consistency."
            )
        elif avg_sleep >= 7:
            observations.append(
                f"Sleep averaged {avg_sleep:.2f} hours on recorded days, which supports recovery and appetite control."
            )

    if avg_steps is not None:
        if avg_steps < 6000:
            observations.append(
                f"Steps averaged {avg_steps:.0f} on logged days, which is a lower-activity week."
            )
        elif avg_steps >= 9000:
            observations.append(
                f"Steps averaged {avg_steps:.0f} on logged days, which is a strong activity baseline."
            )

    action_items = []

    if avg_protein is not None and avg_protein < 100:
        action_items.append(
            "Set one repeatable protein anchor before 1 PM each day, and make it the same easy choice most weekdays."
        )

    if avg_sleep is not None and avg_sleep < 6.5:
        action_items.append(
            "Treat sleep entry as part of your morning routine and aim to bring your weekly average up by at least 20 to 30 minutes per night."
        )

    if avg_steps is not None and avg_steps < 7000:
        action_items.append(
            "Add one deliberate movement block on your lowest-activity days instead of trying to force a big step count every day."
        )

    if avg_deficit is not None and avg_deficit < 150:
        action_items.append(
            "Tighten one calorie leak that repeats during the week rather than trying to overhaul the whole plan."
        )

    if not action_items:
        action_items.append(
            "Keep the current routine steady for another week and focus on consistency rather than adding new rules."
        )

    message = [title]

    message.append("")
    message.append("Summary")
    if avg_weight is not None:
        message.append(f"- Average weight: {avg_weight:.1f}")
    if avg_burn is not None:
        message.append(f"- Average burn: {avg_burn:.0f}")
    if avg_food is not None:
        message.append(f"- Average calories eaten: {avg_food:.0f}")
    if avg_deficit is not None:
        message.append(f"- Average deficit: {avg_deficit:.0f}")
    if avg_protein is not None:
        message.append(f"- Average protein: {avg_protein:.0f}g")
    if avg_sleep is not None:
        message.append(f"- Average sleep: {avg_sleep:.2f}h")
    if avg_steps is not None:
        message.append(f"- Average steps: {avg_steps:.0f}")
    if weights:
        message.append(f"- Weight entries used: {len(weights)}")
    if protein:
        message.append(f"- Protein days used: {len(protein)}")
    if sleep:
        message.append(f"- Sleep days used: {len(sleep)}")
    if steps:
        message.append(f"- Step days used: {len(steps)}")

    message.append("")
    message.append("What mattered this week")
    for note in observations[:4]:
        message.append(f"- {note}")

    message.append("")
    message.append("Consistency checks")
    if steps:
        message.append(f"- Days at or above 8,000 steps: {steps_hit_days}/{len(steps)}")
    if protein:
        message.append(f"- Days at or above 100g protein: {protein_hit_days}/{len(protein)}")
    if sleep:
        message.append(f"- Days at or above 7 hours sleep: {sleep_hit_days}/{len(sleep)}")

    message.append("")
    message.append("Next-week focus")
    for item in action_items[:3]:
        message.append(f"- {item}")

    return "\n".join(message)
