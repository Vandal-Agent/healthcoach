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

# Photo Tools

Photo Tools are available from the Telegram Food menu.

## Restaurant Menu Photos

Flow:
- User selects Read a restaurant menu photo or sends a menu photo directly.
- HealthCoach reads only information visible in the photograph.
- It recommends up to three promising entrees.
- Printed calories are displayed only when visible in the photograph.
- Nutrition that is not printed is not invented.
- Menu-photo recommendations are advisory and are never logged automatically.

## Actual Meal Photos

Flow:
- User selects Estimate an actual meal photo or captions a photo with
  "Estimate this meal."
- HealthCoach estimates ranges for calories, protein, carbohydrates, and fat.
- The estimate lists visible components, portion assumptions, and uncertainty.
- HealthCoach asks for high-impact clarification such as protein type,
  preparation method, sauce, dressing, or added oil.
- The user selects how much of the pictured portion was eaten.
- The user selects the meal using Telegram buttons.
- HealthCoach calculates the midpoint of each refined range and shows a final
  confirmation before logging.
- The user may log the estimate, change details, or cancel.
- Logged photo entries are marked as estimated and retain their historical
  nutrition snapshot.
- Fiber, sugar, and sodium remain unknown rather than being fabricated from
  appearance alone.
- Nothing is logged without explicit user confirmation.

The Food menu is grouped into Daily Food, My Foods, and Tools so these
capabilities remain discoverable as HealthCoach grows.

---


# Barcode Scanner

Barcode Scanner v1 is available under Food > Photo Tools.

Supported workflow:

- Accept a clear Telegram photo of a product barcode.
- Accept typed GTIN-8, UPC-A, EAN-13, or GTIN-14 digits.
- Validate barcode length and checksum before lookup.
- Preserve small outside digits and leading zeroes.
- Check USDA FoodData Central for an exact branded barcode.
- Fall back to Open Food Facts when USDA has no exact match.
- Clearly identify community-contributed Open Food Facts data.
- Reject records without a usable serving size or calories.
- Display serving-level calories, protein, carbohydrates, fat,
  fiber, sugar, and sodium.
- Save a confirmed product to the Saved Foods library.
- Log a confirmed number of servings to a selected meal.
- Scale nutrition snapshots by the logged serving quantity.
- Synchronize Food Ledger totals after logging.
- Allow consecutive barcode scans without returning to the menu.
- Check permanent local barcode mappings before outside databases.
- Never save or log anything without user confirmation.

Choosing Log It also stores the product so it can be reused later.

Incomplete barcode records are not treated as zero-calorie foods.
The user is directed toward package-label entry instead.

USDA records are treated as official exact-barcode data. Open Food
Facts records are community-contributed and must be reviewed by the
user before Save Product or Log It makes them trusted Saved Foods.

When neither provider has complete nutrition, HealthCoach offers to
learn the product from its package:

- The user sends a clear Nutrition Facts photo.
- HealthCoach transcribes one printed serving without estimating.
- Missing or unreadable nutrients are rejected rather than changed to
  zero.
- The user supplies the product name and optional brand.
- A final confirmation displays the barcode, serving, and all supported
  nutrition fields.
- Confirmation saves the Food, its nutrition, and a permanent barcode
  mapping.
- Future scans resolve locally before USDA or Open Food Facts.
- Previously logged Food entries retain their original nutrition
  snapshots.

---

# My Pantry

My Pantry is a separate presence-only list under the Telegram Food menu.
It does not treat every Saved Food as currently available.

Supported workflow:

- View the foods currently available at home.
- Add up to 30 items at once using a comma-separated or line-separated
  natural-language list.
- Select Scan product into Pantry before sending a barcode photo, then add
  the confirmed product using the Add to Pantry button.
- Remove one Pantry item without deleting its Saved Food or Food Ledger
  history.
- Clear the entire Pantry only after explicit confirmation.
- Keep Pantry items available until the user removes or clears them.
- Avoid quantity and depletion tracking in this version.
- Request exactly three lunch or dinner ideas at a time.
- Keep lunch ideas at or below 500 calories and dinner ideas at or below
  600 calories.
- Consider nutrition already logged for the current day when explaining
  how each idea fits.
- Use at least one Pantry item and require no more than two additional
  ingredients per idea, aside from salt, pepper, cooking spray, and water.
- Show estimated nutrition, ingredient amounts, and preparation steps.
- Generate three different choices when the user selects More ideas.
- Ask how many servings were eaten and require final confirmation before
  logging the meal.
- Mark logged Pantry meal nutrition as estimated and retain its nutrition
  snapshot in the Food Ledger.

Manual items may be fresh ingredients without nutrition records. Scanned
items retain a link to their Saved Food and active verified nutrition so the
meal-idea engine can use stronger nutrition data when it is available.

Pantry meal ideas do not reduce inventory quantities and are not Saved
Recipes. Saving a generated idea as a reusable recipe is a separate future
stage.

Duplicate names are matched case-insensitively and are not added twice.


# Future Features

- Voice logging
- Saved Recipes
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
