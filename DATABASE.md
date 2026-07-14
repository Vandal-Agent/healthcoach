# HealthCoach Database Design

## Purpose

This document defines the SQLite database for HealthCoach Memory.

The Google Sheet named **Health Tracker** remains the source of truth for daily health facts:

- Steps
- Total calories burned
- Active calories
- Sleep
- Resting heart rate
- Weight
- HRV
- Dietary calories
- Protein

SQLite stores coaching knowledge derived from those facts:

- Observations
- Recommendations
- Outcomes
- Follow-through evidence
- Confidence
- Source and version information

## Database Location

`/home/vandal/bots/healthcoach/data/healthcoach_memory.db`

Rules:

- Do not store secrets in the database.
- Do not commit the database file or its backups to Git.
- Back it up before schema migrations.
- Do not use it to overwrite Google Sheet facts.

## Technology

Memory V1 uses SQLite because it is:

- Free
- Included with Python
- Reliable
- Easy to query
- Easy to back up
- Appropriate for the expected workload
- Replaceable later if a different database is needed

## Design Principles

1. Google Sheets store facts.
2. SQLite stores coaching experience.
3. Follow-through and success are separate.
4. Confidence reflects evidence quality.
5. Rule-based logic comes first.
6. AI can be added later without redesigning the schema.
7. Closed Cases should not normally be rewritten.
8. HealthCoach must still run if Memory is unavailable.

## Schema Version

Initial schema version:

`1`

Each future migration must:

1. Check the current version.
2. Create a backup when needed.
3. Apply only the required changes.
4. Preserve existing records.
5. Record the new version.
6. Log success or failure.
7. Fail safely.

The database must never be silently deleted and recreated during an upgrade.

## Memory V1 Tables

Memory V1 uses three tables:

1. `schema_version`
2. `recommendations`
3. `cases`

Future tables are documented later but should not be created until needed.

---

# Table: schema_version

## Purpose

Tracks the schema version expected by the code.

## Columns

| Column | Type | Rules |
|---|---|---|
| `version` | INTEGER | Primary key, required, positive |
| `applied_at` | TEXT | Required ISO 8601 timestamp |
| `description` | TEXT | Required |

Initial row:

- `version`: `1`
- `description`: `Initial HealthCoach Memory schema`

---

# Table: recommendations

## Purpose

Stores approved recommendations that HealthCoach may select.

This allows recommendations to be enabled, disabled, revised, compared, and later selected by rules or AI without scattering wording throughout the code.

## Columns

| Column | Type | Rules |
|---|---|---|
| `recommendation_id` | INTEGER | Primary key, auto-increment |
| `recommendation_code` | TEXT | Required, unique, stable code |
| `case_type` | TEXT | Required |
| `recommendation_text` | TEXT | Required |
| `recommendation_reason` | TEXT | Optional |
| `expected_metric` | TEXT | Optional |
| `default_expected_threshold` | REAL | Optional |
| `enabled` | INTEGER | Required, default `1`, values `0` or `1` |
| `priority_rank` | INTEGER | Required, lower number means higher priority |
| `created_at` | TEXT | Required ISO 8601 timestamp |
| `updated_at` | TEXT | Required ISO 8601 timestamp |

## Initial Recommendation

- `recommendation_code`: `update_missing_data`
- `case_type`: `missing_data`
- `recommendation_text`: `Update your Lose It totals so I can check today's goals.`

This comes first because it is low risk, easy to evaluate, and already matches current HealthCoach behavior.

---

# Table: cases

## Purpose

Stores one complete coaching episode:

1. Observation
2. Recommendation
3. Expected result
4. Actual result
5. Follow-through evidence
6. Success evaluation
7. Confidence

A day may contain no Cases, one Case, or several Cases.

## Identity and Status

| Column | Type | Rules |
|---|---|---|
| `case_id` | INTEGER | Primary key, auto-increment |
| `created_at` | TEXT | Required ISO 8601 timestamp |
| `case_date` | TEXT | Required local date, `YYYY-MM-DD` |
| `status` | TEXT | Required, default `open` |

Allowed `status` values:

