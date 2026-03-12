import os
import json
import time
import threading
from datetime import datetime, timedelta

import gspread
import pytz
import requests
from flask import Flask, request
from oauth2client.service_account import ServiceAccountCredentials
from loseit_coaching import build_food_coaching

app = Flask(__name__)

# --- Configuration ---
CHAT_ID = os.getenv("HEALTH_CHAT_ID")
TELEGRAM_TOKEN = os.getenv("HEALTH_TELEGRAM_TOKEN")
JSON_PATH = os.getenv("HEALTH_GOOGLE_JSON_PATH")

GEMINI_API_KEY = os.getenv("HEALTH_GEMINI_API_KEY")
AI_MODEL = os.getenv("HEALTH_AI_MODEL", "gemini-1.5-flash")

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

STEP_GOAL = 12000
PROTEIN_GOAL = 130
WEIGHT_GOAL = 190
MIN_COMPLETE_CALORIES = 1200

# Send on the first webhook AFTER these times
MESSAGE_WINDOWS = {
    "morning": {"hour": 8, "minute": 30},
    "midday": {"hour": 13, "minute": 30},
    "evening": {"hour": 18, "minute": 30},
}

STATE_FILE = "/home/vandal/bots/healthcoach/logs/message_state.json"

PACIFIC_TZ = pytz.timezone("US/Pacific")


def send_telegram_msg(message, chat_id=None):
    target_chat_id = str(chat_id or CHAT_ID) if (chat_id or CHAT_ID) else None
    if not TELEGRAM_TOKEN or not target_chat_id:
        print("Missing Telegram token or chat id.", flush=True)
        return False

    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": target_chat_id, "text": message}

    try:
        response = requests.post(url, json=payload, timeout=15)
        if response.status_code != 200:
            print(f"Telegram rejected: {response.text}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)
        return False


def send_food_coaching_message(
    total_burn=None,
    steps=None,
    weight_today=None,
    recent_weight_avg=None,
    sleep=None,
    chat_id=None,
):
    try:
        msg = build_food_coaching(
            total_burn=total_burn,
            steps=steps,
            weight_today=weight_today,
            recent_weight_avg=recent_weight_avg,
            sleep=sleep,
        )
        return send_telegram_msg(msg, chat_id=chat_id)
    except Exception as e:
        print(f"Food coaching error: {e}", flush=True)
        return False


def get_gspread_client():
    creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_PATH, SCOPE)
    return gspread.authorize(creds)


def get_sheet_for_date(target_date):
    client = get_gspread_client()
    spreadsheet = client.open("Health Tracker")
    month_name = target_date.strftime("%B %Y")

    try:
        return spreadsheet.worksheet(month_name)
    except gspread.WorksheetNotFound:
        new_sheet = spreadsheet.add_worksheet(title=month_name, rows="500", cols="10")
        new_sheet.append_row(HEADERS)
        return new_sheet


def get_current_sheet():
    now = datetime.now(PACIFIC_TZ)
    return get_sheet_for_date(now.date())


def safe_float(value, default=0.0):
    try:
        if value in ("", None):
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def safe_int(value, default=0):
    try:
        if value in ("", None):
            return default
        return int(float(value))
    except (ValueError, TypeError):
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
                parts = value.split(":")
                if len(parts) != 2:
                    return None
                hours = float(parts[0])
                minutes = float(parts[1])
                if minutes < 0 or minutes >= 60:
                    return None
                return hours + minutes / 60
            except (ValueError, TypeError):
                return None

    try:
        return float(value)
    except (ValueError, TypeError):
        return None


def normalize_sleep_for_sheet(value):
    if value in ("", None):
        return ""

    if isinstance(value, str):
        value = value.strip()
        if ":" in value:
            return value

    parsed = parse_sleep(value)
    return parsed if parsed is not None else ""


def load_state():
    if not os.path.exists(STATE_FILE):
        return {}

    try:
        with open(STATE_FILE, "r") as f:
            return json.load(f)
    except Exception as e:
        print(f"State load error: {e}", flush=True)
        return {}


def save_state(state):
    try:
        os.makedirs(os.path.dirname(STATE_FILE), exist_ok=True)
        with open(STATE_FILE, "w") as f:
            json.dump(state, f)
    except Exception as e:
        print(f"State save error: {e}", flush=True)


def reset_state_if_new_day(state, today_str):
    if state.get("date") != today_str:
        return {
            "date": today_str,
            "sent": {
                "morning": False,
                "midday": False,
                "evening": False,
            },
            "sleep_prompted_date": None,
            "awaiting_sleep_date": None,
            "telegram_update_offset": state.get("telegram_update_offset"),
        }
    return state


