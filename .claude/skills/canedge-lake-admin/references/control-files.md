# Control files: What the automation does with them

The CANedge Intro describes each feature and its JSON syntax (`canedge-knowledge`, sections below). This file adds
what the code does (automation v4.3.2, `modules/` of canedge-mdftoparquet-automation) and how to fill the files in
reliably. `control_files.py check` runs the same validation code as the functions.

All files are optional and live in the input bucket root: Missing = feature off. Present but invalid = the function
logs `CONTROL FILE ERROR: <file>: <location>: <problem>` and fails for every new log file (v4.3.0+), so nothing is
decoded until the file is fixed or removed. The backlog and aggregation jobs fail the same way.

| File | Intro section | Read by | When it takes effect |
|---|---|---|---|
| `events.json` | `[Add event detection with email notifications]:` | per-file function + backlog (no emails in backlog) | Next uploaded log file |
| `custom-messages.json` | `[Add calculated signals to your data lake]:` | per-file function + backlog | Next uploaded log file |
| `geofences.json` | same (built-in function `custom_geofences`) | via custom-messages.json | Next uploaded log file |
| `dbc-groups.json` | `[Use device specific DBC files]:` | per-file function + backlog | Next uploaded log file |
| `routing.json` | `[Route data to client-specific output buckets]:` | per-file function + backlog, **Amazon only** | Next uploaded log file |
| `aggregations.json` | `[Add trip summaries to your data lake]:` | aggregation job only | Next aggregation run |
| `backlog.json` | `[Process backlog of MDF files]:` | backlog job only | Next backlog run |
| root `can<n>-*.dbc` / `lin<n>-*.dbc` | `[Parquet data lake]:` (DBC prefixes) | per-file function + backlog | Next uploaded log file |

## Names: Use the lake's folder names and the DBC's signal names

- Messages are the Parquet folder names in the output bucket: `CAN<channel>_<DBC message name>`, plus suffixes the
  decoder adds (J1939 source address `_S00`, multiplexor values e.g. `_S_M62_R_M101`), e.g. `CAN1_EEC1_S00`. Exact case.
  The Athena/BigQuery table names (`tbl_<device>_can1_eec1_s00`) are lower case: Never copy those into a file
- Signals are DBC signal names, exact case (`Speed`, not `speed`). A wrong signal in events.json stops the function
  for every log file that contains the message; in aggregations.json the signal is skipped silently
- Device IDs: 8 upper-case hex characters (`2F6913DB`)
- Times and dates: UTC; `from_date`/`start_date`/`end_date` as `YYYY-MM-DD`; raster as `1s`, `0.2s`, `500ms`, `1min`

## events.json

- `messages_match_type`: `equals` with a list of lists (`[["CAN9_GnssSpeed"]]`; every listed message is evaluated
  on its own) or `contains` with a string (`"_DM01_DTC_01"`: all messages whose name contains it)
- Rising edge = signal `>= upper_threshold` after it was `<= lower_threshold` earlier **in the same log file** (no
  state between files). Hysteresis gap for continuous signals; `exact_match: true` (== thresholds) for states
- One email per event and log file at most; backlog runs send none. Emails: AWS SNS topic
  `mdf-to-parquet-lambda-event-sns-<id>` (subscriptions in the SNS console), Google/Azure: alert on the log text
  `NEW EVENT`
- GPS on event rows needs `include_gps_data` + a raster (with `raster: ""` the GPS columns stay empty).
  `include_gps_data: false` needs v4.3.1+: On v4.3.0 and older every log file with an event fails
  (`KeyError 'Latitude'`) - use `true` there (the GPS columns stay empty without GPS data)
- Output: `aggregations/events/...` -> table `tbl_aggregations_events`

## custom-messages.json

- Built-in functions: `combine_dtcs` (J1939 DM1 into one table), `custom_geofences` (+ geofences.json),
  `delta_distance`, `resample`. Other names need a customized function zip; without it the data passes through
- `equals` sets are only built when all listed messages are in the same log file; a raster joins them on time
  (without one the rows are stacked)
