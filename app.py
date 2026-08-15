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

from loseit_coaching import (
    build_food_coaching,
    build_food_ledger_coaching_data,
    build_weekly_health_report,
)
from loseit_email_reader import download_latest_loseit_csv
from loseit_parser import parse_loseit_csv
from food.database import (
    get_pending_unresolved_foods,
    get_portion_profile,
    get_unresolved_food,
    save_portion_profile,
    save_unresolved_food,
    set_unresolved_food_status,
    update_unresolved_food_details,
)
from food.interpreter import (
    FoodInterpretation,
    clean_interpretation_missing_fields,
    interpret_food_message,
    normalize_signature_food,
)
from food.library import (
    add_food_with_nutrition,
    add_user_nutrition_version,
    list_user_saved_foods,
)
from food.ledger import (
    add_food_entry,
    delete_food_favorite,
    delete_food_entry,
    find_recent_duplicate_entry,
    get_daily_totals,
    list_food_favorites,
    list_food_entries,
    save_food_favorite_from_entry,
    update_food_entry,
)
from food.nutrition_provider import lookup_official_nutrition
from food.menu_photo_advisor import (
    analyze_food_photo,
    analyze_menu_photo,
    download_telegram_photo,
    format_food_photo_estimate,
    format_menu_photo_analysis,
    is_food_photo_request,
    midpoint_food_photo_nutrition,
    refine_food_photo_estimate,
)
from food.restaurant_advisor import recommend_restaurant_entrees
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


def menu_reply_markup(message):
    """Return an iPhone-friendly Telegram keyboard for menu messages."""
    one_time = False

    if "Undo this food entry?" in message:
        rows = [["Yes", "No"]]
        one_time = True
    elif (
        (
            "Nutrition has not been looked up or saved yet." in message
            or message.startswith("Please reply:\n1. Correct")
        )
        and "1. Correct" in message
    ):
        rows = [["Correct", "Edit", "Cancel"]]
        one_time = True
    elif (
        (
            "Nothing has been logged yet." in message
            or message.startswith("Please reply:\n1. Log It")
        )
        and "1. Log It" in message
    ):
        rows = [
            ["Log It", "Enter custom nutrition"],
            ["Edit", "Cancel"],
        ]
        one_time = True
    elif (
        ("Unknown food " in message and "1. Enter nutrition" in message)
        or "Choose another option." in message
        or "food remains in the queue." in message
    ):
        rows = [
            ["Enter nutrition", "Edit"],
            ["Automatic lookup", "Skip for now"],
            ["Cancel this food"],
        ]
        one_time = True
    elif "1. Enter package label nutrition" in message:
        rows = [
            ["Enter package label nutrition"],
            ["Different description", "Save for later"],
            ["Cancel"],
        ]
        one_time = True
    elif (
        "Tell me any details that would improve this estimate."
        in message
    ):
        rows = [["Skip details"], ["Cancel"]]
        one_time = True
    elif "How much of the pictured food did you eat?" in message:
        rows = [
            ["All of it", "About 3/4"],
            ["About half", "Cancel"],
        ]
        one_time = True
    elif (
        "Which meal should this estimate be logged under?"
        in message
    ):
        rows = [
            ["Before breakfast", "Breakfast"],
            ["Morning snack", "Lunch"],
            ["Afternoon snack", "Dinner"],
            ["Dessert"],
            ["Cancel"],
        ]
        one_time = True
    elif "Log this estimated meal?" in message:
        rows = [
            ["Log Estimate", "Change details"],
            ["Cancel"],
        ]
        one_time = True
    elif "Log another one?" in message:
        rows = [["Log another", "Keep first"]]
        one_time = True
    elif "1. Save" in message and "2. Edit" in message:
        rows = [["Save", "Edit", "Cancel"]]
        one_time = True
    elif (
        ("Anything else for " in message or "Anything else from " in message)
        and "1. Yes" in message
    ):
        rows = [["Yes", "No"]]
        one_time = True
    elif any(
        marker in message
        for marker in (
            "Choose a food to edit:",
            "Choose a food to save as a favorite:",
            "Choose a favorite to log:",
            "Choose a favorite to remove:",
            "Choose a saved food to view:",
            "Choose a saved food to edit:",
        )
    ):
        choices = re.findall(r"(?m)^(\d+)\. ", message)
        rows = [
            choices[index:index + 3]
            for index in range(0, len(choices), 3)
        ]
        rows.append(["Back", "Cancel"])
    elif "What would you like to change?" in message:
        rows = [
            ["Quantity", "Meal"],
            ["Back", "Cancel"],
        ]
    elif "Which meal was this?" in message:
        choices = re.findall(
            r"(?m)^\d+\.\s+(.+?)\s*$",
            message,
        )
        rows = [
            choices[index:index + 2]
            for index in range(0, len(choices), 2)
        ]
        rows.append(["Cancel"])
        one_time = True
    elif "Which meal was this for?" in message:
        rows = [
            ["Before breakfast", "Breakfast"],
            ["Morning snack", "Lunch"],
            ["Afternoon snack", "Dinner"],
            ["Dessert"],
            ["Cancel"],
        ]
        one_time = True
    elif "Choose the new meal:" in message:
        rows = [
            ["Before breakfast", "Breakfast"],
            ["School snack", "Lunch"],
            ["Afternoon snack", "Dinner"],
            ["Dessert"],
            ["Back", "Cancel"],
        ]
    elif "Apply this food change?" in message:
        rows = [["Yes", "No"]]
        one_time = True
    elif any(
        marker in message
        for marker in (
            "Save this favorite?",
            "Quick-log this favorite?",
            "Remove this favorite?",
            "Save this saved food?",
            "Save these nutrition changes?",
        )
    ):
        rows = [["Yes", "No"]]
        one_time = True
    elif "Restaurant Recommendations\n\n" in message:
        rows = [
            ["Search again", "Back"],
            ["Cancel"],
        ]
    elif "Saved Food Details\n\n" in message:
        rows = [["Back", "Cancel"]]
    elif "Saved Foods Menu\n\n" in message:
        rows = [
            ["Browse saved foods", "Add saved food"],
            ["Edit saved food"],
            ["Back", "Cancel"],
        ]
    elif "Restaurant Menu\n\n" in message:
        rows = [
            ["Find best choices online"],
            ["Back", "Cancel"],
        ]
    elif "Favorites Menu\n\n" in message:
        rows = [
            ["Quick log", "Save today's food"],
            ["Remove favorite"],
            ["Back", "Cancel"],
        ]
    elif "Photo Tools Menu\n\n" in message:
        rows = [
            ["Read menu photo"],
            ["Estimate meal photo"],
            ["Back", "Cancel"],
        ]
    elif (
        "Send a clear restaurant menu photo." in message
        or "Send a clear photo of the actual meal." in message
    ):
        rows = [["Back", "Cancel"]]
    elif "Food Menu\n\n" in message:
        rows = [
            ["Log food", "Show today"],
            ["Edit today", "Undo last"],
            ["Favorites", "Saved foods"],
            ["Photo tools", "Restaurant"],
            ["Update unknown foods"],
            ["Back", "Cancel"],
        ]
    elif "Health Menu\n\n" in message:
        rows = [
            ["Current status", "Record sleep"],
            ["Record weight"],
            ["Back", "Cancel"],
        ]
    elif "Reports Menu\n\n" in message:
        rows = [
            ["Today", "Weekly report"],
            ["Back", "Cancel"],
        ]
    elif "HealthCoach Menu\n\n" in message:
        rows = [
            ["Food", "Health"],
            ["Reports", "Help"],
            ["Cancel"],
        ]
    else:
        return None

    return {
        "keyboard": rows,
        "resize_keyboard": True,
        "one_time_keyboard": one_time,
    }


def send_telegram_msg(
    message,
    chat_id=None,
    *,
    remove_keyboard=False,
):
    target_chat_id = str(chat_id or CHAT_ID) if (chat_id or CHAT_ID) else None

    if not TELEGRAM_TOKEN or not target_chat_id:
        logging.error("Missing Telegram credentials")
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": message}

    if remove_keyboard:
        payload["reply_markup"] = {"remove_keyboard": True}
    else:
        reply_markup = menu_reply_markup(message)
        if reply_markup:
            payload["reply_markup"] = reply_markup

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

    return (
        True,
        "Recorded sleep as "
        f"{format_sleep_for_humans(normalized_sleep)} for today.",
    )


def set_today_weight(weight):
    """Save one official weight for today; later entries correct it."""
    try:
        normalized_weight = float(weight)
    except (TypeError, ValueError):
        return False, "I couldn't understand that weight."

    if not 50 <= normalized_weight <= 700:
        return (
            False,
            "That weight seems outside the expected range. "
            "Please enter pounds between 50 and 700.",
        )

    now = datetime.now(PACIFIC_TZ)
    sheet = get_current_sheet()
    today_str = now.strftime("%m/%d/%Y")
    row_index, existing_row, _ = get_today_row_index_and_row(
        sheet,
        today_str,
    )
    timestamp = now.strftime("%m/%d/%Y %I:%M %p")
    replacing = False

    if row_index and existing_row:
        while len(existing_row) < 10:
            existing_row.append("")

        replacing = existing_row[6] not in ("", None)
        merged = list(existing_row[:10])
        merged[0] = timestamp
        merged[6] = normalized_weight

        sheet.update(
            range_name=f"A{row_index}:J{row_index}",
            values=[merged],
        )
    else:
        row = [
            timestamp,
            "",
            "",
            "",
            "",
            "",
            normalized_weight,
            "",
            "",
            "",
        ]
        sheet.append_row(row)

    action = "Corrected" if replacing else "Recorded"
    return (
        True,
        f"{action} today's official weight to "
        f"{normalized_weight:.1f} lbs.",
    )


def answer_sleep_status():
    raw_sleep = get_today_sleep_raw()
    if raw_sleep in ("", None):
        return 'No, sleep is not recorded for today. Reply with "Record my sleep as 6:22".'
    return f"Yes, sleep is recorded for today: {format_sleep_for_humans(raw_sleep)}."


