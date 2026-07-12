# HealthCoach Memory Design

## Purpose

HealthCoach Memory stores coaching knowledge that is not contained in the Google Sheet.

The Google Sheet remains the permanent record of daily health facts.

Examples:

- Steps
- Calories burned
- Dietary calories
- Protein
- Sleep
- Resting heart rate
- Weight
- HRV

Memory stores what HealthCoach learned from those facts.

Examples:

- What HealthCoach observed
- What recommendation it gave
- Whether the recommendation was followed
- What happened afterward
- Whether the recommendation was successful
- How confident HealthCoach is in that conclusion

## Core Concept: Cases

A Case is one complete coaching episode.

A Case follows this sequence:

1. HealthCoach observes a situation.
2. HealthCoach selects a recommendation.
3. HealthCoach records the expected result.
4. HealthCoach later evaluates the actual result.
5. The completed Case becomes historical evidence.

A Case is not the same as a daily health record.

A single day may contain:

- No Cases
- One Case
- Multiple Cases

Examples:

- Low protein at midday
- Low steps in the evening
- Poor recovery after insufficient sleep

Each may become a separate Case.

## Storage Decision

Memory V1 will use SQLite.

Reasons:

- No additional monthly cost
- Included with Python
- No separate database server
- Reliable structured storage
- Easy to query
- Easy to back up
- Suitable for future pattern detection
- Large enough for the expected HealthCoach workload

The database should be stored inside the HealthCoach project data directory.

Proposed location:

`/home/vandal/bots/healthcoach/data/healthcoach_memory.db`

The database file must not contain credentials or secrets.

## Separation of Responsibilities

### Google Sheets

Stores daily health measurements.

Answers:

- What happened?
- What were today's numbers?
- What has changed over time?

### SQLite Memory

Stores coaching Cases and learned patterns.

Answers:

- What did HealthCoach notice?
- What recommendation was given?
- Was it followed?
- Did it work?
- What has worked in similar situations?

### Python Rule Engine

Memory V1 will use free deterministic Python rules.

Responsibilities:

- Create observations
- Rank approved recommendations
- Define expected outcomes
- Evaluate measurable outcomes
- Calculate confidence
- Retrieve similar historical Cases

### Future AI Layer

AI may later assist with:

- Generating observations
- Selecting recommendations
- Interpreting complex outcomes
- Personalizing message wording
- Finding less obvious patterns

The database and Case structure must support both rules and AI.

## Case Lifecycle

### 1. Open

A Case is created when HealthCoach identifies a coachable situation.

The Case contains:

- Observation
- Supporting data
- Recommendation
- Expected result

The outcome fields are not yet complete.

### 2. Evaluated

The Case is evaluated after enough time has passed.

The evaluation records:

- Actual result
- Whether the recommendation appears to have been followed
- Whether it was successful
- Confidence

### 3. Closed

Once evaluated, the Case becomes historical evidence.

Closed Cases should not normally be edited.

Corrections may be allowed when data was clearly wrong, but routine rewriting of history should be avoided.

## Case Fields

### Identity

#### `case_id`

Unique numeric identifier assigned by SQLite.

#### `created_at`

Date and time the Case was created.

#### `case_date`

The local calendar date associated with the Case.

#### `status`

Allowed values:

- `open`
- `evaluated`
- `closed`
- `cancelled`

### Classification

#### `case_type`

The main coaching issue.

Initial allowed values may include:

- `low_protein`
- `low_steps`
- `poor_sleep`
- `recovery_concern`
- `high_dietary_calories`
- `weight_change`
- `missing_data`
- `positive_progress`

Additional types may be added later.

#### `priority`

Allowed values:

- `low`
- `medium`
- `high`

Priority describes how important the issue was when the Case was created.

#### `tags`

Optional structured labels used for filtering similar Cases.

Examples:

- `weekday`
- `weekend`
- `protein`
- `lunch`
- `evening`
- `travel`
- `recovery`

Tags should be stored in a consistent machine-readable form.

### Observation

#### `observation`

Human-readable description of what HealthCoach noticed.

