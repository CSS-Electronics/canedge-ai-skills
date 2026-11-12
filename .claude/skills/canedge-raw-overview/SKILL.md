---
name: canedge-raw-overview
description: >
  Overview of RAW CAN/LIN data across a folder of CANedge log files (100s of files, many devices) as an
  interactive single-file dashboard, no DBC needed: Bus activity, trips, frames/s, IDs + storage share, meta
  names + config history. Use for "give me an overview of my raw data / log files", "what's on my CAN bus",
  "which CAN IDs are in this data", "when was the bus active", "compare device A vs B". DBC decoding:
  canedge-mdf2parquet.
---

# canedge-raw-overview

Turns CANedge log files (MF4/MFC/MFE/MFM) into cached per-file raw stats, then into an interactive dashboard
for humans (one self-contained `report.html`) and a compact `stats.json` for the agent. Read
[conventions.md](../canedge-workspace/references/conventions.md) first (layout, data safety, projects).

Requires the `canedge-mdf2csv` skill (the converter binary + CSV format; ~7-15 MB/s per worker) and
`canedge-reports` (report CSS + the vendored uPlot chart library). For a few quick questions on single files, `canedge-mdf2csv`'s
`csv_summary.py` is enough - use this skill for the overview across many files.

## Scripts

Run from the repo root with `.venv\Scripts\python.exe` (Linux: `.venv/bin/python`). Details: `--help`.

| Script | Use it for |
|---|---|
| `scripts/raw_stats.py <paths> \| --project <p>` | Header step for every file (serial, session, split, firmware, config CRC32, meta name from the MF4 header; MFC/MFE/MFM: from a finalized copy of one file per session, conventions section 8), then convert each file with mdf2csv (temp CSV deleted at once), write `data/raw-stats/<DEVICE>/<SESSION>/<SPLIT>.json`. Cached by size + mtime (header fields refreshed without re-conversion). Size guard, `--sample N`, `--workers N` (default up to 4 files in parallel, ~2.3x; `1` = one at a time). CANmod.router: With `--project`, devices whose DBCs include the router DBC are demuxed first (mdf2mdf; CAN11-14 = S1-S4, cached per mapping); `--muxtp 010:11` without a project; un-demuxed router data is flagged |
| `scripts/raw_aggregate.py <paths> \| --device ID \| --project <p>` | Merge cached stats into `stats.json` (per device: Recording time, bus activity per minute (active / silent) + trips (active periods), FW/config changes, frames/s per channel, top IDs, static candidates, cross-device comparison). Filters: `--start/--end`, `--top`, `--gap-h`, `--trip-min-min` (3), `--activity-ignore CAN9` |
| `scripts/build_dashboard.py <same selectors> [--tz <IANA>]` | Writes `stats.json` + `report.html`: ONE self-contained file (data, app, uPlot inlined; works offline, can be emailed). The user filters devices and the time range in the browser; nothing is re-processed |
| `scripts/facts.py` | (library) The dashboard facts: Additive per-device time buckets (1 min .. 1 day, size-budgeted), trips, files, IDs per day - from the same per-minute model as `stats.json` |

Field definitions, activity states, bus-load formula and storage estimate: [references/raw-stats.md](references/raw-stats.md).

## Workflow

1. List the data first (`canedge-workspace/scripts/mf4_files.py <folder>`) to see devices, file count and size
2. Compute stats:
   ```
   .venv\Scripts\python.exe .claude/skills/canedge-raw-overview/scripts/raw_stats.py --project projects/<name>
   ```
   Exit code 3 = **size guard** (> 1 GB uncached): Tell the user the printed file count, GB and time
   estimate, then re-run with `--yes` only after they agree, or offer `--sample 5` / `--device` / `--session`
3. Build the dashboard (default folder `projects/<name>/reports/<yyyymmdd>-raw-overview/`; without a project
   `TEMP/reports/<yyyymmdd>-raw-overview/`):
   ```
   .venv\Scripts\python.exe .claude/skills/canedge-raw-overview/scripts/build_dashboard.py --project projects/<name>
   ```
   Tell the user they can filter devices and the period in the dashboard (and share the view: the URL keeps it)
4. Read the generated `stats.json` in full, then answer in chat with the 3-5 key facts, ending with the bold
   **Open dashboard** link from the printed `OPEN` line.
   Give times in local time (as in the report), with UTC only where it helps