def extract_sleep_value_from_text(text, *, allow_bare=False):
    """Extract flexible sleep durations and return decimal hours."""
    lowered = text.lower().strip()

    if allow_bare:
        bare_match = re.fullmatch(
            r"([0-9]+(?:\.[0-9]+|:[0-9]{1,2})?)",
            lowered,
        )
        if bare_match:
            value = parse_sleep(bare_match.group(1))
            if value is not None and 0 < value <= 24:
                return value

    range_match = re.search(
        r"(?:slept\s+)?from\s+"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?\s+"
        r"(?:to|until|till)\s+"
        r"(\d{1,2})(?::(\d{2}))?\s*(am|pm)?",
        lowered,
    )

    if range_match:
        start_hour = int(range_match.group(1))
        start_minute = int(range_match.group(2) or 0)
        start_period = range_match.group(3)
        end_hour = int(range_match.group(4))
        end_minute = int(range_match.group(5) or 0)
        end_period = range_match.group(6)

        def minutes_after_midnight(hour, minute, period):
            if minute >= 60 or hour > 12:
                return None
            if period == "am":
                hour = 0 if hour == 12 else hour
            elif period == "pm":
                hour = 12 if hour == 12 else hour + 12
            return hour * 60 + minute

        start = minutes_after_midnight(
            start_hour,
            start_minute,
            start_period,
        )
        end = minutes_after_midnight(
            end_hour,
            end_minute,
            end_period,
        )

        if start is not None and end is not None:
            if start_period is None and end_period is None:
                if start_hour <= 12 and end_hour <= 12:
                    # Assume a typical overnight range.
                    if start_hour < 7:
                        start += 12 * 60

            duration = end - start
            if duration <= 0:
                duration += 24 * 60

            hours = duration / 60
            if 0 < hours <= 24:
                return hours

    half_match = re.search(
        r"(?:i\s+)?(?:slept|got|had)\s+"
        r"(\d+)\s+and\s+a\s+half\s+hours?",
        lowered,
    )
    if half_match:
        return float(half_match.group(1)) + 0.5

    hours_minutes_match = re.search(
        r"(?:i\s+)?(?:slept|got|had)\s+"
        r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)"
        r"(?:\s+(?:and\s+)?(\d+)\s*(?:minutes?|mins?))?",
        lowered,
    )
    if hours_minutes_match:
        hours = float(hours_minutes_match.group(1))
        minutes = float(hours_minutes_match.group(2) or 0)
        value = hours + minutes / 60
        if 0 < value <= 24 and minutes < 60:
            return value

    match = re.search(
        r"(?:record\s+my\s+sleep\s+as|"
        r"record\s+sleep\s+as|"
        r"sleep\s+(?:was|is))\s+"
        r"([0-9]+(?:\.[0-9]+|:[0-9]{1,2})?)",
        lowered,
    )

    if match:
        value = parse_sleep(match.group(1))
        if value is not None and 0 < value <= 24:
            return value

    return None


def extract_weight_value_from_text(text, *, allow_bare=False):
    """Extract a plausible weight in pounds from natural language."""
    lowered = text.lower().strip()

    patterns = [
        r"(?:record\s+my\s+weight\s+as|"
        r"record\s+weight\s+as|"
        r"i\s+weighed|"
        r"i\s+weigh|"
        r"my\s+weight\s+(?:was|is))\s+"
        r"(\d{2,3}(?:\.\d+)?)",
    ]

    if allow_bare:
        patterns.append(r"(\d{2,3}(?:\.\d+)?)")

    for pattern in patterns:
        match = re.fullmatch(pattern, lowered) or re.search(
            pattern,
            lowered,
        )
        if not match:
            continue

        value = float(match.group(1))

        if 50 <= value <= 700:
            return value

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
        logging.info(
            "Skipping food coaching: no yesterday row found"
        )
        return False

    entries = list_food_entries(
        entry_date=yesterday_date
    )
    totals = get_daily_totals(yesterday_date)
    food_data = build_food_ledger_coaching_data(
        entries,
        totals,
    )

    today_row = get_row_for_date(today_date)
    today_metrics = row_to_metrics(today_row)

    recent_weight_avg = get_recent_average_weight(
        today_date
    )
    weight_today = (
        today_metrics["weight"]
        if (
            today_metrics
            and today_metrics["weight"] is not None
        )
        else yesterday["weight"]
    )
    sleep_today = (
        today_metrics["sleep_hours"]
        if today_metrics
        else None
    )

    try:
        msg = build_food_coaching(
            total_burn=yesterday["total_cals"],
            steps=yesterday["steps"],
            weight_today=weight_today,
            recent_weight_avg=recent_weight_avg,
            sleep=sleep_today,
            food_data=food_data,
        )
        return send_telegram_msg(msg)
    except Exception as error:
        logging.error(
            "Food coaching error: %s",
            error,
        )
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


def send_weekly_report(chat_id=None, *, remove_keyboard=False):
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

    return send_telegram_msg(
        full_message,
        chat_id=chat_id,
        remove_keyboard=remove_keyboard,
    )


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

        fluid_units = {
            "fl oz",
            "floz",
            "fluid ounce",
            "fluid ounces",
        }

        if normalized_serving_unit in fluid_units:
            if unit not in {"oz", "ounce", "ounces"}:
                raise ValueError(
                    "Fluid ounces cannot be converted from weight."
                )

            multiplier = amount / float(serving_amount)

            if multiplier <= 0:
                raise ValueError(
                    "Calculated serving quantity was invalid."
                )

            return multiplier

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


def resolve_non_restaurant_quantity(
    *,
    food_id: int,
    quantity: float | None,
    quantity_description: str | None,
    serving_amount: float | None,
    serving_unit: str | None,
    size: str | None = None,
) -> float:
    """
    Use plain numeric counts directly and convert only explicit portions.
    """
    description = str(
        quantity_description or ""
    ).strip().lower()
    normalized_serving_unit = str(
        serving_unit or ""
    ).strip().lower()

    if normalized_serving_unit in {
        "fl oz",
        "floz",
        "fluid ounce",
        "fluid ounces",
    }:
        size_description = str(size or "").strip().lower()
        fluid_match = re.fullmatch(
            r"(\d+(?:\.\d+)?)\s*"
            r"(?:fl\s*oz|fluid\s*ounces?|oz|ounces?)",
            size_description,
        )

        if fluid_match:
            description = f"{fluid_match.group(1)} oz"

    requires_conversion = bool(
        re.search(
            r"\b(?:servings?|g|grams?|grm|oz|ounces?|handful)\b",
            description,
        )
    )

    if not requires_conversion:
        return float(quantity or 1.0)

    return resolve_packaged_serving_multiplier(
        food_id=food_id,
        quantity=quantity,
        quantity_description=description,
        serving_amount=serving_amount,
        serving_unit=serving_unit,
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
            "2. Enter custom nutrition",
            "3. Edit",
            "4. Cancel",
        ]
    )

    return "\n".join(lines)


