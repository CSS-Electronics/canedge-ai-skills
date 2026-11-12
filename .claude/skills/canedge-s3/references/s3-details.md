# canedge-s3 details

## S3 layout + object metadata

- Log files: `<device>/<session>/<split>[-<suffix>].<ext>`, device = 8 hex chars, session/split = 8 digits,
  ext `MF4` / `MFC` / `MFE` / `MFM`. Example: `12345678/00000086/00000003-6501A2B3.MF4`. The CANedge2/3 adds
  the suffix on upload (hex upload time). The skills ignore it: It is a technicality, not a reliable time, and
  missing on files uploaded by other tools (e.g. SD-card imports keep `<split>.MF4`)
- A lost upload confirmation makes the device upload the same file again: Same session/split, different suffix.
  Treat these as duplicates (the inventory counts them, `fetch_logs.py` downloads one copy)
- Other objects (listed separately): `<device>/device.json` (device status, re-uploaded at every heartbeat),
  `<device>/config-XX.YY.json`, firmware/OTA files, and in the bucket root the data lake files (`canX-*.dbc`,
  `dbc-groups.json`, `events.json`, `custom-messages.json`, `aggregations.json`, `backlog.json`, ...): Checked and
  written with `canedge-lake-admin` (this skill only reads them)
- CANedge2/CANedge3 uploads carry user metadata (boto3 returns the keys lower-cased in `head_object` `Metadata`):

| Key | Meaning |
|---|---|
| `timestamp` | The file-created (= logging start) UTC time, format `20260616T013000Z` (FW 01.09.01+, see below) |
| `fw` / `hw` | Firmware / hardware version of the device |
| `net` | Network used for the upload (e.g. WiFi/LTE) |
| `put-index` | Upload index |

- Files uploaded by other tools (manual upload, bucket copy/import) may lack the metadata. The CSS
  `s3-upload-log-files` example (api-examples repo) and `canedge-s3-sd-upload` set the same keys, with `Timestamp` = the first frame
- `LastModified` = upload (or copy) time. It is **never** used as logging time

## `timestamp` across firmware versions

Only FW 01.09.01+ writes a consistent value. Older firmware (per the firmware changelog):

| FW | `timestamp` |
|---|---|
| < 01.02.03 | Upload time |
| 01.02.03 - 01.03.00 | File modified time (~ file end), `...Z` |
| 01.03.01 - 01.08.00 | File modified time, local time, no `Z` |
| 01.08.01 - 01.08.04 | File modified time, with `Z` but still local time |
| 01.08.05 | File-created, with `Z` but still local time |
| 01.09.01+ | File-created, UTC, `Z` |

- The scripts read every value as written (with or without `Z`, as if UTC) and never fail on the format. They
  do not correct values per firmware
- `s3_inventory.timestamp_quality()`: `exact` = `fw` >= 01.09.01, or `net` = `sd_manual` (set by an SD upload
  script from the first frame); `approx` = older or unknown `fw`. A `Z` alone does not prove UTC
- `approx` values can be off by the time zone (up to +/-14 h) and may mark the file END. Do not expect them to
  match the first frame in the file; widen time windows by ~14 h plus one split, then take exact times from the
  downloaded files (MF4 header + frames)
- `exact` values match the first frame within seconds (the file is created just before its first frame; on a
  quiet bus the first frame can come later)

## Logging time and order (per object)

- Logging time = `timestamp` metadata only. The skills never read inside log files on S3 (compressed/encrypted
  files cannot be parsed, and the MF4 header time is the SESSION start, not the split start)
- Without `timestamp`: The file is counted, ordered by session/split, but excluded from time filters and
  heatmaps (reported as a count)
- Recording order = (session, split). The session counter increases at every power-on and when a session
  reaches 256 splits; it jumps to 50000000 if the device's backup battery was replaced (still ascending)

## Inventory cache: `data/s3-inventory/<bucket>/`

One file per device (`<device>.json`: Its log files, `device.json`, configs) plus `_bucket.json` (root objects,
e.g. DBCs), so single-device work never loads the fleet:

