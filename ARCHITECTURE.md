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

The Health menu's on-demand Daily Health Insight reads up to thirty days of
Health Tracker rows. `health_insights.py` calculates personal-baseline,
completed-day, rolling-average, trend, and data-completeness evidence before
Gemini is called. The model receives only that evidence and returns structured
prose tied to evidence IDs; exact measurements remain deterministic. Invalid
or unavailable generated prose falls back to a deterministic explanation.

Message windows:

8:30  morning recap
1:30  midday check
6:30  evening reminder
Sunday 9:30 AM Pacific  Food Library Health Check reminder

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
rhr_measured_at (optional actual source time for rhr)
weight
hrv
hrv_measured_at (optional actual source time for hrv)
dietary_calories
protein
sleep_hours (sometimes)
exercise_minutes (optional Apple Exercise Time total for today)
cardio_fitness (optional Apple Health Cardio Fitness in mL/kg/min)
cardio_fitness_measured_at (optional actual source time)
walking_heart_rate_average (optional Apple Health Walking Heart Rate Average)
walking_heart_rate_measured_at (optional actual source time)
blood_pressure_systolic (optional paired Apple Health reading)
blood_pressure_diastolic (optional paired Apple Health reading)
blood_pressure_measured_at (required source time for a paired reading)
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
Cardio Fitness
Walking Heart Rate Average
Blood Pressure Systolic
Blood Pressure Diastolic
Blood Pressure Measured At
RHR Measured At
HRV Measured At
Cardio Fitness Measured At
Walking Heart Rate Measured At

The RHR column remains column F. Current status and Health History display
recorded resting heart rate without medical interpretation.

Cardio Fitness is appended as column L. Existing rows and sheets remain
valid; missing Cardio Fitness values are not converted to zero.

Walking Heart Rate Average is appended as column M and is stored in beats
per minute without medical interpretation.

Blood-pressure systolic, diastolic, and source measurement time are appended
as columns N through P. HealthCoach accepts and displays them only as one
complete same-day pair, and does not diagnose or rate the reading.

Apple source times for resting heart rate, HRV, Cardio Fitness, and walking
heart rate are appended as columns Q through T. When a source time is sent,
its Pacific calendar date must match the Tracker row or the value and time are
ignored together. This prevents an older latest-available Apple sample from
being counted as a new daily observation. Payloads and legacy rows without
these optional source times remain readable during the Shortcut transition.

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

Prevents duplicate messages. The offset advances after each individual
update even when that update raises an application error. A failed update is
logged, the user is notified, and later Telegram messages continue instead
of remaining blocked behind one repeatedly failing update.

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
