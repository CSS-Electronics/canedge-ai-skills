# Troubleshooting the data lake automation

`lake_ops.py status` first, then the symptom. Report each finding with the script output that shows it.

## Resources (`<id>` = the lake ID of the profile)

| | Amazon | Google |
|---|---|---|
| Per-file function | Lambda `mdf-to-parquet-lambda-function-<id>` (S3 event on `.MF4/.MFC/.MFE/.MFM`, upper case) | Cloud Run function `<id>-mdf-to-parquet` (every new object; the code filters) |
| Function logs | CloudWatch `/aws/lambda/mdf-to-parquet-lambda-function-<id>` (created at the first run) | Log view `canedge-<id>` |
| Backlog / trip summary / table mapping | Glue jobs `<id>-process-backlog`, `<id>-process-aggregation`, `<id>-map-tables`; logs `/aws-glue/canedge-<id>/output` + `/error` (stream = run ID) | HTTP functions `<id>-mdf-to-parquet-backlog`, `<id>-aggregation`, `<id>-bq-map-tables` (Cloud Scheduler jobs, paused) |
| Error alerts | CloudWatch alarm `mdf-to-parquet-lambda-error-alarm-<id>` -> SNS `mdf-to-parquet-lambda-error-sns-<id>` (email) | none by default |
| Event emails | SNS `mdf-to-parquet-lambda-event-sns-<id>` | Log metric on `NEW EVENT` -> alert policy |

Azure (not supported by `lake_ops.py`: The `azure` profile holds only the Synapse SQL login for queries): Function
App `mdftoparquet-<id>` (logs: Application Insights, `traces | where customDimensions.Category ==
"Host.Function.Console"`), Container App Jobs `backlog-<id>`, `aggregation-<id>`, `synapse-map-tables-<id>` (Run now,
Execution history > Logs; console logs can lag up to 15 min), event emails via the Azure Monitor alert on `NEW EVENT`.
Give the user the Intro steps. Control files: `control_files.py` with the Flexify S3 profile of the input container
(it sees a generic S3 bucket, so tell the user yourself: `routing.json` has no effect on Azure, and the backlog /
aggregation jobs stop after 60 min: split large backlogs). Data checks: `canedge-lake-query --synapse`; new devices/messages need a
`synapse-map-tables-<id>` run.

## Reading a function log

Sections in order: `DOWNLOAD OBJECTS`, `DBC DECODE MDF TO PARQUET`, `ADD CUSTOM MESSAGES`, `DETECT EVENTS`,
`LOAD ROUTING CONFIG` (Amazon), `DO FINAL PROCESSING`. The last section before a failure shows where it failed.

| Line | Meaning |
|---|---|
| `Execution succeeded` | Parquet files uploaded |
| `Execution succeeded (no Parquet files decoded)` | The decoder found no DBC message in the file: Wrong DBC prefix/channel, DBC not for this bus, or no data (`canedge-mdf2parquet` to test locally) |
| `CONTROL FILE ERROR: <file>: ...` | A control file is invalid: Fix (`check` + `push`) or `remove` it; then re-process the affected files (backlog) |
| `No DBC files with valid prefix in input bucket` | No `can<n>-*.dbc` / `lin<n>-*.dbc` in the root (or not in the device's dbc-groups entry) |
| `MF4 decoding failed (returncode N)` | Decoder error: Encrypted file without `passwords.json`, damaged file, DBC syntax (`canedge-dbc` lint) |
| `Task timed out` / `Runtime exited` | Very large file or too little memory/time (stack settings; contact CSS) |
| `<file> not found - feature off` (INFO) | Normal: That optional control file is not used |
| `Routed upload failed - falling back to default bucket` | routing.json client bucket missing or not allowed |
| v4.2.1 and older: `Failed to download events.json ... (404)` at ERROR level | Harmless: The optional file is missing |

Failed AWS invocations are retried up to twice (asynchronous S3 events), and each failure triggers the error alarm email.

## Symptom -> checks

**A file is not in the lake** (`diagnose KEY` runs all of these):
1. Object exists, upper-case suffix, `<device>/<session>/<split>` path
2. Function log for the key: None = the function never saw it (uploaded before the deployment -> backlog; trigger
   missing); a result line = see the table above
3. Parquet `<session>_<split>.parquet` in the device's message folders (device ID from the log file header)
4. Tables in the catalog: Parquet present but no table -> `start --job map-tables` (on request)

**No (new) data in Grafana**: Run 1-4 for a recent file of the device; then `canedge-lake-query` `lake_schema.py`
for the device/message tables; then the dashboard: Time range where the data is, data source (`Amazon Athena` /
`Google BigQuery`), the device and message variables. New messages appear in Grafana only after the table mapping.
No new log files at all after a recent firmware/config update or bucket move: `canedge-ota` `status` (heartbeat,
pending firmware, config loaded) and `verify --plan` of that update.

**Events**: Never fire -> `logs --grep "Processing event"` (`No matching decoded files` = message name; `No events
found` = thresholds never crossed within one file); always fire -> thresholds/hysteresis, dry-run count; emails
missing (AWS) -> SNS subscription confirmed? Backlog runs never send emails. Stop a flood: `remove events.json`.

**Trip summary empty**: `runs --job aggregation` + `logs --source aggregation`: `No Parquet files found for <msg>` =
message name/period; no trips = `trip_identifier` message has no data that day or trips shorter than
`trip_min_length_min`. The table `tbl_aggregations_tripsummary` needs a table mapping run after the first output.

**Backlog did nothing / partly**: `logs --source backlog`: `entry ... ignored`, `BACKLOG: PROCESS BATCH i OF n`,
`Error processing batch i`. A FAILED run (v4.3.0+) means at least one batch failed: Its log lines say which.

**Jobs**: `runs` shows state + error. On v4.2.1 and older a SUCCEEDED run proves nothing - read the logs.
