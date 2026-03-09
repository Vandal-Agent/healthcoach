import os
import json
from flask import Flask, request
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import pytz
import requests

app = Flask(__name__)

# --- Configuration ---
CHAT_ID = os.getenv("HEALTH_CHAT_ID")
TELEGRAM_TOKEN = os.getenv("HEALTH_TELEGRAM_TOKEN")
JSON_PATH = os.getenv("HEALTH_GOOGLE_JSON_PATH")

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

# Goals
STEP_GOAL = 12000
PROTEIN_GOAL = 130
WEIGHT_GOAL = 190

# Message timing windows
# Each window is inclusive and only sends once per day
MESSAGE_WINDOWS = {
    "morning": {"hour": 8, "minute_start": 20, "minute_end": 40},
    "midday": {"hour": 13, "minute_start": 20, "minute_end": 40},
    "evening": {"hour": 18, "minute_start": 20, "minute_end": 40},
}

STATE_FILE = "/home/vandal/bots/healthcoach/logs/message_state.json"


def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram rejected: {response.text}", flush=True)
            return False
        return True
    except Exception as e:
        print(f"Telegram error: {e}", flush=True)
        return False


def get_current_sheet():
    creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_PATH, SCOPE)
    client = gspread.authorize(creds)
    spreadsheet = client.open("Health Tracker")
    tz = pytz.timezone("US/Pacific")
    month_name = datetime.now(tz).strftime("%B %Y")

    try:
        return spreadsheet.worksheet(month_name)
    except gspread.WorksheetNotFound:
        new_sheet = spreadsheet.add_worksheet(title=month_name, rows="500", cols="10")
        new_sheet.append_row(HEADERS)
        return new_sheet


def safe_float(value, default=0.0):
    try:
        if value in ("", None):
            return default
        return float(value)
    except (ValueError, TypeError):
        return default


def collect_recent_numeric_values(all_rows, col_index, limit=7):
    values = []
    data_rows = all_rows[1:] if len(all_rows) > 1 else []

    for r in reversed(data_rows):
        if len(r) > col_index and r[col_index] not in ("", None):
            val = safe_float(r[col_index], None)
            if val is not None and val > 0:
                values.append(val)
                if len(values) >= limit:
                    break

    values.reverse()
    return values


def average_or_default(values, default):
    return sum(values) / len(values) if values else default


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
        }
    return state


def current_window_name(now):
    for name, cfg in MESSAGE_WINDOWS.items():
        if now.hour == cfg["hour"] and cfg["minute_start"] <= now.minute <= cfg["minute_end"]:
            return name
    return None


def should_send_for_window(state, window_name):
    if not window_name:
        return False
    return not state.get("sent", {}).get(window_name, False)


def mark_window_sent(state, window_name):
    if "sent" not in state:
        state["sent"] = {}
    state["sent"][window_name] = True
    return state


def build_morning_message(weight, sleep_hours, rhr, avg_sleep, avg_rhr, avg_weight):
    to_go = weight - WEIGHT_GOAL
    weight_vs_avg = weight - avg_weight if avg_weight else 0

    if weight_vs_avg <= -0.5:
        weight_trend_msg = "You're running below your recent average. Nice trend."
    elif weight_vs_avg >= 0.5:
        weight_trend_msg = "You're a bit above your recent average. Could be water or sodium."
    else:
        weight_trend_msg = "You're right around your recent average."

    sleep_note = ""
    if sleep_hours > 0 and avg_sleep > 0:
        if sleep_hours < avg_sleep - 1:
            sleep_note = "Sleep was below your recent average."
        elif sleep_hours > avg_sleep + 0.5:
            sleep_note = "Sleep was better than your recent average."

    rhr_note = ""
    if rhr > 0 and avg_rhr > 0:
        if rhr >= avg_rhr + 5:
            rhr_note = "RHR is elevated today."
        elif rhr <= avg_rhr - 3:
            rhr_note = "RHR looks better than your recent average."

    msg = (
        f"Morning check\n"
        f"Weight: {weight:.1f} lbs\n"
        f"Sleep: {sleep_hours:.2f} hrs\n"
        f"RHR: {rhr:.0f}\n"
        f"Goal gap: {to_go:.1f} lbs to {WEIGHT_GOAL}\n"
        f"3-day sleep avg: {avg_sleep:.2f} hrs\n"
        f"7-day RHR avg: {avg_rhr:.1f}\n"
        f"{weight_trend_msg}"
    )

    if sleep_note:
        msg += f"\n{sleep_note}"
    if rhr_note:
        msg += f"\n{rhr_note}"

    msg += f"\nFocus today: {STEP_GOAL} steps and {PROTEIN_GOAL}g protein."
    return msg


def build_midday_message(steps, active_cals, protein):
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

    return (
        f"1:30 check\n"
        f"Steps: {steps}\n"
        f"Active cals: {active_cals:.0f}\n"
        f"Protein: {protein:.0f}g\n"
        f"{pace_check}"
        + (f"\n{protein_note}" if protein_note else "")
    )


