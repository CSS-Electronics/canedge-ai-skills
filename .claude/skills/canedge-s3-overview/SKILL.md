---
name: canedge-s3-overview
description: >
  Interactive dashboard of a CANedge S3 bucket: Devices, log files + GB per logging and upload day, devices that
  stopped logging or uploading, firmware and config sync, non-log objects. Use for "give me an overview of my S3
  bucket", "which devices were active last month", "are any devices no longer uploading", "which firmware do my
  devices run". Listing or downloading files: canedge-s3.
---

# canedge-s3-overview

Builds ONE self-contained `report.html` dashboard (+ `report.json` with the same numbers for you) from the canedge-s3
inventory cache. Read-only on S3, log files are never downloaded. Requires `canedge-s3` (`s3_inventory.py`) and
`canedge-reports` (dashboard kit), both imported via sys.path. Details: [references/overview-details.md](references/overview-details.md).

## Script

```
.venv\Scripts\python.exe .claude/skills/canedge-s3-overview/scripts/s3_overview.py [--days 30] [--window DAYS]
    [--upload-delays] [--project projects/<name> | --out <folder>] [--offline] [--yes] [--env <name>] [--as-of <UTC>] [--tz <IANA>]
```

Updates the inventory first unless `--offline`: Lists the bucket, then reads the metadata of the first/last file
per device and of the files uploaded in the window (cached, reruns only read new uploads). Default window: The
whole history up to 50,000 log files, else the last 90 days. Above 50,000 HEAD requests it stops with
`HEAD GUARD` (count, cost, time): Ask the user, then add `--yes`, or narrow with `--prefix` / `--window`.
`--json` prints the key numbers.

Tabs: Overview (KPIs: Scope, Logging, Uploads, Device status), Devices (searchable table, status badges), Activity
(GB per device and logging day + logged vs uploaded per day), Meta (device.json status as in CANcloud: FW, config
CRC32 + sync, SD used/wear, last heartbeat; firmware history from the log files), Objects (non-log files).
Every device folder is listed, also devices that only upload device.json (no log files).

## Workflow

1. Run the script (with `--project` if a project exists)
2. Summarize the key numbers in chat (devices, not logging / not uploading, config not synced), ending with the bold
   **Open dashboard** link from the printed `OPEN` line. Answer follow-up questions from `report.json`
3. For files of a specific device or period: Hand over to `canedge-s3` (`fetch_logs.py`)
4. A device that stopped logging or uploading, or a config that is not synced: `canedge-troubleshoot`

## Rules

- Keep "logged" (the `Timestamp` metadata = when the data was recorded) and "uploaded" (S3 `LastModified`)
  apart in every answer: A device can log without uploading (no connectivity) or upload old data late
- Files without `Timestamp` (manual uploads, bucket copies) are counted but have no logging day: Say so, they are
  not "missing data"
- Days without data are normal for loggers that only run with the ignition: Describe them neutrally
- Copied/imported buckets: `LastModified` is the copy time, so upload figures show the copy, not the device
- Read-only: Never write to the bucket
