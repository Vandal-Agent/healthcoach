HealthCoach Bot – Architecture Summary

Server

DigitalOcean Ubuntu
User: vandal

Project location:

/home/vandal/bots/healthcoach

Service:

/etc/systemd/system/healthcoach.service

Useful commands:

sudo systemctl restart healthcoach
sudo systemctl status healthcoach
sudo journalctl -u healthcoach -f
Core Components
1. app.py

Main controller.

Responsibilities:

Flask webhook endpoint /webhook

receives iPhone Shortcut health data

updates Google Sheet

controls message timing

Telegram polling thread

sends coaching messages

Message windows:

8:30  morning recap
1:30  midday check
6:30  evening reminder

Morning flow:

Webhook arrives
↓
If sleep missing
    prompt user via Telegram
↓
Sleep reply received
↓
Morning recap generated
↓
Food coaching message sent
2. loseit_parser.py

Reads the Lose It CSV export.

File location:

/home/vandal/bots/healthcoach/data/latest_loseit.csv

Extracts:

calories
protein
carbs
fat
fiber
sugar
sodium

Also builds:

meal_totals
top_calorie_foods

Safety features:

CSV missing check

rounded totals

3. loseit_coaching.py

Food coaching engine.

Input:

total_burn
steps
weight_today
recent_weight_avg
sleep

Reads food data via:

parse_loseit_csv()

Generates:

calorie balance
protein evaluation
fiber check
sugar warning
sodium warning
breakfast analysis
steps context
weight context
sleep context
top calorie foods

Returns a formatted Telegram message.

Data Sources
1. iPhone Shortcut

Webhook sends:

steps
active_calories
total_calories
rhr (optional Apple Health resting heart rate in bpm)
weight
hrv
dietary_calories
protein
sleep_hours (sometimes)
exercise_minutes (optional Apple Exercise Time total for today)
2. Lose It email

Daily CSV attachment downloaded to:

/data/latest_loseit.csv

Parser extracts food totals.

3. Google Sheet

Spreadsheet:

Health Tracker

Monthly tabs:

March 2026
April 2026
etc

Columns:

Timestamp
Steps
Total Cals
Active Cals
Sleep
RHR
Weight
HRV
Dietary Cals
Protein
Exercise Minutes

The RHR column remains column F. Current status and Health History display
recorded resting heart rate without medical interpretation.

Telegram Messaging

Bot sends:

Morning
sleep prompt (if missing)
morning recap
food coaching
Midday
step pace
protein check
food logging check
Evening
step goal status
protein goal status
food log completeness
State Tracking

File:

logs/message_state.json

Tracks:

messages already sent
sleep prompt status
telegram polling offset

Prevents duplicate messages.

Environment Variables

Located in:

/home/vandal/.env

Important keys:

HEALTH_TELEGRAM_TOKEN
HEALTH_CHAT_ID
HEALTH_GOOGLE_JSON_PATH
HEALTH_GEMINI_API_KEY
HEALTH_AI_MODEL
GitHub Backup

Repository:

https://github.com/Vandal-Agent/healthcoach

Backup commands:

cd ~/bots/healthcoach
git add .
git commit -m "description"
git push
Bot Lifecycle

Daily operation:

iPhone shortcut sends data
↓
Webhook updates sheet
↓
Scheduler checks time window
↓
Telegram messages sent
↓
LoseIt CSV parsed
↓
Food coaching generated
Stability Status

Current system version: Stable v1

Verified components:

webhook ingestion

sheet writing

Telegram messaging

LoseIt parsing

coaching engine

state tracking

Bot can now run unattended.