- `custom_message_name` becomes the table folder: Letters, digits, `_`, starting with a letter; unique

## aggregations.json

- `config.trip.trip_gap_min` and `trip_min_length_min` are required; trips are split at midnight (UTC)
- `device_clusters[].devices` must list the devices (an empty list processes none)
- Methods: `min max avg median sum first last delta_sum delta_sum_pos delta_sum_neg`
- `previous_day` for a daily schedule, `specific_period` for one-off runs; output overwrites the day's file
- Reads only the default output bucket (not routed client buckets)

## backlog.json

- Entries: Device `2F6913DB/`, session `2F6913DB/00000001/` or a log file key; device IDs are upper-cased, other
  entries are ignored with a warning
- Batching: Sessions with fewer than `min` files are merged per device up to `max` files; larger sessions run alone
- Files processed before get a second Parquet file (duplicate rows): Delete their Parquet files first if needed
- Run limit: AWS = the Glue job's timeout (60 min in stacks before 2026-11-11, 180 min after; per run:
  `lake_ops.py start --timeout`), Google 60 min. ~33 GB of log files per hour (one decoder core): Split large
  backlogs (SKILL.md workflow 2); re-run the table mapping after new devices/messages

## dbc-groups.json, routing.json

- dbc-groups: `{"dbc_groups": [{"devices": [...], "dbc_files": ["can1-x.dbc"]}]}`; the first group with the device
  wins; devices in no group get all root DBCs. Invalid -> nothing is decoded. Workspace helper:
  `canedge-workspace` `dbc_groups.py check` (same format)
- dbc-groups `common_dbc_files` (optional, v4.4.0+): Top-level list of root DBCs added to the files of every device
  in a group (e.g. the 22 J1939-73 DM1 DBCs on CAN1 + CAN2), instead of repeating them per group. Older automations
  ignore the key silently (grouped devices decode without them): `check` reports it as an error there
- routing (Amazon): `mirror_to_default` must be `true`/`false` without quotes; client buckets
  `<input>-<client>-parquet` must exist (else the default bucket is used); `from_date` = recording date

## Testing before an upload

- `check` (always), then for events: a dry-run count on the lake (how many log files would have fired)
- A first real test: Upload the file, wait for the next device upload (or re-upload one recent log file of a test
  device with `canedge-s3-sd-upload`), then `logs --since 1h --grep "Processing event"`
  (the run should show `Wrote event Parquet file` or `No events found`, and on AWS `Published message ... SNS`)
- Backlog: Start with one session, check `runs` + `logs`, then the rest

## Lakes on older versions

v4.3.2: Same rules as v4.4.0, except dbc-groups.json `common_dbc_files` is ignored (no error, the grouped devices
just miss those DBCs; `check` reports it as an error) and unknown top-level keys give no warning.

v4.3.1: Same rules as v4.3.2, except dbc-groups.json only accepts single-digit channel prefixes (`can1-`, not the
CANmod.router `can11-`): A group with `can11-*.dbc` is a CONTROL FILE ERROR (`check` reports it as an error).

v4.3.0: As v4.3.1, and `include_gps_data: false` in events.json fails when an event fires (`check` reports it as
an error for these lakes).

v4.2.1 and older:

- Invalid JSON in events.json/custom-messages.json was skipped silently; structural errors failed with unclear
  errors (`KeyError`, `UnboundLocalError`)
- events.json `equals`: Only the first inner list was evaluated
- Backlog: Batching depended on the number of entries; ignored entries were not reported; Glue runs and Google HTTP
  calls showed success even when processing failed - read the logs, not the run state
- Missing optional files were logged as ERROR on Amazon (harmless noise)
- routing.json: `"mirror_to_default": "false"` (a string) counted as true

Recommend the update to the latest version (Intro: Update the stack / Terraform) before relying on these behaviors.
`status`/`check` read the version from the newest function zip in the bucket root: Confirm the stack (AWS) or
Terraform deployment (Google) actually uses it.
