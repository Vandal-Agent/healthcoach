import os
import json
import time
import threading
import logging
import re
from datetime import datetime, timedelta

import gspread
import pytz
import requests
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials

from loseit_coaching import build_food_coaching, build_weekly_health_report
from loseit_email_reader import download_latest_loseit_csv
from loseit_parser import parse_loseit_csv
from food.database import (
    get_pending_unresolved_foods,
    get_portion_profile,
    save_portion_profile,
    save_unresolved_food,
)
from food.interpreter import (
    FoodInterpretation,
    clean_interpretation_missing_fields,
    interpret_food_message,
    normalize_signature_food,
)
from food.library import add_food_with_nutrition
from food.ledger import (
    add_food_entry,
    delete_food_entry,
    find_recent_duplicate_entry,
    get_daily_totals,
)
from food.nutrition_provider import lookup_official_nutrition
from food.resolver import resolve_food
from conversation_engine import (
    cancel_conversation,
    complete_conversation,
    get_active_conversation,
    start_conversation,
    update_conversation,
)
from memory.cases import (
    create_case,
    evaluate_missing_data_cases,
)

app = Flask(__name__)

PACIFIC_TZ = pytz.timezone("US/Pacific")

CHAT_ID = os.getenv("HEALTH_CHAT_ID")
TELEGRAM_TOKEN = os.getenv("HEALTH_TELEGRAM_TOKEN")
JSON_PATH = os.getenv("HEALTH_GOOGLE_JSON_PATH")

STATE_FILE = "/home/vandal/bots/healthcoach/logs/state.json"
LOG_FILE = "/home/vandal/bots/healthcoach/logs/healthcoach.log"
GOALS_FILE = "/home/vandal/bots/healthcoach/goals.json"
LOSEIT_HISTORY_FILE = "/home/vandal/bots/healthcoach/data/loseit_history.json"
MEMORY_START_DATE = os.getenv("HEALTH_MEMORY_START_DATE", "")

SCOPE = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
]

HEADERS = [
    "Timestamp",
    "Steps",
    "Total Cals",
    "Active Cals",
    "Sleep",
    "RHR",
    "Weight",
    "HRV",
    "Dietary Cals",
    "Protein",
]

EARLY_PROTEIN_MEALS = {"Breakfast", "School Snacks", "Lunch"}

logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}
    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(state):
    os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f)


def reset_state_for_new_day(state, today_str):
    if state.get("date") != today_str:
        return {
            "date": today_str,
            "food_coaching_sent": False,
            "midday_sent": False,
            "evening_sent": False,
            "sleep_reminder_sent": False,
            "goal_check_sent": False,
            "weekly_sent": False,
            "telegram_update_offset": state.get("telegram_update_offset"),
        }
    return state


def load_goals():
    if not os.path.exists(GOALS_FILE):
        return {"active_goals": []}
    try:
        with open(GOALS_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"active_goals": []}


def save_goals(data):
    with open(GOALS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_active_goals():
    data = load_goals()
    return data.get("active_goals", [])


def initialize_goals_if_needed(reference_date):
    data = load_goals()
    goals = data.get("active_goals", [])
    changed = False

    for goal in goals:
        if goal.get("start_date") in ("", None):
            goal["start_date"] = reference_date.strftime("%Y-%m-%d")
            changed = True

    if changed:
        save_goals(data)


def load_loseit_history():
    if not os.path.exists(LOSEIT_HISTORY_FILE):
        return {"days": {}}
    try:
        with open(LOSEIT_HISTORY_FILE, "r") as f:
            return json.load(f)
    except Exception:
        return {"days": {}}


def save_loseit_history(data):
    os.makedirs(os.path.dirname(LOSEIT_HISTORY_FILE), exist_ok=True)
    with open(LOSEIT_HISTORY_FILE, "w") as f:
        json.dump(data, f, indent=2)


def _normalize_loseit_date(date_str):
    if not date_str:
        return None

    candidates = [
        "%m/%d/%Y",
        "%Y-%m-%d",
        "%m/%d/%y",
    ]

    for fmt in candidates:
        try:
            return datetime.strptime(date_str.strip(), fmt).strftime("%Y-%m-%d")
        except Exception:
            continue

    return None


def archive_latest_loseit_day():
    parsed = parse_loseit_csv()
    foods = parsed.get("foods", []) or []
    totals = parsed.get("totals", {}) or {}
    meal_totals = parsed.get("meal_totals", {}) or {}

    if not foods:
        logging.info("Lose It archive skipped: no foods found in latest CSV")
        return False

    raw_dates = {item.get("date", "").strip() for item in foods if item.get("date")}
    normalized_dates = {_normalize_loseit_date(d) for d in raw_dates}
    normalized_dates = {d for d in normalized_dates if d}

    if len(normalized_dates) != 1:
        logging.warning("Lose It archive skipped: expected one day, found dates=%s", list(normalized_dates))
        return False

    day_key = list(normalized_dates)[0]

    meal_protein = {}
    for meal_name, meal_data in meal_totals.items():
        meal_protein[meal_name] = round(float(meal_data.get("protein", 0) or 0), 1)

    early_protein = round(
        sum(meal_protein.get(meal_name, 0) for meal_name in EARLY_PROTEIN_MEALS),
        1,
    )

    history = load_loseit_history()
    history.setdefault("days", {})

    history["days"][day_key] = {
        "date": day_key,
        "totals": {
            "calories": float(totals.get("calories", 0) or 0),
            "protein": float(totals.get("protein", 0) or 0),
            "carbs": float(totals.get("carbs", 0) or 0),
            "fat": float(totals.get("fat", 0) or 0),
            "fiber": float(totals.get("fiber", 0) or 0),
            "sugar": float(totals.get("sugar", 0) or 0),
            "sodium": float(totals.get("sodium", 0) or 0),
        },
        "meal_protein": meal_protein,
        "early_protein": early_protein,
    }

    save_loseit_history(history)
    logging.info("Archived Lose It day %s with early protein %.1f", day_key, early_protein)
    return True


def send_telegram_msg(message, chat_id=None):
    target_chat_id = str(chat_id or CHAT_ID) if (chat_id or CHAT_ID) else None

    if not TELEGRAM_TOKEN or not target_chat_id:
        logging.error("Missing Telegram credentials")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": message}

    try:
        r = requests.post(url, json=payload, timeout=15)
        if r.status_code != 200:
            logging.error("Telegram rejected: %s", r.text)
            return False

        response_data = r.json()
        result = response_data.get("result") or {}

        return result.get("message_id") or True
    except Exception as e:
        logging.error("Telegram error: %s", e)
        return False


def refresh_loseit():
    try:
        logging.info("Refreshing Lose It email")
        path = download_latest_loseit_csv()
        if path:
            logging.info("Lose It CSV updated: %s", path)
            archive_latest_loseit_day()
        else:
            logging.info("No new Lose It CSV found")
    except Exception as e:
        logging.error("Lose It refresh failed: %s", e)


def get_gspread_client():
    creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_PATH, SCOPE)
    return gspread.authorize(creds)


def get_sheet_for_date(target_date):
    client = get_gspread_client()
    spreadsheet = client.open("Health Tracker")
    month = target_date.strftime("%B %Y")

    try:
        return spreadsheet.worksheet(month)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=month, rows="500", cols="10")
        ws.append_row(HEADERS)
        return ws


def get_current_sheet():
    return get_sheet_for_date(datetime.now(PACIFIC_TZ).date())


def safe_float(value, default=0.0):
    try:
        if value in ("", None):
            return default
        return float(value)
    except Exception:
        return default


def safe_int(value, default=0):
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except Exception:
        return default


def parse_sleep(value):
    if value in ("", None):
        return None

    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
        if ":" in value:
            try:
                h, m = value.split(":")
                return float(h) + float(m) / 60
            except Exception:
                return None

    try:
        return float(value)
    except Exception:
        return None


def normalize_sleep_for_sheet(value):
    if value in ("", None):
        return ""
    if isinstance(value, str) and ":" in value:
        return value.strip()
    parsed = parse_sleep(value)
    return parsed if parsed is not None else ""


def format_sleep_for_humans(raw_value):
    if raw_value in ("", None):
        return "not recorded"

    if isinstance(raw_value, str):
        stripped = raw_value.strip()
        if not stripped:
            return "not recorded"
        if ":" in stripped:
            parsed = parse_sleep(stripped)
            if parsed is not None:
                return f"{stripped} ({parsed:.2f} hrs)"
            return stripped
        parsed = parse_sleep(stripped)
        if parsed is not None:
            return f"{parsed:.2f} hrs"
        return stripped

    parsed = parse_sleep(raw_value)
    if parsed is None:
        return "not recorded"
    return f"{parsed:.2f} hrs"


def parse_row_date(row):
    if not row or not row[0]:
        return None
    try:
        return datetime.strptime(row[0], "%m/%d/%Y %I:%M %p").date()
    except Exception:
        return None


def parse_timestamp(timestamp_str):
    if not timestamp_str:
        return None
    try:
        dt = datetime.strptime(timestamp_str, "%m/%d/%Y %I:%M %p")
        return PACIFIC_TZ.localize(dt)
    except Exception:
        return None


def row_to_metrics(row):
    if not row:
        return None

    padded = list(row) + [""] * (10 - len(row))

    weight_val = safe_float(padded[6], None)
    if weight_val == 0:
        weight_val = None

    return {
        "timestamp": padded[0],
        "steps": safe_int(padded[1], 0),
        "total_cals": safe_float(padded[2], 0),
        "active_cals": safe_float(padded[3], 0),
        "sleep_hours": parse_sleep(padded[4]),
        "sleep_raw": padded[4],
        "rhr": safe_float(padded[5], 0),
        "weight": weight_val,
        "hrv": safe_float(padded[7], 0),
        "dietary_cals": safe_float(padded[8], 0),
        "protein": safe_float(padded[9], 0),
    }


def get_row_for_date(target_date):
    sheet = get_sheet_for_date(target_date)
    rows = sheet.get_all_values()

    target_str = target_date.strftime("%m/%d/%Y")
    match = None

    for row in rows[1:]:
        if row and row[0].startswith(target_str):
            match = row

    return match


def get_today_row_index_and_row(sheet, today_str):
    rows = sheet.get_all_values()
    match_index = None
    match_row = None

    for i, row in enumerate(rows[1:], start=2):
        if row and row[0].startswith(today_str):
            match_index = i
            match_row = row

    if match_row:
        while len(match_row) < 10:
            match_row.append("")

    return match_index, match_row, rows


def get_recent_rows(reference_date, days_back=10, exclude_dates=None):
    exclude_dates = exclude_dates or set()
    rows = []

    seen_months = {}
    for i in range(days_back + 1):
        d = reference_date - timedelta(days=i)
        key = d.strftime("%Y-%m")
        seen_months[key] = d

    for sample_date in seen_months.values():
        try:
            sheet = get_sheet_for_date(sample_date)
            rows.extend(sheet.get_all_values()[1:])
        except Exception as e:
            logging.error("Recent rows read error: %s", e)

    filtered = []
    min_date = reference_date - timedelta(days=days_back)

    for row in rows:
        row_date = parse_row_date(row)
        if not row_date:
            continue
        if row_date < min_date or row_date > reference_date:
            continue
        if row_date in exclude_dates:
            continue
        filtered.append(row)

    filtered.sort(key=lambda r: parse_row_date(r))
    return filtered


def collect_recent_numeric_values(rows, col_index, limit=7, minimum_valid=None, parser=None):
    parser = parser or (lambda x: safe_float(x, None))
    values = []

    for row in reversed(rows):
        if len(row) <= col_index:
            continue
        raw = row[col_index]
        if raw in ("", None):
            continue

        val = parser(raw)
        if val is None:
            continue

        if minimum_valid is not None and val <= minimum_valid:
            continue

        values.append(val)
        if len(values) >= limit:
            break

    values.reverse()
    return values


def average_or_default(values, default=None):
    return sum(values) / len(values) if values else default


def get_recent_average_weight(reference_date, days_back=10, limit=7):
    recent_rows = get_recent_rows(
        reference_date=reference_date,
        days_back=days_back,
        exclude_dates={reference_date},
    )
    weights = collect_recent_numeric_values(
        recent_rows,
        6,
        limit=limit,
        minimum_valid=50,
    )
    return average_or_default(weights, None)


def update_or_insert_today(sheet, row, now):
    today_str = now.strftime("%m/%d/%Y")
    row_index, existing_row, _ = get_today_row_index_and_row(sheet, today_str)

    if row_index and existing_row:
        merged = [
            row[0],
            row[1] if row[1] not in ("", None) else existing_row[1],
            row[2] if row[2] not in ("", None) else existing_row[2],
            row[3] if row[3] not in ("", None) else existing_row[3],
            row[4] if row[4] not in ("", None) else existing_row[4],
            row[5] if row[5] not in ("", None) else existing_row[5],
            row[6] if row[6] not in ("", None) else existing_row[6],
            row[7] if row[7] not in ("", None) else existing_row[7],
            row[8] if row[8] not in ("", None) else existing_row[8],
            row[9] if row[9] not in ("", None) else existing_row[9],
        ]
        sheet.update(
            range_name=f"A{row_index}:J{row_index}",
            values=[merged],
        )
        logging.info("Updated row %s for %s", row_index, today_str)
    else:
        sheet.append_row(row)
        logging.info("Appended new row for %s", today_str)


