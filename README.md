# HealthCoach

Personal health coaching bot running on a DigitalOcean Ubuntu server.

## Current Capabilities

- Receives health data from an iPhone Shortcut
- Updates the Google Sheet named `Health Tracker`
- Tracks steps, Apple Exercise Minutes, resting heart rate, protein,
  sleep, weight, HRV, and calorie data
- Shows 7-, 14-, and 30-day weight, sleep, exercise, and resting
  heart-rate history in Telegram, including daily entries, missing
  days, averages, and weight change
- Saves Pantry meal ideas as reusable recipes with ingredients,
  preparation, nutrition, Heart-Healthy Pick guidance, and confirmed
  serving-based logging
- Edits or deletes Saved Recipes without rewriting previously logged meals
- Renames, updates, or removes Saved Foods while preserving food-log history
- Reviews yesterday's Food Ledger and safely copies one meal or the
  entire day after confirmation
- Manages a safety-capped weight goal with manual calorie-target updates,
  burn-based projections, and remaining-calorie feedback after food logging
- Uses the Food Ledger rather than Lose It for current nutrition totals
- Sends daily and weekly coaching through Telegram
- Runs scheduled health checks and reminders

## Project Location

/home/vandal/bots/healthcoach

## Service

/etc/systemd/system/healthcoach.service

Useful commands:

- sudo systemctl status healthcoach
- sudo systemctl restart healthcoach
- sudo journalctl -u healthcoach -f

## Configuration

Live environment variables are stored centrally in:

/home/vandal/.env

Do not commit the real .env file or any secrets to GitHub.

## Documentation

- ARCHITECTURE.md
- PROJECT_WORKFLOW.md
- PROJECT_NOTES.md
- /home/vandal/BOT_SYSTEM.md

## Status

Active and currently in an observation phase before the next round of improvements.
