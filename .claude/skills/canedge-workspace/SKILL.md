---
name: canedge-workspace
description: >
  Shared conventions for all canedge-* skills: Folder layout, raw data safety, analysis projects, the
  DBC-to-device mapping (dbc-groups.json), UTC handling, credentials (data lake profiles + setup window), the setup check and log file
  listing. Use at the start of any CANedge data task ("analyze my CANedge data", "set up a project", "check my
  setup", "list my log files", "which DBC applies to which device") and when the user provides new log or DBC
  files.
---

# canedge-workspace

The glue for all `canedge-*` skills. **Read [references/conventions.md](references/conventions.md)
before your first data task in a session** - it defines the folder layout, data safety rules,
`project.json`, `dbc-groups.json`, UTC handling and credentials that every other skill relies on.

## Hard rules (summary)

1. Never move, rename, modify or delete raw log files. Never pass `-d` to the CSS tools
2. Never copy raw data into projects - reference it by path
3. Size guard: Ask before processing/downloading more than ~1 GB
4. Never assume all DBC files apply to all devices - ask the user once (usually they do; if not, `dbc-groups.json`)
5. Never ask for keys in the chat, print them or store them outside `credentials/` (one profile per bucket / data lake, `--env <name>`): Use `setup_env.py` instead
6. Never load raw frames/rows into the conversation - use the scripts and read their summaries

## Replies to the user

- Short and to the point: What was done, the result (one table or a few bullets), the next step or question. The
  user should be able to skim it in seconds
- Only what matters for the user's next decision: No background explanations, internal checks or caveats that
  change nothing. Mention a caveat only when it applies (e.g. nothing about Timestamps when they are exact, FW >=
  01.09.01; say so only when they are approximate)
- Leave out what doesn't apply to the user's setup (e.g. BigQuery or Google for an AWS user) and don't list a
  script's checks one by one: Summarize them ("all checks passed") and name only failures and what the user needs next
- Only state what a script or the data showed. Never infer the state of something unchecked (e.g. "the decoded data
  is in your data lake" because a data lake is deployed: The backlog may not be processed yet). Check it cheaply
  first, or leave it out
- A page (report, dashboard, analysis page, config or DBC diff) created or updated in this turn: End the reply with
  its link in bold on its own line, e.g. `**[Open report](file:///C:/.../report.html)**`. Copy the URL from the
  script's `OPEN <label>: file:///...` line, never build it by hand. Label it after the `OPEN` label ("Open report",
  "Open dashboard"), one link per new page, no link when no page was built. Also open the turn's final
  page in the browser: `.venv\Scripts\python.exe .claude/skills/canedge-workspace/scripts/paths.py open <file>`
  (some clients, e.g. the VS Code chat, open file links in an editor tab). Only the final page, not each iteration
  (config variants, rebuilds), and not when the user asked you not to
- Questions about a page ("what does the report show", "which config is this"): Read the JSON next to it
  (`report.json` / `stats.json` / `results.json`, the same data the user sees, conventions section 6) and answer
  directly. Never open the page in a browser tool (Playwright, DevTools) or parse its HTML
- Writing style (replies, reports, `project.md`): No em/en dashes as punctuation (use a colon, comma, parentheses or
  a new sentence). Capitalize the first word after a colon ("Stops of 1.4 s or more: Possibly a slow charger
  shutdown"). Plain, factual wording without filler or hype

## Asking for context

Before a substantial analysis or troubleshooting task, ask for the 1-3 facts the user can answer in a sentence
that would change the approach (vehicle/machine type, protocol, the symptom and when it occurs, a known-good
baseline) - in one message, each with why it matters, offering to proceed without them. Never ask what the data,
the DBC, `project.md` or a cheap script can answer. Record the answers in `project.md` "Context + caveats".

## Scripts

Run with the project venv from the repo root (`.venv\Scripts\python.exe` on Windows,
`.venv/bin/python` on macOS/Linux):

| Script | Use it for |
|---|---|
| `scripts/check_setup.py` | First-time / troubleshooting check: Python packages, the MF4 tools (vs the pinned version), data lake profiles (never printed), BigQuery key file, optional `--selftest` (the tools end to end on the bundled `sample-data/`), `--cloud` (the setup's tests on the profile; Azure: Synapse login + one OPENROWSET read) / `--bigquery` / `--grafana [name]` connectivity tests, `--env <name>` |
| `scripts/setup_env.py [--cloud aws\|google\|s3\|azure] \| --key-file <path> --bucket <name> \| --grafana` | Connect a bucket / data lake: A window where the user pastes the keys + bucket name (or, Google data lake, the programmatic access key file without a window; Azure data lake: the Synapse SQL login); derives the rest, tests S3 + Athena/BigQuery/Synapse and writes `credentials/<input bucket>.env` (Azure: `<workspace>.env`). `--grafana`: The same window for a Grafana URL + service account token, writes `credentials/grafana/<host>.env` (canedge-grafana). Prints the result without secrets (conventions section 7) |
| `scripts/new_project.py <slug> ... \| --list` | Create `projects/<yyyy-mm-dd>-<slug>/` with `project.json`, `project.md`, `dbc/`, `parquet/`, `temp/`, `reports/`; `--list`: The existing projects (description, devices, sources, report folders) to pick one to reuse |
| `scripts/paths.py` (module) | Output paths for the other skills' scripts: `report_dir(slug, project)` (`reports/<yyyymmdd>-<slug>/`, without a project `TEMP/reports/...`), `open_line(path, label)` (the `OPEN <label>: file:///...` line); CLI `paths.py open <file>` opens a page in the default browser |
| `scripts/mf4_files.py <paths> \| --project <p>` | List log files with device/session/split, size, firmware, config CRC32, meta name, bit-rates, header start time - without decoding (header fields from .MF4 only; MFC/MFE/MFM: `fill_headers()`, conventions section 8). `--json` / `--out` for the agent |
| `scripts/env.py`, `tools.py`, `mf4_files.py`, `dbc_groups.py`, `muxtp.py` (modules) | Shared helpers for the other skills' scripts: Credentials (data lake profiles, `lake()` names, AWS clients, BigQuery key, Synapse login), the CSS MF4 tools (path, version, mdf2csv to frames incl. CANmod.router demux via mdf2mdf, storage weight per frame), MF4 header + file listing, DBC groups/channels/PGN, CAN ID display (`fmt_can_id`), CANmod.router demux mapping from the router DBC (`muxtp.py`) |
| `scripts/muxtp.py show --project <p> \| dbc --id 010:11 [--id 012:15] -o <file>` | CANmod.router: The project's demux mapping + mdf2mdf flags (from the router DBC), or write a router DBC for a custom tunnel ID / daisy-chained routers (conventions section 5b) |
| `scripts/dbc_groups.py check\|show\|propose` | Validate the DBC mapping (files, channels, devices; without `dbc-groups.json` all DBCs in `dbc/`) incl. duplicate CAN IDs / J1939 PGNs within a DBC (the decoder refuses it) and overlaps across DBCs on the same channel with the same message name (mixed Parquet schemas) (`check --dbc <files>` without a project), show a device's DBCs, or propose a mapping from file names |

## Typical start of a task

1. `check_setup.py` (once per session, or when something fails; `--selftest` on the first run or after a tool update).
   No `.venv/` yet or tools missing: Offer to set everything up, then run the installer (conventions section 1 "Setup")
2. `mf4_files.py <folder>` to see what data the user has (devices, sessions, size, time span)
3. Project (conventions section 3): A task that produces something the user keeps runs in a project. Reuse a
   matching one (`new_project.py --list`: Same devices/bucket and topic), else create one without asking and name it
   in the reply; ask only if two could fit. Quick looks that only answer in chat need none (scratch in `TEMP/`).
   Continuing a project: Read its `project.md` and `project.json` first, before running anything. New project:
   Record sources, devices, timezone and the user's context in `project.json` / `project.md`
4. Before decoding: Resolve the DBC mapping per the conventions (data lake `dbc-groups.json` >
   bucket-root DBCs > ask the user if the DBCs apply to all devices). Copy the DBCs into `dbc/`; add a
   `dbc-groups.json` only if they differ per device. Run `dbc_groups.py check`
5. Hand over to the task skill (see the table below); shareable results go into a report via `canedge-reports`
6. Update the project's `project.md` so follow-up sessions start with the right context: Lasting
   facts (which bus to trust, DBC issues, logging gaps, the user's definitions) go in "Context +
   caveats", edited in place and kept current; dated results go in the findings log, one line each

## Which skill for which job

| Task | Skill |
|---|---|
| CANedge facts: Configuration fields, hardware, log file format, S3 behaviour, tools | `canedge-knowledge` |
| Convert log files to raw CAN/LIN frames (CSV), quick raw-frame questions | `canedge-mdf2csv` |
| CANmod.router: Demux router data (CAN11-14), demuxed / decompressed / decrypted MF4 copies (asammdf, Vector tools) | `canedge-mdf2mdf` (conventions section 5b) |
| Raw data overview across many log files (bus activity, frames/s, trips, IDs, storage, meta names + config changes) | `canedge-raw-overview` |
| DBC-decode log files to a local Parquet data lake (no data lake yet, ad hoc decoding) | `canedge-mdf2parquet` |
| List / download log files from S3 (first/last N, sessions, logging time), uploads per period, any object (config, device.json, DBC) | `canedge-s3` |
| S3 bucket overview dashboard (devices, logged vs uploaded per day, devices not logging/uploading, firmware) | `canedge-s3-overview` |
| Archive / move log files to another bucket (cutoff date, Glacier) | `canedge-s3-migrate` |
| Over-the-air updates via S3: Firmware, push a config / change fields on one or many devices, encrypt passwords, move devices to another bucket/server | `canedge-ota` |
| Upload SD-card log files to S3 with the CANedge S3 metadata | `canedge-s3-sd-upload` |
| Query decoded signals with SQL: Local Parquet lake (DuckDB) or the cloud data lake (Athena, BigQuery, Synapse), fleet-scale analyses | `canedge-lake-query` |
| Configure the data lake automation (events/alerts, calculated signals, trip summaries, backlog, device DBCs, routing), function/job logs, "why is file X not in the lake", start backlog/aggregation/mapping jobs | `canedge-lake-admin` |
| Analysis questions as an interactive page (what is wrong with X, which vehicle has an issue + root cause, duty cycles, outliers, indicators on some vehicles). Log files not decoded yet: Decode them all first (`canedge-mdf2parquet`) | `canedge-lake-analysis` |
| Build + check Configuration Files (filters, transmit lists, control signals), filter effect on file size; CANmod.router configs (S1-S4 filters, router report) | `canedge-config` |
| Review, fix, create DBC files | `canedge-dbc` |
| Standing dashboards | `canedge-grafana` |
| Device not recording / not uploading / config not applied / no useful data (workflow + device.json check) | `canedge-troubleshoot` |
| A skill had a real, general defect this session: Propose a minimal fix, implement on OK, offer a pull request | `canedge-improve` |

Avoid custom MF4 parsers, decode loops over raw logs in python-can/cantools, driving GUI tools, and
downloading GBs from S3 when a data lake query (Athena, BigQuery, Synapse) can answer the question. Use only the Python
packages in `requirements.txt` (the `.venv`); never install packages. Interactive browsing of single
files (asammdf) and S3 / OTA management (CANcloud) are for the user, not the agent. So is the free
[Parquet viewer](https://www.csselectronics.com/pages/parquet-viewer-can-bus-data-lake): The user plots and inspects
a local Parquet data lake folder, a zip or single Parquet files in the browser (nothing uploaded, also offline).
Suggest it when the user wants to browse decoded data themselves.

## Related skills

- `canedge-knowledge` - CANedge facts (configuration, folder structure, tools, data lakes)
- `canedge-reports` - shareable HTML/JSON reports
