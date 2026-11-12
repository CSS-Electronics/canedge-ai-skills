---
name: canedge-grafana
description: >
  Build Grafana dashboards (JSON for import) on the CANedge Parquet data lake via the Amazon Athena, Google
  BigQuery or Azure Synapse (Microsoft SQL Server) data source, following the official CANedge templates; check names against the lake, summarize and
  validate existing dashboards; import, check and edit them in the user's Grafana (service account token). Use for
  "build a Grafana dashboard for ...", "add a panel", "turn this analysis into a dashboard", "dashboard for my
  fleet", "Grafana Athena/BigQuery/Synapse", "put it in my Grafana", "connect my Grafana", or when reviewing/fixing a
  dashboard JSON.
---

# canedge-grafana

Grafana stays the tool for standing monitoring; this skill lets the agent turn findings (e.g. from
`canedge-lake-query` or a `canedge-lake-analysis` page) into importable dashboards that match the official
templates. It writes JSON files; with a Grafana profile it also imports them into the user's Grafana and checks every
panel there ("In the user's Grafana" below). Never touches the cloud account. Shared rules:
[conventions.md](../canedge-workspace/references/conventions.md). Spec format:
[references/dashboard-spec.md](references/dashboard-spec.md).

## Scripts + assets

Run from the repo root with `.venv\Scripts\python.exe` (`.venv/bin/python` on macOS/Linux):

