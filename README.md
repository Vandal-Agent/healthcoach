# HealthCoach

Personal health coaching bot running on a DigitalOcean Ubuntu server.

## Current Capabilities

- Receives health data from an iPhone Shortcut
- Updates the Google Sheet named `Health Tracker`
- Tracks steps, Apple Exercise Minutes, resting heart rate, walking
  heart-rate average, Cardio Fitness, paired blood-pressure readings,
  protein, sleep, weight, HRV, and calorie data
- Shows 7-, 14-, and 30-day weight, sleep, exercise, resting
  heart-rate, HRV, walking heart-rate, Cardio Fitness, and blood-pressure
  history in
  Telegram, including daily entries, missing days, averages, and
  changes
- Adds a non-diagnostic Heart Health Report with recorded averages,
  first-to-last trends, data completeness, and supporting exercise,
  sleep, and weight context
- Labels no more than one evidence-supported Heart-Healthy Pick in Pantry,
  cited online Restaurant, and visible menu-photo recommendations
- Builds a reviewable, deduplicated Pantry list from one or more shelf photos;
  detected items remain unsaved until explicit confirmation and do not invent
  quantities or nutrition
- Organizes large Pantry lists with partial-name search across Pantry names,
  linked Food names, and brands; separate storage-area and food-type labels;
  safe item renaming; versioned nutrition correction or relinking; Pantry-only
  unlinking and deletion; 12-item Telegram pages; explicit confirmation; and
  visible linked-nutrition coverage
- Guides the user through Pantry items that still need nutrition and links each
  existing item—after review—to trusted existing user, package-label, USDA, or
  approved-source nutrition; an exact verified lookup; a barcode; a confirmed
  Nutrition Facts photo; or a complete manual label without duplicating the
  Pantry item or inventing nutrition
- Filters existing Pantry nutrition choices to close food-name and product-form
  matches so unrelated records are never presented as likely links
- Saves Pantry meal ideas as reusable recipes and builds new recipes from a
  numbered, paginated My Pantry chooser backed by version-linked Saved Food
  ingredients, explicit amounts, serving yields, calculated per-serving
  nutrition, preparation, and confirmation
- Imports pasted recipe text or recipe photos into the same review-first,
  version-linked Recipe Builder while blocking unresolved major ingredients
  and documenting any user-approved trace exclusions; unresolved ingredients
  have tap-friendly exact, confirmed-generic, simpler-description, and manual
  Saved Food paths without silent substitution; the final review has a
  numbered editor for correcting amounts, replacing foods, adding missing
  ingredients, and removing optional ingredients before save
- Edits or deletes Saved Recipes without rewriting previously logged meals
- Searches the complete Food Library by name, brand, or alias and explains
  whether each record is in My Pantry, Saved Recipes, Favorites, Entered
  Foods, or only prior Food Ledger history
- Manages a found Food from one contextual page: personal/search name,
  Pantry placement and classification, versioned serving and nutrition
  correction (including honestly unavailable nutrient fields), and safe
  removal or archival without rewriting past logs
- Reviews conservative possible Food Library duplicates, lets the user keep
  both or choose the future primary record, and redirects reusable links while
  preserving old logs, Nutrition Versions, and saved recipe calculations
- Runs a read-only Food Library health check that counts Pantry items needing
  nutrition or organization, foods with unavailable nutrient fields,
  non-recipe records with untrusted sources, provider records due for a source
  recheck, and intentionally preserved Nutrition Version history; each finding
  routes to an existing confirmed management workflow and nothing is changed
  automatically
- Sends a once-weekly Sunday 9:30 AM Pacific reminder to run the read-only Food
  Library Health Check; the reminder never runs maintenance or changes data
  automatically
- Renames, updates, or removes manually Entered Foods while preserving
  food-log history
- Reviews yesterday's Food Ledger and safely copies one meal or the
  entire day after confirmation
- Logs a forgotten food only to the immediately preceding day through either
  the Food menu or an explicit natural-language `yesterday` request, with an
  exact-date confirmation and updated yesterday totals
- Manages a safety-capped weight goal with manual calorie-target updates,
  burn-based projections, and remaining-calorie feedback after food logging
- Uses the Food Ledger rather than Lose It for current nutrition totals
- Sends daily and weekly coaching through Telegram
- Skips and reports an individual failed Telegram update so one malformed
  interaction cannot freeze all later messages
- Claims each Telegram update before handling it so a repeated numeric reply
  cannot cross into and confirm the next conversation step
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