def sync_food_ledger_totals_to_sheet(target_date):
    """
    Recalculate one day's nutrition from the Food Ledger and
    overwrite only Dietary Cals and Protein in the Health Tracker.
    """
    totals = get_daily_totals(target_date)

    dietary_cals = round(
        float(totals.get("calories") or 0),
        3,
    )
    protein = round(
        float(totals.get("protein_g") or 0),
        3,
    )

    sheet = get_sheet_for_date(target_date)
    date_str = target_date.strftime("%m/%d/%Y")

    row_index, existing_row, _ = (
        get_today_row_index_and_row(
            sheet,
            date_str,
        )
    )

    if row_index and existing_row:
        sheet.update(
            range_name=f"I{row_index}:J{row_index}",
            values=[[dietary_cals, protein]],
        )

        logging.info(
            "Synced Food Ledger nutrition to row %s for %s: "
            "calories=%s protein=%s",
            row_index,
            date_str,
            dietary_cals,
            protein,
        )

    else:
        now = datetime.now(PACIFIC_TZ)

        timestamp = (
            now.strftime("%m/%d/%Y %I:%M %p")
            if now.date() == target_date
            else target_date.strftime("%m/%d/%Y 12:00 AM")
        )

        row = [
            timestamp,
            "",
            "",
            "",
            "",
            "",
            "",
            "",
            dietary_cals,
            protein,
        ]

        sheet.append_row(row)

        logging.info(
            "Created Health Tracker row from Food Ledger for %s: "
            "calories=%s protein=%s",
            date_str,
            dietary_cals,
            protein,
        )

    return {
        "entry_date": target_date.isoformat(),
        "dietary_cals": dietary_cals,
        "protein": protein,
    }


def build_progress_message(label, metrics):
    if not metrics:
        return f"{label}\nNo data recorded yet today."

    sleep_text = "not recorded"
    if metrics["sleep_hours"] is not None:
        sleep_text = format_sleep_for_humans(metrics.get("sleep_raw"))

    weight_text = "not recorded"
    if metrics["weight"] is not None:
        weight_text = f"{metrics['weight']:.1f} lbs"

    return (
        f"{label}\n"
        f"Steps: {metrics['steps']}\n"
        f"Total burn: {metrics['total_cals']:.0f}\n"
        f"Calories consumed: {metrics['dietary_cals']:.0f}\n"
        f"Protein: {metrics['protein']:.0f}g\n"
        f"Sleep: {sleep_text}\n"
        f"Weight: {weight_text}"
    )


def get_today_metrics():
    today_row = get_row_for_date(datetime.now(PACIFIC_TZ).date())
    return row_to_metrics(today_row)


def get_today_sleep_raw():
    metrics = get_today_metrics()
    if not metrics:
        return ""
    return metrics.get("sleep_raw", "")


def sleep_is_recorded_for_today():
    raw_sleep = get_today_sleep_raw()
    return raw_sleep not in ("", None)


def set_today_sleep(sleep_text):
    normalized_sleep = normalize_sleep_for_sheet(sleep_text)
    if normalized_sleep == "":
        return False, "I couldn't understand that sleep value. Try something like 6:22 or 6.5."

    now = datetime.now(PACIFIC_TZ)
    sheet = get_current_sheet()
    today_str = now.strftime("%m/%d/%Y")
    row_index, existing_row, _ = get_today_row_index_and_row(sheet, today_str)
    timestamp = now.strftime("%m/%d/%Y %I:%M %p")

    if row_index and existing_row:
        while len(existing_row) < 10:
            existing_row.append("")
        merged = list(existing_row[:10])
        merged[0] = timestamp
        merged[4] = normalized_sleep
        sheet.update(
            range_name=f"A{row_index}:J{row_index}",
            values=[merged],
        )
        logging.info("Updated sleep in row %s for %s", row_index, today_str)
    else:
        row = [
            timestamp,
            "",
            "",
            "",
            normalized_sleep,
            "",
            "",
            "",
            "",
            "",
        ]
        sheet.append_row(row)
        logging.info("Appended new row with sleep for %s", today_str)

    return True, f"Recorded sleep as {sleep_text.strip()} for today."


def answer_sleep_status():
    raw_sleep = get_today_sleep_raw()
    if raw_sleep in ("", None):
        return 'No, sleep is not recorded for today. Reply with "Record my sleep as 6:22".'
    return f"Yes, sleep is recorded for today: {format_sleep_for_humans(raw_sleep)}."


def extract_sleep_value_from_text(text):
    lowered = text.lower().strip()

    match = re.search(
        r"(?:record\s+my\s+sleep\s+as|record\s+sleep\s+as|sleep\s+(?:was|is))\s+([0-9]+(?:\.[0-9]+|:[0-9]{1,2})?)",
        lowered,
    )

    if match:
        return match.group(1)

    return None


def is_sleep_status_question(text):
    lowered = text.lower().strip()

    patterns = [
        r"did\s+i\s+record\s+my\s+sleep(?:\s+today)?\??",
        r"did\s+i\s+record\s+my\s+sleep\s+for\s+today\??",
        r"have\s+i\s+recorded\s+my\s+sleep(?:\s+today)?\??",
        r"is\s+my\s+sleep\s+recorded(?:\s+today)?\??",
        r"did\s+you\s+record\s+my\s+sleep(?:\s+today)?\??",
        r"what\s+is\s+my\s+sleep(?:\s+today)?\??",
    ]

    return any(re.search(pattern, lowered) for pattern in patterns)


def is_run_weekly_report_command(text):
    lowered = text.lower().strip()
    return lowered in {
        "run weekly report",
        "/runweeklyreport",
        "run the weekly report",
    }


def get_time_aware_meal_options():
    """Return meal choices appropriate for the current local time."""
    hour = datetime.now().astimezone().hour

    if hour < 11:
        return [
            "breakfast",
            "morning snack",
        ]

    if hour < 16:
        return [
            "morning snack",
            "lunch",
            "afternoon snack",
        ]

    return [
        "afternoon snack",
        "dinner",
        "dessert",
    ]


def format_meal_selection_prompt(interpretation):
    """Format a focused time-aware meal question."""
    options = get_time_aware_meal_options()

    lines = ["I interpreted this as:"]

    fields = (
        ("Restaurant", interpretation.restaurant),
        ("Brand", interpretation.brand),
        ("Food", interpretation.food_name),
        ("Size", interpretation.size),
        ("Quantity", interpretation.quantity),
        ("Amount", interpretation.quantity_description),
        ("Drink", interpretation.drink),
    )

    for label, value in fields:
        if value not in (None, ""):
            lines.append(f"{label}: {value}")

    if interpretation.assumptions:
        lines.append("")
        lines.append("Assumptions:")

        for assumption in interpretation.assumptions:
            lines.append(f"- {assumption}")

    lines.append("")
    lines.append("Which meal was this?")

    for index, option in enumerate(options, start=1):
        lines.append(f"{index}. {option.title()}")

    return "\n".join(lines)


def parse_meal_selection(text, options):
    """Resolve a numbered or natural-language meal response."""
    cleaned = text.strip().lower()

    if cleaned.isdigit():
        index = int(cleaned) - 1

        if 0 <= index < len(options):
            return options[index]

    aliases = {
        "breakfast": "breakfast",
        "morning snack": "morning snack",
        "school snack": "morning snack",
        "lunch": "lunch",
        "afternoon snack": "afternoon snack",
        "dinner": "dinner",
        "dessert": "dessert",
    }

    for phrase, meal in aliases.items():
        if cleaned == phrase or phrase in cleaned:
            if meal in options:
                return meal

    return None


def format_daily_food_totals():
    """Format today's Food Ledger totals for Telegram."""
    today = datetime.now(PACIFIC_TZ).date()
    totals = get_daily_totals(today)

    return "\n".join(
        [
            "Today's food totals:",
            "",
            (
                "Calories: "
                + format_display_number(
                    totals["calories"],
                    decimals=0,
                )
            ),
            (
                "Protein: "
                + format_display_number(
                    totals["protein_g"],
                    decimals=1,
                )
                + " g"
            ),
            (
                "Carbohydrates: "
                + format_display_number(
                    totals["carbohydrates_g"],
                    decimals=1,
                )
                + " g"
            ),
            (
                "Fat: "
                + format_display_number(
                    totals["fat_g"],
                    decimals=1,
                )
                + " g"
            ),
            (
                "Fiber: "
                + format_display_number(
                    totals["fiber_g"],
                    decimals=1,
                )
                + " g"
            ),
            (
                "Sugar: "
                + format_display_number(
                    totals["sugar_g"],
                    decimals=1,
                )
                + " g"
            ),
            (
                "Sodium: "
                + format_display_number(
                    totals["sodium_mg"],
                    decimals=0,
                )
                + " mg"
            ),
        ]
    )


def format_food_interpretation(interpretation):
    """Format a food interpretation for Telegram review."""
    lines = ["I interpreted this as:"]

    if interpretation.is_combo_meal:
        fields = (
            ("Restaurant", interpretation.restaurant),
            ("Combo", "Yes"),
            ("Entrée", interpretation.combo_entree),
            (
                "Side",
                " ".join(
                    part
                    for part in (
                        interpretation.combo_side_size,
                        interpretation.combo_side,
                    )
                    if part
                ),
            ),
            (
                "Drink",
                " ".join(
                    part
                    for part in (
                        interpretation.combo_drink_size,
                        interpretation.combo_drink,
                    )
                    if part
                ),
            ),
            ("Quantity", interpretation.quantity),
            ("Meal", interpretation.meal_category),
        )
    else:
        fields = (
            ("Restaurant", interpretation.restaurant),
            ("Brand", interpretation.brand),
            ("Food", interpretation.food_name),
            ("Size", interpretation.size),
            ("Quantity", interpretation.quantity),
            ("Amount", interpretation.quantity_description),
            ("Meal", interpretation.meal_category),
            ("Drink", interpretation.drink),
        )

    for label, value in fields:
        if value not in (None, ""):
            lines.append(f"{label}: {value}")

    if interpretation.assumptions:
        lines.append("")
        lines.append("Assumptions:")
        for assumption in interpretation.assumptions:
            lines.append(f"- {assumption}")

    if interpretation.missing_fields:
        lines.append("")
        lines.append(
            "Still needed: "
            + ", ".join(interpretation.missing_fields)
        )

    if interpretation.clarification_question:
        lines.append("")
        lines.append(interpretation.clarification_question)

    lines.append("")
    lines.append(
        "Nutrition has not been looked up or saved yet."
    )

    if not interpretation.missing_fields:
        lines.append("")
        lines.append("1. Correct")
        lines.append("2. Edit")
        lines.append("3. Cancel")

    return "\n".join(lines)


def build_help_message():
    return (
        "I can currently help with:\n"
        "- /status\n"
        '- Record sleep, like: "Record my sleep as 6:22"\n'
        '- Check sleep, like: "Did I record my sleep today?"\n'
        '- Run the weekly report, like: "run weekly report"'
    )


def build_last_week_rows(reference_date):
    end_date = reference_date - timedelta(days=1)
    start_date = end_date - timedelta(days=6)

    rows = get_recent_rows(reference_date, days_back=10)
    week = []

    for row in rows:
        row_date = parse_row_date(row)
        if not row_date:
            continue
        if start_date <= row_date <= end_date:
            metrics = row_to_metrics(row)
            if not metrics:
                continue
            week.append({
                "date": row_date.strftime("%Y-%m-%d"),
                "timestamp": metrics.get("timestamp"),
                "steps": metrics.get("steps") if metrics.get("steps") > 0 else None,
                "total_cals": metrics.get("total_cals") if metrics.get("total_cals") > 0 else None,
                "dietary_cals": metrics.get("dietary_cals") if metrics.get("dietary_cals") > 0 else None,
                "protein": metrics.get("protein") if metrics.get("protein") > 0 else None,
                "sleep_hours": metrics.get("sleep_hours"),
                "weight": metrics.get("weight"),
            })

    week.sort(key=lambda x: x["date"])
    return week


def get_loseit_history_for_week(reference_date):
    end_date = reference_date - timedelta(days=1)
    start_date = end_date - timedelta(days=6)

    history = load_loseit_history()
    days = history.get("days", {})

    results = {}
    current = start_date
    while current <= end_date:
        key = current.strftime("%Y-%m-%d")
        if key in days:
            results[key] = days[key]
        current += timedelta(days=1)

    return results


def evaluate_goals_for_week(week_rows, reference_date):
    goals = get_active_goals()
    lines = []
    loseit_week = get_loseit_history_for_week(reference_date)

    for goal in goals:
        goal_type = goal.get("type")
        target = goal.get("target")
        start_date = goal.get("start_date")
        weeks_active = goal.get("weeks_active", 0)

        if goal_type == "steps_daily":
            valid_days = [row for row in week_rows if row.get("steps") is not None]
            hits = sum(1 for row in valid_days if row.get("steps", 0) >= target)
            if valid_days:
                lines.append(
                    f"Steps goal ({target:,}/day): hit {hits}/{len(valid_days)} tracked days."
                )
            else:
                lines.append(
                    f"Steps goal ({target:,}/day): no tracked days available."
                )

        elif goal_type == "protein_early":
            valid_days = []
            hits = 0

            for day_key, day_data in sorted(loseit_week.items()):
                early_protein = day_data.get("early_protein")
                if early_protein is None:
                    continue
                valid_days.append(day_key)
                if float(early_protein) >= float(target):
                    hits += 1

            if valid_days:
                lines.append(
                    f"Protein goal ({target:.0f}g by 1 PM): hit {hits}/{len(valid_days)} Lose It days using Breakfast + School Snacks + Lunch."
                )
            else:
                lines.append(
                    f"Protein goal ({target:.0f}g by 1 PM): no archived Lose It days available."
                )

        else:
            lines.append(f"Unknown goal type: {goal_type}")

        if start_date:
            lines.append(f"Goal start date: {start_date} | weeks active: {weeks_active}")

    return lines