def build_evening_message(
    steps,
    active_cals,
    dietary_cals,
    protein,
    sleep_hours,
    rhr,
    hrv,
    deficit,
    avg_sleep,
    avg_rhr,
    avg_hrv,
):
    step_status = "✅" if steps >= STEP_GOAL else "❌"
    protein_status = "✅" if protein >= PROTEIN_GOAL else "❌"

    recovery_flags = []
    if sleep_hours > 0 and avg_sleep > 0 and sleep_hours < avg_sleep - 1:
        recovery_flags.append("sleep below recent average")
    if rhr > 0 and avg_rhr > 0 and rhr >= avg_rhr + 5:
        recovery_flags.append("RHR elevated")
    if hrv > 0 and avg_hrv > 0 and hrv <= avg_hrv - 10:
        recovery_flags.append("HRV suppressed")

    msg = (
        f"6:30 daily summary\n"
        f"Steps: {steps}/{STEP_GOAL} {step_status}\n"
        f"Protein: {protein:.0f}/{PROTEIN_GOAL}g {protein_status}\n"
        f"Active cals: {active_cals:.0f}\n"
        f"Food cals: {dietary_cals:.0f}\n"
        f"Sleep: {sleep_hours:.2f} hrs\n"
        f"RHR: {rhr:.0f}\n"
        f"HRV: {hrv:.2f}\n"
        f"Estimated deficit: {deficit:.0f} cals"
    )

    if recovery_flags:
        msg += "\nRecovery flags: " + ", ".join(recovery_flags)

    if deficit > 500:
        msg += "\nStrong fat-loss day."
    elif deficit < 0:
        msg += "\nYou likely ate above burn today."

    if steps < STEP_GOAL:
        msg += "\nTomorrow focus: close the step gap earlier in the day."
    elif protein < PROTEIN_GOAL:
        msg += "\nTomorrow focus: get protein up sooner."
    else:
        msg += "\nOverall: solid day."

    return msg


@app.route("/webhook", methods=["POST"])
def add_data():
    data = request.json or {}
    print("Incoming webhook data:", data, flush=True)

    sheet = get_current_sheet()

    # 1. Capture Metrics
    steps = int(safe_float(data.get("steps"), 0))
    total_cals = safe_float(data.get("total_calories"), 0)
    active_cals = safe_float(data.get("active_calories"), 0)
    sleep_hours = safe_float(data.get("sleep_hours"), 0)
    rhr = safe_float(data.get("rhr"), 0)
    weight = safe_float(data.get("weight"), 0)
    hrv = safe_float(data.get("hrv"), 0)
    dietary_cals = safe_float(data.get("dietary_calories"), 0)
    protein = safe_float(data.get("protein"), 0)

    # 2. Time Handling
    tz = pytz.timezone("US/Pacific")
    now = datetime.now(tz)
    today_str = now.strftime("%m/%d/%Y")
    timestamp = now.strftime("%m/%d/%Y %I:%M %p")

    # 3. Build Row
    row = [
        timestamp,
        steps,
        total_cals,
        active_cals,
        sleep_hours,
        rhr,
        weight,
        hrv,
        dietary_cals,
        protein,
    ]

    # 4. Read Existing Sheet
    all_rows = sheet.get_all_values()

    # 5. Update today's row or append new one
    if len(all_rows) > 1 and str(all_rows[-1][0]).startswith(today_str):
        sheet.update(range_name=f"A{len(all_rows)}:J{len(all_rows)}", values=[row])
        all_rows[-1] = [str(x) for x in row]
    else:
        sheet.append_row(row)
        all_rows.append([str(x) for x in row])

    # 6. Trend Analysis
    # 0 Timestamp, 1 Steps, 2 Total Cals, 3 Active Cals, 4 Sleep, 5 RHR,
    # 6 Weight, 7 HRV, 8 Dietary Cals, 9 Protein
    rhr_values = collect_recent_numeric_values(all_rows, 5, limit=7)
    weight_values = collect_recent_numeric_values(all_rows, 6, limit=7)
    sleep_values = collect_recent_numeric_values(all_rows, 4, limit=3)
    hrv_values = collect_recent_numeric_values(all_rows, 7, limit=7)

    avg_rhr = average_or_default(rhr_values, rhr)
    avg_weight = average_or_default(weight_values, weight)
    avg_sleep = average_or_default(sleep_values, sleep_hours)
    avg_hrv = average_or_default(hrv_values, hrv)

    # 7. One-message-per-window logic
    state = load_state()
    state = reset_state_if_new_day(state, today_str)

    window_name = current_window_name(now)
    sent_message = False
    sent_type = None
    msg = None

    if should_send_for_window(state, window_name):
        if window_name == "morning":
            msg = build_morning_message(
                weight=weight,
                sleep_hours=sleep_hours,
                rhr=rhr,
                avg_sleep=avg_sleep,
                avg_rhr=avg_rhr,
                avg_weight=avg_weight,
            )
        elif window_name == "midday":
            msg = build_midday_message(
                steps=steps,
                active_cals=active_cals,
                protein=protein,
            )
        elif window_name == "evening":
            deficit = (active_cals + 2200) - dietary_cals
            msg = build_evening_message(
                steps=steps,
                active_cals=active_cals,
                dietary_cals=dietary_cals,
                protein=protein,
                sleep_hours=sleep_hours,
                rhr=rhr,
                hrv=hrv,
                deficit=deficit,
                avg_sleep=avg_sleep,
                avg_rhr=avg_rhr,
                avg_hrv=avg_hrv,
            )

        if msg:
            success = send_telegram_msg(msg)
            if success:
                state = mark_window_sent(state, window_name)
                sent_message = True
                sent_type = window_name
                save_state(state)

    if not sent_message:
        save_state(state)

    return {
        "status": "ok",
        "message_sent": sent_message,
        "message_type": sent_type,
        "current_window": window_name,
    }, 200


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000)
