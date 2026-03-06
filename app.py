from flask import Flask, request
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from datetime import datetime
import pytz
import requests

app = Flask(__name__)

# --- Configuration ---
# Your numeric ID from @userinfobot
CHAT_ID = "7917222975"
# Your Bot Token from @BotFather
TELEGRAM_TOKEN = "8293480756:AAHECbyXe48CS6Zq7UnI57zsMRC3DWwSMs8"
# Path to your Google Credentials
JSON_PATH = '/home/vandal/bots/healthcoach/plucky-mode-488303-g6-97c5fde1077d---019646ae-c369-46ee-af41-6886dd7dd560.json'

SCOPE = ["https://spreadsheets.google.com/feeds", "https://www.googleapis.com/auth/drive"]
HEADERS = ["Timestamp", "Steps", "Total Cals", "Active Cals", "Sleep", "RHR", "Weight", "Workouts", "Dietary Cals", "Protein"]

# Goals
STEP_GOAL = 12000
PROTEIN_GOAL = 130
WEIGHT_GOAL = 190

def send_telegram_msg(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {"chat_id": CHAT_ID, "text": message}
    try:
        response = requests.post(url, json=payload, timeout=10)
        if response.status_code != 200:
            print(f"Telegram rejected: {response.text}")
    except Exception as e:
        print(f"Telegram error: {e}")

def get_current_sheet():
    creds = ServiceAccountCredentials.from_json_keyfile_name(JSON_PATH, SCOPE)
    client = gspread.authorize(creds)
    spreadsheet = client.open("Health Tracker")
    tz = pytz.timezone('US/Pacific')
    month_name = datetime.now(tz).strftime("%B %Y")
    try:
        return spreadsheet.worksheet(month_name)
    except gspread.WorksheetNotFound:
        new_sheet = spreadsheet.add_worksheet(title=month_name, rows="500", cols="10")
        new_sheet.append_row(HEADERS)
        return new_sheet

@app.route('/webhook', methods=['POST'])
def add_data():
    data = request.json
    sheet = get_current_sheet()

    # 1. Capture Metrics
    steps        = int(data.get('steps') or 0)
    weight       = float(data.get('weight') or 0)
    protein      = float(data.get('protein') or 0)
    rhr          = float(data.get('rhr') or 0)
    dietary_cals = float(data.get('dietary_calories') or 0)
    active_cals  = float(data.get('active_calories') or 0)
    total_cals   = float(data.get('total_calories') or 0)

    # 2. Time Handling
    tz = pytz.timezone('US/Pacific')
    now = datetime.now(tz)
    current_hour = now.hour
    today_str = now.strftime("%m/%d/%Y")

    # 3. Update Sheet
    row = [now.strftime("%m/%d/%Y %I:%M %p"), steps, total_cals, active_cals,
           data.get('sleep_hours'), rhr, weight, data.get('workouts'),
           dietary_cals, protein]

    all_rows = sheet.get_all_values()

    # Check if we update today or add new
    if len(all_rows) > 1 and str(all_rows[-1][0]).startswith(today_str):
        sheet.update(range_name=f'A{len(all_rows)}', values=[row])
        all_rows[-1] = row # Update local copy
    else:
        sheet.append_row(row)
        all_rows.append(row)

    # 4. Trend Analysis (Last 7 Days for RHR)
    # Safely skip the header row (index 0) and check if the value is actually a number
    rhr_values = []
    # Grab the last 8 rows, but ignore the very first row (header) if the sheet is new
    for r in all_rows[-8:-1]:
        if r != all_rows[0] and len(r) > 5 and r[5]:
            try:
                val = float(r[5])
                if val > 0:
                    rhr_values.append(val)
            except ValueError:
                pass # Skip if it's text like "RHR"

    avg_rhr = sum(rhr_values) / len(rhr_values) if rhr_values else rhr

    # 5. Coaching Logic
    if current_hour < 11:
        to_go = weight - WEIGHT_GOAL
        msg = f"Morning, Tracy! Weight: {weight} lbs. You're {to_go:.1f} lbs from your 190 goal. Let's chase those {STEP_GOAL} steps!"

    elif 11 <= current_hour < 17:
        pace_check = "Great rhythm!" if steps >= 5000 else "A bit behind pace for 12k—time for a quick move?"
        msg = f"1:00 PM Check: {steps} steps. {pace_check}"

    else:
        # Evening Final Audit
        # Calculate Deficit: (Active + Estimated BMR) - Food
        # Using 2200 as a rough resting burn for your stats
        deficit = (active_cals + 2200) - dietary_cals

        step_status = "✅" if steps >= STEP_GOAL else "❌"
        protein_status = "✅" if protein >= PROTEIN_GOAL else "❌"

        rhr_warning = ""
        if rhr > avg_rhr + 5:
            rhr_warning = "\n⚠️ RHR is elevated today. Prioritize sleep tonight!"

        msg = f"6:30 PM Final Audit:\n"
        msg += f"Steps: {steps}/{STEP_GOAL} {step_status}\n"
        msg += f"Protein: {protein}/{PROTEIN_GOAL}g {protein_status}\n"
        msg += f"Daily Deficit: {deficit:.0f} cals.{rhr_warning}"

        if deficit > 500:
            msg += "\n🔥 Excellent fat-burning day!"

    send_telegram_msg(msg)
    return msg

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