def increment_goal_weeks_if_due(reference_date):
    data = load_goals()
    goals = data.get("active_goals", [])
    changed = False

    for goal in goals:
        start_date = goal.get("start_date")
        if not start_date:
            continue
        try:
            start_dt = datetime.strptime(start_date, "%Y-%m-%d").date()
        except Exception:
            continue

        weeks_elapsed = max(0, (reference_date - start_dt).days // 7)
        if goal.get("weeks_active", 0) != weeks_elapsed:
            goal["weeks_active"] = weeks_elapsed
            changed = True

    if changed:
        save_goals(data)


def run_130_goal_check():
    now = datetime.now(PACIFIC_TZ)
    today = now.date()

    row = get_row_for_date(today)
    metrics = row_to_metrics(row)

    if not metrics:
        return send_telegram_msg("1:30 check\nNo data recorded yet today.")

    last_update = parse_timestamp(metrics.get("timestamp"))
    cutoff = now.replace(hour=13, minute=0, second=0, microsecond=0)

    if not last_update or last_update < cutoff:
        memory_enabled = True

        if MEMORY_START_DATE:
            try:
                configured_start = datetime.strptime(
                    MEMORY_START_DATE,
                    "%Y-%m-%d",
                ).date()
                memory_enabled = today >= configured_start
            except ValueError:
                logging.error(
                    "Invalid HEALTH_MEMORY_START_DATE: %s",
                    MEMORY_START_DATE,
                )

        evaluation_due = now.replace(
            hour=19,
            minute=0,
            second=0,
            microsecond=0,
        )

        if memory_enabled:
            try:
                case_result = create_case(
                    case_date=today,
                    case_type="missing_data",
                    priority="medium",
                    observation_code="food_data_missing_after_midday",
                    observation=(
                        "No food update was available after the midday check."
                    ),
                    supporting_data={
                        "check_time": now.isoformat(timespec="seconds"),
                        "last_food_update": (
                            last_update.isoformat(timespec="seconds")
                            if last_update
                            else None
                        ),
                        "dietary_cals": metrics.get("dietary_cals", 0),
                        "protein": metrics.get("protein", 0),
                    },
                    data_confidence=0.95,
                    recommendation_code="update_missing_data",
                    expected_result="Nutrition data appears later today.",
                    evaluation_due_at=evaluation_due.isoformat(
                        timespec="seconds"
                    ),
                    tags=["midday", "missing_data"],
                )
                logging.info(
                    "Missing-data Memory Case %s: case_id=%s",
                    (
                        "created"
                        if case_result["created"]
                        else "already exists"
                    ),
                    case_result["case"]["case_id"],
                )
            except Exception:
                logging.exception(
                    "Could not create missing-data Memory Case"
                )
        else:
            logging.info(
                "Memory Case collection begins on %s",
                MEMORY_START_DATE,
            )

        return send_telegram_msg(
            "1:30 check\nI do not see a food update after 1:00 PM yet. Update your spreadsheet food totals and I will check your goals."
        )

    goals = get_active_goals()
    messages = []

    for goal in goals:
        goal_type = goal.get("type")
        target = goal.get("target")

        if goal_type == "protein_early":
            protein = metrics.get("protein", 0)
            if protein >= target:
                messages.append(f"Protein goal hit: {protein:.0f}g by the latest update.")
            else:
                messages.append(f"Protein so far: {protein:.0f}g / {target}g.")

        elif goal_type == "steps_daily":
            steps = metrics.get("steps", 0)
            if steps >= target:
                messages.append(f"Steps goal already hit: {steps:,} / {target:,}.")
            else:
                messages.append(f"Steps so far: {steps:,} / {target:,}.")

    if not messages:
        messages.append("No active goals found.")

    return send_telegram_msg("1:30 check\n" + "\n".join(messages))


def send_food_coaching_for_yesterday():
    today_date = datetime.now(PACIFIC_TZ).date()
    yesterday_date = today_date - timedelta(days=1)

    yesterday_row = get_row_for_date(yesterday_date)
    yesterday = row_to_metrics(yesterday_row)

    if not yesterday:
        logging.info("Skipping food coaching: no yesterday row found")
        return False

    refresh_loseit()

    today_row = get_row_for_date(today_date)
    today_metrics = row_to_metrics(today_row)

    recent_weight_avg = get_recent_average_weight(today_date)
    weight_today = today_metrics["weight"] if today_metrics and today_metrics["weight"] is not None else yesterday["weight"]
    sleep_today = today_metrics["sleep_hours"] if today_metrics else None

    try:
        msg = build_food_coaching(
            total_burn=yesterday["total_cals"],
            steps=yesterday["steps"],
            weight_today=weight_today,
            recent_weight_avg=recent_weight_avg,
            sleep=sleep_today,
        )
        return send_telegram_msg(msg)
    except Exception as e:
        logging.error("Food coaching error: %s", e)
        return False


def send_midday_update():
    today_row = get_row_for_date(datetime.now(PACIFIC_TZ).date())
    metrics = row_to_metrics(today_row)
    return send_telegram_msg(build_progress_message("1:00 update", metrics))


def send_evening_update():
    now = datetime.now(PACIFIC_TZ)
    today = now.date()

    today_row = get_row_for_date(today)
    metrics = row_to_metrics(today_row)

    latest_update = None
    nutrition_data_available = False
    dietary_cals = 0.0
    protein = 0.0

    if metrics:
        parsed_update = parse_timestamp(metrics.get("timestamp"))
        latest_update = (
            parsed_update.isoformat(timespec="seconds")
            if parsed_update
            else None
        )
        dietary_cals = metrics.get("dietary_cals", 0)
        protein = metrics.get("protein", 0)

        cutoff = now.replace(
            hour=13,
            minute=0,
            second=0,
            microsecond=0,
        )
        nutrition_data_available = bool(
            parsed_update and parsed_update >= cutoff
        )

    try:
        evaluation_results = evaluate_missing_data_cases(
            case_date=today,
            nutrition_data_available=nutrition_data_available,
            dietary_cals=dietary_cals,
            protein=protein,
            latest_update=latest_update,
        )
        logging.info(
            "Evaluated %s missing-data Memory Cases",
            len(evaluation_results),
        )
    except Exception:
        logging.exception(
            "Could not evaluate missing-data Memory Cases"
        )

    return send_telegram_msg(
        build_progress_message("7:00 update", metrics)
    )


def send_sleep_reminder_if_missing():
    if sleep_is_recorded_for_today():
        return False

    message = 'You have not recorded sleep yet today. Reply with "Record my sleep as 6:22".'
    return send_telegram_msg(message)


def send_weekly_report(chat_id=None):
    now = datetime.now(PACIFIC_TZ).date()

    initialize_goals_if_needed(now)
    increment_goal_weeks_if_due(now)

    week_rows = build_last_week_rows(now)
    base_report = build_weekly_health_report(
        week_rows,
        title="Weekly health report (previous Sunday through Saturday)",
    )
    goal_lines = evaluate_goals_for_week(week_rows, now)

    full_message = (
        f"{base_report}\n\n"
        "Goal results\n"
        + "\n".join(f"- {line}" for line in goal_lines)
    )

    return send_telegram_msg(full_message, chat_id=chat_id)


def scheduler_loop():
    logging.info("Scheduler started")

    while True:
        try:
            now = datetime.now(PACIFIC_TZ)
            today_str = now.strftime("%m/%d/%Y")

            state = load_state()
            state = reset_state_for_new_day(state, today_str)

            if now.hour == 8 and now.minute == 0 and not state.get("sleep_reminder_sent", False):
                if send_sleep_reminder_if_missing():
                    state["sleep_reminder_sent"] = True
                    save_state(state)
                time.sleep(60)

            if now.hour == 8 and now.minute == 15:
                refresh_loseit()
                time.sleep(60)

            if now.hour >= 8 and now.minute >= 30 and not state.get("food_coaching_sent", False):
                if send_food_coaching_for_yesterday():
                    state["food_coaching_sent"] = True
                    save_state(state)

            if (now.hour > 13 or (now.hour == 13 and now.minute >= 0)) and not state.get("midday_sent", False):
                if send_midday_update():
                    state["midday_sent"] = True
                    save_state(state)

            if now.hour == 13 and now.minute == 30 and not state.get("goal_check_sent", False):
                if run_130_goal_check():
                    state["goal_check_sent"] = True
                    save_state(state)
                time.sleep(60)

            if (now.hour > 19 or (now.hour == 19 and now.minute >= 0)) and not state.get("evening_sent", False):
                if send_evening_update():
                    state["evening_sent"] = True
                    save_state(state)

            if now.weekday() == 6 and now.hour == 9 and now.minute == 0 and not state.get("weekly_sent", False):
                if send_weekly_report():
                    state["weekly_sent"] = True
                    save_state(state)
                time.sleep(60)

        except Exception as e:
            logging.error("Scheduler error: %s", e)

        time.sleep(20)


def resolve_or_create_verified_food(
    *,
    food_name: str,
    size: str | None,
    restaurant: str | None,
    brand: str | None = None,
) -> dict:
    """
    Resolve a Food Library item or retrieve and save verified nutrition.

    This function never estimates or fabricates nutrition.
    """
    serving_description = size or "standard"

    resolution = resolve_food(
        food_name=food_name,
        serving_description=serving_description,
        brand=brand,
        restaurant=restaurant,
    )

    if resolution["found"]:
        return {
            "food": resolution["food"],
            "nutrition": resolution["nutrition"],
            "source": "food_library",
            "verification_source": (
                resolution["food"].get(
                    "verification_source"
                )
            ),
        }

    provider_result = lookup_official_nutrition(
        restaurant=restaurant,
        food_name=food_name,
        size=size,
        brand=brand,
    )

    if not provider_result["found"]:
        clarification = provider_result.get(
            "clarification_question"
        )
        missing = provider_result.get("missing_fields") or []

        details = []

        if missing:
            details.append(
                "Missing: " + ", ".join(missing)
            )

        if clarification:
            details.append(clarification)

        suffix = (
            " " + " ".join(details)
            if details
            else ""
        )

        raise ValueError(
            f"Verified nutrition could not be found for "
            f"{food_name}.{suffix}"
        )

    provider_food = provider_result["food"]
    provider_nutrition = provider_result["nutrition"]
    verification = provider_result["verification"]

    saved = add_food_with_nutrition(
        canonical_name=provider_food["canonical_name"],
        serving_description=(
            provider_food["serving_description"]
        ),
        serving_amount=provider_food["serving_amount"],
        serving_unit=provider_food["serving_unit"],
        verification_status=verification["status"],
        verification_source=verification["source"],
        calories=provider_nutrition["calories"],
        protein_g=provider_nutrition["protein_g"],
        carbohydrates_g=(
            provider_nutrition["carbohydrates_g"]
        ),
        fat_g=provider_nutrition["fat_g"],
        fiber_g=provider_nutrition["fiber_g"],
        sugar_g=provider_nutrition["sugar_g"],
        sodium_mg=provider_nutrition["sodium_mg"],
        brand=provider_food["brand"],
        restaurant=provider_food["restaurant"],
        food_type=provider_food["food_type"],
        source_item_id=verification["source_item_id"],
        source_url=verification["source_url"],
    )

    return {
        "food": saved["food"],
        "nutrition": saved["nutrition"],
        "source": "verified_lookup",
        "verification_source": verification["source"],
    }


def resolve_packaged_serving_multiplier(
    *,
    food_id: int,
    quantity: float | None,
    quantity_description: str | None,
    serving_amount: float | None,
    serving_unit: str | None,
) -> float:
    """
    Convert a packaged-food amount into package servings.

    This function never estimates vague amounts.
    """
    description = (
        quantity_description or ""
    ).strip().lower()

    natural_quantity_phrases = {
        # Servings
        "a serving": "1 serving",
        "one serving": "1 serving",
        "one servings": "1 serving",
        "two servings": "2 servings",
        "three servings": "3 servings",
        "four servings": "4 servings",
        "half serving": "0.5 serving",
        "half a serving": "0.5 serving",
        "one half serving": "0.5 serving",
        "one and a half servings": "1.5 servings",
        "one and one half servings": "1.5 servings",

        # Ounces
        "an ounce": "1 oz",
        "one ounce": "1 oz",
        "two ounces": "2 oz",
        "three ounces": "3 oz",
        "four ounces": "4 oz",
        "half ounce": "0.5 oz",
        "half an ounce": "0.5 oz",
        "one half ounce": "0.5 oz",
        "one and a half ounces": "1.5 oz",
        "one and one half ounces": "1.5 oz",

        # Grams
        "one gram": "1 g",
        "two grams": "2 g",
        "three grams": "3 g",
        "four grams": "4 g",
    }

    description = natural_quantity_phrases.get(
        description,
        description,
    )

    if not description:
        raise ValueError(
            "A packaged-food amount is required."
        )

    serving_match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*servings?",
        description,
    )

    if serving_match:
        servings = float(serving_match.group(1))

        if servings <= 0:
            raise ValueError(
                "Serving quantity must be greater than zero."
            )

        return servings

    amount_match = re.fullmatch(
        r"(\d+(?:\.\d+)?)\s*"
        r"(g|gram|grams|grm|oz|ounce|ounces)",
        description,
    )

    if amount_match:
        amount = float(amount_match.group(1))
        unit = amount_match.group(2)

        if amount <= 0:
            raise ValueError(
                "Food amount must be greater than zero."
            )

        if serving_amount is None or not serving_unit:
            raise ValueError(
                "The package serving size is unavailable."
            )

        normalized_serving_unit = (
            str(serving_unit).strip().lower()
        )

        if normalized_serving_unit in {
            "g",
            "gram",
            "grams",
            "grm",
        }:
            serving_grams = float(serving_amount)
        elif normalized_serving_unit in {
            "oz",
            "ounce",
            "ounces",
        }:
            serving_grams = (
                float(serving_amount) * 28.349523125
            )
        else:
            raise ValueError(
                "This food serving cannot be converted "
                "safely from grams or ounces."
            )

        amount_grams = amount

        if unit in {"oz", "ounce", "ounces"}:
            amount_grams = amount * 28.349523125

        multiplier = (
            amount_grams / serving_grams
        )

        if multiplier <= 0:
            raise ValueError(
                "Calculated serving quantity was invalid."
            )

        return multiplier

    handful_phrase = None

    for candidate_phrase in (
        "small handful",
        "large handful",
        "handful",
    ):
        if candidate_phrase in description:
            handful_phrase = candidate_phrase
            break

    if handful_phrase:
        profile = get_portion_profile(
            food_id=int(food_id),
            phrase=handful_phrase,
        )

        if profile is None:
            raise ValueError(
                f"I do not have a saved {handful_phrase} amount "
                "for this food yet."
            )

        estimated_amount = float(
            profile["estimated_amount"]
        )

        estimated_unit = str(
            profile["estimated_unit"]
        ).strip().lower()

        if estimated_unit in {
            "g",
            "gram",
            "grams",
            "grm",
        }:
            amount_grams = estimated_amount
        elif estimated_unit in {
            "oz",
            "ounce",
            "ounces",
        }:
            amount_grams = (
                estimated_amount * 28.349523125
            )
        elif estimated_unit in {
            "serving",
            "servings",
        }:
            return estimated_amount
        else:
            raise ValueError(
                "The saved portion profile uses an unsupported unit."
            )

        if serving_amount is None or not serving_unit:
            raise ValueError(
                "The package serving size is unavailable."
            )

        normalized_serving_unit = (
            str(serving_unit).strip().lower()
        )

        if normalized_serving_unit in {
            "g",
            "gram",
            "grams",
            "grm",
        }:
            serving_grams = float(serving_amount)
        elif normalized_serving_unit in {
            "oz",
            "ounce",
            "ounces",
        }:
            serving_grams = (
                float(serving_amount) * 28.349523125
            )
        else:
            raise ValueError(
                "This saved portion profile cannot be converted "
                "safely to the food's base serving."
            )

        multiplier = (
            amount_grams / serving_grams
        )

        if multiplier <= 0:
            raise ValueError(
                "Saved portion quantity was invalid."
            )

        return multiplier

    raise ValueError(
        "I could not convert that packaged-food amount. "
        "Use a serving count, grams, or ounces."
    )


