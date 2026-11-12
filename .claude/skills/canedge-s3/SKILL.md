---
name: canedge-s3
description: >
  Read-only work with CANedge2/3 log files in an S3 bucket: Cached inventory (devices, files, sessions,
  metadata), downloads by first/last N files, session or logging time, uploads per period, and any object
  (config, device.json, DBC). Use for "which devices uploaded data", "is device X still uploading", "get the log
  files from device X between A and B", "download the config of device X". Visual bucket overview:
  canedge-s3-overview.
---

# canedge-s3

Inventories the S3 input bucket (cached, incremental) and downloads log files into the shared cache
`data/s3/<bucket>/`. Never writes to S3 and never reads inside log files (they may be compressed/encrypted).
Shared rules (folders, size guard, credentials, UTC): [conventions.md](../canedge-workspace/references/conventions.md).
Details (key format, metadata, cache, IAM): [references/s3-details.md](references/s3-details.md).

## Scripts

Run from the repo root with `.venv\Scripts\python.exe` (`.venv/bin/python` on macOS/Linux):

| Script | Use it for |
|---|---|
| `scripts/s3_inventory.py` | Device folders in the bucket (1 request per 1,000 devices) |
| `scripts/s3_inventory.py --prefix <device>/ \| --all [--metadata] [--yes]` | Per-device summary: Files, GB, sessions, first/last file, logging range (from the first/last file), duplicate uploads, non-log objects. `--metadata` reads every file's metadata: Files without `Timestamp`, approximate Timestamps, all FW versions |
| `scripts/s3_inventory.py --prefix <device>/ \| --all --upload-start UTC [--upload-end UTC]` | Log files UPLOADED in a period (S3 LastModified): Files, GB, sessions, first/last upload per device + per day (`--json`). Listing only, no HEADs. An S3 troubleshooting view, not logging time |
| `scripts/fetch_logs.py --device X [--first N \| --last N] [--session S] [--key K] [--start UTC] [--end UTC] [--dry-run] [--max-gb N]` | Download a device's log files to `data/s3/<bucket>/<key>` (skips existing files with the same size) |
| `scripts/s3_object.py ls [PREFIX] [--folders] \| head KEY \| get KEY ...` | Any object: List a prefix (`ls --folders` = the bucket root: device folders + root files like DBCs), all metadata of one key, download configs, device.json, DBCs or single files |