def format_unresolved_food_review(
    item: dict,
    *,
    position: int,
    total: int,
) -> str:
    """Format one queued unresolved Food review prompt."""
    lines = [
        f"Unknown food {position} of {total}",
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
        lines.append(f"Amount: {item['quantity_description']}")
    elif item.get("quantity") is not None:
        lines.append(
            "Quantity: "
            + format_display_number(float(item["quantity"]))
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

    return "\n".join(lines)


def show_unresolved_food_review(
    *,
    chat_id: int | str,
    pending: list[dict] | None = None,
    unresolved_food_id: int | None = None,
    skipped_ids: list[int] | None = None,
) -> bool:
    """Start review state and display one pending unresolved Food."""
    items = pending or get_pending_unresolved_foods()

    skipped = {int(value) for value in (skipped_ids or [])}
    available = [
        item
        for item in items
        if int(item["unresolved_food_id"]) not in skipped
    ]

    if not available:
        cancel_conversation(chat_id)
        send_telegram_msg(
            "There are no more unreviewed unknown foods "
            "in this session.",
            chat_id=chat_id,
        )
        return False

    position = 0

    if unresolved_food_id is not None:
        for index, candidate in enumerate(available):
            if int(candidate["unresolved_food_id"]) == int(
                unresolved_food_id
            ):
                position = index
                break

    item = available[position]

    start_conversation(
        chat_id=chat_id,
        conversation_type="unresolved_food_review",
        current_step="menu",
        known_data={
            "_unresolved_food_id": item["unresolved_food_id"],
            "_skipped_unresolved_food_ids": sorted(skipped),
        },
        missing_fields=[],
        original_message=str(item.get("original_text") or ""),
    )

    send_telegram_msg(
        format_unresolved_food_review(
            item,
            position=position + 1,
            total=len(available),
        ),
        chat_id=chat_id,
    )
    return True


def healthcoach_main_menu_text() -> str:
    return (
        "HealthCoach Menu\n\n"
        "1. Food\n"
        "2. Health\n"
        "3. Reports\n"
        "4. Help\n\n"
        "Reply cancel to close the menu."
    )


def healthcoach_food_menu_text() -> str:
    return (
        "Food Menu\n\n"
        "DAILY FOOD\n"
        "1. Log food\n"
        "2. Show today's food\n"
        "3. Edit today's food\n"
        "4. Undo last food\n\n"
        "MY FOODS\n"
        "5. Favorites\n"
        "6. Saved foods\n\n"
        "TOOLS\n"
        "7. Photo tools\n"
        "8. Restaurant\n"
        "9. Update unknown foods\n"
        "10. Back"
    )


def healthcoach_photo_tools_menu_text() -> str:
    return (
        "Photo Tools Menu\n\n"
        "1. Read a restaurant menu photo\n"
        "2. Estimate an actual meal photo\n"
        "3. Back\n\n"
        "Nothing is logged without your confirmation."
    )


def healthcoach_saved_foods_menu_text() -> str:
    return (
        "Saved Foods Menu\n\n"
        "1. Browse saved foods\n"
        "2. Add saved food\n"
        "3. Edit saved food\n"
        "4. Back"
    )


def format_saved_food_choices(foods: list[dict]) -> str:
    lines = ["Choose a saved food to view:", ""]

    for index, food in enumerate(foods, start=1):
        lines.append(
            f"{index}. {food['canonical_name']} — "
            f"{food.get('serving_description') or '1 serving'}"
        )

    lines.extend(["", "Reply Back to return."])
    return "\n".join(lines)


def format_saved_food_edit_choices(
    foods: list[dict],
) -> str:
    lines = ["Choose a saved food to edit:", ""]

    for index, food in enumerate(foods, start=1):
        lines.append(
            f"{index}. {food['canonical_name']} — "
            f"{food.get('serving_description') or '1 serving'}"
        )

    lines.extend(["", "Reply Back to return."])
    return "\n".join(lines)


def format_saved_food_details(food: dict) -> str:
    def nutrient(field: str, suffix: str) -> str:
        value = float(food.get(field) or 0)
        return (
            f"{format_display_number(value)} {suffix}"
        ).strip()

    source = str(
        food.get("verification_source") or "user entered"
    ).replace("_", " ").title()

    return (
        "Saved Food Details\n\n"
        f"Food: {food['canonical_name']}\n"
        f"Serving: "
        f"{food.get('serving_description') or '1 serving'}\n"
        f"Nutrition version: {food.get('version_number') or 1}\n"
        f"Source: {source}\n\n"
        f"Calories: {nutrient('calories', 'cal')}\n"
        f"Protein: {nutrient('protein_g', 'g')}\n"
        f"Carbohydrates: "
        f"{nutrient('carbohydrates_g', 'g')}\n"
        f"Fat: {nutrient('fat_g', 'g')}\n"
        f"Fiber: {nutrient('fiber_g', 'g')}\n"
        f"Sugar: {nutrient('sugar_g', 'g')}\n"
        f"Sodium: {nutrient('sodium_mg', 'mg')}\n\n"
        "Reply Back to return or Cancel to close."
    )


def healthcoach_restaurant_menu_text() -> str:
    return (
        "Restaurant Menu\n\n"
        "1. Find best choices online\n"
        "2. Back\n\n"
        "Recommendations use current cited menu information. "
        "Nothing is logged automatically."
    )


def clean_restaurant_display_text(value) -> str:
    text = str(value or "")
    replacements = {
        "Â®": "",
        "â„¢": "",
        "â€™": "'",
        "â€“": "-",
        "â€”": "-",
        "®": "",
        "™": "",
        "’": "'",
        "–": "-",
        "—": "-",
    }
    for original, replacement in replacements.items():
        text = text.replace(original, replacement)
    return text.strip()


def format_restaurant_advice(advice: dict) -> str:
    name = clean_restaurant_display_text(
        advice.get("restaurant_display_name")
        or "Restaurant"
    )
    candidates = list(advice.get("candidates") or [])

    if not advice.get("found") or not candidates:
        notes = list(advice.get("notes") or [])
        reason = notes[0] if notes else (
            "No supported current menu recommendations were found."
        )
        return (
            "Restaurant Recommendations\n\n"
            f"{name}\n\n"
            f"I could not find three supported choices. {reason}\n\n"
            "Try including the city and state, or check the restaurant "
            "name.\n\n"
            "Reply Search again, Back, or Cancel."
        )

    lines = ["Restaurant Recommendations", "", name]

    for index, candidate in enumerate(candidates, start=1):
        lines.extend(["", f"{index}. {clean_restaurant_display_text(candidate['item_name'])}"])
        if candidate.get("nutrition_status") == "official":
            nutrition_parts = []
            if candidate.get("calories") is not None:
                nutrition_parts.append(
                    f"{format_display_number(float(candidate['calories']), decimals=0)} cal"
                )
            if candidate.get("protein_g") is not None:
                nutrition_parts.append(
                    f"{format_display_number(float(candidate['protein_g']))} g protein"
                )
            lines.append(
                "Official nutrition: "
                + (
                    ", ".join(nutrition_parts)
                    if nutrition_parts
                    else "values not listed"
                )
            )
        else:
            lines.append(
                "Nutrition: not published; menu-based recommendation"
            )
        lines.append(
            f"Why: {clean_restaurant_display_text(candidate['recommendation_reason'])}"
        )
        lines.append(
            f"Source: {clean_restaurant_display_text(candidate['source_title'])}"
        )
        lines.append(str(candidate["source_url"]))

    lines.extend(
        [
            "",
            "Menu availability can change. Nothing has been logged.",
            "",
            "Reply Search again, Back, or Cancel.",
        ]
    )
    return "\n".join(lines)


def healthcoach_favorites_menu_text() -> str:
    return (
        "Favorites Menu\n\n"
        "1. Quick log\n"
        "2. Save today's food\n"
        "3. Remove favorite\n"
        "4. Back"
    )


def healthcoach_health_menu_text() -> str:
    return (
        "Health Menu\n\n"
        "1. Current status\n"
        "2. Record sleep\n"
        "3. Record weight\n"
        "4. Back"
    )


def healthcoach_reports_menu_text() -> str:
    return (
        "Reports Menu\n\n"
        "1. Today's summary\n"
        "2. Weekly report\n"
        "3. Back"
    )


def format_daily_food_log(entry_date) -> str:
    entries = list_food_entries(entry_date=entry_date)

    if not entries:
        return "No foods are logged for today."

    lines = ["Today's Food Log"]
    current_meal = None

    for entry in entries:
        meal = str(entry.get("meal_category") or "Other").title()

        if meal != current_meal:
            lines.extend(["", meal])
            current_meal = meal

        name = format_food_display_name(
            canonical_name=str(
                entry.get("canonical_name") or "Food"
            ),
            restaurant=entry.get("restaurant"),
            size=None,
        )
        quantity = float(entry.get("quantity") or 1.0)
        calories = float(entry.get("calories") or 0.0)
        protein = float(entry.get("protein_g") or 0.0)

        lines.append(
            "- "
            f"{format_display_number(quantity)} × {name}: "
            f"{format_display_number(calories, decimals=0)} cal, "
            f"{format_display_number(protein)} g protein"
        )

    totals = get_daily_totals(entry_date)
    lines.extend(
        [
            "",
            (
                "Total: "
                f"{format_display_number(totals['calories'], decimals=0)} "
                "calories, "
                f"{format_display_number(totals['protein_g'])} g protein"
            ),
        ]
    )
    return "\n".join(lines)


def format_edit_food_choices(entries: list[dict]) -> str:
    lines = ["Choose a food to edit:", ""]

    for index, entry in enumerate(entries, start=1):
        name = format_food_display_name(
            canonical_name=str(
                entry.get("canonical_name") or "Food"
            ),
            restaurant=entry.get("restaurant"),
            size=None,
        )
        lines.append(
            f"{index}. {name} — "
            f"{str(entry.get('meal_category') or 'Other').title()}, "
            f"quantity {format_display_number(float(entry.get('quantity') or 1))}"
        )

    lines.extend(["", "Reply Back to return."])
    return "\n".join(lines)


def format_save_favorite_choices(entries: list[dict]) -> str:
    lines = ["Choose a food to save as a favorite:", ""]

    for index, entry in enumerate(entries, start=1):
        name = format_food_display_name(
            canonical_name=str(entry.get("canonical_name") or "Food"),
            restaurant=entry.get("restaurant"),
            size=None,
        )
        lines.append(
            f"{index}. {name} — "
            f"{str(entry.get('meal_category') or 'Other').title()}, "
            f"quantity {format_display_number(float(entry.get('quantity') or 1))}"
        )

    lines.extend(["", "Reply Back to return."])
    return "\n".join(lines)


def format_favorite_choices(
    favorites: list[dict],
    *,
    action: str,
) -> str:
    heading = (
        "Choose a favorite to remove:"
        if action == "remove"
        else "Choose a favorite to log:"
    )
    lines = [heading, ""]

    for index, favorite in enumerate(favorites, start=1):
        name = format_food_display_name(
            canonical_name=str(
                favorite.get("canonical_name") or "Food"
            ),
            restaurant=favorite.get("restaurant"),
            size=None,
        )
        lines.append(
            f"{index}. {name} — "
            f"{str(favorite.get('meal_category') or 'Other').title()}, "
            f"quantity {format_display_number(float(favorite.get('quantity') or 1))}"
        )

    lines.extend(["", "Reply Back to return."])
    return "\n".join(lines)


def format_edit_food_action(entry: dict) -> str:
    name = format_food_display_name(
        canonical_name=str(entry.get("canonical_name") or "Food"),
        restaurant=entry.get("restaurant"),
        size=None,
    )
    return (
        f"Editing: {name}\n"
        f"Meal: {str(entry.get('meal_category') or 'Other').title()}\n"
        f"Quantity: {format_display_number(float(entry.get('quantity') or 1))}\n\n"
        "What would you like to change?\n"
        "1. Quantity\n"
        "2. Meal\n"
        "3. Back"
    )


def handle_food_photo_conversation(
    *,
    active_conversation: dict,
    text: str,
    chat_id: int | str,
) -> bool:
    current_step = active_conversation.get("current_step")
    known_data = dict(
        active_conversation.get("known_data") or {}
    )
    lowered = text.lower().strip()

    if lowered in {"cancel", "exit", "quit", "close"}:
        cancel_conversation(chat_id)
        send_telegram_msg(
            "Meal-photo estimate cancelled. Nothing was logged.",
            chat_id=chat_id,
            remove_keyboard=True,
        )
        return True

    if current_step == "clarification":
        estimate = dict(known_data.get("estimate") or {})

        if lowered not in {
            "skip",
            "skip details",
            "no details",
        }:
            send_telegram_msg(
                "Thanks. I'm refining the estimate with those "
                "details.",
                chat_id=chat_id,
            )

            try:
                estimate = refine_food_photo_estimate(
                    estimate,
                    user_details=text,
                )
            except Exception:
                logging.exception(
                    "Food-photo estimate refinement failed"
                )
                send_telegram_msg(
                    "I couldn't refine the estimate from that "
                    "description. Try different details, reply "
                    "Skip details, or Cancel.",
                    chat_id=chat_id,
                )
                return True

        update_conversation(
            chat_id=chat_id,
            current_step="portion",
            known_data={
                "estimate": estimate,
                "clarification": (
                    None
                    if lowered in {
                        "skip",
                        "skip details",
                        "no details",
                    }
                    else text
                ),
            },
            missing_fields=[],
        )

        send_telegram_msg(
            format_food_photo_estimate(estimate)
            + "\n\n"
            "How much of the pictured food did you eat?\n"
            "1. All of it\n"
            "2. About 3/4\n"
            "3. About half\n"
            "4. Cancel",
            chat_id=chat_id,
        )
        return True

    if current_step == "portion":
        portions = {
            "1": 1.0,
            "all": 1.0,
            "all of it": 1.0,
            "2": 0.75,
            "3/4": 0.75,
            "75%": 0.75,
            "about 3/4": 0.75,
            "3": 0.5,
            "half": 0.5,
            "1/2": 0.5,
            "50%": 0.5,
            "about half": 0.5,
        }

        portion_fraction = portions.get(lowered)

        if portion_fraction is None:
            send_telegram_msg(
                "Please choose All of it, About 3/4, "
                "About half, or Cancel.",
                chat_id=chat_id,
            )
            return True

        update_conversation(
            chat_id=chat_id,
            current_step="meal",
            known_data={
                "portion_fraction": portion_fraction,
            },
            missing_fields=[],
        )

        send_telegram_msg(
            "Which meal should this estimate be logged under?\n\n"
            "Before breakfast\n"
            "Breakfast\n"
            "Morning snack\n"
            "Lunch\n"
            "Afternoon snack\n"
            "Dinner\n"
            "Dessert",
            chat_id=chat_id,
        )
        return True

    if current_step == "meal":
        meal_aliases = {
            "before breakfast": "before breakfast",
            "breakfast": "breakfast",
            "morning snack": "school snack",
            "school snack": "school snack",
            "lunch": "lunch",
            "afternoon snack": "afternoon snack",
            "dinner": "dinner",
            "dessert": "dessert",
        }

        meal_category = meal_aliases.get(lowered)

        if meal_category is None:
            send_telegram_msg(
                "Please choose one of the displayed meal options.",
                chat_id=chat_id,
            )
            return True

        estimate = dict(known_data.get("estimate") or {})
        portion_fraction = float(
            known_data.get("portion_fraction") or 1.0
        )

        nutrition = midpoint_food_photo_nutrition(
            estimate,
            portion_fraction=portion_fraction,
        )

        dish_name = str(
            estimate.get("dish_name")
            or "Photo-estimated meal"
        ).strip()

        update_conversation(
            chat_id=chat_id,
            current_step="confirm",
            known_data={
                "meal_category": meal_category,
                "nutrition": nutrition,
                "dish_name": dish_name,
            },
            missing_fields=[],
        )

        send_telegram_msg(
            "Log this estimated meal?\n\n"
            f"Food: {dish_name}\n"
            f"Meal: {meal_category.title()}\n"
            f"Amount eaten: "
            f"{format_display_number(portion_fraction * 100, decimals=0)}%\n\n"
            "Estimated midpoint nutrition:\n"
            f"Calories: {format_display_number(nutrition['calories'], decimals=0)}\n"
            f"Protein: {format_display_number(nutrition['protein_g'])} g\n"
            f"Carbohydrates: "
            f"{format_display_number(nutrition['carbohydrates_g'])} g\n"
            f"Fat: {format_display_number(nutrition['fat_g'])} g\n\n"
            "This will be logged as a visual estimate.\n\n"
            "1. Log Estimate\n"
            "2. Change details\n"
            "3. Cancel",
            chat_id=chat_id,
        )
        return True

    if current_step == "confirm":
        if lowered in {
            "2",
            "change",
            "change details",
            "edit",
        }:
            update_conversation(
                chat_id=chat_id,
                current_step="clarification",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                "Tell me any details that would improve this "
                "estimate. Include the meat or protein, cooking "
                "method, and any oil, sauce, or dressing.\n\n"
                "Reply with details, Skip details, or Cancel.",
                chat_id=chat_id,
            )
            return True

        if lowered not in {
            "1",
            "yes",
            "log",
            "log estimate",
        }:
            send_telegram_msg(
                "Please choose Log Estimate, Change details, "
                "or Cancel.",
                chat_id=chat_id,
            )
            return True

        nutrition = dict(known_data.get("nutrition") or {})
        estimate = dict(known_data.get("estimate") or {})
        dish_name = str(
            known_data.get("dish_name")
            or "Photo-estimated meal"
        ).strip()
        meal_category = str(
            known_data.get("meal_category") or ""
        ).strip()

        timestamp = datetime.now(PACIFIC_TZ).isoformat(
            timespec="seconds"
        )

        try:
            created = add_food_with_nutrition(
                canonical_name=dish_name,
                serving_description=(
                    f"1 visual estimate {timestamp}"
                ),
                serving_amount=1.0,
                serving_unit="estimated meal",
                verification_status="estimated",
                verification_source="visual_estimate",
                calories=nutrition["calories"],
                protein_g=nutrition["protein_g"],
                carbohydrates_g=nutrition[
                    "carbohydrates_g"
                ],
                fat_g=nutrition["fat_g"],
                fiber_g=None,
                sugar_g=None,
                sodium_mg=None,
                food_type="meal",
            )

            entry = add_food_entry(
                entry_date=datetime.now(PACIFIC_TZ).date(),
                meal_category=meal_category,
                food_id=int(created["food"]["food_id"]),
                quantity=1.0,
                logging_source="telegram_ai",
                original_text=(
                    active_conversation.get("original_message")
                    or "Food photo"
                ),
                quantity_is_estimated=True,
                user_confirmed=True,
            )
        except Exception:
            logging.exception(
                "Food-photo estimated meal logging failed"
            )
            send_telegram_msg(
                "I couldn't log that estimate safely. Nothing "
                "was added to today's food log.",
                chat_id=chat_id,
            )
            return True

        complete_conversation(chat_id)

        send_telegram_msg(
            "Estimated meal logged.\n\n"
            f"Food: {dish_name}\n"
            f"Meal: {meal_category.title()}\n"
            f"Calories: "
            f"{format_display_number(float(entry['calories'] or 0), decimals=0)}\n"
            f"Protein: "
            f"{format_display_number(float(entry['protein_g'] or 0))} g\n\n"
            "This entry is marked as a visual estimate.",
            chat_id=chat_id,
            remove_keyboard=True,
        )
        return True

    cancel_conversation(chat_id)
    send_telegram_msg(
        "That meal-photo estimate expired. Please send the "
        "photo again.",
        chat_id=chat_id,
        remove_keyboard=True,
    )
    return True


def process_telegram_update(update):
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    message_id = message.get("message_id")
    text = (message.get("text") or "").strip()
    caption = (message.get("caption") or "").strip()
    photo_sizes = list(message.get("photo") or [])

    if CHAT_ID and str(chat_id) != str(CHAT_ID):
        return

    if photo_sizes:
        file_id = photo_sizes[-1].get("file_id")
        photo_conversation = get_active_conversation(chat_id)
        photo_step = (
            photo_conversation.get("current_step")
            if photo_conversation
            and photo_conversation.get("conversation_type")
            == "healthcoach_menu"
            else None
        )

        food_photo = (
            photo_step == "await_food_photo"
            or (
                photo_step != "await_menu_photo"
                and is_food_photo_request(caption)
            )
        )

        if not file_id:
            send_telegram_msg(
                "I received the photo, but Telegram did not "
                "provide a usable file.",
                chat_id=chat_id,
            )
            return

        if food_photo:
            progress_message = (
                "I'm examining the meal and estimating nutrition "
                "ranges. This may take a moment."
            )
        else:
            progress_message = (
                "I'm reading the menu photo and looking for up "
                "to three promising entrees. This may take a moment."
            )

        send_telegram_msg(
            progress_message,
            chat_id=chat_id,
        )

        try:
            image_bytes, mime_type = download_telegram_photo(
                telegram_token=TELEGRAM_TOKEN,
                file_id=file_id,
            )

            if food_photo:
                result = analyze_food_photo(
                    image_bytes,
                    mime_type=mime_type,
                    user_context=caption,
                )

                start_conversation(
                    chat_id=chat_id,
                    conversation_type="food_photo_estimate",
                    current_step="clarification",
                    known_data={
                        "estimate": result,
                        "photo_caption": caption,
                    },
                    missing_fields=[],
                    original_message=caption or "Food photo",
                )

                response_message = (
                    format_food_photo_estimate(result)
                    + "\n\n"
                    "Tell me any details that would improve this "
                    "estimate. The most useful details are:\n"
                    "- Type of meat or protein\n"
                    "- Grilled, fried, breaded, or another method\n"
                    "- Added oil, sauce, or dressing\n\n"
                    "Reply with the details, Skip details, or Cancel."
                )
            else:
                result = analyze_menu_photo(
                    image_bytes,
                    mime_type=mime_type,
                    user_context=caption,
                )
                response_message = format_menu_photo_analysis(
                    result
                )
        except Exception:
            logging.exception(
                "%s photo analysis failed",
                "Food" if food_photo else "Menu",
            )

            if food_photo:
                error_message = (
                    "I couldn't estimate that meal photo. Try a "
                    "brighter picture showing the entire plate."
                )
            else:
                error_message = (
                    "I couldn't analyze that menu photo. Try a "
                    "closer, brighter picture with the item names "
                    "and descriptions in focus."
                )

            send_telegram_msg(
                error_message,
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            response_message,
            chat_id=chat_id,
        )
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

    if lowered_text in {"/menu", "menu", "main menu"}:
        start_conversation(
            chat_id=chat_id,
            conversation_type="healthcoach_menu",
            current_step="main",
            known_data={},
            missing_fields=[],
            original_message=text,
        )
        send_telegram_msg(
            healthcoach_main_menu_text(),
            chat_id=chat_id,
        )
        return

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

        show_unresolved_food_review(
            chat_id=chat_id,
            pending=pending,
        )
        return

    active_conversation = get_active_conversation(chat_id)

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "food_photo_estimate"
    ):
        if handle_food_photo_conversation(
            active_conversation=active_conversation,
            text=text,
            chat_id=chat_id,
        ):
            return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "healthcoach_menu"
    ):
        current_step = active_conversation.get("current_step")
        known_data = dict(
            active_conversation.get("known_data") or {}
        )
        lowered = text.lower().strip()
        today = datetime.now(PACIFIC_TZ).date()

        if lowered in {"cancel", "exit", "quit", "close"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Menu closed.",
                chat_id=chat_id,
                remove_keyboard=True,
            )
            return

        if lowered in {"menu", "main", "main menu"}:
            update_conversation(
                chat_id=chat_id,
                current_step="main",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                healthcoach_main_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "main":
            if lowered in {"1", "food"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "health"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="health",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_health_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"3", "reports"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="reports",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_reports_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "help"}:
                send_telegram_msg(
                    "You can still use natural language at any time "
                    "after closing the menu.\n\n"
                    "Examples:\n"
                    "- For lunch I had...\n"
                    "- Record my sleep as 7:15\n"
                    "- Run my weekly report\n\n"
                    "Reply menu to return or cancel to close.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_main_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "food":
            if lowered in {"1", "log", "log food"}:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "Send the food naturally, including the meal.\n\n"
                    "Example: For lunch I had a turkey sandwich.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"2", "show", "show today"}:
                send_telegram_msg(
                    format_daily_food_log(today),
                    chat_id=chat_id,
                )
                return

            if lowered in {"3", "edit", "edit today", "edit today's food"}:
                entries = sorted(
                    list_food_entries(entry_date=today),
                    key=lambda entry: int(entry["food_entry_id"]),
                    reverse=True,
                )

                if not entries:
                    send_telegram_msg(
                        "There are no food entries to edit today.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_select",
                    known_data={
                        "_edit_food_entry_ids": [
                            int(entry["food_entry_id"])
                            for entry in entries
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_edit_food_choices(entries),
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "undo", "undo last"}:
                entries = list_food_entries(entry_date=today)

                if not entries:
                    send_telegram_msg(
                        "There is no food entry to undo today.",
                        chat_id=chat_id,
                    )
                    return

                latest = max(
                    entries,
                    key=lambda entry: int(
                        entry["food_entry_id"]
                    ),
                )
                update_conversation(
                    chat_id=chat_id,
                    current_step="undo_food_confirmation",
                    known_data={
                        "_undo_food_entry_id": (
                            latest["food_entry_id"]
                        ),
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Undo this food entry?\n\n"
                    f"Meal: {str(latest['meal_category']).title()}\n"
                    f"Food: {latest['canonical_name']}\n"
                    f"Calories: {format_display_number(float(latest.get('calories') or 0), decimals=0)}\n\n"
                    "1. Yes, undo it\n"
                    "2. No, keep it",
                    chat_id=chat_id,
                )
                return

            if lowered in {"5", "favorites", "favorite"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorites",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_favorites_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "6",
                "saved foods",
                "saved food",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "7",
                "photo",
                "photo tools",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="photo_tools",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_photo_tools_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "8",
                "restaurant",
                "restaurant choices",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="restaurant",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_restaurant_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "9",
                "unknown",
                "update unknown foods",
            }:
                pending = get_pending_unresolved_foods()
                if not pending:
                    send_telegram_msg(
                        "There are no unknown foods waiting.",
                        chat_id=chat_id,
                    )
                else:
                    show_unresolved_food_review(
                        chat_id=chat_id,
                        pending=pending,
                    )
                return

            if lowered in {"10", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="main",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_main_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_food_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "photo_tools":
            if lowered in {
                "1",
                "read menu photo",
                "menu photo",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_menu_photo",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Send a clear restaurant menu photo.\n\n"
                    "HealthCoach will recommend up to three "
                    "promising choices using visible information.",
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "2",
                "estimate meal photo",
                "meal photo",
                "estimate food photo",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="await_food_photo",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Send a clear photo of the actual meal.\n\n"
                    "HealthCoach will estimate nutrition, ask for "
                    "important details, and log nothing without "
                    "your confirmation.",
                    chat_id=chat_id,
                )
                return

            if lowered in {"3", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_photo_tools_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step in {
            "await_menu_photo",
            "await_food_photo",
        }:
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="photo_tools",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_photo_tools_menu_text(),
                    chat_id=chat_id,
                )
                return

            if current_step == "await_menu_photo":
                message = "Send a clear restaurant menu photo."
            else:
                message = (
                    "Send a clear photo of the actual meal."
                )

            send_telegram_msg(
                message + "\n\nReply Back or Cancel to leave.",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_foods":
            if lowered in {
                "1",
                "browse",
                "browse saved foods",
            }:
                foods = list_user_saved_foods()

                if not foods:
                    send_telegram_msg(
                        "There are no manually saved foods yet.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_browse",
                    known_data={
                        "_saved_food_ids": [
                            int(food["food_id"])
                            for food in foods
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_choices(foods),
                    chat_id=chat_id,
                )
                return

            if lowered in {
                "2",
                "add",
                "add saved food",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_add_name",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "What should this saved food be called?\n\n"
                    "Example: Turkey Fiesta Bowl",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {
                "3",
                "edit",
                "edit saved food",
            }:
                foods = list_user_saved_foods()

                if not foods:
                    send_telegram_msg(
                        "There are no manually saved foods to edit.",
                        chat_id=chat_id,
                    )
                    return

                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_food_edit_select",
                    known_data={
                        "_saved_food_ids": [
                            int(food["food_id"])
                            for food in foods
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_saved_food_edit_choices(foods),
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_saved_foods_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_edit_select":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            food_ids = list(
                known_data.get("_saved_food_ids") or []
            )

            try:
                selection = int(lowered)
            except ValueError:
                selection = 0

            if selection < 1 or selection > len(food_ids):
                send_telegram_msg(
                    "Choose one of the numbered saved foods, "
                    "or reply Back.",
                    chat_id=chat_id,
                )
                return

            food_id = int(food_ids[selection - 1])
            food = next(
                (
                    item for item in list_user_saved_foods()
                    if int(item["food_id"]) == food_id
                ),
                None,
            )

            if food is None:
                send_telegram_msg(
                    "That saved food is no longer available.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_edit_calories",
                known_data={
                    "_saved_food_edit_id": food_id,
                    "_saved_food_edit_name": (
                        food["canonical_name"]
                    ),
                    "_saved_food_edit_serving": (
                        food.get("serving_description")
                        or "1 serving"
                    ),
                    "_saved_food_edit_old_version": int(
                        food.get("version_number") or 1
                    ),
                },
                missing_fields=[],
            )
            send_telegram_msg(
                f"Editing nutrition for "
                f"{food['canonical_name']}\n"
                f"Serving: "
                f"{food.get('serving_description') or '1 serving'}\n\n"
                f"Current calories: "
                f"{format_display_number(float(food.get('calories') or 0))}\n\n"
                "Enter the new calories for one serving.",
                chat_id=chat_id,
                remove_keyboard=True,
            )
            return

        if current_step in {
            "saved_food_edit_calories",
            "saved_food_edit_protein",
            "saved_food_edit_carbohydrates",
            "saved_food_edit_fat",
            "saved_food_edit_fiber",
            "saved_food_edit_sugar",
            "saved_food_edit_sodium",
        }:
            cleaned_number = (
                text.strip().lower()
                .replace(",", "")
                .replace("calories", "")
                .replace("calorie", "")
                .replace("cals", "")
                .replace("cal", "")
                .replace("grams", "")
                .replace("gram", "")
                .replace("mg", "")
                .replace("g", "")
                .strip()
            )

            try:
                number = float(cleaned_number)
            except ValueError:
                send_telegram_msg(
                    "Please enter a non-negative number.",
                    chat_id=chat_id,
                )
                return

            if number < 0:
                send_telegram_msg(
                    "Nutrition values cannot be negative.",
                    chat_id=chat_id,
                )
                return

            field_steps = {
                "saved_food_edit_calories": (
                    "_saved_food_edit_calories",
                    "saved_food_edit_protein",
                    "Enter the new grams of protein.",
                ),
                "saved_food_edit_protein": (
                    "_saved_food_edit_protein_g",
                    "saved_food_edit_carbohydrates",
                    "Enter the new grams of carbohydrates.",
                ),
                "saved_food_edit_carbohydrates": (
                    "_saved_food_edit_carbohydrates_g",
                    "saved_food_edit_fat",
                    "Enter the new grams of fat.",
                ),
                "saved_food_edit_fat": (
                    "_saved_food_edit_fat_g",
                    "saved_food_edit_fiber",
                    "Enter the new grams of fiber.",
                ),
                "saved_food_edit_fiber": (
                    "_saved_food_edit_fiber_g",
                    "saved_food_edit_sugar",
                    "Enter the new grams of sugar.",
                ),
                "saved_food_edit_sugar": (
                    "_saved_food_edit_sugar_g",
                    "saved_food_edit_sodium",
                    "Enter the new milligrams of sodium.",
                ),
            }

            updated = dict(known_data)

            if current_step in field_steps:
                field, next_step, prompt = field_steps[current_step]
                updated[field] = number
                update_conversation(
                    chat_id=chat_id,
                    current_step=next_step,
                    known_data=updated,
                    missing_fields=[],
                )
                send_telegram_msg(prompt, chat_id=chat_id)
                return

            updated["_saved_food_edit_sodium_mg"] = number
            new_version = int(
                updated["_saved_food_edit_old_version"]
            ) + 1

            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_edit_confirmation",
                known_data=updated,
                missing_fields=[],
            )
            send_telegram_msg(
                "Save these nutrition changes?\n\n"
                f"Food: {updated['_saved_food_edit_name']}\n"
                f"Serving: {updated['_saved_food_edit_serving']}\n"
                f"New nutrition version: {new_version}\n\n"
                f"Calories: "
                f"{format_display_number(updated['_saved_food_edit_calories'])}\n"
                f"Protein: "
                f"{format_display_number(updated['_saved_food_edit_protein_g'])} g\n"
                f"Carbohydrates: "
                f"{format_display_number(updated['_saved_food_edit_carbohydrates_g'])} g\n"
                f"Fat: "
                f"{format_display_number(updated['_saved_food_edit_fat_g'])} g\n"
                f"Fiber: "
                f"{format_display_number(updated['_saved_food_edit_fiber_g'])} g\n"
                f"Sugar: "
                f"{format_display_number(updated['_saved_food_edit_sugar_g'])} g\n"
                f"Sodium: "
                f"{format_display_number(updated['_saved_food_edit_sodium_mg'])} mg\n\n"
                "Previously logged entries will not change.\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_edit_confirmation":
            if lowered in {"2", "no"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "No nutrition changes were saved.\n\n"
                    + healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return

            try:
                version = add_user_nutrition_version(
                    food_id=int(
                        known_data["_saved_food_edit_id"]
                    ),
                    calories=float(
                        known_data["_saved_food_edit_calories"]
                    ),
                    protein_g=float(
                        known_data["_saved_food_edit_protein_g"]
                    ),
                    carbohydrates_g=float(
                        known_data[
                            "_saved_food_edit_carbohydrates_g"
                        ]
                    ),
                    fat_g=float(
                        known_data["_saved_food_edit_fat_g"]
                    ),
                    fiber_g=float(
                        known_data["_saved_food_edit_fiber_g"]
                    ),
                    sugar_g=float(
                        known_data["_saved_food_edit_sugar_g"]
                    ),
                    sodium_mg=float(
                        known_data["_saved_food_edit_sodium_mg"]
                    ),
                )
            except Exception:
                logging.exception("Could not edit saved food")
                send_telegram_msg(
                    "I could not save those changes. "
                    "The existing nutrition is still active.",
                    chat_id=chat_id,
                )
                return

            food_name = str(
                known_data["_saved_food_edit_name"]
            )
            update_conversation(
                chat_id=chat_id,
                current_step="saved_foods",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                f"Updated {food_name} to nutrition version "
                f"{version['version_number']}.\n"
                "Previously logged entries were not changed.\n\n"
                + healthcoach_saved_foods_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_add_name":
            name = text.strip()

            if len(name) < 2:
                send_telegram_msg(
                    "Please enter a food name with at least two characters.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_add_serving",
                known_data={"_saved_food_name": name},
                missing_fields=[],
            )
            send_telegram_msg(
                "What is one serving?\n\n"
                "Examples:\n"
                "- 1 bowl\n"
                "- 1 bar\n"
                "- 12 fl oz\n"
                "- 4 ounces",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_add_serving":
            serving = text.strip()

            if len(serving) < 2:
                send_telegram_msg(
                    "Please describe one serving, such as 1 bowl.",
                    chat_id=chat_id,
                )
                return

            updated = dict(known_data)
            updated["_saved_food_serving"] = serving
            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_add_calories",
                known_data=updated,
                missing_fields=[],
            )
            send_telegram_msg(
                "How many calories are in that serving?",
                chat_id=chat_id,
            )
            return

        if current_step in {
            "saved_food_add_calories",
            "saved_food_add_protein",
            "saved_food_add_carbohydrates",
            "saved_food_add_fat",
            "saved_food_add_fiber",
            "saved_food_add_sugar",
            "saved_food_add_sodium",
        }:
            cleaned_number = (
                text.strip().lower()
                .replace(",", "")
                .replace("calories", "")
                .replace("calorie", "")
                .replace("cals", "")
                .replace("cal", "")
                .replace("grams", "")
                .replace("gram", "")
                .replace("mg", "")
                .replace("g", "")
                .strip()
            )

            try:
                number = float(cleaned_number)
            except ValueError:
                send_telegram_msg(
                    "Please enter a non-negative number.",
                    chat_id=chat_id,
                )
                return

            if number < 0:
                send_telegram_msg(
                    "Nutrition values cannot be negative.",
                    chat_id=chat_id,
                )
                return

            field_steps = {
                "saved_food_add_calories": (
                    "_saved_food_calories",
                    "saved_food_add_protein",
                    "How many grams of protein?",
                ),
                "saved_food_add_protein": (
                    "_saved_food_protein_g",
                    "saved_food_add_carbohydrates",
                    "How many grams of carbohydrates?",
                ),
                "saved_food_add_carbohydrates": (
                    "_saved_food_carbohydrates_g",
                    "saved_food_add_fat",
                    "How many grams of fat?",
                ),
                "saved_food_add_fat": (
                    "_saved_food_fat_g",
                    "saved_food_add_fiber",
                    "How many grams of fiber?",
                ),
                "saved_food_add_fiber": (
                    "_saved_food_fiber_g",
                    "saved_food_add_sugar",
                    "How many grams of sugar?",
                ),
                "saved_food_add_sugar": (
                    "_saved_food_sugar_g",
                    "saved_food_add_sodium",
                    "How many milligrams of sodium?",
                ),
            }

            updated = dict(known_data)

            if current_step in field_steps:
                field, next_step, prompt = field_steps[current_step]
                updated[field] = number
                update_conversation(
                    chat_id=chat_id,
                    current_step=next_step,
                    known_data=updated,
                    missing_fields=[],
                )
                send_telegram_msg(prompt, chat_id=chat_id)
                return

            updated["_saved_food_sodium_mg"] = number
            update_conversation(
                chat_id=chat_id,
                current_step="saved_food_add_confirmation",
                known_data=updated,
                missing_fields=[],
            )
            send_telegram_msg(
                "Save this saved food?\n\n"
                f"Food: {updated['_saved_food_name']}\n"
                f"Serving: {updated['_saved_food_serving']}\n"
                f"Calories: {format_display_number(updated['_saved_food_calories'])}\n"
                f"Protein: {format_display_number(updated['_saved_food_protein_g'])} g\n"
                f"Carbohydrates: {format_display_number(updated['_saved_food_carbohydrates_g'])} g\n"
                f"Fat: {format_display_number(updated['_saved_food_fat_g'])} g\n"
                f"Fiber: {format_display_number(updated['_saved_food_fiber_g'])} g\n"
                f"Sugar: {format_display_number(updated['_saved_food_sugar_g'])} g\n"
                f"Sodium: {format_display_number(updated['_saved_food_sodium_mg'])} mg\n\n"
                "1. Yes\n"
                "2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_add_confirmation":
            if lowered in {"2", "no"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "No saved food was added.\n\n"
                    + healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "yes", "save"}:
                send_telegram_msg(
                    "Please choose Yes or No.",
                    chat_id=chat_id,
                )
                return

            serving_description = str(
                known_data["_saved_food_serving"]
            ).strip()
            serving_match = re.fullmatch(
                r"\s*([0-9]+(?:\.[0-9]+)?)\s+(.+?)\s*",
                serving_description,
            )

            if serving_match:
                serving_amount = float(serving_match.group(1))
                serving_unit = serving_match.group(2)
            else:
                serving_amount = 1.0
                serving_unit = serving_description

            try:
                result = add_food_with_nutrition(
                    canonical_name=str(
                        known_data["_saved_food_name"]
                    ).strip(),
                    serving_description=serving_description,
                    serving_amount=serving_amount,
                    serving_unit=serving_unit,
                    verification_status="verified",
                    verification_source="user_entered",
                    calories=float(
                        known_data["_saved_food_calories"]
                    ),
                    protein_g=float(
                        known_data["_saved_food_protein_g"]
                    ),
                    carbohydrates_g=float(
                        known_data["_saved_food_carbohydrates_g"]
                    ),
                    fat_g=float(
                        known_data["_saved_food_fat_g"]
                    ),
                    fiber_g=float(
                        known_data["_saved_food_fiber_g"]
                    ),
                    sugar_g=float(
                        known_data["_saved_food_sugar_g"]
                    ),
                    sodium_mg=float(
                        known_data["_saved_food_sodium_mg"]
                    ),
                )
            except Exception:
                logging.exception("Could not add saved food")
                send_telegram_msg(
                    "I could not save that food. Nothing was changed.",
                    chat_id=chat_id,
                )
                return

            food = result["food"]
            created = bool(result.get("created"))
            update_conversation(
                chat_id=chat_id,
                current_step="saved_foods",
                known_data={},
                missing_fields=[],
            )

            if created:
                result_message = (
                    f"Saved {food['canonical_name']} "
                    "to your food library."
                )
            else:
                result_message = (
                    f"{food['canonical_name']} already exists. "
                    "No duplicate was created."
                )

            send_telegram_msg(
                result_message
                + "\n\n"
                + healthcoach_saved_foods_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "saved_food_browse":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="saved_foods",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_saved_foods_menu_text(),
                    chat_id=chat_id,
                )
                return

            food_ids = list(
                known_data.get("_saved_food_ids") or []
            )

            try:
                selection = int(lowered)
            except ValueError:
                selection = 0

            if selection < 1 or selection > len(food_ids):
                send_telegram_msg(
                    "Choose one of the numbered saved foods, "
                    "or reply Back.",
                    chat_id=chat_id,
                )
                return

            selected_id = int(food_ids[selection - 1])
            foods = list_user_saved_foods()
            selected = next(
                (
                    food for food in foods
                    if int(food["food_id"]) == selected_id
                ),
                None,
            )

            if selected is None:
                send_telegram_msg(
                    "That saved food is no longer available.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                format_saved_food_details(selected),
                chat_id=chat_id,
            )
            return

        if current_step == "restaurant":
            if lowered in {
                "1",
                "find best choices online",
                "find choices",
                "search",
                "search again",
            }:
                update_conversation(
                    chat_id=chat_id,
                    current_step="restaurant_query",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "What restaurant are you at?\n\n"
                    "Include the city and state for a local restaurant.\n"
                    "Example: Red Robin in Redding, California",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"2", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_restaurant_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "restaurant_query":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="restaurant",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_restaurant_menu_text(),
                    chat_id=chat_id,
                )
                return

            if len(text.strip()) < 2:
                send_telegram_msg(
                    "Please send the restaurant name. Include the city "
                    "and state if it is a local restaurant.",
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                "I'm checking the current menu and cited nutrition. "
                "This may take a moment.",
                chat_id=chat_id,
                remove_keyboard=True,
            )

            try:
                advice = recommend_restaurant_entrees(text)
            except Exception:
                logging.exception(
                    "Restaurant recommendation lookup failed"
                )
                advice = {
                    "found": False,
                    "restaurant_display_name": text,
                    "candidates": [],
                    "notes": [
                        "The online menu lookup failed. Please try again."
                    ],
                }

            update_conversation(
                chat_id=chat_id,
                current_step="restaurant",
                known_data={
                    "_restaurant_query": text,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                format_restaurant_advice(advice),
                chat_id=chat_id,
            )
            return

        if current_step == "favorites":
            if lowered in {"1", "quick log", "quick"}:
                favorites = list_food_favorites()
                if not favorites:
                    send_telegram_msg(
                        "You do not have any saved favorites yet.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorite_quick_select",
                    known_data={
                        "_favorite_ids": [
                            int(item["food_favorite_id"])
                            for item in favorites
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_favorite_choices(
                        favorites,
                        action="log",
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "save today's food", "save today"}:
                entries = sorted(
                    list_food_entries(entry_date=today),
                    key=lambda item: int(item["food_entry_id"]),
                    reverse=True,
                )
                if not entries:
                    send_telegram_msg(
                        "There are no foods logged today to save.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorite_save_select",
                    known_data={
                        "_entry_ids": [
                            int(item["food_entry_id"])
                            for item in entries
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_save_favorite_choices(entries),
                    chat_id=chat_id,
                )
                return

            if lowered in {"3", "remove favorite", "remove"}:
                favorites = list_food_favorites()
                if not favorites:
                    send_telegram_msg(
                        "You do not have any saved favorites yet.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorite_remove_select",
                    known_data={
                        "_favorite_ids": [
                            int(item["food_favorite_id"])
                            for item in favorites
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_favorite_choices(
                        favorites,
                        action="remove",
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"4", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_favorites_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step in {
            "favorite_quick_select",
            "favorite_save_select",
            "favorite_remove_select",
        }:
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorites",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_favorites_menu_text(),
                    chat_id=chat_id,
                )
                return

            try:
                choice = int(lowered)
            except ValueError:
                choice = 0

            if current_step == "favorite_save_select":
                ids = list(known_data.get("_entry_ids") or [])
                entries = sorted(
                    list_food_entries(entry_date=today),
                    key=lambda item: int(item["food_entry_id"]),
                    reverse=True,
                )
                if choice < 1 or choice > len(ids):
                    send_telegram_msg(
                        format_save_favorite_choices(entries),
                        chat_id=chat_id,
                    )
                    return
                entry_id = int(ids[choice - 1])
                entry = next(
                    (
                        item for item in entries
                        if int(item["food_entry_id"]) == entry_id
                    ),
                    None,
                )
                if entry is None:
                    send_telegram_msg(
                        "That food entry no longer exists.",
                        chat_id=chat_id,
                    )
                    return
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorite_save_confirmation",
                    known_data={"_entry_id": entry_id},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Save this favorite?\n\n"
                    f"Food: {entry['canonical_name']}\n"
                    f"Meal: {str(entry['meal_category']).title()}\n"
                    f"Quantity: {format_display_number(float(entry['quantity']))}\n\n"
                    "1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return

            favorites = list_food_favorites()
            ids = list(known_data.get("_favorite_ids") or [])
            if choice < 1 or choice > len(ids):
                send_telegram_msg(
                    format_favorite_choices(
                        favorites,
                        action=(
                            "remove"
                            if current_step == "favorite_remove_select"
                            else "log"
                        ),
                    ),
                    chat_id=chat_id,
                )
                return
            favorite_id = int(ids[choice - 1])
            favorite = next(
                (
                    item for item in favorites
                    if int(item["food_favorite_id"])
                    == favorite_id
                ),
                None,
            )
            if favorite is None:
                send_telegram_msg(
                    "That favorite no longer exists.",
                    chat_id=chat_id,
                )
                return

            is_remove = current_step == "favorite_remove_select"
            update_conversation(
                chat_id=chat_id,
                current_step=(
                    "favorite_remove_confirmation"
                    if is_remove
                    else "favorite_quick_confirmation"
                ),
                known_data={"_favorite_id": favorite_id},
                missing_fields=[],
            )
            send_telegram_msg(
                (
                    "Remove this favorite?\n\n"
                    if is_remove
                    else "Quick-log this favorite?\n\n"
                )
                + f"Food: {favorite['canonical_name']}\n"
                + f"Meal: {str(favorite['meal_category']).title()}\n"
                + f"Quantity: {format_display_number(float(favorite['quantity']))}\n\n"
                + "1. Yes\n2. No",
                chat_id=chat_id,
            )
            return

        if current_step in {
            "favorite_save_confirmation",
            "favorite_quick_confirmation",
            "favorite_remove_confirmation",
        }:
            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="favorites",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "No changes were made.\n\n"
                    + healthcoach_favorites_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered not in {"1", "yes"}:
                prompt = {
                    "favorite_save_confirmation": "Save this favorite?",
                    "favorite_quick_confirmation": "Quick-log this favorite?",
                    "favorite_remove_confirmation": "Remove this favorite?",
                }[current_step]
                send_telegram_msg(
                    prompt + "\n\n1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return

            if current_step == "favorite_save_confirmation":
                try:
                    favorite = save_food_favorite_from_entry(
                        int(known_data["_entry_id"])
                    )
                except (KeyError, ValueError) as error:
                    send_telegram_msg(str(error), chat_id=chat_id)
                    return
                result_message = (
                    f"Saved {favorite['canonical_name']} as a favorite."
                )

            elif current_step == "favorite_remove_confirmation":
                removed = delete_food_favorite(
                    int(known_data.get("_favorite_id") or 0)
                )
                result_message = (
                    "Favorite removed."
                    if removed
                    else "That favorite no longer exists."
                )

            else:
                favorite_id = int(
                    known_data.get("_favorite_id") or 0
                )
                favorite = next(
                    (
                        item for item in list_food_favorites()
                        if int(item["food_favorite_id"])
                        == favorite_id
                    ),
                    None,
                )
                if favorite is None:
                    send_telegram_msg(
                        "That favorite no longer exists.",
                        chat_id=chat_id,
                    )
                    return
                duplicate = find_recent_duplicate_entry(
                    entry_date=today,
                    meal_category=favorite["meal_category"],
                    food_id=int(favorite["food_id"]),
                    quantity=float(favorite["quantity"]),
                    window_minutes=5,
                )
                if duplicate is not None:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="favorites",
                        known_data={},
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        "That exact favorite was logged within the "
                        "last five minutes, so it was not logged again.\n\n"
                        + healthcoach_favorites_menu_text(),
                        chat_id=chat_id,
                    )
                    return
                try:
                    add_food_entry(
                        entry_date=today,
                        meal_category=favorite["meal_category"],
                        food_id=int(favorite["food_id"]),
                        quantity=float(favorite["quantity"]),
                        logging_source="telegram_ai",
                        original_text="Quick logged from favorites",
                        quantity_is_estimated=False,
                        user_confirmed=True,
                    )
                except ValueError as error:
                    update_conversation(
                        chat_id=chat_id,
                        current_step="favorites",
                        known_data={},
                        missing_fields=[],
                    )
                    send_telegram_msg(
                        str(error) + "\n\n"
                        + healthcoach_favorites_menu_text(),
                        chat_id=chat_id,
                    )
                    return
                except Exception:
                    logging.exception("Favorite quick log failed")
                    send_telegram_msg(
                        "The favorite could not be logged.",
                        chat_id=chat_id,
                    )
                    return
                try:
                    sync_food_ledger_totals_to_sheet(today)
                except Exception:
                    logging.exception(
                        "Favorite Google Sheet sync failed"
                    )
                    result_message = (
                        f"Quick logged {favorite['canonical_name']}, "
                        "but the Google Sheet totals could not be updated."
                    )
                else:
                    result_message = (
                        f"Quick logged {favorite['canonical_name']} for "
                        f"{str(favorite['meal_category']).title()}."
                    )

            update_conversation(
                chat_id=chat_id,
                current_step="favorites",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                result_message + "\n\n"
                + healthcoach_favorites_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "edit_food_select":
            if lowered == "back":
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            entry_ids = list(
                known_data.get("_edit_food_entry_ids") or []
            )

            try:
                choice = int(lowered)
            except ValueError:
                choice = 0

            if choice < 1 or choice > len(entry_ids):
                entries = sorted(
                    list_food_entries(entry_date=today),
                    key=lambda entry: int(entry["food_entry_id"]),
                    reverse=True,
                )
                send_telegram_msg(
                    format_edit_food_choices(entries),
                    chat_id=chat_id,
                )
                return

            entry_id = int(entry_ids[choice - 1])
            entry = next(
                (
                    candidate
                    for candidate in list_food_entries(
                        entry_date=today
                    )
                    if int(candidate["food_entry_id"]) == entry_id
                ),
                None,
            )

            if entry is None:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That food entry no longer exists.\n\n"
                    + healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="edit_food_action",
                known_data={"_edit_food_entry_id": entry_id},
                missing_fields=[],
            )
            send_telegram_msg(
                format_edit_food_action(entry),
                chat_id=chat_id,
            )
            return

        if current_step == "edit_food_action":
            entry_id = known_data.get("_edit_food_entry_id")
            entry = next(
                (
                    candidate
                    for candidate in list_food_entries(
                        entry_date=today
                    )
                    if int(candidate["food_entry_id"])
                    == int(entry_id)
                ),
                None,
            ) if entry_id is not None else None

            if entry is None:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "That food entry no longer exists.\n\n"
                    + healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"1", "quantity"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_quantity",
                    known_data=known_data,
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Send the new numeric quantity.\n\n"
                    "Examples: 0.5, 1, or 1.3 servings.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"2", "meal"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_meal",
                    known_data=known_data,
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Choose the new meal:\n\n"
                    "Before breakfast\nBreakfast\nSchool snack\n"
                    "Lunch\nAfternoon snack\nDinner\nDessert",
                    chat_id=chat_id,
                )
                return

            if lowered in {"3", "back"}:
                entries = sorted(
                    list_food_entries(entry_date=today),
                    key=lambda candidate: int(
                        candidate["food_entry_id"]
                    ),
                    reverse=True,
                )
                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_select",
                    known_data={
                        "_edit_food_entry_ids": [
                            int(candidate["food_entry_id"])
                            for candidate in entries
                        ],
                    },
                    missing_fields=[],
                )
                send_telegram_msg(
                    format_edit_food_choices(entries),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                format_edit_food_action(entry),
                chat_id=chat_id,
            )
            return

        if current_step == "edit_food_quantity":
            match = re.fullmatch(
                r"(?:quantity\s+)?"
                r"((?:\d+(?:\.\d+)?)|(?:\.\d+))"
                r"(?:\s+servings?)?",
                lowered,
            )

            if match is None or float(match.group(1)) <= 0:
                send_telegram_msg(
                    "Please send a quantity greater than zero.\n"
                    "Examples: 0.5, 1, or 1.3 servings.",
                    chat_id=chat_id,
                )
                return

            new_quantity = float(match.group(1))
            update_conversation(
                chat_id=chat_id,
                current_step="edit_food_confirmation",
                known_data={
                    **known_data,
                    "_edit_field": "quantity",
                    "_edit_value": new_quantity,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Apply this food change?\n\n"
                f"New quantity: {format_display_number(new_quantity)}\n\n"
                "1. Yes\n2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "edit_food_meal":
            meal_aliases = {
                "before breakfast": "before breakfast",
                "breakfast": "breakfast",
                "school snack": "school snack",
                "morning snack": "school snack",
                "lunch": "lunch",
                "afternoon snack": "afternoon snack",
                "snack": "afternoon snack",
                "dinner": "dinner",
                "dessert": "dessert",
            }

            if lowered == "back":
                entry_id = known_data.get("_edit_food_entry_id")
                entry = next(
                    (
                        candidate
                        for candidate in list_food_entries(
                            entry_date=today
                        )
                        if int(candidate["food_entry_id"])
                        == int(entry_id)
                    ),
                    None,
                ) if entry_id is not None else None
                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_action",
                    known_data=known_data,
                    missing_fields=[],
                )
                if entry is not None:
                    send_telegram_msg(
                        format_edit_food_action(entry),
                        chat_id=chat_id,
                    )
                return

            new_meal = meal_aliases.get(lowered)
            if new_meal is None:
                send_telegram_msg(
                    "Choose the new meal:\n\n"
                    "Before breakfast\nBreakfast\nSchool snack\n"
                    "Lunch\nAfternoon snack\nDinner\nDessert",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="edit_food_confirmation",
                known_data={
                    **known_data,
                    "_edit_field": "meal",
                    "_edit_value": new_meal,
                },
                missing_fields=[],
            )
            send_telegram_msg(
                "Apply this food change?\n\n"
                f"New meal: {new_meal.title()}\n\n"
                "1. Yes\n2. No",
                chat_id=chat_id,
            )
            return

        if current_step == "edit_food_confirmation":
            if lowered in {"2", "no", "back"}:
                entry_id = known_data.get("_edit_food_entry_id")
                entry = next(
                    (
                        candidate
                        for candidate in list_food_entries(
                            entry_date=today
                        )
                        if int(candidate["food_entry_id"])
                        == int(entry_id)
                    ),
                    None,
                ) if entry_id is not None else None
                update_conversation(
                    chat_id=chat_id,
                    current_step="edit_food_action",
                    known_data={"_edit_food_entry_id": entry_id},
                    missing_fields=[],
                )
                if entry is not None:
                    send_telegram_msg(
                        "No changes were made.\n\n"
                        + format_edit_food_action(entry),
                        chat_id=chat_id,
                    )
                return

            if lowered not in {"1", "yes"}:
                send_telegram_msg(
                    "Apply this food change?\n\n1. Yes\n2. No",
                    chat_id=chat_id,
                )
                return

            entry_id = known_data.get("_edit_food_entry_id")
            edit_field = known_data.get("_edit_field")
            edit_value = known_data.get("_edit_value")

            try:
                if entry_id is None:
                    updated = None
                elif edit_field == "quantity":
                    updated = update_food_entry(
                        int(entry_id),
                        quantity=float(edit_value),
                    )
                elif edit_field == "meal":
                    updated = update_food_entry(
                        int(entry_id),
                        meal_category=str(edit_value),
                    )
                else:
                    updated = None
            except ValueError as error:
                send_telegram_msg(str(error), chat_id=chat_id)
                return

            if updated is None:
                send_telegram_msg(
                    "That food entry could not be updated.",
                    chat_id=chat_id,
                )
                return

            try:
                sync_food_ledger_totals_to_sheet(today)
            except Exception:
                logging.exception(
                    "Google Sheet sync failed after Food edit"
                )
                result_message = (
                    "Food was updated, but the Google Sheet "
                    "could not be updated."
                )
            else:
                result_message = (
                    "Food updated and today's totals recalculated."
                )

            update_conversation(
                chat_id=chat_id,
                current_step="food",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                result_message + "\n\n" + healthcoach_food_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "undo_food_confirmation":
            if lowered in {"1", "yes", "undo"}:
                entry_id = known_data.get("_undo_food_entry_id")
                deleted = (
                    delete_food_entry(int(entry_id))
                    if entry_id is not None
                    else False
                )

                if not deleted:
                    send_telegram_msg(
                        "That food entry no longer exists.",
                        chat_id=chat_id,
                    )
                else:
                    try:
                        sync_food_ledger_totals_to_sheet(today)
                    except Exception:
                        logging.exception(
                            "Google Sheet sync failed after Food undo"
                        )
                        send_telegram_msg(
                            "Food was removed, but the Google Sheet "
                            "could not be updated.",
                            chat_id=chat_id,
                        )
                    else:
                        send_telegram_msg(
                            "The last food entry was removed and "
                            "today's totals were updated.",
                            chat_id=chat_id,
                        )

                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "no", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="food",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "Food entry kept.\n\n"
                    + healthcoach_food_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                "Please choose:\n"
                "1. Yes, undo it\n"
                "2. No, keep it",
                chat_id=chat_id,
            )
            return

        if current_step == "health":
            if lowered in {"1", "status", "current status"}:
                metrics = get_today_metrics()
                send_telegram_msg(
                    build_progress_message(
                        "Current status",
                        metrics,
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "sleep", "record sleep"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="health_sleep_entry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "How much did you sleep?\n\n"
                    "Examples:\n"
                    "- 7:15\n"
                    "- I slept 7 hours\n"
                    "- I got 6 and a half hours\n"
                    "- I slept from 10:30 PM to 5:45 AM",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"3", "weight", "record weight"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="health_weight_entry",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    "What was your morning weight?\n\n"
                    "Examples: 214.6 or I weighed 214.6 this morning.",
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"4", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="main",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_main_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_health_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "health_sleep_entry":
            sleep_value = extract_sleep_value_from_text(
                text,
                allow_bare=True,
            )

            if sleep_value is None:
                send_telegram_msg(
                    "I couldn't understand that sleep amount. "
                    "Try 7:15, 7 hours, or 6 and a half hours.",
                    chat_id=chat_id,
                )
                return

            success, response = set_today_sleep(sleep_value)
            update_conversation(
                chat_id=chat_id,
                current_step="health",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                response + "\n\n" + healthcoach_health_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "health_weight_entry":
            weight_value = extract_weight_value_from_text(
                text,
                allow_bare=True,
            )

            if weight_value is None:
                send_telegram_msg(
                    "I couldn't understand that weight. "
                    "Enter pounds, such as 214.6.",
                    chat_id=chat_id,
                )
                return

            success, response = set_today_weight(weight_value)
            update_conversation(
                chat_id=chat_id,
                current_step="health",
                known_data={},
                missing_fields=[],
            )
            send_telegram_msg(
                response + "\n\n" + healthcoach_health_menu_text(),
                chat_id=chat_id,
            )
            return

        if current_step == "reports":
            if lowered in {"1", "today", "today's summary"}:
                metrics = get_today_metrics()
                send_telegram_msg(
                    build_progress_message(
                        "Today's summary",
                        metrics,
                    ),
                    chat_id=chat_id,
                )
                return

            if lowered in {"2", "weekly", "weekly report"}:
                cancel_conversation(chat_id)
                send_weekly_report(
                    chat_id=chat_id,
                    remove_keyboard=True,
                )
                return

            if lowered in {"3", "back"}:
                update_conversation(
                    chat_id=chat_id,
                    current_step="main",
                    known_data={},
                    missing_fields=[],
                )
                send_telegram_msg(
                    healthcoach_main_menu_text(),
                    chat_id=chat_id,
                )
                return

            send_telegram_msg(
                healthcoach_reports_menu_text(),
                chat_id=chat_id,
            )
            return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "unresolved_food_review"
        and active_conversation.get("current_step")
        == "edit_details"
    ):
        known_data = dict(
            active_conversation.get("known_data") or {}
        )
        unresolved_food_id = known_data.get(
            "_unresolved_food_id"
        )
        skipped_ids = list(
            known_data.get("_skipped_unresolved_food_ids") or []
        )

        try:
            interpretation = interpret_food_message(text)
        except Exception:
            logging.exception(
                "Unresolved Food detail interpretation failed"
            )
            send_telegram_msg(
                "I could not interpret those details. Try again.",
                chat_id=chat_id,
            )
            return

        if (
            unresolved_food_id is None
            or not interpretation.is_food_logging_request
            or not interpretation.food_name
        ):
            send_telegram_msg(
                "Please send a complete corrected food description.",
                chat_id=chat_id,
            )
            return

        existing = get_unresolved_food(int(unresolved_food_id))

        if existing is None:
            show_unresolved_food_review(chat_id=chat_id)
            return

        updated = update_unresolved_food_details(
            int(unresolved_food_id),
            original_text=text,
            meal_category=(
                interpretation.meal_category
                or existing.get("meal_category")
            ),
            food_name=interpretation.food_name,
            brand=interpretation.brand,
            restaurant=interpretation.restaurant,
            size=interpretation.size,
            quantity=interpretation.quantity,
            quantity_description=(
                interpretation.quantity_description
            ),
        )

        send_telegram_msg(
            "Unknown food details updated.",
            chat_id=chat_id,
        )
        show_unresolved_food_review(
            chat_id=chat_id,
            unresolved_food_id=int(
                updated["unresolved_food_id"]
            ),
            skipped_ids=skipped_ids,
        )
        return

    if (
        active_conversation
        and active_conversation.get("conversation_type")
        == "unresolved_food_review"
        and active_conversation.get("current_step") == "menu"
    ):
        lowered = text.lower().strip()
        known_data = dict(
            active_conversation.get("known_data") or {}
        )
        unresolved_food_id = known_data.get(
            "_unresolved_food_id"
        )
        skipped_ids = list(
            known_data.get("_skipped_unresolved_food_ids") or []
        )
        item = (
            get_unresolved_food(int(unresolved_food_id))
            if unresolved_food_id is not None
            else None
        )

        if item is None or item.get("status") != "pending":
            show_unresolved_food_review(chat_id=chat_id)
            return

        if lowered in {"1", "enter nutrition", "nutrition"}:
            item_data = dict(item)
            item_data.update(
                {
                    "_unresolved_food_id": unresolved_food_id,
                    "_entry_date": item.get("entry_date"),
                    "_manual_label_field": "serving_size",
                    "_skipped_unresolved_food_ids": skipped_ids,
                }
            )

            start_conversation(
                chat_id=chat_id,
                conversation_type="food_interpretation",
                current_step="manual_label_entry",
                known_data=item_data,
                missing_fields=[],
                original_message=str(
                    item.get("original_text") or ""
                ),
            )

            send_telegram_msg(
                "What serving size applies to this food?\n"
                "Examples: 28 g, 1 oz, 1 serving.",
                chat_id=chat_id,
            )
            return

        if lowered in {"2", "change", "change name", "edit"}:
            update_conversation(
                chat_id=chat_id,
                current_step="edit_details",
                known_data=known_data,
                missing_fields=[],
            )
            send_telegram_msg(
                "Send the complete corrected food description.\n\n"
                "Example: For breakfast I had one medium apple.",
                chat_id=chat_id,
            )
            return

        if lowered in {"exit", "quit", "stop reviewing"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Unknown-food review closed. No foods were changed.",
                chat_id=chat_id,
            )
            return

        if lowered in {"3", "retry", "automatic lookup"}:
            logging.info(
                "Retrying unresolved Food lookup: id=%s "
                "food=%r size=%r brand=%r restaurant=%r",
                unresolved_food_id,
                item.get("food_name"),
                item.get("size"),
                item.get("brand"),
                item.get("restaurant"),
            )

            try:
                provider_result = lookup_official_nutrition(
                    restaurant=item.get("restaurant"),
                    food_name=item.get("food_name"),
                    size=item.get("size"),
                    brand=item.get("brand"),
                )
            except Exception:
                logging.exception(
                    "Unresolved Food automatic lookup failed"
                )
                send_telegram_msg(
                    "The automatic lookup failed. The food remains "
                    "in the queue.",
                    chat_id=chat_id,
                )
                return

            if not provider_result.get("found"):
                provider_notes = [
                    str(note)
                    for note in (
                        provider_result.get("notes") or []
                    )
                    if str(note).strip()
                ]
                logging.info(
                    "Unresolved Food lookup unsupported: id=%s "
                    "missing=%r question=%r notes=%r",
                    unresolved_food_id,
                    provider_result.get("missing_fields"),
                    provider_result.get("clarification_question"),
                    provider_notes,
                )

                explanation = (
                    provider_notes[0]
                    if provider_notes
                    else "No verified match was returned."
                )
                send_telegram_msg(
                    "I still could not verify this food "
                    "automatically.\n\n"
                    f"Reason: {explanation}\n\n"
                    "Choose another option.",
                    chat_id=chat_id,
                )
                return

            provider_food = provider_result["food"]
            provider_nutrition = provider_result["nutrition"]
            verification = provider_result["verification"]

            try:
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
            except Exception:
                logging.exception(
                    "Unresolved verified Food save failed"
                )
                send_telegram_msg(
                    "Nutrition was found, but it could not be saved.",
                    chat_id=chat_id,
                )
                return

            saved_food = saved["food"]
            saved_nutrition = saved["nutrition"] or {}

            try:
                quantity = resolve_non_restaurant_quantity(
                    food_id=saved_food["food_id"],
                    quantity=item.get("quantity"),
                    quantity_description=item.get(
                        "quantity_description"
                    ),
                    size=item.get("size"),
                    serving_amount=saved_food.get("serving_amount"),
                    serving_unit=saved_food.get("serving_unit"),
                )
            except ValueError as error:
                send_telegram_msg(str(error), chat_id=chat_id)
                return

            pending_components = [
                {
                    "role": "Food",
                    "food_id": saved_food["food_id"],
                    "canonical_name": saved_food["canonical_name"],
                    "restaurant": saved_food.get("restaurant"),
                    "size": item.get("size"),
                    "quantity": quantity,
                    "calories": saved_nutrition.get("calories"),
                    "protein_g": saved_nutrition.get("protein_g"),
                    "verification_source": verification["source"],
                }
            ]

            review_data = {
                **dict(item),
                "_unresolved_food_id": unresolved_food_id,
                "_entry_date": item.get("entry_date"),
                "_pending_components": pending_components,
                "_skipped_unresolved_food_ids": skipped_ids,
            }

            start_conversation(
                chat_id=chat_id,
                conversation_type="food_interpretation",
                current_step="nutrition_confirmation",
                known_data=review_data,
                missing_fields=[],
                original_message=str(
                    item.get("original_text") or ""
                ),
            )

            prompt_message_id = send_telegram_msg(
                format_pending_nutrition_confirmation(
                    pending_components,
                    meal_category=item.get("meal_category"),
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

        if lowered in {"4", "skip", "skip for now"}:
            skipped_ids.append(int(unresolved_food_id))
            pending = get_pending_unresolved_foods()

            show_unresolved_food_review(
                chat_id=chat_id,
                pending=pending,
                skipped_ids=skipped_ids,
            )
            return

        if lowered in {"5", "cancel", "cancel this food"}:
            set_unresolved_food_status(
                int(unresolved_food_id),
                status="cancelled",
            )

            pending = get_pending_unresolved_foods()

            if pending:
                send_telegram_msg(
                    "Unknown food cancelled.",
                    chat_id=chat_id,
                )
                show_unresolved_food_review(
                    chat_id=chat_id,
                    pending=pending,
                    skipped_ids=skipped_ids,
                )
            else:
                cancel_conversation(chat_id)
                send_telegram_msg(
                    "Unknown food cancelled. The queue is empty.",
                    chat_id=chat_id,
                )
            return

        send_telegram_msg(
            "Please choose 1, 2, 3, 4, or 5.",
            chat_id=chat_id,
        )
        return

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
                quantity = resolve_non_restaurant_quantity(
                    food_id=saved_food["food_id"],
                    quantity=known_data.get("quantity"),
                    quantity_description=known_data.get(
                        "quantity_description"
                    ),
                    size=known_data.get("size"),
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
        target_entry_date = datetime.now(PACIFIC_TZ).date()
        stored_entry_date = known_data.get("_entry_date")

        if stored_entry_date:
            try:
                target_entry_date = datetime.strptime(
                    str(stored_entry_date),
                    "%Y-%m-%d",
                ).date()
            except ValueError:
                logging.warning(
                    "Invalid unresolved Food entry date: %s",
                    stored_entry_date,
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
                for component in pending_components:
                    duplicate = find_recent_duplicate_entry(
                        entry_date=target_entry_date,
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
                        entry_date=target_entry_date,
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
                    target_entry_date
                )
            except Exception:
                logging.exception(
                    "Food Ledger Google Sheet sync failed"
                )
                send_telegram_msg(
                    "Food was logged, but the Google Sheet "
                    "nutrition totals could not be updated.",
                    chat_id=chat_id,
                )

            unresolved_food_id = known_data.get(
                "_unresolved_food_id"
            )

            if unresolved_food_id is not None:
                set_unresolved_food_status(
                    int(unresolved_food_id),
                    status="resolved",
                )
                complete_conversation(chat_id)

                send_telegram_msg(
                    "Food logged for "
                    f"{target_entry_date.isoformat()} "
                    f"({meal_category.title()}).",
                    chat_id=chat_id,
                )

                pending = get_pending_unresolved_foods()
                skipped_ids = list(
                    known_data.get(
                        "_skipped_unresolved_food_ids"
                    )
                    or []
                )

                if pending:
                    show_unresolved_food_review(
                        chat_id=chat_id,
                        pending=pending,
                        skipped_ids=skipped_ids,
                    )
                else:
                    cancel_conversation(chat_id)
                    send_telegram_msg(
                        "All unknown foods have been reviewed.",
                        chat_id=chat_id,
                    )
                return

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

        if lowered in {
            "2",
            "enter custom nutrition",
            "custom nutrition",
            "custom",
        }:
            if len(pending_components) != 1 or not meal_category:
                send_telegram_msg(
                    "Custom nutrition can currently replace one food "
                    "at a time. Please edit this entry and send one "
                    "food description.",
                    chat_id=chat_id,
                )
                return

            update_conversation(
                chat_id=chat_id,
                current_step="manual_label_entry",
                known_data={
                    **known_data,
                    "_manual_label_field": "serving_size",
                    "_custom_nutrition_override": True,
                },
                missing_fields=[],
            )

            send_telegram_msg(
                "Enter custom nutrition for this food. The verified "
                "match will not be logged.\n\n"
                "What serving size applies?\n"
                "Examples: 1 serving, 28 g, or 1 oz.",
                chat_id=chat_id,
            )
            return

        if lowered in {"3", "edit"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Send the corrected food description as a new message.",
                chat_id=chat_id,
            )
            return

        if lowered in {"4", "cancel", "no"}:
            cancel_conversation(chat_id)
            send_telegram_msg(
                "Food entry cancelled. Nothing was logged.",
                chat_id=chat_id,
            )
            return

        send_telegram_msg(
            "Please reply:\n"
            "1. Log It\n"
            "2. Enter custom nutrition\n"
            "3. Edit\n"
            "4. Cancel",
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
                            resolve_non_restaurant_quantity(
                                food_id=food["food_id"],
                                quantity=known_data.get("quantity"),
                                quantity_description=known_data.get(
                                    "quantity_description"
                                ),
                                size=known_data.get("size"),
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
                        resolve_non_restaurant_quantity(
                            food_id=saved_food["food_id"],
                            quantity=known_data.get("quantity"),
                            quantity_description=known_data.get(
                                "quantity_description"
                            ),
                            size=known_data.get("size"),
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

    weight_value = extract_weight_value_from_text(text)
    if weight_value is not None:
        success, response = set_today_weight(weight_value)
        send_telegram_msg(response, chat_id=chat_id)
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
    # Preserve today's existing official weight.
    _, existing_row, _ = get_today_row_index_and_row(
        sheet,
        now.strftime("%m/%d/%Y"),
    )
    if (
        existing_row
        and len(existing_row) > 6
        and existing_row[6] not in ("", None)
    ):
        weight = None

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