def format_display_number(
    value: float | int | None,
    *,
    decimals: int = 1,
) -> str:
    """Format nutrition numbers for human-readable Telegram output."""
    if value is None:
        return ""

    number = float(value)

    if number.is_integer():
        return str(int(number))

    if decimals == 0:
        return f"{number:.0f}"

    return f"{number:.{decimals}f}".rstrip("0").rstrip(".")


def format_food_display_name(
    *,
    canonical_name: str,
    restaurant: str | None = None,
    size: str | None = None,
) -> str:
    """Return a friendlier food name without changing stored data."""
    name = canonical_name.strip()

    upper_restaurant_prefixes = {
        "BURGER KING,": "Burger King",
        "McDONALD'S,": "McDonald's",
    }

    packaged_brand_prefixes = {
        "SNYDER'S OF HANOVER,": "Snyder's of Hanover",
    }

    detected_restaurant = restaurant
    detected_brand = None

    for prefix, friendly_restaurant in upper_restaurant_prefixes.items():
        if name.startswith(prefix):
            name = name[len(prefix):].strip()
            detected_restaurant = (
                detected_restaurant
                or friendly_restaurant
            )
            break

    if not detected_restaurant:
        for prefix, friendly_brand in packaged_brand_prefixes.items():
            if name.startswith(prefix):
                name = name[len(prefix):].strip()
                detected_brand = friendly_brand
                break

    replacements = {
        "french fries": "Fries",
        "French Fries": "Fries",
    }

    name = replacements.get(name, name)

    if name.islower() or name.isupper():
        name = name.title()

    parts = []

    if detected_restaurant:
        parts.append(detected_restaurant.strip())
    elif detected_brand:
        parts.append(detected_brand)

    if size:
        raw_size = str(size).strip()
        normalized_size = raw_size.title()
        lowered_size = raw_size.lower()

        quantity_like_size = (
            re.fullmatch(
                r"\d+(?:\.\d+)?\s*"
                r"(?:g|gram|grams|grm|oz|ounce|ounces|"
                r"serving|servings)",
                lowered_size,
            )
            is not None
            or lowered_size in {
                "a serving",
                "one serving",
                "half serving",
                "half a serving",
                "one half serving",
                "one and a half servings",
                "one and one half servings",
                "an ounce",
                "one ounce",
                "half ounce",
                "half an ounce",
                "one half ounce",
                "one and a half ounces",
                "one and one half ounces",
                "two ounces",
                "three ounces",
                "four ounces",
            }
        )

        if (
            not quantity_like_size
            and normalized_size.lower() not in name.lower()
        ):
            parts.append(normalized_size)

    parts.append(name)

    return " ".join(part for part in parts if part)


def format_nutrition_source(value: str | None) -> str:
    """Return a readable nutrition-source label."""
    normalized = (value or "").strip().lower()

    labels = {
        "fdc.nal.usda.gov": "USDA FoodData Central",
        "mcdonalds.com": "McDonald's official nutrition",
        "tacobell.com": "Taco Bell official nutrition",
        "burgerking.com": "Burger King official nutrition",
        "wendys.com": "Wendy's official nutrition",
        "world.openfoodfacts.org": "Open Food Facts",
        "openfoodfacts.org": "Open Food Facts",
        "user_package_label": "Package label entered by user",
    }

    return labels.get(
        normalized,
        value or "Verified saved nutrition",
    )


def format_pending_nutrition_confirmation(
    components: list[dict],
    *,
    meal_category: str,
) -> str:
    """Format verified nutrition before Food Ledger logging."""
    lines = [
        "Verified nutrition:",
        "",
    ]

    total_calories = 0.0
    total_protein = 0.0

    for component in components:
        quantity = float(component.get("quantity") or 1.0)
        calories = component.get("calories")
        protein = component.get("protein_g")

        display_name = format_food_display_name(
            canonical_name=component["canonical_name"],
            restaurant=component.get("restaurant"),
            size=component.get("size"),
        )

        line = (
            f"{component.get('role', 'Food')}: "
            f"{display_name}"
        )

        if quantity != 1:
            line += f" × {quantity:g}"

        if calories is not None:
            scaled_calories = float(calories) * quantity
            total_calories += scaled_calories
            line += (
                " — "
                + format_display_number(
                    scaled_calories,
                    decimals=0,
                )
                + " calories"
            )

        if protein is not None:
            total_protein += float(protein) * quantity

        lines.append(line)

        source = component.get("verification_source")

        if source:
            lines.append(
                "Source: "
                + format_nutrition_source(source)
            )

    lines.extend(
        [
            "",
            f"Meal: {meal_category.title()}",
            (
                "Total calories: "
                + format_display_number(
                    total_calories,
                    decimals=0,
                )
            ),
            (
                "Total protein: "
                + format_display_number(
                    total_protein,
                    decimals=1,
                )
                + " g"
            ),
            "",
            "Nothing has been logged yet.",
            "",
            "1. Log It",
            "2. Edit",
            "3. Cancel",
        ]
    )

    return "\n".join(lines)


