# Playbook: Diagnose intermittent issues (faults, DTCs, warnings, odd behaviour)

For "which unit has issues", "what causes fault X", "why does this warning come on", "it sometimes behaves
oddly". A unit is whatever one device logs: A vehicle, machine, robot, vessel, aircraft, battery, test rig. One unit
or a fleet. The answer has two parts, and the page tells both: **What we see** (which units, which indicators,
when, how often) and **what appears to drive it** (the root cause lead, with how sure it is). Indicators are not
causes: They mark the periods to zoom into.

## 0. Context

Ask once (canedge-workspace "Asking for context"), then continue with or without answers:
- What the user, operator or technician observes, and in which situation (operating mode, task, load, after a
  restart or a long stop)
- Which unit, if known; known repairs, tests, updates or changes (they produce fault bursts or shift the baseline)

No symptom given: Find it in the data, and say in the report that the conclusion rests on the data alone. A known
symptom decides which indicator anchors the analysis (step 3).

## 1. All data in scope

All log files of all units in scope in the lake. Check first what is already there (`project.md`, the lake's
devices and date range, a cloud lake): Data that is already decoded is used as is, never decoded again. Only files
missing from the lake are decoded (`canedge-mdf2parquet`; at the size guard, recommend all missing files, not a
sample). Never pick files to decode from raw-stats patterns: That decides the answer before the data is seen.

## 2. Indicators on every unit and day

An indicator = anything that marks "not normal":
- **Reported by the system**: DTCs, fault/error flags and codes, warnings, alarms, degraded or safe modes, error
  counters. Catalog: `lake_schema.py --catalog --find "dtc|fault|error|warn|alarm|fail|safe|limp|degrad|lamp|
  status|mia"` plus the DBC comments and value tables (what a bit or state means, which node sends it)
- **Found in the data** (no flag exists, e.g. "odd behaviour"): A signal outside its normal range or rate of
  change, a commanded vs actual mismatch (setpoint vs feedback), an unexpected state, a node that goes silent while
  the bus is up, restarts, many short sessions in a row (power cycles), coverage gaps. Define each as a condition
- **Reported by the user**: Times when it happened (events with a time and a label)

`lake_query.py episodes` per indicator and unit (`--state` / `--above` / `--below`), saved with `--out` into one
folder (e.g. `data/ep/`); `summary --by day` for counts per day. Several units: **Report every unit with issues**,
even when the user asks for "the" unit; do not assume one. States set briefly on every unit at every start-up are
not issues.

## 3. Incidents, order and the anchor

`events.py incidents --episodes "data/ep/*.parquet" [--merge 30min]` (the merge window matches the process: seconds
for a fast control loop, hours for a slow thermal one): Episodes of a unit close together form one incident, with
the indicators in the order they started.
- **Count incidents and days, not episodes**: One stuck unit restarted ten times is one incident. Rank units and
  candidate issues by incidents, days and severity (in operation vs idle), never by raw event counts
- **Order inside an incident**: The first indicator is closest to the trigger; the later ones are often
  consequences (e.g. one supply dip sets several communication-loss flags at once)
- **Order across incidents**: An incident shortly after another (`after_prev_h`) may be its aftermath: Recovery
  attempts (power cycles, many short sessions), a stuck state, tests. Check it before calling it a second issue.
  Two candidate issues: Test whether one follows the other
- **The anchor**: The indicator that starts incidents, recurs and fits the symptom; a code with a defined meaning
  (DBC comment, manufacturer documentation) is usually the sharpest. Anchor each incident at its onset (the moment
  it becomes active), not at its later occurrences

## 4. Operating state and reference moments

At every onset, find the operating state from the data (e.g. idle, start-up, ramp, steady load, a task or mode,
shutdown) and the phase within it. Then pick **reference moments**: The same state and phase without the problem,
aligned on the same trigger (e.g. every other start-up: the moment power is applied; every other run of the same
task). The comparison is only fair when the references are the same kind of moment. Which units:
- **Fleet: Units that never show the problem are the healthy baseline.** The affected unit's own "normal" moments
  may already be degraded (the issue is there, only below the level that sets the fault), so they are not a clean
  baseline. Compare them separately: Affected unit normal vs healthy units shows whether the unit is off all the
  time (a degradation) or only at the onsets (a trigger)
- **One unit only**: Its own normal periods are the reference, ideally from before the problem first appeared
- Few onsets (< 3): Say so; every onset counts

## 5. Before, at and after the onset

`events.py align --events onsets.csv --reference refs.csv --all` (or `--match` for the relevant nodes) ranks every
signal by when it changes relative to the onset, against the reference moments:
- **before** (changes in the lead-up and not at the references): The leads for a cause. Look for drift, erratic or
  noisy values, a limit approaching or reached, a mismatch growing, a message rate changing, a node going silent
- **at onset**: Part of the trigger or its immediate effect; the order within seconds matters (`resample` at 100 ms,
  or `--trace` at `--interval 100ms` with a short `--pre`/`--post`)
- **after**: Usually consequences (they can still describe the impact)
- **also normal** / no change: Rule them out explicitly when they were candidates

The windows follow the process (`--pre`, `--post`, `--gap`): Seconds for an electrical or control fault, minutes to
hours for thermal, wear or leak effects; vary them once (a slow drift and a sudden step need different windows).
Then `--trace` the 2-4 signals that carry the story: The aligned panels (problem vs reference, `--out` JSON
`panels`) go onto the page.

## 6. From correlation to a cause

A signal that changes with the problem is a lead, not a cause:
- **Timing**: Before the onset can drive it; after it is more likely a result
- **Mechanism**: What would link it to the indicator? Use the DBC (comments, units, value tables, which node sends
  it), the component's role (sensor, controller, actuator, power supply, communication) and its documentation:
  Look up what a code or flag means for that component (e.g. the manufacturer's code list) when the DBC does not
  say
- **Consistency**: Does the pattern hold at every onset? Is there a counter-example: The same pattern without the
  problem (a reference moment that looks like an onset)? A near-miss weakens a simple threshold story
- **Alternatives**: Name the runner-up explanation and why the data rules it out, or that it cannot
- Phrase the result as a lead with its confidence ("appears to", "likely") and what would confirm it (a test, a
  measurement, a signal that is not logged)

## 7. The page

- **Headline**: Symptom + cause in one line, e.g. "Pump 3: Pressure alarms during start-up, likely a sticking valve"
- **Summary** (the story): What we see (which units, how often, in which situation) -> what happens before every
  onset (the trigger) -> the likely cause and its mechanism -> what it is not (the ruled-out alternative) -> the
  caveat (how many onsets, what is not logged)
- **Hero visual**: The aligned panel of the signal that shows the cause (problem onsets vs reference moments, 0 =
  onset), not a count bar chart
- **Supporting**: Incidents per unit (timeline or table with the indicator order), the onset table (time, state,
  the key values), the reference comparison
- Each unit with issues appears; one with a different mechanism gets its own line
