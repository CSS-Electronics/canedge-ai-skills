---
name: canedge-s3-migrate
description: >
  Archive or migrate CANedge log files between S3 buckets, server-side in AWS CloudShell: Select by logging-time
  cutoff per device, copy (optionally to Glacier etc.), verify, optionally delete the verified originals. Use
  for "archive all data older than X", "move old log files to Glacier", "free up my input bucket", "migrate
  devices to another bucket". SD-card uploads: canedge-s3-sd-upload.
---

# canedge-s3-migrate

Plans and guides S3-to-S3 migrations of CANedge log files. The work runs in **AWS CloudShell** under the user's
console login: The agent never copies or deletes S3 objects itself. All data stays in AWS (server-side copy), so
requests cost cents even for 100s of GB. Shared rules: [conventions.md](../canedge-workspace/references/conventions.md).
Details: [references/selection.md](references/selection.md) (which files get selected and why),
[references/archive-bucket.md](references/archive-bucket.md) (archive bucket, storage classes, costs, side effects).

## Script

`scripts/s3_migrate.py` - one self-contained file (Python 3.9+, boto3) that the user uploads to CloudShell.
Every step reads and updates one manifest (`migrate-<utc>.json` + `.csv`):

| Step | Does | Writes to S3 |
|---|---|---|
| `plan --src B --dst B2 --before <UTC> \| --all [--devices ..] [--exclude-devices ..] [--storage-class C] [--include-other]` | List, HEAD the files uploaded after the cutoff, select, per-device summary, warnings, cost estimate | No |
| `copy --manifest M [--limit N]` | Server-side copy, same keys, metadata kept (or rebuilt); skips files already copied | Destination |
| `verify --manifest M` | Size, ETag, storage class (+ metadata where `plan` read it) of each copy -> `verified` | No |
| `delete-source --manifest M --confirm-bucket <src> [--limit N]` | Deletes only verified log files that are unchanged since the plan | Source (deletes) |

`--help` lists all options. Offline test of a selection: `plan --inventory data/s3-inventory/<bucket>` (the canedge-s3 cache).

## Workflow

1. Clarify: Source bucket, cutoff date (UTC) or all, devices (all/some/excluded), destination bucket, storage
   class, copy only or move (copy + delete). Explain the side effects up front (archive-bucket.md)
2. Optional preview with `canedge-s3` (`s3_inventory.py`, read-only, local): Devices, files, GB, logging ranges
3. Destination bucket: If it does not exist, give the user the CloudShell commands from archive-bucket.md
   (same region, public access blocked). Help pick the storage class
4. User: CloudShell (console, same region) -> *Actions* -> *Upload file* -> `s3_migrate.py`, then run `plan`
   and paste the summary. Review it together: Counts/GB per device, archived range, kept reasons,
   `undetermined` files, clock flags, warnings, cost
5. `copy --limit 5` -> `verify` -> check the summary -> full `copy` -> `verify` (re-run `copy` + `verify` after
   failures or a closed CloudShell session: Both resume)
6. Move only: After an explicit user OK, `delete-source --confirm-bucket <src>` (optionally `--limit 5` first)
7. Optional check: `canedge-s3` inventory of the destination (needs read credentials for it)

## Rules

- Never run `copy` or `delete-source` yourself, and never with the profile keys: The user runs them in CloudShell
- Always plan -> review -> test copy -> copy -> verify; delete only verified copies, after an explicit user OK
- Logging time = `Timestamp` metadata; never `LastModified` (upload time) or the key suffix. `LastModified`
  only proves "uploaded before the cutoff" (selection.md). Never read log files on S3
- Never select data newer than the cutoff: Keep the cutoff rule from selection.md, do not "fix" kept files by hand
- Non-log objects (device.json, configs, firmware, DBCs) are copied only with `--include-other` and never deleted
- Never print credentials; the manifest holds keys + metadata only

## Related

- `canedge-s3` (read-only inventory + downloads), `canedge-s3-overview` (bucket overview report)
- `canedge-knowledge` (S3 object naming + metadata), `canedge-lake-query` (the Parquet data lake is not affected)
- **CANcloud**: Browse the archive bucket by logging into it (e.g. with a partial Configuration File holding its
  S3 details)
