---
name: canedge-lake-analysis
description: >
  Answer analysis questions on a CANedge Parquet data lake (local, Athena, BigQuery, Synapse) as one interactive,
  shareable page: Device + period filters, findings that jump to the data, KPIs, charts, episodes and GNSS
  context. Use for "what is wrong with vehicle X", "which vehicle has intermittent issues / DTCs and what is the
  root cause", "why does X show this fault and the others not", "duty cycle
  / driving profile across our fleet", "which vehicle deviates", "does X correlate with Y". Queries run via
  canedge-lake-query.
---

# canedge-lake-analysis

The analysis layer on top of `canedge-lake-query`: Recipes for recurring question types, `compare.py` (what
deviates), and a generic analysis page on the shared dashboard kit, so no analysis page is built from scratch. The
agent writes one `analysis.py` per question; the page shows its `results.json`. Read the conventions first
([canedge-workspace](../canedge-workspace/references/conventions.md)) and use `canedge-lake-query` for every query
(its rules apply: Schema + DBC catalog first, cost guards, read-only, UTC).

## Scripts

| Script | Use it for |
|---|---|
| `scripts/build_analysis.py <folder>/results.json [--custom-js f.js]` | Build `report.html` from `results.json`. As a module: `new_results()`, `facts_summary()`, `facts_hist()`, `facts_episodes()`, `save()` |
| `scripts/compare.py --summary f.parquet [g.parquet | "*.parquet"] --target X [--baseline A,B \| --window a..b --baseline-window c..d]` | Rank the deviations of a device vs other devices, or of a window vs its baseline days (robust z, % diff, coverage, signals on one side only). No lake access. `--json` includes a ready table panel |
| `scripts/context.py --athena --results <folder>/results.json --signals a,b [--gnss] [--detail a..b]` | Context for the page: Signals + GNSS route as resampled tiers (whole window + detail around each finding), trips (start, place, average), point budget; rebuilds the page |
| `scripts/events.py incidents --episodes "data/ep/*.parquet"` | Indicator episodes -> incidents per device (merged within 30 min), indicators in onset order, which one comes first, incidents that follow another within hours (aftermath?) |
| `scripts/events.py align --project p --events onsets.csv --reference refs.csv --all [--trace a,b]` | Every signal before / at / after each onset vs reference moments (same state without the fault): Ranked by phase, silent messages; `--trace` = aligned panels for the page |

Run from the repo root with `.venv\Scripts\python.exe`; `--help` on every script.

## Playbooks

A playbook = the best practice for one kind of question, read before starting it (more are added over time):

| Question | Playbook |
|---|---|
| Intermittent issues, faults, DTCs, warnings, odd behaviour: Which units, what happens, root cause (any application: vehicles, machines, robots, vessels, rigs) | [references/playbooks/intermittent-faults.md](references/playbooks/intermittent-faults.md) |

## Question types

Start each one with `lake_schema.py --catalog --device X --start .. --end ..` (one day, one device per vehicle type):
What exists, has values and what it means (DBC comments, units, value tables). Principle: **Screen with
`summary`, drill into flagged days** (`episodes`, `resample` on a short window), never the other way round.

**Breadth before depth** (what is wrong, which vehicle, root cause): A wrong root cause costs far more than a few
extra queries, so the conclusion must rest on all data in scope (all files decoded, every device and day screened),
not on a subset. Say what was covered and why the runner-up was ruled out. A local lake has no query cost: When in
doubt, run the broader query. Faults and DTCs: Follow the playbook above.

| Question | Path |
|---|---|
| Value over a period ("average engine speed in May for Truck 1") | `summary --signals s --device X --by total` (answer in chat; page optional). Row averages are sample-weighted: Check the rate or use `hist` |
| When is X above Y / spikes | `episodes --signal X --above Y` (`--min-duration 0` for spikes) |
| Duty cycle, driving/load profile, exposure (hours at temperature, humidity, load) | `hist --signal X --bins ... --by day` per device (states: `--states`, labels from the value table) |
| What is wrong with X (in a period) | `summary --all --device X --by day` over the period + a baseline period, then `compare.py --window --baseline-window`; coverage changes (messages vanish) first, then signal shifts; drill with `episodes` / context |
| X vs the fleet / which vehicle deviates | `summary --signals ... --by day` for all devices, `compare.py --target X` (or each device in turn); same-type vehicles only (other DBCs = other signals, listed as "only target / baseline") |
| An issue indicator on some vehicles only (a fault/DTC, warning, error counter, state) | The playbook (intermittent faults). In short: Find the indicator in the catalog (`--find` matches DBC comments + value table labels), `episodes --state V` (or `--above`) per device, `events.py incidents`, then `events.py align` on the onsets vs reference moments. Protocol-agnostic: The DBC gives the meaning, do not assume a protocol; protocol tips (e.g. assembling J1939 DM1 DTCs): The CANedge Intro protocol sections (`canedge-knowledge`) |
| Correlation across vehicles (e.g. cell imbalance vs humidity) | Daily `summary --match` (e.g. all cell voltages: spread = max of max - min of min per day) + `hist` exposure per device -> pandas. State n devices and days; correlation is not causation. Signal not in the lake (e.g. humidity): Say so and ask, do not substitute |
| One event at a known time T (a lamp, a dropout, a trip) | Find T (`episodes` on the indicator), then `events.py align --events T --all --pre 60s --gap 3s` (or `summary --all --by total` on [T-60 s, T-3 s) vs [T-3 s, T+3 s)): Signals leaving the earlier min/max are the candidates (skip counters/CRCs); `resample` at 100 ms around T for the order. Compare with a normal occurrence (e.g. a commanded stop) and check which alternative states did NOT occur |
| Did X happen on Y (or why not) | Before "no": Check each copy of the indicator (bus, source ID) on a device with known events (one copy may never set), coverage per day, that the trigger condition was logged at all, and state jumps across logging gaps (unlogged events). Answer "not seen" vs "cannot tell" |