- `open`
- `evaluated`
- `closed`
- `cancelled`

## Classification

| Column | Type | Rules |
|---|---|---|
| `case_type` | TEXT | Required |
| `priority` | TEXT | Required |
| `tags_json` | TEXT | Optional valid JSON array |

Initial `case_type` values may include:

- `missing_data`
- `low_protein`
- `low_steps`
- `poor_sleep`
- `recovery_concern`
- `high_dietary_calories`
- `weight_change`
- `positive_progress`

Allowed `priority` values:

- `low`
- `medium`
- `high`

## Observation

| Column | Type | Rules |
|---|---|---|
| `observation_code` | TEXT | Required stable code |
| `observation` | TEXT | Required human-readable description |
| `supporting_data_json` | TEXT | Required valid JSON object |
| `data_confidence` | REAL | Required, `0.0` to `1.0` |

Example observation code:

`protein_below_midday_goal`

## Meal Opportunity Evidence

| Column | Type | Rules |
|---|---|---|
| `meal_category` | TEXT | Optional |
| `estimated_window_start` | TEXT | Optional local time, `HH:MM` |
| `estimated_window_end` | TEXT | Optional local time, `HH:MM` |

Expected Lose It meal categories:

- `Before`
- `Breakfast`
- `Morning Snack`
- `Lunch`
- `Afternoon Snack`
- `Dinner`
- `Dessert`

Initial estimated windows:

| Meal category | Window |
|---|---|
| Before | 05:00–08:00 |
| Breakfast | 06:00–10:00 |
| Morning Snack | 09:00–12:00 |
| Lunch | 11:00–14:00 |
| Afternoon Snack | 13:30–17:00 |
| Dinner | 16:30–20:00 |
| Dessert | 18:00–23:00 |

These are behavioral estimates, not exact eating timestamps. They should become configurable later.

## Recommendation

| Column | Type | Rules |
|---|---|---|
| `recommendation_id` | INTEGER | Optional foreign key |
| `recommendation_code` | TEXT | Required |
| `recommendation_text` | TEXT | Required exact wording sent |
| `recommendation_reason` | TEXT | Optional |
| `expected_result` | TEXT | Required |
| `expected_metric` | TEXT | Optional |
| `expected_threshold` | REAL | Optional |
| `evaluation_due_at` | TEXT | Required ISO 8601 timestamp |

The Case stores the recommendation code and wording directly so historical Cases remain understandable if the recommendation table later changes.

## Outcome

| Column | Type | Rules |
|---|---|---|
| `followed_status` | TEXT | Required, default `unknown` |
| `follow_through_evidence` | TEXT | Optional |
| `follow_through_confidence` | REAL | Optional, `0.0` to `1.0` |
| `actual_result` | TEXT | Optional until evaluation |
| `actual_value` | REAL | Optional |
| `successful` | INTEGER | `1`, `0`, or `NULL` |
| `outcome_confidence` | REAL | Optional, `0.0` to `1.0` |
| `evaluated_at` | TEXT | Optional ISO 8601 timestamp |
| `closed_at` | TEXT | Optional ISO 8601 timestamp |

Allowed `followed_status` values:

- `yes`
- `no`
- `partial`
- `likely`
- `unknown`

Important rule:

A recommendation may be successful while follow-through remains unknown. HealthCoach must not assume that an improved result proves the recommendation was followed.

## Source and Version Tracking

| Column | Type | Rules |
|---|---|---|
| `observation_source` | TEXT | Required, default `rules` |
| `recommendation_source` | TEXT | Required, default `rules` |
| `evaluator_source` | TEXT | Optional until evaluation |
| `logic_version` | TEXT | Required |
| `model_name` | TEXT | Optional, blank for rules |
| `prompt_version` | TEXT | Optional, blank for rules |

Allowed source values:

- `rules`
- `ai`
- `hybrid`
- `user`

Initial logic version:

`memory_v1_rules_1`

## Audit

| Column | Type | Rules |
|---|---|---|
| `notes` | TEXT | Optional |
| `created_by` | TEXT | Required, default `healthcoach` |
| `updated_at` | TEXT | Required ISO 8601 timestamp |