5. Follow-up questions ("which IDs only on device B", "when did the config change", "what is the rate of
   0x0CF00400", "when was the vehicle in use"): Answer from `stats.json` (e.g. `states.segments` lists the
   trips (active periods) with their log files - use them to pick files for decoding). For another cut,
   re-run `raw_aggregate.py` with filters (`--device`, `--start/--end`, `--top 200`) - never re-process
   the log files for this

## Output layers

- **Human**: `report.html`, an interactive dashboard: Device multi-select + time-range brush / date inputs
  at the top; six tabs: Overview (default: four KPI groups, scope / activity / traffic / content), Timeline,
  FPS, Trips, Storage, Meta (configuration history: meta name / config CRC32 / firmware periods and changes)
  (each starting with a per-device table; a Meta column shows each device's latest meta name, if any; tables sort by column, page 20 rows with
  Prev/Next/Show all, the Trips and IDs tables have a search box, IDs an 'Only static IDs' toggle), all recomputed in the browser for the selection. Charts:
  uPlot (zoom by dragging = sets the period). PDF = the browser's Print / Save as PDF of the current view.
  Fully programmatic, no generated prose, the same layout for every dataset. Do not add or drop sections, and do not write interpretation into the report: Your own
  interpretation (key insights, what the states likely mean, what to look at next) goes in the chat, or in
  a separate canedge-reports report
- **Agent**: `stats.json` (`canedge-raw-aggregate/2`, ~10-100 KB) next to the report. Per-file stats in
  `data/raw-stats/` give the raw frames per channel and ID, e.g. to compare with the rows a DBC decode produced

## Rules

- Bus activity (and trips) counts every channel by default. If a device shows very long trips with a low
  average FPS, check its CAN9 IDs: CAN-internal messages logged continuously (e.g. TimeExternal `0x005` at 1 Hz,
  or GNSS on a parked vehicle) keep the logger "active". Suggest rebuilding with `--activity-ignore CAN9`
  (`raw_aggregate.py` / `build_dashboard.py`; CAN9 stays in FPS and storage) and mention the config finding
  (canedge-config: the CAN-internal filters decide what the logger records)

- Input log files are read-only: Never pass `-d` to mdf2csv, never move/rename/delete logs
- Never load CSV rows into the conversation; only the scripts touch frames
- Respect the size guard: No `--yes` without the user's OK for > 1 GB
- Talk in frames/s (FPS), not bus load: FPS is counted, bus load is an estimate (kept in stats.json for
  users who ask). Storage share is an estimate too; error frames are not counted (not exported by mdf2csv)
- Bus activity (active = frames on a bus) says nothing about why: Confirm with a decoded signal before
  calling a trip a "drive" (it can be a wake-up, charging, a diagnostic session). Periods without log data
  are not "gaps": The data cannot tell why nothing was recorded
- Device ID comes from the MF4 header serial; a differing folder name is reported (`path_device`)
- Ask before deleting `data/raw-stats/` (it is a cache other skills reuse)
- CANmod.router: A `NOTE: ... looks like CANmod.router data that is not demuxed` (CAN2 0x010/0x012 CAN FD) means the
  router channels are hidden in tunnel frames. Confirm the router with the user, add the router DBC to the project
  (workspace conventions 5b) and re-run both scripts: The dashboard then shows CAN11-14 as "router 0x010 S1-S4". The
  router channels have no bit-rate in the MF4 header (it is in the router config)

## Tool + platform

`raw_stats.py` uses the converter in `canedge-mdf2csv/bin/` (installed by `install.bat` / `install.sh` for
Windows and Linux). Another build: Pass `--tool <path>` or set `CANEDGE_MDF2CSV`. Encrypted files: `projects/<name>/passwords.json` (or `--passwords <path>`); without one they are refused.

## Related skills

- `canedge-mdf2csv` - the converter (required); `canedge-reports` - the report CSS (`build_report.py`, imported)
  and `assets/uplot/` (vendored uPlot 1.6.31, MIT), both inlined into the dashboard
- `canedge-workspace` - conventions, projects, file listing (`mf4_files.py`)
- `canedge-mdf2parquet` - DBC decoding
- `canedge-config` - turn findings (high-rate or static IDs) into filter/prescaler settings; its `simulate_config.py`
  simulates them on a log file this overview helps to pick