## The analysis page

1. Write `projects/<p>/reports/<yyyymmdd>-<slug>/analysis.py`: Queries via `connect()`, results via the
   `build_analysis` helpers, `save(R, folder)` -> `results.json` + `report.html`
   ([references/results-spec.md](references/results-spec.md)). The reply ends with the bold **Open report** link
   from the printed `OPEN` line
2. **Panels**: The fixed visual assets cover most analyses, so pick them instead of writing chart code: Stat
   panels (`kpis`), tables, time series, bars (time / share / category), histograms, episode timelines, context
   charts + GNSS route with markers. **Facts** (filterable): Mergeable per device + period (`summary` sums/counts, `hist` seconds, episode rows). The
   page re-aggregates them for the selected devices + period. **Fixed** panels (`"mode": "fixed"` + `computed`):
   Percentiles, compare scores, correlations: Shown as computed, dimmed when the selection differs
3. **Answer first**: `answer.headline` (the conclusion in words), `summary` (3-5 one-line sentences), one hero card
   with the number that carries the answer + its evidence visual, max 3 supporting cards (results-spec.md)
4. **Context**: `context.py` (or `build_context()`) with the signals that explain the findings and `--gnss`;
   detail tiers come from the findings' windows (`--detail a..b` for more)
5. **Custom visual** only where no panel type fits: `ANALYSIS.panel("name", (el, view, R, panel) => ...)` in a
   `custom.js` next to the script, using the kit (`SERIES`, `stackedBars`, `TRACK`, `DASH.table`)
6. Build (fix every `build_analysis.py` warning), run the checklist, then give the user the path and the summary

## Page checklist

Think like a consultant briefing a manager: Every element answers part of the question, or it goes.

- **Scope**: The window comes from the data, not from a guess: Each car's first and last day (`summary --by total`
  gives `first_t` / `last_t`); say so if you cut it. Never judge coverage from a truncated listing
- **Message**: The headline states the conclusion; bullet 1 gives the evidence that supports it (what was compared,
  how close or far apart), then support, the one real difference, the caveat that changes how to read the page.
  A number only appears where it supports its own claim. Plain sentences, no jargon, bold once at most, unproven
  causes phrased as leads ("possibly"). A clear "no difference" is a valid answer. One line per bullet (max ~90
  characters); detail goes into the tables or (i) hovers
- **Numbers**: Units from the DBC, checked against a reference when unsure (e.g. a calculated speed in m/s, not
  km/h). Compare like with like: One row per car, one column per measure, one unit per column; bars only within
  a column. State what an average covers (per row or time-weighted, while driving or incl. standstill)
- **Visuals**: The Overview shows the core finding as a picture (`answer.visual`: the evidence, e.g. the aligned
  onset traces of the signal that explains a fault), not only cards. The chart type follows the data resolution and
  the question. Lines (time series, routes) for detailed
  data, e.g. one trip or one day; for overviews of resampled data (days, months) aggregate per period and car:
  Bars per day / week, one dot per trip on the map. Never a line across pauses or between averages hours apart. A
  panel without a clear question goes
- **Comparisons**: Every colour maps to a named entry in the legend; a highlighted condition (e.g. "fault followed")
  gets a shade per entry, not one shared colour. Name the roles ("problem" / "reference") the same in every chart,
  table and hover. Many reference devices: One aggregate (median, band) vs the target, not a legend entry each.
  Custom panels need hover values + a one-line caption (what one dot / line is)
- **Layout**: Stat cards equal and short (title, value, comparison, note: one line each), no tags or badges, no
  text that wraps or shifts content; explanations in (i) hovers, not on the page
- **Check**: Open `report.html` from disk (as the user will), every tab, desktop + phone width; click the cards and
  the map; a page that needs explaining is not done

## Rules

- Device IDs are unique, meta names are not: Key everything by ID; ask when a name matches several devices
- Never present a DBC signal as available unless the lake has values for it
- A deviation or correlation is a lead: Confirm it in the data (episodes, context) before calling it a cause.
  Indicators (DTCs, fault flags) mark when something is wrong, they are not the cause; a change after the onset is
  more likely a consequence than a driver
- A root cause from a subset of the data (some files, days or devices) is not a conclusion: Broaden first
  ("Breadth before depth")
- The page holds aggregates only (no raw rows), stays below ~15 MB (`context.py --points`); maps show where
  vehicles drive: Share only with the user's OK
- Basemap: Esri light gray (works from disk, no key)

## Related skills

- `canedge-lake-query` - all queries (`summary`, `episodes`, `hist`, `resample`, `--catalog`)
- `canedge-reports` - the dashboard kit (`series.js`, `track.js`, `stacked-bars.js`, `dashboard.js`); static
  HTML reports
- `canedge-grafana` - standing dashboards for the cloud lake (this page is the ad hoc, offline counterpart)
- `canedge-dbc` - DBC meaning and fixes when signals look wrong
