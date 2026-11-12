# canedge-s3-overview details

## S3 requests (`s3_overview.py`)

- One listing of the bucket/prefix, then a HEAD (metadata) for the first + last log file per device and for every
  file uploaded since the window start minus 1 day. Older uploads were logged before the window: Counted in the
  bucket totals (files, GB) without a HEAD, so they have no logging day, and firmware covers the files read only
- Window (`--window`): Default the whole history if the bucket/prefix has <= 50,000 log files, else 90 days (if all
  data is older, the last 90 days with data). Above 50,000 HEADs: `HEAD GUARD`, ask the user, then `--yes`

## Figures

- Logging time = the `Timestamp` metadata (file start). Logged per day = files/GB by the UTC day their logging
  started; a file spanning midnight counts on its start day
- Upload time = S3 `LastModified`. Uploaded per day = files/GB by the UTC day they arrived
- Not logging (`--days`, 30): No log file started in the N days before the reference time (`--as-of`, default
  now). Not uploading: No upload in those N days. Both are per device, whatever the selected period
- Devices table: Files/size/Days in the selected period (logging time); Last logged + Upload age over all data. FW =
  the newest file's firmware, `+N` = more versions (hover; the Meta tab lists all with first/last logged)
- Status tags (short, lower case): `no logs`, `no uploads` (nothing in the `--days` before the reference time),
  `not synced` (config). Upload age / Heartbeat age (Meta tab): Time to the report time as a bar, days (or hours if
  all devices are within 48 h), 1 decimal, the date on hover. Units, size (MB/GB) and optional columns follow all
  devices, not the filter, so columns do not move when filtering
- Upload delay (`--upload-delays`, Devices column): Median `LastModified - Timestamp`. Includes the file's own
  length; meaningless on copied buckets
- Device status (KPIs + Meta tab), from device.json (a snapshot as of the device's last heartbeat = its
  LastModified): Firmware the devices run now, config sync (CRC32 of `<device>/<cfg_name>` in S3 = the device's
  `cfg_crc32`), SD used % (`space_used_mb`), SD wear % (`sd_used_lifespan`), meta name. Devices without device.json
  show "-"; firmware then falls back to the newest log file
- Not shown in the dashboard, only in `report.json` (for you): Files with an approximate Timestamp (FW < 01.09.01,
  local time and/or the file end; legacy, most fleets run newer firmware) and duplicate uploads. Files without
  Timestamp: A Devices column only when there are any

## Output

- `report.html` (dashboard, single file, works offline) + `report.json` (`summary` + per-device rows)
- Default folder: `projects/<name>/reports/<yyyymmdd>-s3-overview/` with `--project`, else
  `TEMP/reports/<yyyymmdd>-s3-overview-<bucket>/`
