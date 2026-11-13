---
name: canedge-lake-admin
description: >
  Configure and troubleshoot a deployed CANedge data lake automation (AWS, Google): Write, check and upload the
  control files in the input bucket root (events.json, custom-messages.json, geofences.json, aggregations.json,
  backlog.json, dbc-groups.json, routing.json, root DBC files), read the function/job logs and start the backlog,
  trip summary or table mapping jobs on request. Use for "alert me when signal X goes above Y", "add a trip summary",
  "process the backlog of devices X, Y before date Z", "why is my Lambda not running on file X", "why is there no
  data in Grafana", "did my backlog run work". Reading the lake: canedge-lake-query; the bucket: canedge-s3.
---

# canedge-lake-admin

Changes the data lake's behavior through the control files in the input bucket root, and checks what the automation
did with a file, a control file or a job. How each feature works is in the CANedge Intro (`canedge-knowledge`,
`[Parquet data lake - advanced topics]:` and its sub-pages): Read the relevant section before writing a file.
Shared rules: [conventions.md](../canedge-workspace/references/conventions.md).

Needs a data lake profile with the data lake's programmatic access keys (AWS) or key file (Google). Logs and jobs
also need a stack with `ProgrammaticAccessPolicy-<id>` (AWS data lake stack from 2026-11, `status` says if not).
Azure: Logs and jobs are not supported (Azure portal, see [troubleshooting.md](references/troubleshooting.md));
control files work through the Flexify S3 profile of the input container.

## Scripts

Run from the repo root with `.venv\Scripts\python.exe .claude/skills/canedge-lake-admin/scripts/<script>`
(`--env NAME` for the profile, `--json` for details):

| Command | Does | Writes / starts |
|---|---|---|
| `control_files.py check FILE [--offline]` | The automation's own validation (vendored `_automation_control_files.py`, the code the functions run) + cross-checks: Message folders (case-sensitive), signal names in the root DBCs, devices, DBC files, routing buckets, backlog files + duplicates | - |
| `control_files.py pull NAME` | Current root file -> `data/lake-admin/<bucket>/drafts/NAME` | - |
| `control_files.py backlog --device X [--start T] [--end T]` | Builds `drafts/backlog.json` from the S3 inventory (logging time from the 'Timestamp' metadata) + a rough run time vs the job's limit | - |
| `control_files.py push FILE [--as NAME]` | Re-checks (refuses on errors), backs up the root object to `data/lake-admin/<bucket>/backups/`, uploads, verifies, prints the diff. A DBC in one folder (v4.5.0+): `--as dbc/can1-x.dbc` | Bucket root (DBC: or one folder) |
| `control_files.py remove NAME` | Backup, then delete (the feature is off again, e.g. to stop an email flood) | Bucket root |
| `lake_ops.py status` | Automation version, root control files + DBCs, function results (24 h), last job runs | - |
| `lake_ops.py diagnose KEY` | One log file: Object + trigger suffix -> invocation result + errors -> Parquet output -> catalog tables | - |
| `lake_ops.py logs --source lambda\|backlog\|aggregation\|map-tables [--since 6h] [--key K] [--grep T] [--errors]` | Log lines per invocation/run with the result | - |
| `lake_ops.py runs [--job J]` | Job runs (AWS Glue; Google: HTTP requests) | - |
| `lake_ops.py start --job backlog\|aggregation\|map-tables [--timeout MIN] [--wait]` | Checks the job's JSON in the bucket + no active run, then starts it. `--timeout` (AWS): This run's limit instead of the job's (max 2880) | Job run |

## Workflows

**1. Event, calculated signal or trip summary** ("alert me when the battery temperature exceeds 45 °C"):

1. Find the signal: `canedge-lake-query` `lake_schema.py --athena|--bigquery --find <text> --dbc <folder>` (lake
   tables + DBC meaning) and the root DBCs (`check` downloads them to `data/lake-admin/<bucket>/root/dbc/`, or
   `canedge-s3` `s3_object.py get`; then `canedge-dbc` `dbc_info.py --find`). Names in the files are the lake folder names (`CAN1_BMS_Temps`, exact case)
   and DBC signal names (exact case), not the lower-case Athena names. A signal with a long name attribute
   (`SystemSignalLongSymbol`) goes by its `SG_` name (the decoder ignores the long name that cantools shows)
2. Agree with the user: Devices, message, signal(s), thresholds (hysteresis gap for continuous signals,
   `exact_match` for states), raster, email text. Restate it in one short table before writing
3. `pull` the current file and add the entry (never drop existing entries), then `check` until it has no errors
4. Events: Count how often it would have fired on recent lake data (`canedge-lake-query`, e.g. rows above the
   threshold per file and day) - each file with a start event sends one email