All scripts take `--bucket` (default: the profile's S3 input bucket) and `--env <name>` (data lake profile `credentials/<name>.env`). `s3_inventory.py --offline` and `fetch_logs.py --cached`
work from the cache without S3 calls. See `--help` for the rest.

## When to inventory (S3 requests)

Listing is cheap (1 request per 1,000 files). The logging time costs one HEAD request per file (1M files:
~$0.40 and over an hour), so the scripts read it only where the question needs it and cache it per device
(`data/s3-inventory/<bucket>/<device>.json`; reruns only read new uploads):

- First look at an unknown bucket: `s3_inventory.py` without arguments (device folders only)
- Questions about one or a few devices: Always `--prefix <device>/`, never `--all`
- Fleet questions ("which devices uploaded", "is X still uploading"): `--all` (2 HEADs per device), or
  `canedge-s3-overview` for a report
- `--metadata` only when the question needs every file (e.g. "which files lack a Timestamp")
- `fetch_logs.py` lists the device itself: Order selections need no HEADs, time windows only for files uploaded
  since `--start` minus 1 day (a file is uploaded after it is logged)
- `HEAD GUARD` (> 50,000 HEADs): Tell the user the count, cost and time; add `--yes` only after they agree, or
  scope it down
- Data from the past with a recent cache: `--offline` / `--cached` (check the "cache synced" time)

Ask "logged or uploaded?" when a period question is unclear ("data from last week"): Logging time =
`fetch_logs.py --start/--end` (+ `--dry-run` for files/GB), upload time = `s3_inventory.py --upload-start/--upload-end`.

## Selecting log files

| The user asks for | Use |
|---|---|
| "first / last log file(s) of device X" | `--first N` / `--last N` (recording order: Session, then split) |
| "session 62", "the last file of session 62" | `--session 62` (+ `--last 1`) |
| "the data from 1 to 15 June" | `--start 2026-06-01 --end 2026-06-16` (logging time, needs `Timestamp` metadata; + the file that started before the start, as it may contain it) |
| A specific file | `--key <device>/<session>/<split>.MF4` |

Recording order is exact without any timestamps: The session counter increases at every power-on (and after
256 splits), the split counter restarts at 1 in each session. With cyclic logging, the oldest files may have
been deleted, so the first available file can be mid-session.

## Logging time vs upload time

- Logging time = the `Timestamp` metadata (UTC start of the file), set by the CANedge when it uploads
- Exact only from FW 01.09.01 (`fw` metadata). Older firmware: Approximate (may be local time and/or the file
  end; `Z` or not). The inventory counts these per device. Never expect them to match the first frame
- Time windows on approximate devices: Widen `--start/--end` by ~14 h plus one split, then check the exact times
  in the downloaded files (`fetch_logs.py` prints this hint)
- Files uploaded by other means (manual upload, bucket copy) usually have no `Timestamp`. They cannot be
  time-filtered: Say so, and select them by order (`--first/--last/--session`) instead
- `LastModified` is the UPLOAD (or copy) time: Never use it as logging time; it only answers upload questions
  ("is device X still uploading")
- The suffix in uploaded file names (`<split>-6501A2B3.MF4`) is ignored: Not a reliable time, and missing on
  files uploaded by other tools
- Duplicate uploads of the same session/split (lost upload confirmation) are downloaded once

## Workflow

1. `s3_inventory.py` (device folders), then `--prefix <device>/` for the devices in question (see above)
2. `fetch_logs.py ... --dry-run` first, show the user the file count, GB and egress estimate, then run without
   `--dry-run`. Above 1 GB the script stops (`SIZE GUARD`): Ask the user, then pass the size they agreed to as
   `--max-gb N` (one confirmation covers a whole device or a loop over devices), or narrow the selection
3. Add the printed `project.json` source snippet to the project (`canedge-workspace`), then hand over to
   e.g. `canedge-mdf2csv` / `canedge-raw-overview` (raw data) or `canedge-mdf2parquet` (decoding)

## Large downloads (a whole device or bucket)

- Downloading is rarely needed: Suggest the alternatives first (query the Parquet data lake with
  `canedge-lake-query --athena` / `--bigquery` / `--synapse`, or pick periods/sessions with `fetch_logs.py`)
- Suggest the data lake only if `lake_schema.py` shows tables for the devices in question (deployed is not decoded:
  Files uploaded before the deployment need the backlog processed, canedge-knowledge Parquet data lake backlog).
  Without tables, offer processing the backlog as the alternative to downloading
- AWS bills data transfer out of S3 (egress, ~$0.09/GB): Always state GB + egress estimate from the dry run (or
  `s3_inventory.py --all`, which needs no HEADs) before a large download
- Whole device: `fetch_logs.py --device X --dry-run`, then `--max-gb <agreed GB>`. Whole bucket: Loop over the
  devices with the same `--max-gb`, or for 100s of GB suggest `aws s3 sync s3://<bucket> <folder>` (AWS CLI)
- Re-runs resume: Files already in `data/s3/` with the same size are skipped

## Rules

- Read-only: Never put/copy/delete objects, never change bucket settings
- Downloads go to `data/s3/` only, and are never moved, renamed or edited afterwards
- Empty bucket/prefix: Report it plainly (devices not uploading yet, wrong bucket or prefix)

## Credentials

The profile (`credentials/<input bucket>.env`, created with `setup_env.py`, see canedge-workspace conventions
section 7) holds the keys and the input bucket. All of these work: The data lake stack's programmatic access keys,
the device keys (only an S3 bucket, no data lake), a read-only IAM user (see references), Google's programmatic
access key file (via `gcs_s3.py`, no S3 keys needed) or device keys, and S3-compatible servers (endpoint in the
profile). The output bucket is `<input>-parquet`.
Several buckets: One profile each, every script takes `--env <name>`; the project's S3 source notes it (`"env"`).

## Related

- **CANcloud** (CSS Electronics web tool): Browsing and downloading single files, device status, OTA config/FW
  updates incl. the batch manager. This skill covers the cross-device and selection questions
- `canedge-s3-overview` (bucket overview report), `canedge-workspace` (projects, sources)
- `canedge-s3-migrate` (archive/move log files to another bucket, e.g. Glacier: The writing counterpart, run
  by the user in AWS CloudShell)
- `canedge-s3-sd-upload` (upload SD-card files with the CANedge S3 metadata, e.g. for files "without Timestamp")
- `canedge-lake-query` (query the decoded Parquet data lake instead of downloading MF4 files)
- `canedge-knowledge` (Docs `[S3 management]:` section: Object naming, metadata, upload behaviour)