Example:

`Protein was 42 grams at 1:30 PM, below the 80 gram midday goal.`

#### `observation_code`

Stable machine-readable code.

Example:

`protein_below_midday_goal`

This allows future logic and AI to refer to the same issue consistently.

#### `supporting_data`

A structured snapshot of the relevant health data at the time the Case was created.

Example contents:

- Protein
- Steps
- Sleep
- Dietary calories
- Time of day
- Goal values

Supporting data should be stored as JSON.

#### `data_confidence`

Confidence that the underlying data is sufficiently complete and accurate.

Range:

- `0.0` to `1.0`

### Recommendation

#### `recommendation_code`

Stable machine-readable recommendation identifier.

Examples:

- `eat_protein_bar`
- `take_evening_walk`
- `prioritize_recovery`
- `update_missing_data`

#### `recommendation_text`

The actual recommendation shown to the user.

Example:

`Have a protein bar this afternoon to close the protein gap.`

#### `recommendation_reason`

Why this recommendation was selected.

Example:

`A protein bar is a practical way to add enough protein before dinner.`

#### `expected_result`

Human-readable definition of success.

Example:

`Reach at least 80 grams of protein by the end of the day.`

#### `expected_metric`

The main metric used to judge the result.

Example:

`protein`

#### `expected_threshold`

The target numeric value when applicable.

Example:

`80`

#### `evaluation_due_at`

The date and time after which the Case may be evaluated.

### Outcome

#### `followed_status`

Allowed values:

- `yes`
- `no`
- `partial`
- `unknown`

HealthCoach should not assume a recommendation was followed unless there is evidence.

#### `actual_result`

Human-readable summary of what happened.

Example:

`Protein finished at 94 grams.`

#### `actual_value`

Numeric result when applicable.

Example:

`94`

#### `successful`

Allowed values:

- `1` for successful
- `0` for unsuccessful
- `NULL` when unknown

#### `outcome_confidence`

Confidence that the success or failure conclusion is valid.

Range:

- `0.0` to `1.0`

#### `evaluated_at`

Date and time the outcome was evaluated.

### Source and Version Tracking

These fields preserve a clean path from rule-based logic to future AI.

#### `observation_source`

Initial value:

- `rules`

Future allowed values may include:

- `ai`
- `hybrid`
- `user`

#### `recommendation_source`

Initial value:

- `rules`

Future allowed values may include:

- `ai`
- `hybrid`
- `user`

#### `evaluator_source`

Initial value:

- `rules`

Future allowed values may include:

- `ai`
- `hybrid`
- `user`

#### `logic_version`

Version of the rule logic that created or evaluated the Case.

Example:

`memory_v1_rules_1`

#### `model_name`

Blank for rule-based decisions.

Later, this may identify the AI model used.

#### `prompt_version`

Blank for rule-based decisions.

Later, this may identify the prompt or AI instruction version.

### Notes and Audit

#### `notes`

Optional internal explanation.

#### `closed_at`

Date and time the Case was finalized.

#### `created_by`

Initial value:

`healthcoach`

Future values may distinguish automated and user-created Cases.

## Initial Outcome Rules

Memory V1 should begin with outcomes that can be measured objectively.

### Low Protein Case

Observation:

Protein is below the configured target at the scheduled coaching time.

Possible recommendation:

Consume a specific approved protein option.

Success may mean:

- Final protein reaches the target
- Protein increases by a meaningful amount
- The recommendation reduces the gap substantially

Follow-through may remain `unknown` unless the user confirms what they ate or the intervention can be inferred with reasonable confidence.

### Low Steps Case

Observation:

Steps are below the expected pace late in the day.

Possible recommendation:

Take an evening walk.

Success may mean:

- Final steps reach the daily target
- Steps increase by the expected amount after the recommendation

Follow-through may remain `unknown` unless the user confirms the walk.

### Missing Data Case

Observation:

Required data was not updated by the expected time.

Possible recommendation:

Update the missing health or nutrition data.

Success may mean:

- The missing value appears later that day

This is one of the easiest Case types to evaluate reliably.