| Script / asset | Use it for |
|---|---|
| `scripts/build_dashboard.py --spec spec.json --out dashboard.json --athena\|--bigquery\|--synapse [--env name]` | Build a dashboard from a compact spec (timeseries, stat, table, bargauge, barchart, piechart, state-timeline, geomap, dynamic signal panel, fleet panels, shared queries, links) for Athena, BigQuery or Synapse in the style below, then validate it. With the lake flag: BigQuery/Synapse message case (+ BigQuery dataset) from the lake, every table and column checked |
| `scripts/validate_dashboard.py <dashboard.json> [--summary] [--athena\|--bigquery\|--synapse [--env name]]` | Static checks (ids, grid, datasources, variables, SQL macros per backend, device dashboard structure, unknown keys per Grafana's JSON schemas); with a lake flag also tables + columns + a missing GPS map. `--summary` first lists panels, queries, variables (learn from a template, review a user dashboard) |
| `scripts/grafana_api.py status\|datasources\|folders\|search\|import\|export\|check\|shot\|delete [--grafana-env name] [--json]` | The user's Grafana (profile from `setup_env.py --grafana`): `import <json> [--folder F] [--overwrite] [--datasource-uid U]` uploads with the data source mapped and prints the `OPEN Grafana dashboard:` URL; `check <uid\|json> [--from --to] [--panel ..] [--yes]` runs every panel's queries with the variables resolved (OK + rows / no data / error); `export <uid> -o f.json`; `shot <uid> [--from --to] [--full] -o f.png` (the dashboard as the user sees it, 1920x1080; needs Playwright, see "Review"); `delete <uid> --yes` |
| `assets/templates/` | The three official dynamic templates (Athena, BigQuery, Synapse; the builder copies their `$device` variable, dynamic panel and its dropdowns) + links to all official templates (GPS, trip summary, event summary), see its README |
| `assets/grafana-schema/` + `scripts/update_schemas.py` | Grafana's JSON schemas (pinned, see SOURCE.md) for the unknown-key check; the script refreshes them and re-checks the official templates (maintenance) |

The lake flags are those of `canedge-lake-query` (`--athena`, `--bigquery`, `--synapse`, `--project`, `--lake`, `--env`): Only
the cached schema is read, no data is scanned.

## Workflow

1. **Know the data**: `canedge-lake-query/scripts/lake_schema.py --athena|--bigquery|--synapse` (devices with meta names), then
   `--catalog --device X --dbc <p> --start .. --end ..` on one day: Which signals have values, their units and value
   tables. Never guess names. Note GPS messages (`latitude` + `longitude`) for the map. Trip/event panels only if
   the lake lists `tbl_aggregations_tripsummary` / `_events`
2. **Pick the base**: For a question, write a spec (`projects/<name>/grafana/<slug>-spec.json`, see
   [assets/examples/gps-fleet-spec.json](assets/examples/gps-fleet-spec.json): A device dashboard in the GPS
   template layout + a fleet row). Per-device dashboards follow "Device dashboards" below. For an official template
   (GPS route, trip or event summary), download it from the [templates README](assets/templates/README.md) into the
   project, read it with `validate_dashboard.py --summary`, then edit it
3. **Build against the lake**: `build_dashboard.py --spec ... --out projects/<name>/grafana/<slug>.json --athena`
   (or `--bigquery` / `--synapse`, + `--env`). Fix all errors; explain remaining warnings to the user
4. **Test custom SQL** (`sql` panels only; generated panels are tested): Replace the macros by literals for one day
   (sql-patterns.md 2 of lake-query) and run it with `lake_query.py --athena|--bigquery|--synapse sql`. BigQuery and
   Synapse panels are in their own SQL: Write the query in the Athena dialect, take the translation from `--bigquery
   sql ... --dry-run` / `--synapse sql ... --dry-run` (T-SQL with OPENROWSET), put the macros back in (Synapse: the
   template's filepath() filter, `r` as the OPENROWSET alias)
5. **Hand over**: Offer once to put it into the user's Grafana directly (below). Else, or if declined: The file path
   and the import steps: Grafana > Dashboards > New > Import > upload the JSON > Load. The data source must be named
   `Amazon Athena` / `Google BigQuery` / `Microsoft SQL Server` (as for the official templates), or rebuild with
   `--datasource-uid <uid>`.
   BigQuery without a lake flag: The user sets the dataset field in the dashboard. Tell the user to set the time
   range to where the data is

Editing an existing dashboard JSON from the user: `validate_dashboard.py --summary` first, then targeted edits
(keep ids/uids), then validate with the lake flag.

## In the user's Grafana

Optional, after a dashboard is built and validated:

1. **Profile**: `grafana_api.py status` shows the URL, the token's role and whether the `Amazon Athena` / `Google
   BigQuery` / `Microsoft SQL Server` data source exists. No profile yet: Explain that a small window opens for the Grafana URL and a service
   account token (Grafana > Administration > Users and access > Service accounts > Add service account, role
   **Editor** > Add service account token), then open it with canedge-workspace `setup_env.py --grafana`. Editor
   imports and edits dashboards and folders and runs their queries, which is all this needs (the skill never creates
   or changes data sources: The user adds them as the CANedge Intro describes). Permissions per data source need
   Grafana Enterprise/Cloud. Viewer: `check` only
2. **Import**: `grafana_api.py import <dashboard.json> [--folder "CANedge"]`. It maps the data source refs to the
   user's data source and keeps the uid, so a re-import updates the dashboard in place. No data source named as
   expected and several of the type: `--datasource-uid <uid>` (from `datasources`). The import refuses an Athena data
   source whose database is not the data lake's (Grafanas often serve several lakes, and `Amazon Athena` may be
   another one): Pick the right one with `--datasource-uid`; none fits: The user adds one (Admin; Athena user's keys,
   region, `dbparquet<id>`, `athenaworkgroup-<id>`). Synapse: The same for a `Microsoft SQL Server` data source whose
   host or database is not the Azure profile's (Intro Grafana-Synapse: host = Serverless SQL endpoint, database,
   SQL Server Authentication, min time interval 1ms)
3. **Check**: `grafana_api.py check <uid> --from <day with data> --to <next day>` runs every panel's queries through
   Grafana. Athena/BigQuery/Synapse queries cost money: Use one day with data; above 20 queries it stops, tell the user the
   number and add `--yes` after their OK. Fix errors and "no data" panels in the JSON, validate, import again
4. **Review** (below): Screenshots, fix, import again
5. **Give the user** the URL in bold from the `OPEN Grafana dashboard:` line, with the time range to look at

Edits of a dashboard in Grafana: `export <uid> -o projects/<name>/grafana/<slug>.json`, edit, `validate_dashboard.py`,
`import` (same uid). The tool tags its dashboards `canedge-ai`; it refuses to replace any other dashboard (same uid,
or same title in the folder) unless `--overwrite`, which needs the user's OK (e.g. they asked to edit their own
dashboard).

## Dashboard design + style

Decide the type first:

- **Trip / drive** (one vehicle, one trip): Time series + stats of that trip. The default time range is ONE
  specific trip (absolute `time`, e.g. the trip the question is about), never `now-7d`
- **Comparison / trend** (across trips, devices, weeks): Aggregating SQL per category (e.g. per device and setting,
  time-weighted bins with `LAG` / `date_diff`), shown as stats, bar charts and pies. Default time range = the data
  period. Built both: Link them both ways (`links`; a table column links to the trip dashboard with its time range)

SQL a human can read and edit: One commented base table (e.g. one row per device and minute, or per trip), then one
`GROUP BY` per panel. No 1 s data and no window functions over months. Measure each query (run time, data scanned:
a few seconds per panel). Several panels on the same rows: One query + `from_panel` (Grafana's `-- Dashboard --`
data source).

Style (firm rules; the builder applies them):

- Stats: One value per panel (or one per device / row), background color with white text, large value, no
  sparkline, short title. Units that cannot scale wrongly (`suffix: km`, not `lengthkm`: "1 Mm")
- Time series of CAN data: Always stepped (`stepAfter`), line width 2, points auto size 1, opacity gradient,
  fill 40
- Never a fixed axis max or min that can hide values: `min: 0` or soft limits (`max` becomes a soft max)
- Bars + bar gauges: The unit once (title or axis label), plain numbers on the bars without overlaps, a legend for
  the colors, the same color per device on every panel. Pies: Unit none, the unit in the title
- Dynamic signal panel: Opens on one signal (`message` + `signal`), names trimmed (Athena pads them)
- Tables: Units in the column headers; test link columns in the browser

## Review

`check` passing does not catch wrong units, cut titles, empty pies or broken links: Look at the dashboard at
1920x1080. `grafana_api.py shot <uid> [--from --to] [--full] -o projects/<name>/grafana/<slug>.png`, then read the
PNG and fix what looks wrong. It needs Playwright, which is not in requirements.txt (about 150 MB with Chromium):
Ask the user once, then `.venv\Scripts\pip install playwright` + `.venv\Scripts\python -m playwright install
chromium`. Without it: Ask the user to look. Loading the dashboard runs its queries (cost as `check`).

## Device dashboards

A dashboard that shows one device at a time (the signals, states and position of one vehicle or machine) is a
device dashboard. It uses the structure of the official templates, so users switch devices and signals the same
way in every dashboard:

- **Variables**: `{"type": "device"}`, the template's `$device` dropdown from `tbl_aggregations_devicemeta` (meta
  names shown, IDs as values). A fixed list (`"devices": [...]`) only if devicemeta is missing or only some devices
  fit. All device panels use `${device:csv}` (the builder's default), never a hardcoded ID
- **Dynamic signal panel, always**: One `{"type": "dynamic"}` panel. The builder copies the template panel and adds
  its `$message` > `$signal` > `$signal_value` (hidden) > `$aggregation` dropdowns, so any other signal of the
  selected device is one click away. Compact as in the GPS templates (w 8-10, h 7), not full width. Optional
  `"message"` preselects one (e.g. the most relevant message)
- **GPS map, if the lake has positions**: A `geomap` route colored by speed (the templates' km/h scale), top right
  (w 7-9, h 11-12). Positions: CANedge GNSS `can9_gnsspos` + `can9_gnssspeed` (`color_scale` 3.6), CANmod.gps
  `can<n>_gnsspos`, else any message with `latitude` + `longitude`
- **Density**: As in the GPS templates, the first screen (about 17 grid rows) shows 15-20 signals: A top row of
  stats (w 2-4, h 4) for the key values, time series (w 7-8, h 5-7) that group related signals of one unit, state
  timelines (h 4-5) for states. Pick the signals the context calls for (what the user monitors on this kind of
  device). More topics go into `row` sections below (e.g. battery, drivetrain, faults)
- Fleet panels (`devices: [...]`, one series per device) are not per device: A `Fleet` row at the bottom, or their
  own dashboard
- The validator warns on a device dashboard without the dynamic panel and, with a lake flag, without a geomap when
  the lake has GPS positions

## Rules

- Panel SQL follows the official templates (generated query per backend in dashboard-spec.md). Athena: Every
  message table query keeps `date_created BETWEEN $__rawTimeFrom('yyyy/MM/dd') AND $__rawTimeTo('yyyy/MM/dd') AND
  $__timeFilter(t)`. BigQuery: `${dataset}.tbl_...` with the message in its real case, `$__timeFilter(t)` only (no
  `date_created` on message tables). Synapse: `OPENROWSET(BULK '${device:csv}/<Message>/*/*/*/*', ...) AS r` (real
  case) + the `filepath()` folder filter + `$__timeFilter(t)`, never the mapped `tbl_` tables in panels (they
  return no data rows; they feed the dropdowns only), `TOP` instead of `LIMIT`. All return aggregated rows
  (time bucket + `GROUP BY`), never raw frames
- Prefer Grafana transformations (merge, calculateField) over SQL JOINs across messages
- J1939-73 DM1 (DTCs) decoded with the CSS J1939-73 DBCs (`DM01_HEADER` + `DM01_DTC_01..10`, one table per DTC slot
  and source address): Never hardcode source addresses (more ECUs appear later). Query the combined
  `tbl_<device>_can<n>_calc_dm01` (`sa`, `dm01_spn_final`, `dm01_fmi`, `dm01_oc`; active = `dm01_spn_final > 0`)
  from `custom-messages.json` function `combine_dtcs`; missing: Propose adding it (`canedge-lake-admin`, past files
  via a backlog run). Only works with that DBC format. Details: CANedge Intro `[Log J1939 data]:` "Decode J1939-73
  DM1 (DTCs)" (also the SPN name lookup table) and `[Add calculated signals to your data lake]:`
- One topic per dashboard, sized by its context: A device dashboard is dense (above); a narrow question (one
  fault, one KPI across the fleet) can be a few panels. The visual rules of the lake-analysis "Page checklist"
  apply: Units from the DBC (unit ids in dashboard-spec.md), one unit per panel, compare like with like (fleet
  panels: same signal + aggregation per device), the chart type follows the question (time series for signals,
  `state-timeline` with the DBC value table for states, modes and flags, stat for one number), no lines across gaps
  (the builder breaks lines after 5 min without data)
- If only some devices have the messages, the lake check says so: Keep `$device` for a mixed fleet, or use a fixed
  device list
- "Turn this analysis into a dashboard": One dashboard per standing question of the analysis; per device, it is a
  device dashboard (above) built around the signals behind the key cards. The analysis page stays the offline
  answer, Grafana the monitor
- Unknown keys are warnings: Grafana ignores them silently, so a typo never shows an error there
- Grafana: Only import, export and check what the task needs. Never delete (`delete --yes` only when the user asks)
  or change other dashboards, folders or data sources unless asked. Never print, read or ask for the token (the
  profile is created with the setup window)

## Related

- `canedge-lake-query` (dependency: schema, meta names, SQL testing; its `resample` SQL is the literal form of the
  panel query), `canedge-lake-analysis` (ad hoc analysis page instead of a standing dashboard), `canedge-reports`
  (one-off reports), `canedge-knowledge` (CANedge Intro: Grafana-Athena, Grafana-BigQuery and Grafana-Synapse setup guides)