---

# Relationships

One recommendation may be used by many Cases:

```text
recommendations.recommendation_id
        |
        | one-to-many
        v
cases.recommendation_id
```

There is no direct foreign key to Google Sheets.

Cases connect to health facts through:

- `case_date`
- `supporting_data_json`
- Stable metric names

Google Sheets remain the source of truth.

---

# Indexes

Memory V1 should create:

| Index | Column | Purpose |
|---|---|---|
| `idx_cases_case_date` | `case_date` | Date and range lookups |
| `idx_cases_status` | `status` | Find open Cases |
| `idx_cases_type` | `case_type` | Find similar Cases |
| `idx_cases_recommendation_code` | `recommendation_code` | Compare recommendation outcomes |
| `idx_cases_evaluation_due_at` | `evaluation_due_at` | Find Cases ready for evaluation |

The unique constraint on `recommendations.recommendation_code` should provide its own index.

---

# Constraints

The schema should enforce, where practical:

- Valid status values
- Valid priority values
- Valid follow-through values
- Confidence between `0.0` and `1.0`
- `successful` limited to `0`, `1`, or `NULL`
- Unique recommendation codes
- Required timestamps
- Enabled foreign keys

Application code must validate JSON before insertion.

---

# Initial Case Type

The first live Case type is:

`missing_data`

## Trigger

No qualifying food update is available by the scheduled midday check.

## Observation Code

`food_data_missing_after_midday`

## Recommendation Code

`update_missing_data`

## Expected Result

Food data appears later that day.

## Evaluation

Successful when the missing nutrition data becomes available before the Case closes.

## Why It Comes First

- Low risk
- Easy to detect
- Easy to evaluate
- Does not require inferring health causation
- Tests the full Case lifecycle
- Does not change existing coaching decisions

---

# Memory V1 Scope

Memory V1 will support:

- Database initialization
- Recommendation seeding
- Case creation
- Case evaluation
- Case closure
- Retrieval of open Cases
- Retrieval of similar Cases
- Source and version tracking

Memory V1 will not yet support:

- Automatic pattern declarations
- Goal changes
- AI API calls
- Complex recovery judgments
- Medical conclusions
- Recommendation A/B testing
- User-facing database editing
- Automatic playbook ranking

---

# Future Tables

These are approved concepts but should not be created until needed.

## patterns

Stores recurring patterns supported by multiple Cases.

Examples:

- Protein is often low on Tuesdays.
- Afternoon protein interventions have a high success rate.
- Steps are often lower after poor sleep.

Pattern thresholds must be designed before implementation.

## settings

May later store configurable Memory and Pattern Engine values such as:

- Minimum similar Cases
- Minimum confidence
- Pattern expiration rules
- Meal opportunity windows

## case_events

May later store an immutable audit trail:

- Case opened
- Recommendation sent
- Outcome evaluated
- Case closed
- User correction recorded

## playbooks

May later store strategies for common coaching situations:

- Trigger
- Goal
- Approved actions
- Recommendation ranking
- Evaluation rules

Playbooks are not part of Memory V1.

---

# Backup and Migration

Before a schema migration:

1. Stop writes or use a safe transaction.
2. Create a timestamped backup.
3. Apply the migration.
4. Verify the schema version.
5. Verify row counts.
6. Keep the backup until the migration is confirmed.

Backup name:

`healthcoach_memory_YYYYMMDD_HHMMSS.db`

The database must never be silently replaced.

---

# AI Migration Path

AI may later assist with:

- Observations
- Recommendation selection
- Outcome interpretation
- Message wording
- Pattern suggestions

AI-generated decisions must record:

- Source
- Model name
- Prompt version
- Confidence

AI must not:

- Invent health measurements
- Rewrite Google Sheet facts
- Hide its role in a decision
- Remove the deterministic fallback

If AI is unavailable, HealthCoach must continue with rule-based logic.

---

# Schema Version History

## Version 1

Status:

Planned

Includes:

- `schema_version`
- `recommendations`
- `cases`
- Required indexes
- Rule-based and AI-ready source fields
- Meal opportunity evidence fields
