# HealthCoach Vision

## Mission

HealthCoach should evolve from a reporting bot into a personalized health coach.

It should use health, nutrition, activity, recovery, and behavioral data to identify what matters most and provide one practical recommendation with the highest likely value.

The system should become more useful over time by learning from recurring patterns, previous recommendations, and measured outcomes.

## Guiding Principles

### Coach, Do Not Just Report

HealthCoach should interpret data rather than simply repeat numbers.

Every coaching message should help answer:

- What matters today?
- Why does it matter?
- What is the best next action?

### Prioritize One High-Value Action

HealthCoach should avoid overwhelming the user with many recommendations.

When several issues exist, it should identify the single action most likely to improve the day.

### Learn From Outcomes

HealthCoach should remember which recommendations were successful and which were not.

Recommendations that repeatedly work should become more likely to be used again in similar situations.

Recommendations that repeatedly fail should be reconsidered.

### Build Cumulative Personalization

Each day of data should make future coaching more relevant.

The bot should not behave as though every day is the first day it has worked with the user.

### Respect Uncertainty

HealthCoach should distinguish between:

- Complete data
- Missing data
- Weak patterns
- Strong patterns
- Correlation
- Likely cause and effect

It should not present uncertain conclusions as facts.

### Use Existing Data Better

The goal is not to continually add more features or more metrics.

The goal is to make the existing coaching smarter, more selective, and more personal.

### Recognize Success

HealthCoach should identify positive behavior and improvement, not only problems.

Successful routines and interventions are valuable memories and should influence future coaching.

### Avoid Unnecessary Repetition

HealthCoach should not repeatedly give the same advice unless the advice remains relevant and there is a reason to repeat it.

When advice is repeated, the wording and reasoning should reflect the current situation.

## Long-Term Architecture

HealthCoach will gradually develop into five logical components.

### 1. Data Collector

The Data Collector gathers information from sources such as:

- HealthKit
- Lose It
- Google Sheets
- Telegram
- Future integrations

Its responsibility is reliable collection and normalization of data.

### 2. Health Analyzer

The Health Analyzer converts raw data into structured observations.

Potential outputs include:

- Recovery status
- Nutrition status
- Activity status
- Weight trend
- Data completeness
- Confidence level
- Primary concern
- Positive trend

### 3. Coach

The Coach decides:

- What matters today
- What can be ignored
- What recommendation has the highest value
- How urgently the recommendation should be communicated

The Coach should prefer recommendations supported by the user's history.

### 4. Memory

Memory is the highest development priority.

The Memory system should learn from:

- Recurring patterns
- Coaching history
- Successful interventions
- Unsuccessful interventions
- User responses
- Measured outcomes

Memory should allow HealthCoach to become more personalized and effective over time.

### 5. Planner

The Planner is a future component.

It should eventually help prepare for:

- Travel
- Busy weeks
- Recovery days
- Weekends
- Known problem days
- Goal progression
- Schedule disruptions

Planning should be based on observed patterns rather than generic assumptions.

## Memory System Vision

The Memory system should record the relationship between:

1. What HealthCoach observed
2. What HealthCoach recommended
3. What the user did
4. What happened afterward

A coaching memory may include:

- Date
- Situation
- Available data
- Observation
- Recommendation
- Expected outcome
- Actual outcome
- Whether the recommendation was followed
- Confidence in the result

HealthCoach should eventually recognize patterns such as:

- Protein is often low on a particular weekday.
- Steps are often lower after travel.
- Poor sleep is followed by reduced activity.
- Weekend eating differs from weekday eating.
- A protein bar before lunch often fixes a protein shortfall.
- An evening walk often closes a step gap.

Patterns should require repeated evidence before they influence coaching.

## Personalized Coaching Vision

HealthCoach should gradually learn:

- Which recommendations are realistic
- Which recommendations are usually followed
- Which interventions produce measurable improvement
- Which times of day are best for reminders
- Which situations commonly create setbacks
- Which routines help prevent those setbacks

The bot should use that knowledge to choose advice that fits the user rather than relying only on generic health guidance.

## Goal Progression Vision

Goals should change slowly and intentionally.

HealthCoach should consider:

- Consistency
- Difficulty
- Recent trends
- Recovery
- Data quality
- How long the current goal has been active

A goal should not increase simply because it was achieved once.

A goal should not be abandoned simply because it was missed once.

## Recovery Intelligence Vision

HealthCoach should distinguish between:

- A day when more effort would be useful
- A day when recovery should be prioritized
- A day when the available data is insufficient to decide

Recovery decisions may consider:

- Sleep
- Resting heart rate
- HRV
- Recent activity
- Calorie deficit
- Travel
- Consecutive difficult days

The bot should not always recommend more activity when rest may be the better choice.

## Future Ideas

This section captures ideas that may be valuable later without committing to implementation.

- Learn whether protein coaching works better in the morning or near lunch.
- Detect recurring low-protein weekdays.
- Detect travel-related changes in steps, sleep, and eating.
- Identify meals or foods that consistently help meet protein goals.
- Learn whether evening walks reliably close step gaps.
- Recognize when sodium likely explains short-term weight changes.
- Prepare coaching before known busy days.
- Adjust recommendations based on recent recovery.
- Track which recommendation wording leads to better follow-through.
- Avoid repeating interventions that have not helped.
- Recognize positive streaks and successful routines.
- Progress goals only after sustained success.

## What HealthCoach Should Not Become

HealthCoach should not become:

- A stream of raw statistics
- A collection of disconnected features
- A system that gives too many recommendations at once
- A system that treats weak patterns as facts
- A system that forgets previous coaching
- A replacement for professional medical care

The purpose of HealthCoach is to provide practical, personalized guidance from the data already available.