def get_due_window(now, state):
    sent = state.get("sent", {})

    current_minutes = now.hour * 60 + now.minute
    morning_minutes = MESSAGE_WINDOWS["morning"]["hour"] * 60 + MESSAGE_WINDOWS["morning"]["minute"]
    midday_minutes = MESSAGE_WINDOWS["midday"]["hour"] * 60 + MESSAGE_WINDOWS["midday"]["minute"]
    evening_minutes = MESSAGE_WINDOWS["evening"]["hour"] * 60 + MESSAGE_WINDOWS["evening"]["minute"]

    if current_minutes >= evening_minutes and not sent.get("evening", False):
        return "evening"
    if current_minutes >= midday_minutes and not sent.get("midday", False):
        return "midday"
    if current_minutes >= morning_minutes and not sent.get("morning", False):
        return "morning"

    return None


def mark_window_sent(state, window_name):
    if "sent" not in state:
        state["sent"] = {}

    if window_name == "morning":
        state["sent"]["morning"] = True
    elif window_name == "midday":
        state["sent"]["morning"] = True
        state["sent"]["midday"] = True
    elif window_name == "evening":
        state["sent"]["morning"] = True
        state["sent"]["midday"] = True
        state["sent"]["evening"] = True

    return state


def parse_row_date(row):
    if not row or not row[0]:
        return None
    try:
        return datetime.strptime(row[0], "%m/%d/%Y %I:%M %p").date()
    except Exception:
        return None


def row_to_metrics(row):
    if not row or len(row) < 10:
        return None

    weight_val = safe_float(row[6], None)

    return {
        "timestamp": row[0],
        "steps": safe_int(row[1], 0),
        "total_cals": safe_float(row[2], 0),
        "active_cals": safe_float(row[3], 0),
        "sleep_hours": parse_sleep(row[4]) or 0,
        "rhr": safe_float(row[5], 0),
        "weight": weight_val,
        "hrv": safe_float(row[7], 0),
        "dietary_cals": safe_float(row[8], 0),
        "protein": safe_float(row[9], 0),
    }


def get_row_for_date(target_date):
    sheet = get_sheet_for_date(target_date)
    rows = sheet.get_all_values()
    target_str = target_date.strftime("%m/%d/%Y")

    for row in reversed(rows[1:]):
        if row and row[0].startswith(target_str):
            return row
    return None


def get_today_row_index_and_row(sheet, today_str):
    all_rows = sheet.get_all_values()
    for i, existing_row in enumerate(all_rows[1:], start=2):
        if existing_row and existing_row[0]:
            row_date = str(existing_row[0]).strip()
            if row_date.startswith(today_str):
                while len(existing_row) < 10:
                    existing_row.append("")
                return i, existing_row, all_rows
    return None, None, all_rows


def get_recent_rows(reference_date, days_back=10, exclude_dates=None):
    exclude_dates = exclude_dates or set()
    rows = []

    date_list = []
    for i in range(days_back + 1):
        d = reference_date - timedelta(days=i)
        if d not in date_list:
            date_list.append(d)

    month_keys = sorted({d.strftime("%Y-%m") for d in date_list})
    dates_by_month = {}
    for d in date_list:
        dates_by_month.setdefault(d.strftime("%Y-%m"), set()).add(d)

    for month_key in month_keys:
        sample_date = next(iter(dates_by_month[month_key]))
        try:
            sheet = get_sheet_for_date(sample_date)
            sheet_rows = sheet.get_all_values()[1:]
            rows.extend(sheet_rows)
        except Exception as e:
            print(f"Recent rows read error for {month_key}: {e}", flush=True)

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
    values = []
    parser = parser or (lambda x: safe_float(x, None))

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


def average_or_default(values, default):
    return sum(values) / len(values) if values else default


def get_recent_average_weight(reference_date, days_back=10, limit=7):
    recent_rows = get_recent_rows(
        reference_date=reference_date,
        days_back=days_back,
        exclude_dates={reference_date},
    )
    weight_values = collect_recent_numeric_values(
        recent_rows, 6, limit=limit, minimum_valid=50
    )
    return average_or_default(weight_values, None)