### Positive Progress Case

Observation:

A goal or meaningful improvement was achieved.

Recommendation:

Continue the successful behavior.

Success evaluation may not be necessary for every positive Case.

Positive Cases may primarily help identify routines worth reinforcing.

## Follow-Through Versus Success

These are separate concepts.

A recommendation can be followed but fail.

Example:

- Recommendation followed: yes
- Protein goal reached: no

A recommendation can appear successful while follow-through is unknown.

Example:

- Recommendation followed: unknown
- Protein goal reached: yes

HealthCoach must preserve this distinction.

## Confidence Rules

Confidence should reflect evidence quality.

### Higher confidence

Examples:

- Complete health data
- Clear measurable threshold
- Outcome recorded after the expected evaluation time
- User explicitly confirms the action
- Similar result occurs repeatedly

### Lower confidence

Examples:

- Missing data
- Recommendation follow-through is unknown
- Several possible causes exist
- Outcome occurs before enough time has passed
- Only one similar Case exists

HealthCoach should not convert correlation into certainty.

## Pattern Readiness

Memory V1 will store Cases but should not aggressively declare patterns yet.

Future pattern detection may evaluate:

- Day of week
- Time of day
- Travel context
- Sleep context
- Recommendation type
- Follow-through rate
- Success rate
- Data confidence
- Number of similar Cases

A pattern should require repeated evidence.

Example future rule:

`eat_protein_bar` may be considered effective for `low_protein` only after enough comparable Cases exist and the success rate is meaningful.

The exact thresholds will be designed later.

## AI-Ready Design Principles

### Stable machine-readable codes

Observations and recommendations must have consistent codes.

AI may generate wording later, but it should map to approved codes whenever possible.

### Structured supporting data

Supporting facts should be stored as JSON rather than only prose.

This allows both Python and AI to inspect the same evidence.

### Source tracking

Every observation, recommendation, and evaluation must identify whether it came from:

- Rules
- AI
- Hybrid logic
- User input

### Version tracking

HealthCoach must preserve which logic or model produced a decision.

This allows later comparison of:

- Rule performance
- AI performance
- Different model versions
- Different recommendation strategies

### AI should not control objective facts

Google Sheet measurements remain the source of truth.

AI may interpret data, but it must not rewrite or invent health measurements.

### Safe fallback

If AI is later unavailable, HealthCoach should still function using the deterministic rule system.

## Cost Strategy

Memory V1 will have no additional recurring infrastructure cost.

It will use:

- Existing DigitalOcean server
- Python standard library
- SQLite
- Existing Google Sheet data
- Existing Telegram delivery

AI should only be added later when it provides measurable value.

Possible future AI uses should be evaluated for:

- Coaching improvement
- Accuracy
- Reliability
- Cost per use
- Ability to fall back to rules

## Memory V1 Scope

Memory V1 should:

- Create SQLite storage
- Store Cases
- Record rule-based observations
- Record rule-based recommendations
- Evaluate simple measurable outcomes
- Retrieve similar historical Cases
- Preserve source and version information
- Avoid disrupting existing daily coaching

Memory V1 should not yet:

- Autonomously change goals
- Make medical diagnoses
- Declare complex behavioral patterns
- Depend on an AI API
- Replace the Google Sheet
- Rewrite existing historical health data
- Generate many simultaneous recommendations

## Implementation Order

1. Review and approve this design.
2. Define the SQLite schema.
3. Create database initialization code.
4. Add Case creation functions.
5. Add Case evaluation functions.
6. Add read-only test commands.
7. Test with sample Cases.
8. Integrate one Case type at a time.
9. Monitor logs and database results.
10. Add pattern detection only after enough Cases exist.

## Initial Integration Recommendation

The first live Case type should be `missing_data`.

Reasons:

- Low risk
- Easy to identify
- Easy to evaluate
- Does not require guessing whether an intervention caused a health result
- Tests the full Case lifecycle

After that, likely candidates are:

1. `low_protein`
2. `low_steps`
3. `positive_progress`

Recovery Cases should come later because they require more nuanced interpretation.
