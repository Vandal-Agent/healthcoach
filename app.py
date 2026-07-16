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
        return True
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
        evaluation_due = now.replace(
            hour=19,
            minute=0,
            second=0,
            microsecond=0,
        )

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
                "created" if case_result["created"] else "already exists",
                case_result["case"]["case_id"],
            )
        except Exception:
            logging.exception(
                "Could not create missing-data Memory Case"
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


def process_telegram_update(update):
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return

    chat_id = message.get("chat", {}).get("id")
    text = (message.get("text") or "").strip()

    if CHAT_ID and str(chat_id) != str(CHAT_ID):
        return

    if not text:
        return

    if text == "/status":
        metrics = get_today_metrics()
        send_telegram_msg(build_progress_message("Current status", metrics), chat_id=chat_id)
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

    send_telegram_msg(build_help_message(), chat_id=chat_id)


def telegram_poll_loop():
    logging.info("Telegram polling started")

    while True:
        try:
            state = load_state()
            offset = state.get("telegram_update_offset")

            params = {"timeout": 20}
            if offset is not None:
                params["offset"] = int(offset) + 1

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
                state = load_state()
                state["telegram_update_offset"] = update.get("update_id")
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