def build_midday_message(steps, active_cals, protein, dietary_cals):
    if steps >= 7000:
        pace_check = "Great pace. You're tracking toward a strong day."
    elif steps >= 5000:
        pace_check = "Solid pace. Keep moving and you'll finish well."
    else:
        pace_check = "You're behind pace for 12k. A short walk now would help."

    protein_note = ""
    if protein < 40:
        protein_note = "Protein is still low for this point in the day."
    elif protein >= 70:
        protein_note = "Protein pace looks solid."

    food_note = ""
    if dietary_cals < 400:
        food_note = "Food logging looks very light so far."
    elif dietary_cals < 700:
        food_note = "Make sure everything is getting logged."

    msg = (
        f"1:30 check\n"
        f"Steps: {steps}\n"
        f"Active cals: {active_cals:.0f}\n"
        f"Protein: {protein:.0f}g\n"
        f"Food cals logged: {dietary_cals:.0f}\n"
        f"{pace_check}"
    )

    if protein_note:
        msg += f"\n{protein_note}"
    if food_note:
        msg += f"\n{food_note}"

    return msg


def build_evening_message(steps, active_cals, dietary_cals, protein):
    step_status = "✅" if steps >= STEP_GOAL else "❌"
    protein_status = "✅" if protein >= PROTEIN_GOAL else "❌"

    msg = (
        f"6:30 reminder\n"
        f"Steps: {steps}/{STEP_GOAL} {step_status}\n"
        f"Protein: {protein:.0f}/{PROTEIN_GOAL}g {protein_status}\n"
        f"Active cals: {active_cals:.0f}\n"
        f"Food cals logged: {dietary_cals:.0f}\n"
    )

    if dietary_cals < MIN_COMPLETE_CALORIES:
        msg += (
            f"Your food log is still under {MIN_COMPLETE_CALORIES}. "
            f"If you ate more than that, get it logged so tomorrow's recap is reliable."
        )
    else:
        msg += "Your food log is in a believable range for tomorrow's recap."

    return msg


def build_rule_based_morning_recap(yesterday, today_sleep, today_rhr, today_hrv, latest_weight, avg_sleep, avg_rhr):
    cals_complete = yesterday["dietary_cals"] >= MIN_COMPLETE_CALORIES

    msg = "Morning recap\n"
    msg += f"Yesterday steps: {yesterday['steps']}\n"
    msg += f"Yesterday food cals: {yesterday['dietary_cals']:.0f}\n"
    msg += f"Yesterday protein: {yesterday['protein']:.0f}g\n"
    msg += f"Last night sleep: {today_sleep:.2f} hrs\n"
    msg += f"RHR today: {today_rhr:.0f}\n"

    if today_hrv > 0:
        msg += f"HRV today: {today_hrv:.2f}\n"

    if latest_weight is not None:
        msg += f"Weight today: {latest_weight:.1f} lbs\n"

    if not cals_complete:
        msg += (
            f"Yesterday's food log looks incomplete because it was under {MIN_COMPLETE_CALORIES} calories. "
            f"Treat the calorie balance as unreliable.\n"
        )
    else:
        estimated_deficit = (yesterday["active_cals"] + 2200) - yesterday["dietary_cals"]
        if estimated_deficit > 500:
            msg += "Yesterday likely ended in a meaningful calorie deficit.\n"
        elif estimated_deficit < 0:
            msg += "Yesterday likely ended above burn.\n"
        else:
            msg += "Yesterday looked close to maintenance.\n"

    if today_sleep > 0 and avg_sleep > 0 and today_sleep < avg_sleep - 1:
        msg += "Sleep was below your recent average.\n"
    if today_rhr > 0 and avg_rhr > 0 and today_rhr >= avg_rhr + 5:
        msg += "RHR is elevated today.\n"

    msg += f"Focus today: {STEP_GOAL} steps and {PROTEIN_GOAL}g protein."
    return msg.strip()


def call_gemini(prompt):
    if not GEMINI_API_KEY:
        return None

    url = f"https://generativelanguage.googleapis.com/v1beta/models/{AI_MODEL}:generateContent?key={GEMINI_API_KEY}"
    payload = {
        "contents": [
            {
                "parts": [
                    {"text": prompt}
                ]
            }
        ],
        "generationConfig": {
            "temperature": 0.5,
            "maxOutputTokens": 180,
        },
    }

    try:
        response = requests.post(url, json=payload, timeout=30)
        if response.status_code != 200:
            print(f"Gemini rejected: {response.status_code} {response.text}", flush=True)
            return None

        data = response.json()
        candidates = data.get("candidates", [])
        if not candidates:
            return None

        parts = candidates[0].get("content", {}).get("parts", [])
        text = "".join(part.get("text", "") for part in parts).strip()
        return text or None
    except Exception as e:
        print(f"Gemini call error: {e}", flush=True)
        return None