```json
{"schema": "canedge-s3-inventory/3", "bucket": "...", "endpoint": null, "synced_utc": "2026-10-30T09:25:33Z",
 "objects": {"<key>": {"size": 123, "etag": "...", "last_modified": "2026-01-15T01:12:00Z",
                        "kind": "log", "device": "12345678", "session": 86, "split": 3, "format": "MF4",
                        "metadata": {"timestamp": "20260616T013000Z", "fw": "01.09.01", "hw": "00.03", "net": "...", "put-index": "3"}}},
 "device_json": {"etag": "...", "as_of": "2026-10-29T08:15:00Z", "log_meta": "Truck1", "fw_ver": "01.09.01",
                 "cfg_name": "config-01.09.json", "cfg_crc32": "9ECC0C10", "space_used_mb": "36/7572",
                 "sd_used_lifespan": "2", "cfg_s3": {"etag": "...", "crc32": "9ECC0C10", "last_modified": "..."}}}
```

- `update_inventory()` = listing only. `head_objects()` reads the metadata of given keys (16 threads) and is
  called only for what a question needs: `edge_keys()` (first/last file per device), files with
  `uploaded_since()` a time, or all (`--metadata`). An object without a `metadata` key has not been read yet
  (`{}` = read, no metadata)
- `uploaded_since(obj, t)`: An object uploaded before `t` minus 1 day was logged before `t`. LastModified is
  only used as this bound, never as logging time. On copied/imported buckets (LastModified = copy time) it
  skips nothing, but stays correct
- Incremental: Keys that are new or re-written (ETag or LastModified changed) lose their cached metadata; keys
  that disappeared under the listed prefix are dropped. `--refresh` forgets all cached metadata under the prefix
- `HEAD_GUARD` (50,000): `--metadata` and the S3 overview stop above this and print count, cost and time
- `device_json` keeps a whitelist: `log_meta`, `fw_ver`, `cfg_name`, `cfg_crc32`, `space_used_mb`,
  `sd_used_lifespan` (never the public key, ICCID etc.), with `as_of` = the `device.json` LastModified (= the last
  heartbeat). Plus `cfg_s3` = the CRC32 of `<device>/<cfg_name>` in S3 (re-read when its ETag changes): Equal to
  `cfg_crc32` = config synced, as in CANcloud. It is a snapshot of the device's current state: After a config
  update, older log files may carry another meta name (and config CRC32). Per-file meta + config history: The MF4 headers
  (`canedge-raw-overview`). Firmware per file: The `fw` object metadata
- Other skills (e.g. `canedge-s3-overview`, `canedge-s3-migrate --inventory`) may read the cache, but only the
  `s3_inventory.py` functions write it

## Credentials + permissions

- Default: The profile's keys (the data lake stack's programmatic access keys, or the device keys). The scripts only
  call `ListObjectsV2`, `HeadObject` and `GetObject` (downloads, `device.json`, the config CRC32 check, `s3_object.py`)
- Safer: A separate IAM user with the read-only policy from the CANedge Intro
  ([aws-s3-user-policy-bucket-access-read-only.json](https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/transfer/s3-policies/aws-s3-user-policy-bucket-access-read-only.json):
  `s3:Get*` + `s3:List*` on the bucket). Not usable as the device's own upload user
- S3-compatible servers (MinIO, Wasabi, ...) and Google Cloud Storage: `setup_env.py --cloud s3` / `--cloud google` (endpoint in the profile)

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `NoSuchBucket` / `AccessDenied` on list | Wrong bucket/region, or the user's policy does not cover this bucket |
| `AccessDenied` on head/get only | Policy allows `s3:ListBucket` but not `s3:GetObject` |
| Many files "without Timestamp" | Files uploaded/copied by another tool: No logging time, select them by session/split (`--first/--last/--session`) |
| Device shows big upload delays | Poor connectivity (CANedge uploads when online), or the bucket was imported/copied |
| Device missing although it logs | Check its S3 config/connectivity in CANcloud (device.json `last_modified`) |