5. Show the user the diff/entry and the dry-run count; `push` after their OK
6. Confirm on the next uploads: `logs --grep "Processing event" --since 1h` (and `--errors`): `Wrote event Parquet file` / `No events found`

Trip summaries: `aggregations.json` runs only via the aggregation job: `push`, then `start --job aggregation` on
request; the daily schedule (`previous_day` mode) is enabled by the user in the console (AWS Glue trigger
`<id>-process-aggregation-scheduled`, Google Cloud Scheduler). Calculated signals beyond the built-in functions need
a customized function (Intro `[Customize your cloud function]:`) - outside this skill.

**2. Backlog** ("process devices X, Y up to March"):

1. `backlog --device X --device Y --end 2026-03-01`, then `check` the draft: Files, GB, entries that match nothing,
   devices that already have Parquet files (processing those files again adds duplicate rows: the user deletes
   their Parquet files for the period in the console first, or narrows the selection)
2. Run time: Both print a rough estimate (~33 GB of log files per hour: The decoder uses one CPU core, ~10 MB/s)
   and the job's limit (AWS: the Glue job's timeout, 60 min in stacks before 2026-11-11, 180 min after; Google: 60
   min). Tell the user the estimate BEFORE starting. If it exceeds the limit (the check warns), offer: Split the
   backlog into runs (per device or period, `--start/--end`; push + start one at a time), or on AWS one run with
   `start --job backlog --timeout <min>` (the suggested value is in the warning). A run cut by the limit ends as
   TIMEOUT: Batches finished before it are in the lake (each batch uploads at its end), the rest is not. Find the
   processed keys in the run's `Trigger event` log lines (all but the last batch) and build the next backlog from
   the remaining sessions, never re-running processed files (duplicate rows)
3. `push` the backlog.json, then on the user's request `start --job backlog` (`--wait` on AWS)
4. `runs` / `logs --source backlog --errors`; then `start --job map-tables` if the run added new devices/messages

**3. Troubleshooting**: Start with `status`, then follow the symptom in
[troubleshooting.md](references/troubleshooting.md):

| Symptom | First check |
|---|---|
| "My Lambda is not running on file X" / file missing in the lake | `diagnose KEY` |
| No (new) data in Grafana | `diagnose` on a recent key of that device, then `lake_schema.py` (tables), then the dashboard's time range + data source |
| Events never / always fire, emails missing or flooding | `logs --grep "Processing event"`, the dry-run count, `remove events.json` to stop a flood |
| Trip summary empty | `runs --job aggregation`, `logs --source aggregation` |
| Backlog did nothing | `runs --job backlog`, `logs --source backlog` (ignored entries, CONTROL FILE ERROR) |
| New device/message not in Athena/BigQuery | `start --job map-tables` (on request) |

## Rules

- Writes only after the user's OK on the shown diff or entry, jobs only when the user asks for that run. The harness
  asks before `push`, `remove` and `start` - do not work around it
- Never read, write or print `passwords.json`, profiles or key files; never touch device folders or log files
- An invalid control file stops the processing of every new log file (v4.3.0+): Always `check` before `push`, and
  check the logs after it
- The automation version matters: `check` follows the automation's own rules (v4.3.2) and warns for older lakes
  (the version is the newest `mdf-to-parquet-*-vX.Y.Z.zip` in the bucket root - confirm the stack uses it; differences:
  [control-files.md](references/control-files.md)). Updating a lake is the user's step (Intro: update the stack /
  Terraform)
- Report what the scripts showed (workspace "Replies to the user"); an empty output bucket or missing log group is a
  finding, not a guess
- CANmod.router: The decoder demuxes when the router DBC (`can2-canmod-router.dbc`, canedge-mdf2mdf/assets) is in
  the bucket root next to the `can11-`/`can12-`/... DBCs of the router channels. A `dbc-groups.json` listing
  `can11-*.dbc` files needs automation v4.3.2+ (older lakes stop every log file with a CONTROL FILE ERROR; `check`
  reports it as an error there): Update the lake first, or leave `dbc-groups.json` out (all root DBCs for all devices)

## Related

`canedge-knowledge` (feature docs), `canedge-lake-query` (lake content, dry-runs), `canedge-s3` (bucket inventory,
downloads), `canedge-dbc` (lint DBCs before `push`), `canedge-troubleshoot` (device side: not recording/uploading),
`canedge-grafana` (dashboards for event/trip tables), `canedge-ota` (device folders: firmware + config updates,
moving devices to another bucket; a moved device needs the control files in its new bucket, a meta change renames
the device in new log files)