def build_ai_morning_recap(yesterday, today_sleep, today_rhr, today_hrv, latest_weight, avg_sleep, avg_rhr):
    calorie_confidence = (
        "complete enough for calorie analysis"
        if yesterday["dietary_cals"] >= MIN_COMPLETE_CALORIES
        else f"likely incomplete because logged calories are under {MIN_COMPLETE_CALORIES}"
    )

    prompt = f"""
You are a concise health coach. Write 3 to 4 short sentences only.

Use these facts:
Yesterday steps: {yesterday['steps']}
Yesterday active calories: {yesterday['active_cals']:.0f}
Yesterday dietary calories: {yesterday['dietary_cals']:.0f}
Yesterday protein: {yesterday['protein']:.0f}
Last night sleep: {today_sleep:.2f}
Today's RHR: {today_rhr:.0f}
Today's HRV: {today_hrv:.2f}
Today's weight: {"not recorded" if latest_weight is None else f"{latest_weight:.1f}"}
Recent sleep average: {avg_sleep:.2f}
Recent RHR average: {avg_rhr:.1f}
Calorie log confidence: {calorie_confidence}

Rules:
- If calorie log confidence says likely incomplete, say so clearly and do not make a confident calorie-balance claim.
- Do not mention missing data unless it matters.
- End with one practical focus for today.
- Do not use bullets.
- Keep it direct and useful.
""".strip()

    return call_gemini(prompt)


def build_morning_message_from_yesterday(yesterday, today_sleep, today_rhr, today_hrv, latest_weight, avg_sleep, avg_rhr):
    ai_msg = build_ai_morning_recap(
        yesterday=yesterday,
        today_sleep=today_sleep,
        today_rhr=today_rhr,
        today_hrv=today_hrv,
        latest_weight=latest_weight,
        avg_sleep=avg_sleep,
        avg_rhr=avg_rhr,
    )

    if ai_msg:
        return ai_msg

    return build_rule_based_morning_recap(
        yesterday=yesterday,
        today_sleep=today_sleep,
        today_rhr=today_rhr,
        today_hrv=today_hrv,
        latest_weight=latest_weight,
        avg_sleep=avg_sleep,
        avg_rhr=avg_rhr,
    )


def get_today_metrics_and_context():
    now = datetime.now(PACIFIC_TZ)
    today_date = now.date()

    today_row = get_row_for_date(today_date)
    today_metrics = row_to_metrics(today_row) if today_row else None

    recent_rows = get_recent_rows(
        reference_date=today_date,
        days_back=10,
        exclude_dates={today_date},
    )

    sleep_values = collect_recent_numeric_values(
        recent_rows, 4, limit=3, minimum_valid=0, parser=parse_sleep
    )
    rhr_values = collect_recent_numeric_values(recent_rows, 5, limit=7, minimum_valid=0)
    weight_values = collect_recent_numeric_values(recent_rows, 6, limit=7, minimum_valid=50)

    today_sleep = today_metrics["sleep_hours"] if today_metrics else 0
    today_rhr = today_metrics["rhr"] if today_metrics else 0
    today_hrv = today_metrics["hrv"] if today_metrics else 0
    latest_weight = None
    if today_metrics and today_metrics["weight"] is not None:
        latest_weight = today_metrics["weight"]
    elif weight_values:
        latest_weight = weight_values[-1]

    avg_sleep = average_or_default(sleep_values, today_sleep)
    avg_rhr = average_or_default(rhr_values, today_rhr)

    yesterday_date = today_date - timedelta(days=1)
    yesterday_row = get_row_for_date(yesterday_date)
    yesterday_metrics = row_to_metrics(yesterday_row) if yesterday_row else None

    return {
        "today_date": today_date,
        "today_metrics": today_metrics,
        "yesterday_metrics": yesterday_metrics,
        "today_sleep": today_sleep,
        "today_rhr": today_rhr,
        "today_hrv": today_hrv,
        "latest_weight": latest_weight,
        "avg_sleep": avg_sleep,
        "avg_rhr": avg_rhr,
    }