def process_telegram_update(update):
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    text = (message.get("text") or "").strip()

    if CHAT_ID and str(chat_id) != str(CHAT_ID):
        return

    if not text:
        return

    if text == "/status":
        metrics = get_today_metrics()
        send_telegram_msg(
            build_progress_message("Current status", metrics),
            chat_id=chat_id,
        )
        return

    lowered_text = text.lower().strip()

    if lowered_text in {
        "update unknown foods",
        "/updateunknownfoods",
        "update unresolved foods",
    }:
        pending = get_pending_unresolved_foods()

        if not pending:
            send_telegram_msg(
                "There are no unknown foods waiting for nutrition.",
                chat_id=chat_id,
            )
            return

        item = pending[0]

        lines = [
            f"Unknown food 1 of {len(pending)}",
            "",
            f"Date: {item.get('entry_date') or 'Unknown'}",
            (
                "Meal: "
                + str(item.get("meal_category") or "Unknown").title()
            ),
            (
                "Food: "
                + str(item.get("food_name") or "Unknown food")
            ),
        ]

        if item.get("brand"):
            lines.append(f"Brand: {item['brand']}")

        if item.get("restaurant"):
            lines.append(f"Restaurant: {item['restaurant']}")

        if item.get("quantity_description"):
            lines.append(
                f"Amount: {item['quantity_description']}"
            )

        lines.extend(
            [
                "",
                "Original message:",
                str(item.get("original_text") or ""),
                "",
                "1. Enter nutrition",
                "2. Change name/details",
                "3. Try automatic lookup again",
                "4. Skip for now",
                "5. Cancel this food",
            ]
        )

        send_telegram_msg(
            "\n".join(lines),
            chat_id=chat_id,
        )
        return

    active_conversation = get_active_conversation(chat_id)

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "portion_profile_clarification"
    ):
        known_data = dict(
            active_conversation.get("known_data") or {}
        )

        food_id = known_data.get("_portion_profile_food_id")
        phrase = known_data.get("_portion_profile_phrase")

        cleaned = text.strip().lower()

        match = re.fullmatch(
            r"((?:\d+(?:\.\d+)?)|(?:\.\d+))\s*"
            r"(serving|servings|g|gram|grams|grm|"
            r"oz|ounce|ounces)",
            cleaned,
        )

        if match is None:
            send_telegram_msg(
                "Please enter the amount as grams, ounces, "
                "or servings.\n\n"
                "Examples: 28 g, 1 oz, 1.5 servings.",
                chat_id=chat_id,
            )
            return

        amount = float(match.group(1))
        unit = match.group(2)

        if amount <= 0:
            send_telegram_msg(
                "The amount must be greater than zero.",
                chat_id=chat_id,
            )
            return

        if not food_id or not phrase:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "The portion setup could not be completed. "
                "Please send the food again.",
                chat_id=chat_id,
            )
            return

        try:
            save_portion_profile(
                food_id=int(food_id),
                phrase=str(phrase),
                estimated_amount=amount,
                estimated_unit=unit,
                user_confirmed=True,
            )
        except Exception:
            logging.exception(
                "Saving portion profile failed"
            )
            send_telegram_msg(
                "I could not save that portion profile.",
                chat_id=chat_id,
            )
            return

        known_data["quantity_description"] = str(phrase)

        update_conversation(
            chat_id=chat_id,
            current_step="confirmation",
            known_data=known_data,
            missing_fields=[],
        )

        model_data = {
            field: value
            for field, value in known_data.items()
            if field in FoodInterpretation.model_fields
        }

        interpretation = FoodInterpretation.model_validate(
            model_data
        )

        send_telegram_msg(
            "Saved your portion profile.\n\n"
            + format_food_interpretation(interpretation),
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "manual_label_offer"
    ):
        lowered = text.lower().strip()
        known_data = dict(
            active_conversation.get("known_data") or {}
        )

        if lowered in {
            "1",
            "enter",
            "enter package label nutrition",
            "manual",
        }:
            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "serving_size",
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "What serving size is listed on the package?\n\n"
                "Example: 28 g",
                chat_id=chat_id,
            )
            return

        if lowered in {
            "2",
            "try again",
            "different description",
        }:
            cancel_conversation(chat_id)

            send_telegram_msg(
                "Send the food description again with any "
                "additional brand, flavor, or product details.",
                chat_id=chat_id,
            )
            return

        if lowered in {
            "3",
            "save for later",
            "later",
            "save",
        }:
            original_message = (
                active_conversation.get("original_message")
                or known_data.get("food_name")
                or "Unresolved food"
            )

            try:
                saved_unresolved = save_unresolved_food(
                    entry_date=datetime.now(
                        PACIFIC_TZ
                    ).date().isoformat(),
                    meal_category=known_data.get(
                        "meal_category"
                    ),
                    original_text=original_message,
                    food_name=known_data.get("food_name"),
                    brand=known_data.get("brand"),
                    restaurant=known_data.get(
                        "restaurant"
                    ),
                    size=known_data.get("size"),
                    quantity=known_data.get("quantity"),
                    quantity_description=known_data.get(
                        "quantity_description"
                    ),
                )
            except Exception:
                logging.exception(
                    "Saving unresolved Food failed"
                )
                send_telegram_msg(
                    "I could not save that food for later.",
                    chat_id=chat_id,
                )
                return

            cancel_conversation(chat_id)

            send_telegram_msg(
                "Saved for later. No calories or nutrients "
                "were added yet.\n\n"
                "You can finish this food later with "
                "'update unknown foods'.",
                chat_id=chat_id,
            )
            return

        if lowered in {
            "4",
            "cancel",
            "no",
        }:
            cancel_conversation(chat_id)

            send_telegram_msg(
                "Food entry cancelled.",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Please choose:\n"
            "1. Enter package label nutrition\n"
            "2. Try a different description\n"
            "3. Save for later\n"
            "4. Cancel",
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "manual_label_entry"
    ):
        known_data = dict(
            active_conversation.get("known_data") or {}
        )

        current_field = known_data.get(
            "_manual_label_field"
        )

        if current_field == "serving_size":
            cleaned = text.strip().lower()

            match = re.fullmatch(
                r"(\d+(?:\.\d+)?)\s*"
                r"(g|gram|grams|grm|oz|ounce|ounces|"
                r"serving|servings)",
                cleaned,
            )

            if match is None:
                send_telegram_msg(
                    "Please enter the serving size as grams, "
                    "ounces, or servings.\n\n"
                    "Examples: 28 g, 1 oz, 1 serving.",
                    chat_id=chat_id,
                )
                return

            amount = float(match.group(1))
            unit = match.group(2)

            if amount <= 0:
                send_telegram_msg(
                    "The serving size must be greater than zero.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "calories",
                    "_manual_label_serving_amount": amount,
                    "_manual_label_serving_unit": unit,
                    "_manual_label_serving_description": (
                        text.strip()
                    ),
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many calories are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "calories":
            cleaned = text.strip()

            try:
                calories = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter calories as a number. "
                    "Example: 130",
                    chat_id=chat_id,
                )
                return

            if calories < 0:
                send_telegram_msg(
                    "Calories cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "protein_g",
                    "_manual_label_calories": calories,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many grams of protein are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "protein_g":
            cleaned = text.strip()

            try:
                protein_g = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter protein as a number. "
                    "Example: 2",
                    chat_id=chat_id,
                )
                return

            if protein_g < 0:
                send_telegram_msg(
                    "Protein cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "carbohydrates_g",
                    "_manual_label_protein_g": protein_g,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many grams of carbohydrates are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "carbohydrates_g":
            cleaned = text.strip()

            try:
                carbohydrates_g = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter carbohydrates as a number. "
                    "Example: 19",
                    chat_id=chat_id,
                )
                return

            if carbohydrates_g < 0:
                send_telegram_msg(
                    "Carbohydrates cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "fat_g",
                    "_manual_label_carbohydrates_g": carbohydrates_g,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many grams of fat are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "fat_g":
            cleaned = text.strip()

            try:
                fat_g = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter fat as a number. "
                    "Example: 6",
                    chat_id=chat_id,
                )
                return

            if fat_g < 0:
                send_telegram_msg(
                    "Fat cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "fiber_g",
                    "_manual_label_fat_g": fat_g,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many grams of fiber are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "fiber_g":
            cleaned = text.strip()

            try:
                fiber_g = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter fiber as a number. "
                    "Example: 0",
                    chat_id=chat_id,
                )
                return

            if fiber_g < 0:
                send_telegram_msg(
                    "Fiber cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "sugar_g",
                    "_manual_label_fiber_g": fiber_g,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many grams of sugar are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "sugar_g":
            cleaned = text.strip()

            try:
                sugar_g = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter sugar as a number. "
                    "Example: 9",
                    chat_id=chat_id,
                )
                return

            if sugar_g < 0:
                send_telegram_msg(
                    "Sugar cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "sodium_mg",
                    "_manual_label_sugar_g": sugar_g,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "How many milligrams of sodium are listed per serving?",
                chat_id=chat_id,
            )
            return

        if current_field == "sodium_mg":
            cleaned = text.strip()

            try:
                sodium_mg = float(cleaned)
            except ValueError:
                send_telegram_msg(
                    "Please enter sodium as a number. "
                    "Example: 180",
                    chat_id=chat_id,
                )
                return

            if sodium_mg < 0:
                send_telegram_msg(
                    "Sodium cannot be negative.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_confirmation",
                known_data={
                    **known_data,
                    "_manual_label_field": None,
                    "_manual_label_sodium_mg": sodium_mg,
                },
                missing_fields=[],
            )

            serving_description = known_data.get(
                "_manual_label_serving_description"
            )

            send_telegram_msg(
                "Package label entered:\n\n"
                f"Serving: {serving_description}\n"
                f"Calories: {known_data.get('_manual_label_calories'):g}\n"
                f"Protein: {known_data.get('_manual_label_protein_g'):g} g\n"
                f"Carbohydrates: {known_data.get('_manual_label_carbohydrates_g'):g} g\n"
                f"Fat: {known_data.get('_manual_label_fat_g'):g} g\n"
                f"Fiber: {known_data.get('_manual_label_fiber_g'):g} g\n"
                f"Sugar: {known_data.get('_manual_label_sugar_g'):g} g\n"
                f"Sodium: {sodium_mg:g} mg\n\n"
                "1. Save\n"
                "2. Edit\n"
                "3. Cancel",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Manual label entry is incomplete. "
            "Please start the food entry again.",
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "manual_label_confirmation"
    ):
        lowered = text.lower().strip()
        known_data = dict(
            active_conversation.get("known_data") or {}
        )

        if lowered in {"1", "save", "yes"}:
            food_name = known_data.get("food_name")
            brand = known_data.get("brand")
            meal_category = known_data.get("meal_category")

            serving_amount = known_data.get(
                "_manual_label_serving_amount"
            )
            serving_unit = known_data.get(
                "_manual_label_serving_unit"
            )
            serving_description = known_data.get(
                "_manual_label_serving_description"
            )

            if (
                not food_name
                or not meal_category
                or serving_amount is None
                or not serving_unit
                or not serving_description
            ):
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "The package-label entry is incomplete, "
                    "so nothing was saved.",
                    chat_id=chat_id,
                )
                return

            try:
                saved = add_food_with_nutrition(
                    canonical_name=food_name,
                    serving_description=serving_description,
                    serving_amount=float(serving_amount),
                    serving_unit=str(serving_unit),
                    verification_status="verified",
                    verification_source="user_package_label",
                    calories=known_data.get(
                        "_manual_label_calories"
                    ),
                    protein_g=known_data.get(
                        "_manual_label_protein_g"
                    ),
                    carbohydrates_g=known_data.get(
                        "_manual_label_carbohydrates_g"
                    ),
                    fat_g=known_data.get(
                        "_manual_label_fat_g"
                    ),
                    fiber_g=known_data.get(
                        "_manual_label_fiber_g"
                    ),
                    sugar_g=known_data.get(
                        "_manual_label_sugar_g"
                    ),
                    sodium_mg=known_data.get(
                        "_manual_label_sodium_mg"
                    ),
                    brand=brand,
                    restaurant=None,
                    food_type="food",
                    source_item_id=None,
                    source_url=None,
                )
            except Exception:
                logging.exception(
                    "Manual package-label Food save failed"
                )
                send_telegram_msg(
                    "I could not save the package-label nutrition.",
                    chat_id=chat_id,
                )
                return

            saved_food = saved["food"]
            saved_nutrition = saved["nutrition"] or {}

            try:
                quantity = resolve_packaged_serving_multiplier(
                    food_id=saved_food["food_id"],
                    quantity=known_data.get("quantity"),
                    quantity_description=known_data.get(
                        "quantity_description"
                    ),
                    serving_amount=saved_food.get(
                        "serving_amount"
                    ),
                    serving_unit=saved_food.get(
                        "serving_unit"
                    ),
                )
            except ValueError as error:
                send_telegram_msg(
                    str(error),
                    chat_id=chat_id,
                )
                return

            pending_components = [
                {
                    "role": "Food",
                    "food_id": saved_food["food_id"],
                    "canonical_name": saved_food[
                        "canonical_name"
                    ],
                    "restaurant": None,
                    "size": None,
                    "quantity": quantity,
                    "calories": saved_nutrition.get(
                        "calories"
                    ),
                    "protein_g": saved_nutrition.get(
                        "protein_g"
                    ),
                    "verification_source": (
                        "user_package_label"
                    ),
                }
            ]

            update_conversation(
                chat_id=chat_id,
                current_step="nutrition_confirmation",
                known_data={
                    **known_data,
                    "_pending_components": pending_components,
                },
                missing_fields=[],
            )

            prompt_message_id = send_telegram_msg(
                format_pending_nutrition_confirmation(
                    pending_components,
                    meal_category=meal_category,
                ),
                chat_id=chat_id,
            )

            if isinstance(prompt_message_id, int):
                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_nutrition_prompt_message_id": (
                            prompt_message_id
                        ),
                    },
                )

            return

        if lowered in {"2", "edit"}:
            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "serving_size",
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "Let's re-enter the package label.\n\n"
                "What serving size is listed on the package?\n"
                "Example: 28 g",
                chat_id=chat_id,
            )
            return

        if lowered in {"3", "cancel", "no"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Food entry cancelled. Nothing was saved.",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Please choose:\n"
            "1. Save\n"
            "2. Edit\n"
            "3. Cancel",
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "nutrition_confirmation"
    ):
        lowered = text.lower().strip()
        known_data = active_conversation.get("known_data") or {}

        prompt_message_id = known_data.get(
            "_nutrition_prompt_message_id"
        )

        if (
            prompt_message_id is not None
            and message_id is not None
            and int(message_id) <= int(prompt_message_id)
        ):
            logging.info(
                "Ignored Telegram reply %s because it predates "
                "nutrition prompt %s",
                message_id,
                prompt_message_id,
            )
            return

        pending_components = (
            known_data.get("_pending_components") or []
        )
        meal_category = known_data.get("meal_category")
        restaurant = known_data.get("restaurant")
        original_message = active_conversation.get(
            "original_message"
        )

        if known_data.get("_duplicate_warning_pending"):
            if lowered in {
                "1",
                "yes",
                "y",
                "log another",
                "log it again",
            }:
                known_data = {
                    **known_data,
                    "_duplicate_warning_pending": False,
                    "_duplicate_override": True,
                }

                update_conversation(
                    chat_id=chat_id,
                    known_data=known_data,
                )

            elif lowered in {
                "2",
                "no",
                "n",
                "cancel",
                "keep first",
            }:
                cancel_conversation(chat_id)

                send_telegram_msg(
                    "Duplicate entry cancelled. "
                    "The earlier food entry was kept.",
                    chat_id=chat_id,
                )
                return

            else:
                send_telegram_msg(
                    "It looks like this food was just logged.\n\n"
                    "1. Yes, log another\n"
                    "2. No, keep only the first one",
                    chat_id=chat_id,
                )
                return

        if lowered in {"1", "log", "log it", "correct", "yes"}:
            if not pending_components or not meal_category:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "The pending nutrition record is incomplete, "
                    "so nothing was logged.",
                    chat_id=chat_id,
                )
                return

            duplicate_override = bool(
                known_data.get("_duplicate_override")
            )

            if not duplicate_override:
                duplicate_component = None
                duplicate_entry = None
                today = datetime.now(PACIFIC_TZ).date()

                for component in pending_components:
                    duplicate = find_recent_duplicate_entry(
                        entry_date=today,
                        meal_category=meal_category,
                        food_id=int(component["food_id"]),
                        quantity=float(
                            component.get("quantity") or 1.0
                        ),
                        window_minutes=5,
                    )

                    if duplicate is not None:
                        duplicate_component = component
                        duplicate_entry = duplicate
                        break

                if duplicate_entry is not None:
                    display_name = format_food_display_name(
                        canonical_name=duplicate_component[
                            "canonical_name"
                        ],
                        restaurant=duplicate_component.get(
                            "restaurant"
                        ),
                        size=duplicate_component.get("size"),
                    )

                    update_conversation(
                        chat_id=chat_id,
                        known_data={
                            **known_data,
                            "_duplicate_warning_pending": True,
                        },
                    )

                    send_telegram_msg(
                        "It looks like you just logged this:\n\n"
                        f"{display_name}\n"
                        f"{format_display_number(float(duplicate_entry['calories'] or 0), decimals=0)} calories\n\n"
                        "Log another one?\n"
                        "1. Yes, log another\n"
                        "2. No, keep only the first one",
                        chat_id=chat_id,
                    )
                    return

            logged_entries = []

            try:
                for component in pending_components:
                    entry = add_food_entry(
                        entry_date=datetime.now(
                            PACIFIC_TZ
                        ).date(),
                        meal_category=meal_category,
                        food_id=int(component["food_id"]),
                        quantity=float(
                            component.get("quantity") or 1.0
                        ),
                        logging_source="telegram_ai",
                        original_text=original_message,
                        quantity_is_estimated=False,
                        user_confirmed=True,
                    )

                    logged_entries.append(
                        {
                            **component,
                            "entry": entry,
                        }
                    )
            except Exception:
                logging.exception(
                    "Pending nutrition logging failed"
                )

                for logged in logged_entries:
                    try:
                        delete_food_entry(
                            logged["entry"]["food_entry_id"]
                        )
                    except Exception:
                        logging.exception(
                            "Pending nutrition rollback failed"
                        )

                send_telegram_msg(
                    "The food could not be completely logged. "
                    "Any partial ledger entries were removed.",
                    chat_id=chat_id,
                )
                return

            try:
                sync_food_ledger_totals_to_sheet(
                    datetime.now(PACIFIC_TZ).date()
                )
            except Exception:
                logging.exception(
                    "Food Ledger Google Sheet sync failed"
                )
                send_telegram_msg(
                    "Food was logged, but today's Google Sheet "
                    "nutrition totals could not be updated.",
                    chat_id=chat_id,
                )

            complete_conversation(chat_id)

            start_conversation(
                chat_id=chat_id,
                conversation_type="food_meal",
                current_step=(
                    "restaurant_anything_else"
                    if restaurant
                    else "meal_anything_else"
                ),
                known_data={
                    "meal_category": meal_category,
                    "restaurant": restaurant,
                },
                missing_fields=[],
                original_message=original_message,
            )

            lines = [
                (
                    "Meal logged."
                    if len(logged_entries) > 1
                    else "Food logged."
                ),
                "",
            ]

            for component in logged_entries:
                entry = component["entry"]

                display_name = format_food_display_name(
                    canonical_name=component["canonical_name"],
                    restaurant=component.get("restaurant"),
                    size=component.get("size"),
                )

                line = (
                    f"{component.get('role', 'Food')}: "
                    f"{display_name}"
                )

                if entry.get("calories") is not None:
                    line += (
                        " — "
                        + format_display_number(
                            float(entry["calories"]),
                            decimals=0,
                        )
                        + " calories"
                    )

                lines.append(line)

            lines.extend(
                [
                    "",
                    (
                        f"Anything else from {restaurant}?"
                        if restaurant
                        else (
                            f"Anything else for "
                            f"{meal_category.title()}?"
                        )
                    ),
                    "1. Yes",
                    "2. No",
                ]
            )

            prompt_message_id = send_telegram_msg(
                "\n".join(lines),
                chat_id=chat_id,
            )

            if isinstance(prompt_message_id, int):
                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_food_meal_prompt_message_id": (
                            prompt_message_id
                        ),
                    },
                )

            return

        if lowered in {"2", "edit"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Send the corrected food description as a new message.",
                chat_id=chat_id,
            )
            return

        if lowered in {"3", "cancel", "no"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Food entry cancelled. Nothing was logged.",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Please reply:\n"
            "1. Log It\n"
            "2. Edit\n"
            "3. Cancel",
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_meal"
        and active_conversation.get("current_step")
        == "restaurant_anything_else"
    ):
        lowered = text.lower().strip()
        known_data = active_conversation.get("known_data") or {}
        meal_category = known_data.get("meal_category")
        restaurant = known_data.get("restaurant")

        prompt_message_id = known_data.get(
            "_food_meal_prompt_message_id"
        )

        if (
            prompt_message_id is not None
            and message_id is not None
            and int(message_id) <= int(prompt_message_id)
        ):
            logging.info(
                "Ignored Telegram reply %s because it predates "
                "food-meal prompt %s",
                message_id,
                prompt_message_id,
            )
            return

        if lowered in {
            "1",
            "yes",
            "y",
            "add another food",
            "another",
        }:
            update_conversation(
                chat_id=chat_id,
                current_step="awaiting_food",
                known_data={
                    "meal_category": meal_category,
                    "restaurant": restaurant,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                f"What else did you have from {restaurant}?",
                chat_id=chat_id,
            )
            return

        if lowered in {
            "2",
            "no",
            "n",
            "finished",
            "done",
            "nothing else",
        }:
            update_conversation(
                chat_id=chat_id,
                current_step="meal_anything_else",
                known_data={
                    "meal_category": meal_category,
                    "restaurant": None,
                },
                missing_fields=[],
            )

            prompt_message_id = send_telegram_msg(
                f"Anything else for {meal_category.title()}?\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )

            if isinstance(prompt_message_id, int):
                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_food_meal_prompt_message_id": (
                            prompt_message_id
                        ),
                    },
                )

            return

        prompt_message_id = send_telegram_msg(
            f"Anything else from {restaurant}?\n"
            "1. Yes\n"
            "2. No",
            chat_id=chat_id,
        )

        if isinstance(prompt_message_id, int):
            update_conversation(
                chat_id=chat_id,
                known_data={
                    "_food_meal_prompt_message_id": (
                        prompt_message_id
                    ),
                },
            )

        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_meal"
        and active_conversation.get("current_step")
        == "meal_anything_else"
    ):
        lowered = text.lower().strip()
        known_data = active_conversation.get("known_data") or {}
        meal_category = known_data.get("meal_category")

        prompt_message_id = known_data.get(
            "_food_meal_prompt_message_id"
        )

        if (
            prompt_message_id is not None
            and message_id is not None
            and int(message_id) <= int(prompt_message_id)
        ):
            logging.info(
                "Ignored Telegram reply %s because it predates "
                "food-meal prompt %s",
                message_id,
                prompt_message_id,
            )
            return

        if lowered in {
            "1",
            "yes",
            "y",
            "add another food",
            "another",
        }:
            update_conversation(
                chat_id=chat_id,
                current_step="awaiting_food",
                known_data={
                    "meal_category": meal_category,
                    "restaurant": None,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                f"What else did you have for "
                f"{meal_category.title()}?",
                chat_id=chat_id,
            )
            return

        if lowered in {
            "2",
            "no",
            "n",
            "finished",
            "done",
            "nothing else",
        }:
            complete_conversation(chat_id)

            try:
                totals_message = format_daily_food_totals()
            except Exception:
                logging.exception(
                    "Daily Food Ledger totals failed"
                )
                send_telegram_msg(
                    f"{meal_category.title()} is finished, "
                    "but I could not calculate today's totals.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                f"{meal_category.title()} is finished.\n\n"
                f"{totals_message}",
                chat_id=chat_id,
            )
            return

        prompt_message_id = send_telegram_msg(
            f"Anything else for {meal_category.title()}?\n"
            "1. Yes\n"
            "2. No",
            chat_id=chat_id,
        )

        if isinstance(prompt_message_id, int):
            update_conversation(
                chat_id=chat_id,
                known_data={
                    "_food_meal_prompt_message_id": (
                        prompt_message_id
                    ),
                },
            )

        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_meal"
        and active_conversation.get("current_step")
        == "awaiting_food"
    ):
        meal_context = (
            active_conversation.get("known_data") or {}
        )
        meal_category = meal_context.get("meal_category")
        restaurant_context = meal_context.get("restaurant")

        try:
            interpretation = interpret_food_message(text)
        except Exception:
            logging.exception(
                "Additional meal food interpretation failed"
            )
            send_telegram_msg(
                "I could not understand that food. "
                "Please describe it again.",
                chat_id=chat_id,
            )
            return

        if not interpretation.is_food_logging_request:
            send_telegram_msg(
                "Please send the food you want to add.",
                chat_id=chat_id,
            )
            return

        interpretation.meal_category = meal_category

        if (
            restaurant_context
            and not interpretation.restaurant
        ):
            interpretation.restaurant = restaurant_context

        interpretation = clean_interpretation_missing_fields(
            interpretation
        )

        interpretation.missing_fields = [
            field
            for field in interpretation.missing_fields
            if field != "meal_category"
        ]

        if not interpretation.missing_fields:
            interpretation.clarification_question = None

        start_conversation(
            chat_id=chat_id,
            conversation_type="food_interpretation",
            current_step=(
                "confirmation"
                if not interpretation.missing_fields
                else "clarification"
            ),
            known_data=interpretation.model_dump(),
            missing_fields=interpretation.missing_fields,
            original_message=text,
        )

        send_telegram_msg(
            format_food_interpretation(interpretation),
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "clarification"
    ):
        known_data = dict(
            active_conversation.get("known_data") or {}
        )
        previous_missing = list(
            active_conversation.get("missing_fields")
            or known_data.get("missing_fields")
            or []
        )

        if "quantity_description" in previous_missing:
            cleaned_amount = text.strip().lower()

            valid_amount = (
                re.fullmatch(
                    r"\d+(?:\.\d+)?\s*"
                    r"(?:serving|servings|g|gram|grams|grm|"
                    r"oz|ounce|ounces)",
                    cleaned_amount,
                )
                is not None
                or "handful" in cleaned_amount
            )

            if valid_amount:
                known_data["quantity_description"] = text.strip()

                remaining_missing = [
                    field
                    for field in previous_missing
                    if field != "quantity_description"
                ]

                known_data["missing_fields"] = remaining_missing

                if remaining_missing:
                    question_by_field = {
                        "restaurant": "What restaurant was this from?",
                        "brand": "What brand was it?",
                        "food_name": "What food did you have?",
                        "size": "What size was it?",
                        "quantity": "How many did you have?",
                        "quantity_description": "How much did you have?",
                        "meal_category": "Which meal was this for?",
                        "drink": "What drink did you have?",
                        "drink_detail": "What drink did you have?",
                        "food_name_detail": (
                            "Which exact menu item did you have?"
                        ),
                    }

                    next_field = remaining_missing[0]

                    update_conversation(
                        chat_id=chat_id,
                        current_step="clarification",
                        known_data=known_data,
                        missing_fields=remaining_missing,
                    )

                    send_telegram_msg(
                        question_by_field.get(
                            next_field,
                            "What detail should I add?",
                        ),
                        chat_id=chat_id,
                    )
                    return

                known_data["clarification_question"] = None

                update_conversation(
                    chat_id=chat_id,
                    current_step="confirmation",
                    known_data=known_data,
                    missing_fields=[],
                )

                model_data = {
                    field: value
                    for field, value in known_data.items()
                    if field in FoodInterpretation.model_fields
                }

                interpretation = FoodInterpretation.model_validate(
                    model_data
                )

                send_telegram_msg(
                    format_food_interpretation(interpretation),
                    chat_id=chat_id,
                )
                return

        base_text = (
            known_data.get("_accumulated_text")
            or active_conversation.get("original_message")
            or known_data.get("food_name")
            or ""
        )

        accumulated_text = (
            f"{base_text}. Additional detail: {text}"
            if base_text
            else text
        )

        try:
            clarification = interpret_food_message(
                accumulated_text
            )
        except Exception:
            logging.exception(
                "Food clarification interpretation failed"
            )
            send_telegram_msg(
                "I could not understand that clarification. "
                "Please try again.",
                chat_id=chat_id,
            )
            return

        merge_fields = (
            "restaurant",
            "brand",
            "food_name",
            "size",
            "quantity",
            "quantity_description",
            "meal_category",
            "drink",
        )

        for field in merge_fields:
            existing_value = known_data.get(field)
            new_value = getattr(clarification, field)

            if existing_value in (None, "") and new_value not in (
                None,
                "",
            ):
                known_data[field] = new_value

        if "food_name_detail" in previous_missing:
            clarified_food_name = clarification.food_name

            if clarified_food_name not in (None, ""):
                known_data["food_name"] = clarified_food_name

        if "drink_detail" in previous_missing:
            clarified_drink = (
                clarification.drink
                or clarification.food_name
            )

            if clarified_drink not in (
                None,
                "",
                "drink",
                "beverage",
            ):
                known_data["drink"] = clarified_drink
                known_data["food_name"] = clarified_drink

        current_missing_field = (
            previous_missing[0]
            if previous_missing
            else None
        )

        if known_data.get("is_combo_meal"):
            lowered_answer = text.lower().strip()

            size_words = {
                "small",
                "medium",
                "large",
            }

            detected_size = clarification.size

            if detected_size in (None, ""):
                for size_word in size_words:
                    if size_word in lowered_answer.split():
                        detected_size = size_word
                        break

            if (
                current_missing_field == "size"
                and detected_size not in (None, "")
            ):
                known_data["size"] = detected_size
                known_data["combo_side_size"] = detected_size
                known_data["combo_drink_size"] = detected_size

            clarified_combo_drink = clarification.drink

            if (
                current_missing_field == "drink"
                and clarified_combo_drink in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                clarified_combo_drink = text.strip()

            if (
                current_missing_field == "drink"
                and clarified_combo_drink not in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                known_data["drink"] = clarified_combo_drink
                known_data["combo_drink"] = clarified_combo_drink

            if (
                current_missing_field == "size"
                and clarification.drink not in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                known_data["drink"] = clarification.drink
                known_data["combo_drink"] = clarification.drink

        known_data["_accumulated_text"] = accumulated_text

        candidate_missing = []

        for field in (
            previous_missing
            + list(clarification.missing_fields)
        ):
            if field not in candidate_missing:
                candidate_missing.append(field)

        clarification_priority = (
            "restaurant",
            "brand",
            "size",
            "quantity",
            "quantity_description",
            "drink_detail",
            "drink",
            "meal_category",
            "food_name_detail",
            "food_name",
        )

        unresolved_fields = {
            field
            for field in candidate_missing
            if (
                field in {
                    "food_name_detail",
                    "drink_detail",
                }
                or known_data.get(field) in (None, "")
            )
        }

        if (
            "drink_detail" in unresolved_fields
            and clarification.drink not in (
                None,
                "",
                "drink",
                "beverage",
            )
        ):
            unresolved_fields.remove("drink_detail")

        if (
            "food_name_detail" in unresolved_fields
            and clarification.food_name not in (None, "")
            and normalize_signature_food(
                clarification.food_name
            ) not in {
                "burger",
                "burgers",
                "burrito",
                "burritos",
                "cheeseburger",
                "cheeseburgers",
                "fries",
                "french fries",
                "pizza",
                "sandwich",
                "sandwiches",
                "salad",
                "salads",
                "taco",
                "tacos",
            }
        ):
            unresolved_fields.remove("food_name_detail")

        remaining_missing = [
            field
            for field in clarification_priority
            if field in unresolved_fields
        ]

        remaining_missing.extend(
            field
            for field in candidate_missing
            if (
                field in unresolved_fields
                and field not in remaining_missing
            )
        )

        known_data["missing_fields"] = remaining_missing

        question_by_field = {
            "restaurant": "What restaurant was this from?",
            "brand": "What brand was it?",
            "food_name": "What food did you have?",
            "size": "What size was it?",
            "quantity": "How many did you have?",
            "quantity_description": "How much did you have?",
            "meal_category": "Which meal was this for?",
            "drink": "What drink did you have?",
            "drink_detail": "What drink did you have?",
            "food_name_detail": (
                "Which exact menu item did you have?"
            ),
        }

        if remaining_missing:
            next_field = remaining_missing[0]
            question = question_by_field.get(
                next_field,
                "What detail should I add?",
            )

            known_data["clarification_question"] = question

            update_conversation(
                chat_id=chat_id,
                current_step="clarification",
                known_data=known_data,
                missing_fields=remaining_missing,
            )

            send_telegram_msg(
                question,
                chat_id=chat_id,
            )
            return

        known_data["clarification_question"] = None

        update_conversation(
            chat_id=chat_id,
            current_step="confirmation",
            known_data=known_data,
            missing_fields=[],
        )

        model_data = {
            field: value
            for field, value in known_data.items()
            if field in FoodInterpretation.model_fields
        }

        interpretation = FoodInterpretation.model_validate(
            model_data
        )

        send_telegram_msg(
            format_food_interpretation(interpretation),
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "clarification"
    ):
        known_data = dict(
            active_conversation.get("known_data") or {}
        )
        previous_missing = list(
            active_conversation.get("missing_fields")
            or known_data.get("missing_fields")
            or []
        )

        if "quantity_description" in previous_missing:
            cleaned_amount = text.strip().lower()

            valid_amount = (
                re.fullmatch(
                    r"\d+(?:\.\d+)?\s*"
                    r"(?:serving|servings|g|gram|grams|grm|"
                    r"oz|ounce|ounces)",
                    cleaned_amount,
                )
                is not None
                or "handful" in cleaned_amount
            )

            if valid_amount:
                known_data["quantity_description"] = text.strip()

                remaining_missing = [
                    field
                    for field in previous_missing
                    if field != "quantity_description"
                ]

                known_data["missing_fields"] = remaining_missing

                if remaining_missing:
                    question_by_field = {
                        "restaurant": "What restaurant was this from?",
                        "brand": "What brand was it?",
                        "food_name": "What food did you have?",
                        "size": "What size was it?",
                        "quantity": "How many did you have?",
                        "quantity_description": "How much did you have?",
                        "meal_category": "Which meal was this for?",
                        "drink": "What drink did you have?",
                        "drink_detail": "What drink did you have?",
                        "food_name_detail": (
                            "Which exact menu item did you have?"
                        ),
                    }

                    next_field = remaining_missing[0]

                    update_conversation(
                        chat_id=chat_id,
                        current_step="clarification",
                        known_data=known_data,
                        missing_fields=remaining_missing,
                    )

                    send_telegram_msg(
                        question_by_field.get(
                            next_field,
                            "What detail should I add?",
                        ),
                        chat_id=chat_id,
                    )
                    return

                known_data["clarification_question"] = None

                update_conversation(
                    chat_id=chat_id,
                    current_step="confirmation",
                    known_data=known_data,
                    missing_fields=[],
                )

                model_data = {
                    field: value
                    for field, value in known_data.items()
                    if field in FoodInterpretation.model_fields
                }

                interpretation = FoodInterpretation.model_validate(
                    model_data
                )

                send_telegram_msg(
                    format_food_interpretation(interpretation),
                    chat_id=chat_id,
                )
                return

        base_text = (
            known_data.get("_accumulated_text")
            or active_conversation.get("original_message")
            or known_data.get("food_name")
            or ""
        )

        accumulated_text = (
            f"{base_text}. Additional detail: {text}"
            if base_text
            else text
        )

        try:
            clarification = interpret_food_message(
                accumulated_text
            )
        except Exception:
            logging.exception(
                "Food clarification interpretation failed"
            )
            send_telegram_msg(
                "I could not understand that clarification. "
                "Please try again.",
                chat_id=chat_id,
            )
            return

        merge_fields = (
            "restaurant",
            "brand",
            "food_name",
            "size",
            "quantity",
            "quantity_description",
            "meal_category",
            "drink",
        )

        for field in merge_fields:
            existing_value = known_data.get(field)
            new_value = getattr(clarification, field)

            if existing_value in (None, "") and new_value not in (
                None,
                "",
            ):
                known_data[field] = new_value

        if "food_name_detail" in previous_missing:
            clarified_food_name = clarification.food_name

            if clarified_food_name not in (None, ""):
                known_data["food_name"] = clarified_food_name

        if "drink_detail" in previous_missing:
            clarified_drink = (
                clarification.drink
                or clarification.food_name
            )

            if clarified_drink not in (
                None,
                "",
                "drink",
                "beverage",
            ):
                known_data["drink"] = clarified_drink
                known_data["food_name"] = clarified_drink

        current_missing_field = (
            previous_missing[0]
            if previous_missing
            else None
        )

        if known_data.get("is_combo_meal"):
            lowered_answer = text.lower().strip()

            size_words = {
                "small",
                "medium",
                "large",
            }

            detected_size = clarification.size

            if detected_size in (None, ""):
                for size_word in size_words:
                    if size_word in lowered_answer.split():
                        detected_size = size_word
                        break

            if (
                current_missing_field == "size"
                and detected_size not in (None, "")
            ):
                known_data["size"] = detected_size
                known_data["combo_side_size"] = detected_size
                known_data["combo_drink_size"] = detected_size

            clarified_combo_drink = clarification.drink

            if (
                current_missing_field == "drink"
                and clarified_combo_drink in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                clarified_combo_drink = text.strip()

            if (
                current_missing_field == "drink"
                and clarified_combo_drink not in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                known_data["drink"] = clarified_combo_drink
                known_data["combo_drink"] = clarified_combo_drink

            if (
                current_missing_field == "size"
                and clarification.drink not in (
                    None,
                    "",
                    "drink",
                    "beverage",
                )
            ):
                known_data["drink"] = clarification.drink
                known_data["combo_drink"] = clarification.drink

        known_data["_accumulated_text"] = accumulated_text

        candidate_missing = []

        for field in (
            previous_missing
            + list(clarification.missing_fields)
        ):
            if field not in candidate_missing:
                candidate_missing.append(field)

        clarification_priority = (
            "restaurant",
            "brand",
            "size",
            "quantity",
            "quantity_description",
            "drink_detail",
            "drink",
            "meal_category",
            "food_name_detail",
            "food_name",
        )

        unresolved_fields = {
            field
            for field in candidate_missing
            if (
                field in {
                    "food_name_detail",
                    "drink_detail",
                }
                or known_data.get(field) in (None, "")
            )
        }

        if (
            "drink_detail" in unresolved_fields
            and clarification.drink not in (
                None,
                "",
                "drink",
                "beverage",
            )
        ):
            unresolved_fields.remove("drink_detail")

        if (
            "food_name_detail" in unresolved_fields
            and clarification.food_name not in (None, "")
            and normalize_signature_food(
                clarification.food_name
            ) not in {
                "burger",
                "burgers",
                "burrito",
                "burritos",
                "cheeseburger",
                "cheeseburgers",
                "fries",
                "french fries",
                "pizza",
                "sandwich",
                "sandwiches",
                "salad",
                "salads",
                "taco",
                "tacos",
            }
        ):
            unresolved_fields.remove("food_name_detail")

        remaining_missing = [
            field
            for field in clarification_priority
            if field in unresolved_fields
        ]

        remaining_missing.extend(
            field
            for field in candidate_missing
            if (
                field in unresolved_fields
                and field not in remaining_missing
            )
        )

        known_data["missing_fields"] = remaining_missing

        question_by_field = {
            "restaurant": "What restaurant was this from?",
            "brand": "What brand was it?",
            "food_name": "What food did you have?",
            "size": "What size was it?",
            "quantity": "How many did you have?",
            "quantity_description": "How much did you have?",
            "meal_category": "Which meal was this for?",
            "drink": "What drink did you have?",
            "drink_detail": "What drink did you have?",
            "food_name_detail": (
                "Which exact menu item did you have?"
            ),
        }

        if remaining_missing:
            next_field = remaining_missing[0]
            question = question_by_field.get(
                next_field,
                "What detail should I add?",
            )

            known_data["clarification_question"] = question

            update_conversation(
                chat_id=chat_id,
                current_step="clarification",
                known_data=known_data,
                missing_fields=remaining_missing,
            )

            send_telegram_msg(
                question,
                chat_id=chat_id,
            )
            return

        known_data["clarification_question"] = None

        update_conversation(
            chat_id=chat_id,
            current_step="confirmation",
            known_data=known_data,
            missing_fields=[],
        )

        model_data = {
            field: value
            for field, value in known_data.items()
            if field in FoodInterpretation.model_fields
        }

        interpretation = FoodInterpretation.model_validate(
            model_data
        )

        send_telegram_msg(
            format_food_interpretation(interpretation),
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "meal_selection"
    ):
        known_data = active_conversation.get("known_data") or {}
        meal_options = (
            known_data.get("_meal_options")
            or get_time_aware_meal_options()
        )

        selected_meal = parse_meal_selection(
            text,
            meal_options,
        )

        if selected_meal is None:
            lines = ["Please choose one of these meals:"]

            for index, option in enumerate(
                meal_options,
                start=1,
            ):
                lines.append(f"{index}. {option.title()}")

            send_telegram_msg(
                "\n".join(lines),
                chat_id=chat_id,
            )
            return

        known_data["meal_category"] = selected_meal
        known_data["missing_fields"] = [
            field
            for field in known_data.get(
                "missing_fields",
                [],
            )
            if field != "meal_category"
        ]
        known_data["clarification_question"] = None

        update_conversation(
            chat_id=chat_id,
            current_step="confirmation",
            known_data=known_data,
            missing_fields=[],
        )

        interpretation = FoodInterpretation.model_validate(
            known_data
        )

        send_telegram_msg(
            format_food_interpretation(interpretation),
            chat_id=chat_id,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_interpretation"
        and active_conversation.get("current_step")
        == "confirmation"
    ):
        lowered = text.lower().strip()

        if lowered in {"1", "correct", "yes"}:
            known_data = active_conversation.get("known_data") or {}

            food_name = known_data.get("food_name")
            size = known_data.get("size")
            brand = known_data.get("brand")
            restaurant = known_data.get("restaurant")

            if known_data.get("is_combo_meal"):
                meal_category = known_data.get(
                    "meal_category"
                )
                quantity = float(
                    known_data.get("quantity") or 1.0
                )
                original_message = (
                    active_conversation.get(
                        "original_message"
                    )
                )

                combo_components = [
                    {
                        "role": "Entrée",
                        "food_name": known_data.get(
                            "combo_entree"
                        ),
                        "size": None,
                    },
                    {
                        "role": "Side",
                        "food_name": known_data.get(
                            "combo_side"
                        ),
                        "size": known_data.get(
                            "combo_side_size"
                        ),
                    },
                    {
                        "role": "Drink",
                        "food_name": known_data.get(
                            "combo_drink"
                        ),
                        "size": known_data.get(
                            "combo_drink_size"
                        ),
                    },
                ]

                missing_components = [
                    component["role"]
                    for component in combo_components
                    if not component["food_name"]
                ]

                if not meal_category:
                    send_telegram_msg(
                        "The combo meal category is missing, "
                        "so nothing was logged.",
                        chat_id=chat_id,
                    )
                    return

                if missing_components:
                    send_telegram_msg(
                        "The combo is missing: "
                        + ", ".join(missing_components)
                        + ". Nothing was logged.",
                        chat_id=chat_id,
                    )
                    return

                resolved_components = []

                try:
                    for component in combo_components:
                        resolved = (
                            resolve_or_create_verified_food(
                                food_name=component[
                                    "food_name"
                                ],
                                size=component["size"],
                                restaurant=restaurant,
                                brand=None,
                            )
                        )

                        resolved_components.append(
                            {
                                **component,
                                **resolved,
                            }
                        )
                except Exception as error:
                    logging.exception(
                        "Combo nutrition resolution failed"
                    )
                    send_telegram_msg(
                        "I could not verify every component "
                        "of the combo, so nothing was logged.\n\n"
                        f"{error}",
                        chat_id=chat_id,
                    )
                    return

                pending_components = []

                for component in resolved_components:
                    food = component["food"]
                    nutrition = component["nutrition"] or {}

                    pending_components.append(
                        {
                            "role": component["role"],
                            "food_id": food["food_id"],
                            "canonical_name": food[
                                "canonical_name"
                            ],
                            "restaurant": food.get(
                                "restaurant"
                            ),
                            "size": component.get("size"),
                            "quantity": quantity,
                            "calories": nutrition.get(
                                "calories"
                            ),
                            "protein_g": nutrition.get(
                                "protein_g"
                            ),
                            "verification_source": (
                                component.get(
                                    "verification_source"
                                )
                                or food.get(
                                    "verification_source"
                                )
                            ),
                        }
                    )

                update_conversation(
                    chat_id=chat_id,
                    current_step="nutrition_confirmation",
                    known_data={
                        **known_data,
                        "_pending_components": (
                            pending_components
                        ),
                    },
                    missing_fields=[],
                )

                prompt_message_id = send_telegram_msg(
                    format_pending_nutrition_confirmation(
                        pending_components,
                        meal_category=meal_category,
                    ),
                    chat_id=chat_id,
                )

                if isinstance(prompt_message_id, int):
                    update_conversation(
                        chat_id=chat_id,
                        known_data={
                            "_nutrition_prompt_message_id": (
                                prompt_message_id
                            ),
                        },
                    )

                return

            if not food_name:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "I could not identify the food name. "
                    "Please send the food description again.",
                    chat_id=chat_id,
                )
                return

            serving_description = size or "standard"

            try:
                resolution = resolve_food(
                    food_name=food_name,
                    serving_description=serving_description,
                    brand=brand,
                    restaurant=restaurant,
                )
            except Exception:
                logging.exception("Food Library resolution failed")
                send_telegram_msg(
                    "I confirmed the interpretation, but the "
                    "Food Library lookup failed.",
                    chat_id=chat_id,
                )
                return

            if resolution["found"]:
                food = resolution["food"]
                nutrition = resolution["nutrition"] or {}

                meal_category = known_data.get("meal_category")

                if not restaurant:
                    try:
                        quantity = (
                            resolve_packaged_serving_multiplier(
                                food_id=food["food_id"],
                                quantity=known_data.get("quantity"),
                                quantity_description=known_data.get(
                                    "quantity_description"
                                ),
                                serving_amount=food.get(
                                    "serving_amount"
                                ),
                                serving_unit=food.get(
                                    "serving_unit"
                                ),
                            )
                        )
                    except ValueError as error:
                        quantity_description = str(
                            known_data.get(
                                "quantity_description"
                            )
                            or ""
                        ).lower()

                        if (
                            "handful" in quantity_description
                            and "saved" in str(error).lower()
                            and "amount" in str(error).lower()
                        ):
                            portion_phrase = (
                                "small handful"
                                if "small handful"
                                in quantity_description
                                else (
                                    "large handful"
                                    if "large handful"
                                    in quantity_description
                                    else "handful"
                                )
                            )

                            update_conversation(
                                chat_id=chat_id,
                                current_step=(
                                    "portion_profile_clarification"
                                ),
                                known_data={
                                    **known_data,
                                    "_portion_profile_food_id": (
                                        food["food_id"]
                                    ),
                                    "_portion_profile_phrase": (
                                        portion_phrase
                                    ),
                                },
                                missing_fields=[],
                            )

                            send_telegram_msg(
                                f"I don't know your typical "
                                f"{portion_phrase} for this food yet.\n\n"
                                "About how much is it?\n"
                                "Examples: 28 g, 1 oz, "
                                "1.5 servings.",
                                chat_id=chat_id,
                            )
                            return

                        send_telegram_msg(
                            str(error),
                            chat_id=chat_id,
                        )
                        return
                else:
                    quantity = float(
                        known_data.get("quantity") or 1.0
                    )

                if not meal_category:
                    send_telegram_msg(
                        "The meal category is missing, "
                        "so nothing was logged.",
                        chat_id=chat_id,
                    )
                    return

                pending_components = [
                    {
                        "role": "Food",
                        "food_id": food["food_id"],
                        "canonical_name": food[
                            "canonical_name"
                        ],
                        "restaurant": food.get(
                            "restaurant"
                        ),
                        "size": size,
                        "quantity": quantity,
                        "calories": nutrition.get(
                            "calories"
                        ),
                        "protein_g": nutrition.get(
                            "protein_g"
                        ),
                        "verification_source": food.get(
                            "verification_source"
                        ),
                    }
                ]

                update_conversation(
                    chat_id=chat_id,
                    current_step="nutrition_confirmation",
                    known_data={
                        **known_data,
                        "_pending_components": (
                            pending_components
                        ),
                    },
                    missing_fields=[],
                )

                prompt_message_id = send_telegram_msg(
                    format_pending_nutrition_confirmation(
                        pending_components,
                        meal_category=meal_category,
                    ),
                    chat_id=chat_id,
                )

                if isinstance(prompt_message_id, int):
                    update_conversation(
                        chat_id=chat_id,
                        known_data={
                            "_nutrition_prompt_message_id": (
                                prompt_message_id
                            ),
                        },
                    )

                return

            try:
                provider_result = lookup_official_nutrition(
                    restaurant=restaurant,
                    food_name=food_name,
                    size=size,
                    brand=brand,
                )
            except Exception:
                logging.exception(
                    "Official nutrition provider failed"
                )
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "Food interpretation confirmed.\n\n"
                    "The food was not found in the Food Library, "
                    "and the official nutrition lookup failed.\n\n"
                    "Send a new food description to try again.",
                    chat_id=chat_id,
                )
                return

            if not provider_result["found"]:
                update_conversation(
                    chat_id=chat_id,
                    current_step="manual_label_offer",
                    known_data={
                        **known_data,
                        "_manual_label_search_key": (
                            resolution["search_key"]
                        ),
                    },
                    missing_fields=[],
                )

                send_telegram_msg(
                    "I couldn't verify this product automatically.\n\n"
                    "1. Enter package label nutrition\n"
                    "2. Try a different description\n"
                    "3. Save for later\n"
                    "4. Cancel",
                    chat_id=chat_id,
                )
                return

            provider_food = provider_result["food"]
            provider_nutrition = provider_result["nutrition"]
            verification = provider_result["verification"]

            try:
                saved = add_food_with_nutrition(
                    canonical_name=(
                        provider_food["canonical_name"]
                    ),
                    serving_description=(
                        provider_food["serving_description"]
                    ),
                    serving_amount=(
                        provider_food["serving_amount"]
                    ),
                    serving_unit=(
                        provider_food["serving_unit"]
                    ),
                    verification_status=(
                        verification["status"]
                    ),
                    verification_source=(
                        verification["source"]
                    ),
                    calories=provider_nutrition["calories"],
                    protein_g=provider_nutrition["protein_g"],
                    carbohydrates_g=(
                        provider_nutrition["carbohydrates_g"]
                    ),
                    fat_g=provider_nutrition["fat_g"],
                    fiber_g=provider_nutrition["fiber_g"],
                    sugar_g=provider_nutrition["sugar_g"],
                    sodium_mg=provider_nutrition["sodium_mg"],
                    brand=provider_food["brand"],
                    restaurant=provider_food["restaurant"],
                    food_type=provider_food["food_type"],
                    source_item_id=(
                        verification["source_item_id"]
                    ),
                    source_url=verification["source_url"],
                )
            except Exception:
                logging.exception(
                    "Saving verified Food failed"
                )
                send_telegram_msg(
                    "Verified nutrition was found, but I could "
                    "not save it to the Food Library.",
                    chat_id=chat_id,
                )
                return

            saved_food = saved["food"]
            saved_nutrition = saved["nutrition"] or {}

            meal_category = known_data.get("meal_category")

            if not restaurant:
                try:
                    quantity = (
                        resolve_packaged_serving_multiplier(
                            food_id=saved_food["food_id"],
                            quantity=known_data.get("quantity"),
                            quantity_description=known_data.get(
                                "quantity_description"
                            ),
                            serving_amount=saved_food.get(
                                "serving_amount"
                            ),
                            serving_unit=saved_food.get(
                                "serving_unit"
                            ),
                        )
                    )
                except ValueError as error:
                    quantity_description = str(
                        known_data.get(
                            "quantity_description"
                        )
                        or ""
                    ).lower()

                    if (
                        "handful" in quantity_description
                        and "saved" in str(error).lower()
                        and "amount" in str(error).lower()
                    ):
                        portion_phrase = (
                            "small handful"
                            if "small handful"
                            in quantity_description
                            else (
                                "large handful"
                                if "large handful"
                                in quantity_description
                                else "handful"
                            )
                        )

                        update_conversation(
                            chat_id=chat_id,
                            current_step=(
                                "portion_profile_clarification"
                            ),
                            known_data={
                                **known_data,
                                "_portion_profile_food_id": (
                                    saved_food["food_id"]
                                ),
                                "_portion_profile_phrase": (
                                    portion_phrase
                                ),
                            },
                            missing_fields=[],
                        )

                        send_telegram_msg(
                            f"I don't know your typical "
                            f"{portion_phrase} for this food yet.\n\n"
                            "About how much is it?\n"
                            "Examples: 28 g, 1 oz, "
                            "1.5 servings.",
                            chat_id=chat_id,
                        )
                        return

                    send_telegram_msg(
                        str(error),
                        chat_id=chat_id,
                    )
                    return
            else:
                quantity = float(
                    known_data.get("quantity") or 1.0
                )

            if not meal_category:
                send_telegram_msg(
                    "The meal category is missing, "
                    "so nothing was logged.",
                    chat_id=chat_id,
                )
                return

            pending_components = [
                {
                    "role": "Food",
                    "food_id": saved_food["food_id"],
                    "canonical_name": saved_food[
                        "canonical_name"
                    ],
                    "restaurant": saved_food.get(
                        "restaurant"
                    ),
                    "size": size,
                    "quantity": quantity,
                    "calories": saved_nutrition.get(
                        "calories"
                    ),
                    "protein_g": saved_nutrition.get(
                        "protein_g"
                    ),
                    "verification_source": verification[
                        "source"
                    ],
                }
            ]

            update_conversation(
                chat_id=chat_id,
                current_step="nutrition_confirmation",
                known_data={
                    **known_data,
                    "_pending_components": pending_components,
                },
                missing_fields=[],
            )

            prompt_message_id = send_telegram_msg(
                format_pending_nutrition_confirmation(
                    pending_components,
                    meal_category=meal_category,
                ),
                chat_id=chat_id,
            )

            if isinstance(prompt_message_id, int):
                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_nutrition_prompt_message_id": (
                            prompt_message_id
                        ),
                    },
                )

            return

        if lowered in {"2", "edit"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Send the corrected food description as a new message.",
                chat_id=chat_id,
            )
            return

        if lowered in {"3", "cancel", "no"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Food entry cancelled.",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Please reply:\n"
            "1. Correct\n"
            "2. Edit\n"
            "3. Cancel",
            chat_id=chat_id,
        )
        return

    sleep_value = extract_sleep_value_from_text(text)
    if sleep_value is not None:
        success, response = set_today_sleep(sleep_value)
        send_telegram_msg(response, chat_id=chat_id)
        return

    if is_sleep_status_question(text):
        send_telegram_msg(answer_sleep_status(), chat_id=chat_id)
        return

    if is_run_weekly_report_command(text):
        send_weekly_report(chat_id=chat_id)
        return

    try:
        interpretation = interpret_food_message(text)

        if interpretation.is_food_logging_request:
            missing_fields = list(
                interpretation.missing_fields
            )

            if missing_fields == ["meal_category"]:
                meal_options = get_time_aware_meal_options()

                conversation = start_conversation(
                    chat_id=chat_id,
                    conversation_type="food_interpretation",
                    current_step="meal_selection",
                    known_data=interpretation.model_dump(),
                    missing_fields=missing_fields,
                    original_message=text,
                )

                conversation["meal_options"] = meal_options

                update_conversation(
                    chat_id=chat_id,
                    known_data={
                        "_meal_options": meal_options,
                    },
                )

                send_telegram_msg(
                    format_meal_selection_prompt(
                        interpretation
                    ),
                    chat_id=chat_id,
                )
                return

            if missing_fields:
                start_conversation(
                    chat_id=chat_id,
                    conversation_type="food_interpretation",
                    current_step="clarification",
                    known_data=interpretation.model_dump(),
                    missing_fields=missing_fields,
                    original_message=text,
                )

                send_telegram_msg(
                    format_food_interpretation(interpretation),
                    chat_id=chat_id,
                )
                return

            # Fast path for trusted, previously saved foods.
            # Keep the normal confirmation screen whenever the
            # interpretation contains assumptions or is a combo.
            if (
                not interpretation.assumptions
                and not interpretation.is_combo_meal
                and interpretation.food_name
                and interpretation.meal_category
            ):
                try:
                    saved_resolution = resolve_food(
                        food_name=interpretation.food_name,
                        serving_description=(
                            interpretation.size or "standard"
                        ),
                        brand=interpretation.brand,
                        restaurant=interpretation.restaurant,
                    )
                except Exception:
                    logging.exception(
                        "Saved-food fast-path resolution failed"
                    )
                    saved_resolution = {"found": False}

                if saved_resolution.get("found"):
                    saved_food = saved_resolution["food"]
                    saved_nutrition = (
                        saved_resolution.get("nutrition") or {}
                    )

                    try:
                        if (
                            interpretation.brand
                            and not interpretation.restaurant
                        ):
                            quantity = (
                                resolve_packaged_serving_multiplier(
                                    food_id=saved_food["food_id"],
                                    quantity=interpretation.quantity,
                                    quantity_description=(
                                        interpretation.quantity_description
                                    ),
                                    serving_amount=saved_food.get(
                                        "serving_amount"
                                    ),
                                    serving_unit=saved_food.get(
                                        "serving_unit"
                                    ),
                                )
                            )
                        else:
                            quantity = float(
                                interpretation.quantity or 1.0
                            )
                    except ValueError:
                        quantity = None

                    if quantity is not None:
                        pending_components = [
                            {
                                "role": "Food",
                                "food_id": saved_food["food_id"],
                                "canonical_name": saved_food[
                                    "canonical_name"
                                ],
                                "restaurant": saved_food.get(
                                    "restaurant"
                                ),
                                "size": interpretation.size,
                                "quantity": quantity,
                                "calories": saved_nutrition.get(
                                    "calories"
                                ),
                                "protein_g": saved_nutrition.get(
                                    "protein_g"
                                ),
                                "verification_source": saved_food.get(
                                    "verification_source"
                                ),
                            }
                        ]

                        start_conversation(
                            chat_id=chat_id,
                            conversation_type="food_interpretation",
                            current_step="nutrition_confirmation",
                            known_data={
                                **interpretation.model_dump(),
                                "_pending_components": (
                                    pending_components
                                ),
                            },
                            missing_fields=[],
                            original_message=text,
                        )

                        prompt_message_id = send_telegram_msg(
                            format_pending_nutrition_confirmation(
                                pending_components,
                                meal_category=(
                                    interpretation.meal_category
                                ),
                            ),
                            chat_id=chat_id,
                        )

                        if isinstance(prompt_message_id, int):
                            update_conversation(
                                chat_id=chat_id,
                                known_data={
                                    "_nutrition_prompt_message_id": (
                                        prompt_message_id
                                    ),
                                },
                            )

                        return

            start_conversation(
                chat_id=chat_id,
                conversation_type="food_interpretation",
                current_step="confirmation",
                known_data=interpretation.model_dump(),
                missing_fields=[],
                original_message=text,
            )

            send_telegram_msg(
                format_food_interpretation(interpretation),
                chat_id=chat_id,
            )
            return
    except Exception:
        logging.exception("Food interpretation failed")

    send_telegram_msg(build_help_message(), chat_id=chat_id)


