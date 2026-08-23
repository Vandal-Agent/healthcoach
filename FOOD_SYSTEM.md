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
- Barcode
- Recipe
- Manual

Logging source is metadata only. Foods from different HealthCoach entry
methods may coexist in the same meal. Historical Lose It rows remain
readable, but HealthCoach no longer imports, refreshes, or depends on Lose It.

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
- When cited menu details or official nutrition support a clear relative
  choice, label no more than one recommendation as the Heart-Healthy Pick and
  explain the supported strengths and limitations.
- If cited information is insufficient, do not assign the label. Ambiguous or
  unexplained model designations are removed rather than guessed.
- Results are advisory and are never logged automatically.
- Menu availability may change, so the response retains source links and an availability notice.

---

# Photo Tools

Photo Tools are available from the Telegram Food menu.

## Universal Photo Chooser

When a Telegram photo is sent without first selecting a photo tool,
HealthCoach keeps that photo and asks what it should be used for:

- Estimate or log an actual meal.
- Read a restaurant menu.
- Scan a product barcode.
- Scan a product barcode directly into My Pantry.

The selected workflow reuses the original photo; the user does not need to
send it again. Explicit captions such as "Estimate this meal" may continue
directly to their matching workflow. A photo sent while HealthCoach is already
waiting for a menu, meal, barcode, or Nutrition Facts label stays in that
active workflow. Nothing is saved or logged at the chooser step.

## Restaurant Menu Photos

Flow:
- User selects Read a restaurant menu photo or chooses that purpose after
  sending an unprompted photo.
- HealthCoach reads only information visible in the photograph.
- It recommends up to three promising entrees.
- Printed calories are displayed only when visible in the photograph.
- Nutrition that is not printed is not invented.
- When visible menu details support a clear relative choice, label no more
  than one recommendation as the Heart-Healthy Pick and explain only visible
  strengths and limitations.
- If the photo does not support a clear choice, assign no label. Never infer
  sodium, saturated fat, or hidden ingredients from the image.
- Menu-photo recommendations are advisory and are never logged automatically.

Across Pantry, online Restaurant, and menu-photo recommendations,
Heart-Healthy Pick is general food-pattern guidance. It is not a medical
rating, diagnosis, certification, disease-prevention claim, or assessment of
the user's cardiovascular risk.

## Actual Meal Photos

Flow:
- User selects Estimate an actual meal photo or captions a photo with
  "Estimate this meal."
- HealthCoach estimates ranges for calories, protein, carbohydrates, and fat.
- If the photo is unreadable or has no usable nutrition ranges, HealthCoach
  stops the estimate and asks for another photo. It never advances an
  unreadable result to portion, meal, confirmation, or logging steps.
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
- Request Smart Pantry Swaps to receive up to three optional, practical
  replacement ideas ranked by likely value.
- Ground swap reasoning in saved package nutrition when it is available.
  For manual or fresh items without nutrition, use only transparent general
  food-pattern guidance and never invent label values.
- Provide a replacement, reason, package-shopping tip, heart-health context,
  and evidence basis for every suggested swap.
- Return fewer than three swaps, or no swaps, when the Pantry does not contain
  enough meaningful replacement opportunities. Never criticize a sound food
  merely to fill the list.
- Keep Smart Pantry Swaps advisory. Generating or refreshing suggestions never
  removes, replaces, saves, purchases, or logs any food.
- Allow the user to explicitly add any displayed replacement to the persistent
  Shopping List using its numbered Add action.
- Detect when a suitable suggested replacement is already in My Pantry and
  identify it instead of adding an unnecessary Shopping List item.
- Request exactly three lunch or dinner ideas at a time.
- Label exactly one of those three as the Heart-Healthy Pick and explain
  the specific food-pattern strengths behind the selection.
- Base the label on general American Heart Association-aligned food-pattern
  guidance: favor vegetables, fruits, whole grains, legumes, nuts, seeds,
  fish, lean unprocessed protein, and unsaturated plant fats while limiting
  sodium, added sugar, saturated fat, and processed or fatty meats.
- Treat Heart-Healthy Pick only as a food-choice label based on ingredients
  and estimated nutrition. It is never a diagnosis, disease-prevention claim,
  certification, or personal cardiovascular-risk score.
- Keep lunch ideas at or below 500 calories and dinner ideas at or below
  600 calories.
- Consider nutrition already logged for the current day when explaining
  how each idea fits.
- Use at least one Pantry item and require no more than two additional
  ingredients per idea, aside from salt, pepper, cooking spray, and water.
- Show estimated nutrition, ingredient amounts, and preparation steps.
- Allow a generated idea to be saved to the separate Saved Recipes
  library without logging it as eaten.
- Generate three different choices when the user selects More ideas.
- Ask how many servings were eaten and require final confirmation before
  logging the meal.
- Mark logged Pantry meal nutrition as estimated and retain its nutrition
  snapshot in the Food Ledger.

Manual items may be fresh ingredients without nutrition records. Scanned
items retain a link to their Saved Food and active verified nutrition so the
meal-idea engine can use stronger nutrition data when it is available.