def build_today_morning_message():
    ctx = get_today_metrics_and_context()
    yesterday_metrics = ctx["yesterday_metrics"]

    if yesterday_metrics:
        return build_morning_message_from_yesterday(
            yesterday=yesterday_metrics,
            today_sleep=ctx["today_sleep"],
            today_rhr=ctx["today_rhr"],
            today_hrv=ctx["today_hrv"],
            latest_weight=ctx["latest_weight"],
            avg_sleep=ctx["avg_sleep"],
            avg_rhr=ctx["avg_rhr"],
        )

    return (
        "Morning recap\n"
        "I don't have a full row for yesterday yet, so today is just a fresh start.\n"
        f"Focus today: {STEP_GOAL} steps and {PROTEIN_GOAL}g protein."
    )


def set_today_sleep_value(sleep_input):
    now = datetime.now(PACIFIC_TZ)
    today_str = now.strftime("%m/%d/%Y")
    timestamp = now.strftime("%m/%d/%Y %I:%M %p")
    sheet = get_current_sheet()

    today_row_index, existing_row, _ = get_today_row_index_and_row(sheet, today_str)
    sleep_value_for_sheet = normalize_sleep_for_sheet(sleep_input)

    if today_row_index:
        sheet.update(range_name=f"E{today_row_index}", values=[[sleep_value_for_sheet]])
    else:
        row = [
            timestamp,
            "",
            "",
            "",
            sleep_value_for_sheet,
            "",
            "",
            "",
            "",
            "",
        ]
        sheet.append_row(row)


def handle_sleep_reply(text, chat_id):
    now = datetime.now(PACIFIC_TZ)
    today_str = now.strftime("%m/%d/%Y")

    state = load_state()
    state = reset_state_if_new_day(state, today_str)

    if state.get("awaiting_sleep_date") != today_str or state.get("sent", {}).get("morning", False):
        return

    parsed_sleep = parse_sleep(text)
    if parsed_sleep is None:
        send_telegram_msg(
            "I couldn't read that sleep value. Reply like 6:28 or 6.5",
            chat_id=chat_id,
        )
        return

    set_today_sleep_value(text)
    send_telegram_msg(f"Got it. Logged sleep as {text.strip()}.", chat_id=chat_id)

    msg = build_today_morning_message()
    if msg:
        send_telegram_msg(msg, chat_id=chat_id)

        ctx = get_today_metrics_and_context()
        recent_weight_avg = get_recent_average_weight(ctx["today_date"])

        send_food_coaching_message(
            total_burn=ctx["today_metrics"]["total_cals"] if ctx["today_metrics"] else None,
            steps=ctx["today_metrics"]["steps"] if ctx["today_metrics"] else None,
            weight_today=ctx["today_metrics"]["weight"] if ctx["today_metrics"] else None,
            recent_weight_avg=recent_weight_avg,
            sleep=ctx["today_metrics"]["sleep_hours"] if ctx["today_metrics"] else None,
            chat_id=chat_id,
        )

        state = mark_window_sent(state, "morning")
        state["awaiting_sleep_date"] = None
        save_state(state)


def process_telegram_update(update):
    message = update.get("message") or update.get("edited_message") or {}
    if not message:
        return

    chat = message.get("chat", {})
    chat_id = str(chat.get("id", ""))
    text = (message.get("text") or "").strip()

    if not text:
        return

    if CHAT_ID and chat_id != str(CHAT_ID):
        return

    handle_sleep_reply(text, chat_id)


def telegram_poll_loop():
    if not TELEGRAM_TOKEN:
        print("Telegram polling not started because TELEGRAM_TOKEN is missing.", flush=True)
        return

    print("Telegram polling thread started.", flush=True)

    while True:
        try:
            state = load_state()
            offset = state.get("telegram_update_offset")
            params = {"timeout": 20}
            if offset is not None:
                params["offset"] = int(offset) + 1

            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            data = response.json()

            if not data.get("ok"):
                print(f"Telegram getUpdates failed: {data}", flush=True)
                time.sleep(5)
                continue

            updates = data.get("result", [])
            if not updates:
                continue

            for update in updates:
                process_telegram_update(update)
                state = load_state()
                state["telegram_update_offset"] = update.get("update_id")
                save_state(state)

        except Exception as e:
            print(f"Telegram polling error: {e}", flush=True)
            time.sleep(5)


