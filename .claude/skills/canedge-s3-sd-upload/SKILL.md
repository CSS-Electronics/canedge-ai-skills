---
name: canedge-s3-sd-upload
description: >
  Upload CANedge log files from an SD card (or a local copy) to an S3 bucket with the CANedge S3 metadata
  (Timestamp from the first frame, Fw, Hw, Net, Put-Index), so they work like files the device uploaded itself:
  Time filters, the S3 overview and data lake (backlog) processing. Use for "upload my SD card data to S3",
  "import CANedge1 data into my bucket", "add the SD files of device X to the data lake", "my manually uploaded
  files have no Timestamp".
---

# canedge-s3-sd-upload

Reads each local MF4 file (header + first frame) and uploads it to `<device>/<session>/<split>.MF4` with the
metadata keys a CANedge2/3 sets on upload. Files the device already uploaded itself are skipped. Local files are
only read, never moved or changed. Shared rules: [conventions.md](../canedge-workspace/references/conventions.md).
Requires `canedge-s3` (S3 client + key parsing, imported via sys.path).

## Script

Run from the repo root with `.venv\Scripts\python.exe .claude/skills/canedge-s3-sd-upload/scripts/sd_upload.py`:

| Step | Does | Who runs it |
|---|---|---|
| `plan --src <SD card or folder> [--devices ..] [--dst <bucket>]` | Reads the local files, writes `data/sd-upload/sd-upload-<utc>.json` + `.csv`, prints files/GB/logging range per device | Agent (no S3 calls) |
| `upload --manifest M [--limit 5]` | Lists the device folders in the bucket once, uploads the files not in it yet | User |
| `verify --manifest M` | HEAD per uploaded file: Size + metadata | Agent or user (read-only) |

Metadata (as the CSS api-examples `upload_sd_to_s3.py`): `Hw` = hardware version + `/00.00`, `Fw`,
`Net` = `sd_manual`, `Timestamp` = first frame (UTC, exact for any firmware), `Put-Index` = rank of the file
in the device's recording order. `canedge-s3` treats `Net: sd_manual` Timestamps as exact.

## Workflow

1. Clarify: SD card / folder, devices, destination bucket (default: the profile's S3 input bucket). The `--src` folder can
   be the SD card root or its `LOG` folder: Files are found by their `<device>/<session>/<split>.MF4` path
2. Run `plan` and review the summary with the user: Files per device, logging range, files not uploaded
   (see below)
3. The user runs `upload --limit 5`, then `verify`, then `upload` (resumes: Re-run after failures), then
   `verify`. Credentials: The profile (`--env <name>`), with `s3:PutObject` (the data lake stack's programmatic
   access keys or the device keys; a read-only IAM user cannot upload)
4. Optional check: `canedge-s3` `s3_inventory.py --prefix <device>/`

## Files not uploaded

| Status | Why | What to do |
|---|---|---|
| `unsupported` | MFC/MFE/MFM (compressed/encrypted): The first frame cannot be read | Decompress/decrypt to MF4 with `mdf2mdf` (CSS MF4 converters, see `canedge-mdf2csv`) into a new folder, then plan that folder |
| `unreadable` | MF4 header does not match the file path (renamed folder), or the file is damaged/empty | Check the file; never rename log files to make them match |
| `in bucket` (at upload) | The same session/split is already in the bucket (e.g. uploaded by the device) | Nothing: Not uploaded twice |

## Rules

- Never run `upload` yourself: Give the user the command. The agent only plans and verifies
- Never move, rename or edit the local log files
- Uploading into a data lake input bucket triggers the MF4-to-Parquet automation for each file (as a device
  upload would): Tell the user before a large upload
- For S3-to-S3 moves (archive, migration) use `canedge-s3-migrate`: It never reads inside log files on S3
