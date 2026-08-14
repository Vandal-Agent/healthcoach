# HealthCoach Food System

## Vision

Build a trustworthy food logging system that understands natural language, verifies nutrition, and learns over time.

---

# Core Principles

1. Never invent nutrition.
2. AI interprets food, never supplies nutrition.
3. Verify before saving.
4. If unsure, ask.
5. Every nutrition value has a source.
6. Every change is versioned.
7. Historical entries never change.

---

# Architecture

Telegram
↓
Conversation Engine
↓
AI Interpreter
↓
Nutrition Providers / Restaurant Advisor
↓
Food Library
↓
Food Ledger
↓
Favorites / Quick Log
↓
Memory
↓
Coaching

---

# AI Responsibilities

Allowed:
- Understand food descriptions
- Extract structured data
- Ask for missing information

Not Allowed:
- Guess nutrition
- Invent foods
- Invent serving sizes
- Pretend confidence

---

# Nutrition Provider Priority

1. Official restaurant
2. USDA FoodData Central
3. Open Food Facts
4. Other approved providers

If nothing can be verified:
- Tell the user
- Do not invent values

---

# Food Library

Each food has one permanent ID.

Stores:
- Name
- Restaurant
- Brand
- Serving
- Verification source
- Verification date
- Nutrition versions
- Usage count

---

# Nutrition Versions

Never overwrite.

Create a new version when nutrition changes.

Historical entries continue using the version that existed when they were logged.

---

# Food Entries

Each entry stores:
- Date
- Meal
- Food ID
- Nutrition Version
- Quantity
- Logging source
- Original text

Confirmed edits may change an entry's quantity or meal. Quantity edits
rescale the entry's saved nutrition snapshot. They do not rewrite the
Food Library or historical Nutrition Version.

---

# Favorites and Quick Log

Implemented in Food database schema version `4`.

Each favorite stores:
- Food ID
- Default quantity
- Default meal
- Created and updated timestamps

Rules:
- A favorite is saved from a confirmed Food Ledger entry.
- Saving the same Food, quantity, and meal again refreshes the existing favorite.
- Quick Log requires confirmation before creating a new Food Ledger entry.
- Quick Log uses the Food's current active Nutrition Version.
- A five-minute duplicate guard prevents an identical favorite from being logged twice accidentally.
- Favorites can be removed without deleting the Food or any historical Food entries.
- Quick Log recalculates and synchronizes the day's Google Sheet totals.

---

# Portion Profiles

Remember user-confirmed estimates.

Example:
- Medium handful pretzels
- Large bowl cereal

---

# Reverification

Recheck when:
- 20 uses AND 30+ days since last verification
OR
- 6 months since verification

Never overwrite previous versions.

---

# Logging Sources

- Telegram AI
- Telegram Manual
- Lose It
- Barcode
- Recipe
- Manual

---

# Restaurant Assistant

Restaurant Assistant v1 is available from the Telegram Food menu.

Flow:
- User supplies a restaurant name and optional city/state.
- Grounded Google search locates current menu and nutrition sources.
- HealthCoach returns up to three protein-forward, moderate-calorie entrées.
- Recommendations respect the current local time and avoid unavailable time-limited items.
- Every accepted recommendation must match a citation returned by the grounded search.
- Official calories and protein are displayed only when published by the restaurant.
- Missing nutrition remains missing; HealthCoach never estimates it.
- Local menu recommendations may be shown without nutrition when supported by a cited primary menu source.
- Results are advisory and are never logged automatically.
- Menu availability may change, so the response retains source links and an availability notice.

---

# Future Features

- Voice logging
- Barcode scanner
- Meal photos
- Recipe builder
- Same as yesterday
- Restaurant history
- Grocery suggestions
- AI meal planning

---

# Design Philosophy

HealthCoach values trust over convenience.

If it knows, it answers.

If it isn't sure, it asks.

If it cannot verify, it says so.

## Saved Foods Library

The Telegram Food menu includes a Saved Foods submenu.

Supported actions:

- Browse manually entered foods and view complete nutrition.
- Add foods to the library without logging them as eaten.
- Edit nutrition for an existing saved food.
- Scale saved drinks from their base fluid-ounce serving.

Saved Food nutrition is versioned. Editing nutrition creates a new active
version for future food logs. Previously logged entries keep their original
nutrition snapshots and are not recalculated.

Saved Foods accept these nutrition fields:

- Calories
- Protein
- Carbohydrates
- Fat
- Fiber
- Sugar
- Sodium

Duplicate food names and serving descriptions are not created.

## Morning Food Coaching

The morning Food Coaching message is a recap of the previous day.

Nutrition now comes directly from the HealthCoach Food Ledger:

- Calories
- Protein
- Carbohydrates
- Fat
- Fiber
- Sugar
- Sodium
- Meal totals
- Top-calorie foods
- Number of logged entries

Burn and steps come from the previous day's Health Tracker row. Current
weight and sleep context may come from today's Health Tracker row.

The scheduled morning coaching path does not refresh or read Lose It email
data. If the Food Ledger has no entries for the previous day, HealthCoach
reports that no foods were recorded and does not calculate a calorie deficit
or issue low-protein or low-fiber coaching from zero values.