@app.route("/webhook", methods=["POST"])
def add_data():
    data = request.json or {}
    print("Incoming webhook data:", data, flush=True)

    now = datetime.now(PACIFIC_TZ)
    today_date = now.date()
    today_str = now.strftime("%m/%d/%Y")
    timestamp = now.strftime("%m/%d/%Y %I:%M %p")

    sheet = get_current_sheet()

    # 1. Capture today's incoming metrics
    steps = safe_int(data.get("steps"), 0)
    total_cals = safe_float(data.get("total_calories"), 0)
    active_cals = safe_float(data.get("active_calories"), 0)

    sleep_hours_raw = data.get("sleep_hours")
    sleep_hours = parse_sleep(sleep_hours_raw)

    # sanity check for sleep values
    if sleep_hours is not None and (sleep_hours < 0 or sleep_hours > 14):
        sleep_hours = None

    rhr = safe_float(data.get("rhr"), 0)
    weight = safe_float(data.get("weight"), None)
    hrv = safe_float(data.get("hrv"), 0)
    dietary_cals = safe_float(data.get("dietary_calories"), 0)
    protein = safe_float(data.get("protein"), 0)

    # 2. Find today's existing row, if any
    today_row_index, existing_row, _ = get_today_row_index_and_row(sheet, today_str)

    # 3. Update today's row or append new one
    if today_row_index:
        row = [
            timestamp,
            steps,
            total_cals,
            active_cals,
            existing_row[4] if sleep_hours is None else normalize_sleep_for_sheet(sleep_hours_raw),
            rhr,
            existing_row[6] if weight is None else weight,
            hrv,
            dietary_cals,
            protein,
        ]
        sheet.update(range_name=f"A{today_row_index}:J{today_row_index}", values=[row])
    else:
        row = [
            timestamp,
            steps,
            total_cals,
            active_cals,
            "" if sleep_hours is None else normalize_sleep_for_sheet(sleep_hours_raw),
            rhr,
            weight if weight is not None else "",
            hrv,
            dietary_cals,
            protein,
        ]
        sheet.append_row(row)

    # 4. Message state
    state = load_state()
    state = reset_state_if_new_day(state, today_str)
    window_name = get_due_window(now, state)

    sent_message = False
    sent_type = None
    msg = None

    # Refresh today's row after update
    current_today_row = get_row_for_date(today_date)
    current_today_metrics = row_to_metrics(current_today_row) if current_today_row else None
    current_sleep = current_today_metrics["sleep_hours"] if current_today_metrics else 0

    # 5. Scheduled messaging
    if window_name:
        if window_name == "morning":
            if current_sleep <= 0:
                if state.get("sleep_prompted_date") != today_str:
                    msg = "Good morning. How much did you sleep last night? Reply like 6:28"
                    success = send_telegram_msg(msg)
                    if success:
                        state["sleep_prompted_date"] = today_str
                        state["awaiting_sleep_date"] = today_str
                        save_state(state)
                        sent_message = True
                        sent_type = "sleep_prompt"
                else:
                    save_state(state)
            else:
                msg = build_today_morning_message()
                if msg:
                    success = send_telegram_msg(msg)
                    if success:
                        state = mark_window_sent(state, window_name)
                        state["awaiting_sleep_date"] = None
                        save_state(state)
                        sent_message = True
                        sent_type = window_name

                        recent_weight_avg = get_recent_average_weight(today_date)

                        send_food_coaching_message(
                            total_burn=current_today_metrics["total_cals"] if current_today_metrics else None,
                            steps=current_today_metrics["steps"] if current_today_metrics else None,
                            weight_today=current_today_metrics["weight"] if current_today_metrics else None,
                            recent_weight_avg=recent_weight_avg,
                            sleep=current_today_metrics["sleep_hours"] if current_today_metrics else None,
                        )

        elif window_name == "midday":
            msg = build_midday_message(
                steps=steps,
                active_cals=active_cals,
                protein=protein,
                dietary_cals=dietary_cals,
            )
            if msg:
                success = send_telegram_msg(msg)
                if success:
                    state = mark_window_sent(state, window_name)
                    save_state(state)
                    sent_message = True
                    sent_type = window_name

        elif window_name == "evening":
            msg = build_evening_message(
                steps=steps,
                active_cals=active_cals,
                dietary_cals=dietary_cals,
                protein=protein,
            )
            if msg:
                success = send_telegram_msg(msg)
                if success:
                    state = mark_window_sent(state, window_name)
                    save_state(state)
                    sent_message = True
                    sent_type = window_name

    if not sent_message:
        save_state(state)

    return {
        "status": "ok",
        "message_sent": sent_message,
        "message_type": sent_type,
        "current_window": window_name,
    }, 200


if __name__ == "__main__":
    polling_thread = threading.Thread(target=telegram_poll_loop, daemon=True)
    polling_thread.start()
    app.run(host="0.0.0.0", port=5000)