def telegram_poll_loop():
    logging.info("Telegram polling started")

    while True:
        try:
            state = load_state()
            offset = state.get("telegram_update_offset")

            params = {"timeout": 20}
            if offset is not None:
                params["offset"] = int(offset)

            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            r = requests.get(url, params=params, timeout=30)
            r.raise_for_status()
            data = r.json()

            if not data.get("ok"):
                logging.error("Telegram getUpdates failed: %s", data)
                time.sleep(5)
                continue

            for update in data.get("result", []):
                process_telegram_update(update)
                update_id = update.get("update_id")

                if update_id is not None:
                    state = load_state()
                    state["telegram_update_offset"] = int(update_id) + 1
                    save_state(state)

        except Exception as e:
            logging.error("Telegram polling error: %s", e)
            time.sleep(10)


@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.json or {}
    logging.info("Webhook received: %s", data)

    now = datetime.now(PACIFIC_TZ)
    sheet = get_current_sheet()

    timestamp = now.strftime("%m/%d/%Y %I:%M %p")

    steps = safe_int(data.get("steps"), None)
    total = safe_float(data.get("total_calories"), None)
    active = safe_float(data.get("active_calories"), None)
    sleep_raw = data.get("sleep_hours")
    sleep = normalize_sleep_for_sheet(sleep_raw) if sleep_raw not in ("", None) else ""
    rhr = safe_float(data.get("rhr"), None)
    weight = safe_float(data.get("weight"), None)
    hrv = safe_float(data.get("hrv"), None)
    dietary = safe_float(data.get("dietary_calories"), None)
    protein = safe_float(data.get("protein"), None)

    row = [
        timestamp,
        steps if steps is not None else "",
        total if total is not None else "",
        active if active is not None else "",
        sleep,
        rhr if rhr is not None else "",
        weight if weight is not None else "",
        hrv if hrv is not None else "",
        dietary if dietary is not None else "",
        protein if protein is not None else "",
    ]

    update_or_insert_today(sheet, row, now)

    return {"status": "ok"}, 200


if __name__ == "__main__":
    os.makedirs("/home/vandal/bots/healthcoach/logs", exist_ok=True)
    os.makedirs("/home/vandal/bots/healthcoach/data", exist_ok=True)

    t1 = threading.Thread(target=telegram_poll_loop, daemon=True)
    t1.start()

    t2 = threading.Thread(target=scheduler_loop, daemon=True)
    t2.start()

    logging.info("HealthCoach server starting")
    app.run(host="0.0.0.0", port=5000)