Pantry meal ideas do not reduce inventory quantities. They become Saved
Recipes only after explicit confirmation.

Duplicate names are matched case-insensitively and are not added twice.

# Shopping List

The Shopping List is a persistent list available from My Pantry and from the
Smart Pantry Swaps results. It survives service restarts and remains separate
from My Pantry until the user marks an item purchased.

Supported workflow:

- View the current Shopping List.
- Add one or several items manually using a comma-separated or line-separated
  list and explicit confirmation.
- Add an individual Smart Pantry Swap replacement using Add 1, Add 2, or Add 3.
- Preserve the original Pantry item as context for swap-generated additions.
- Avoid duplicate Shopping List names using case-insensitive matching.
- Mark one item purchased only after confirmation. This moves the item into My
  Pantry and removes it from the Shopping List.
- Remove one item or clear the entire Shopping List only after confirmation.
- Never remove or replace the original Pantry item automatically.
- Never log a Shopping List item as eaten.

---

# Saved Recipes

Saved Recipes is a separate library under Food > My Foods.

Supported workflow:

- Save a Pantry meal idea only after explicit confirmation.
- Store its name, intended lunch or dinner type, estimated nutrition,
  ingredient amounts, preparation steps, and estimate notes.
- Preserve the generated Heart-Healthy Pick label and its food-pattern
  explanation when the selected Pantry idea carries that designation.
- Do not retroactively label existing recipes. Clear the designation when
  ingredients or nutrition change because the original basis may no longer
  apply. Name, meal type, summary, and preparation edits preserve it.
- Present the designation as general food-choice guidance, never as a
  medical rating, diagnosis, certification, or disease-prevention claim.
- Do not add anything to the Food Ledger when a recipe is saved.
- Reject duplicate recipe identities rather than silently replacing the
  original recipe.
- Browse recipes alphabetically and view complete preparation details.
- Edit a recipe's name, lunch/dinner type, summary, ingredients,
  preparation steps, or estimated nutrition.
- Require confirmation before saving any recipe change.
- Create a new estimated nutrition version for future logs whenever recipe
  nutrition is edited. Previously logged entries retain their original
  nutrition snapshots.
- Reject a rename when another Food or Saved Recipe already uses that
  identity.
- Delete a Saved Recipe only after explicit confirmation.
- Preserve the recipe's underlying Food record, nutrition versions, and all
  previously logged entries when the reusable recipe is deleted.
- Choose a meal and serving amount when the recipe is actually eaten.
- Show a final confirmation before logging.
- Log from the recipe's existing Food and active nutrition version using
  the `recipe` logging source.
- Mark recipe nutrition as estimated and scale every nutrient by servings.
- Preserve the nutrition snapshot on each Food Ledger entry so later
  nutrition changes cannot rewrite historical logs.

Editing the ingredient or preparation list replaces that complete list.
Ingredient edits use one `amount | ingredient` item per line so amounts stay
separate from ingredient names.

---

# Same as Yesterday

Same as Yesterday is available under Food > Daily Food.

Supported workflow:

- Read food only from the previous calendar day's Food Ledger.
- Display the complete previous-day log before offering any copy action.
- Allow the user to copy one meal or the entire day.
- Show a second confirmation containing the foods, calories, protein, and
  destination before creating entries.
- Copy the exact stored nutrition snapshot and nutrition version from each
  original entry rather than recalculating with newer food data.
- Refuse the complete operation when any selected destination meal already
  has food. Nothing is partially copied.
- Synchronize today's Food Ledger totals after a successful copy.
- Copy food only; weight, sleep, activity, and other health data are never
  copied.

The duplicate protection makes repeated taps safe. A copied meal must go
into an empty meal section for the current day.

---

# Previous-Day Food Logging

The Telegram Food menu includes Log food for yesterday. Natural food messages
that explicitly say `yesterday` use the same previous-day workflow.

Rules:

- The target is always the immediately preceding Pacific calendar day. This
  workflow does not accept arbitrary historical dates.
- Food interpretation, tappable meal selection, Food Library resolution,
  verified nutrition, custom nutrition, and duplicate protection reuse the
  normal Food logging infrastructure.
- Confirmation screens display `Yesterday` and the exact date before anything
  is written.
- Confirmed entries retain nutrition snapshots and are written to yesterday's
  Food Ledger. Yesterday's Google Sheet nutrition totals are then synchronized.
- After each successful entry, HealthCoach shows the complete updated totals
  for yesterday. It does not show today's Weight Goal calorie allowance.
- A morning coaching message that was already delivered is not resent or
  retroactively replaced. Later reports read the corrected Food Ledger.

---

# Weight Goals

Weight Goals is available from Reports > Goals. HealthCoach supports one
active weight-loss goal at a time while retaining archived goal history.

Supported workflow:

- Add a goal weight and date using the latest official morning weight as the
  starting weight.
- View, edit, manually update, remove, and review goal history.
- Recalculate only when the user explicitly chooses Update goal. Viewing a
  goal or logging food never changes the saved calorie target.
- Use the latest official weight and up to seven completed days of Total Burn
  from the Health Tracker sheet. Require at least three valid burn days.
- Save every calculation snapshot, including current weight, burn average,
  required pace, calorie range, safety result, and projected goal-date weight.
- Keep the daily calorie range 150 calories wide and rounded to 50-calorie
  increments.
- Never plan more than 2 lb per week, more than a 1,000-calorie daily deficit,
  or less than 1,500 calories per day.
- When the requested goal is not safely reachable, still provide a safe eating
  range and show the projected weight at the goal date.
- After successful food logging, show today's Food Ledger calories, the saved
  target range, and the remaining range. Warn when any logged item lacks a
  calorie value.
- Never carry excess calories forward as debt. Each calendar day starts fresh.

Weight and Total Burn remain Health Tracker facts. Goal records store planning
settings and calculation snapshots only; they never overwrite Apple Health or
Google Sheet data. Food calories come only from the Food Ledger, including
confirmed visual estimates. Lose It is not used for goal calculations.


# Future Features

- Voice logging
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
- Rename a Saved Food or change its serving description after confirmation.
- Edit nutrition for an existing saved food.
- Remove a Saved Food from the active library after confirmation without
  deleting its Food record, nutrition versions, or historical Food Ledger
  entries.
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
Renames are rejected when another Food already uses the resulting name and
serving identity. Removed foods are marked unverified so they no longer
appear in Saved Foods or resolve as trusted Saved Foods.

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

Burn, steps, and Apple Exercise Minutes come from the previous day's Health
Tracker row. Current weight and sleep context may come from today's Health
Tracker row. Exercise Minutes are coaching and reporting context only; they
are not added to Total Burn or used as extra calories in Weight Goal math.

The iPhone Health Sync Shortcut may send `exercise_minutes` as the summed
Apple Exercise Time for the current day. HealthCoach appends it to column K
of each monthly Health Tracker worksheet. Older rows remain valid and show
Exercise Minutes as not recorded. A recorded zero remains zero rather than
being treated as missing.

The Shortcut may also send `rhr` as Apple Health's resting heart-rate value.
HealthCoach stores it in the existing RHR column F and displays it in Current
Status and 7-, 14-, and 30-day Health History. Missing values remain missing
and do not become zero. Resting heart rate is presented as recorded health
data only; HealthCoach does not diagnose it or assign a cardiovascular-risk
score.

The Shortcut may send `cardio_fitness` as Apple Health's Cardio Fitness
value in mL/kg/min. HealthCoach appends it to column L, shows the recorded
value in Current Status, and includes daily values, the recorded-day average,
and first-to-last recorded change in Health History. Missing values remain
missing and later syncs do not erase an existing value for that day. Cardio
Fitness is never used for calorie calculations, diagnoses, risk ratings, or
automatic fitness classifications.

The Shortcut may send `walking_heart_rate_average` as Apple Health's Walking
Heart Rate Average in beats per minute. HealthCoach appends it to column M,
shows it in Current Status, and includes daily values, the recorded-day
average, and first-to-last recorded change in Health History. Missing values
remain missing and later syncs do not erase an existing value for that day.
Malformed or combined Shortcut values are rejected with a broad ingestion
sanity check: the value must be greater than 0 and no more than 300 bpm.
Previously stored invalid values are displayed as not recorded until a valid
Shortcut sync replaces them.
The metric is recorded without medical interpretation and does not affect
calorie calculations.

The Shortcut may send `blood_pressure_systolic`,
`blood_pressure_diastolic`, and `blood_pressure_measured_at` from one Apple
Health blood-pressure sample. HealthCoach stores the complete pair and its
actual measurement time in columns N through P, shows it in Current Status,
and includes recorded-day averages in 7-, 14-, and 30-day Health History.
The three values must travel together and the source date must match the
Health Tracker day, preventing an older reading from being copied forward.
Blood pressure is informational only and is never diagnosed, rated, or used
for calorie calculations.

## Heart Health Report

Reports includes a Heart Health Report for the last 7, 14, or 30 days. It
reuses the recorded Health Tracker facts already shown in Health History:

- Resting heart rate
- Cardio Fitness
- Walking Heart Rate Average
- Blood pressure
- Exercise Minutes
- Sleep
- Weight

The report shows recorded-day averages, first-to-last recorded changes where
appropriate, the latest blood-pressure pair, and data completeness. Missing
days stay missing and never become zeros. It also points to the existing
Heart-Healthy Pick in Pantry Meal Ideas and Smart Pantry swaps.

The report is informational only. It never diagnoses, assigns a
cardiovascular-risk score, classifies an isolated measurement, changes calorie
targets, or writes back to Apple Health or the Health Tracker.

The scheduled morning coaching path does not refresh or read Lose It email
data. If the Food Ledger has no entries for the previous day, HealthCoach
reports that no foods were recorded and does not calculate a calorie deficit
or issue low-protein or low-fiber coaching from zero values.
