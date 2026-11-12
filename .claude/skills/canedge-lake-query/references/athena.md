# CANedge data lake in Athena: Schema + setup facts

Source: CANedge data lake CloudFormation template (`data-lake-v4.2.1-vG.6.0.json`), the Glue/BigQuery/Synapse
mapping scripts and the official Grafana-Athena templates (see the Parquet data lake pages of the CANedge Intro in `canedge-knowledge`).

## AWS resources (stack outputs)

| Value | In the profile |
|---|---|
| Programmatic access key / secret (data lake stack outputs) | `CANEDGE_ACCESS_KEY_ID` / `_SECRET_ACCESS_KEY` (entered) |
| S3 input bucket | `CANEDGE_INPUT_BUCKET` (entered) |
| Region | `CANEDGE_REGION` (derived from the bucket) |
| UniqueID = lake ID | `CANEDGE_LAKE_ID` (derived from the keys' IAM user `S3LimitedAccessUser-<id>`) |
| Data source `AwsDataCatalog` | (fixed) |
| Database `dbparquet<uniqueid>`, workgroup `athenaworkgroup-<uniqueid>` | derived (`env.py` `lake()`) |

- Parquet data: `s3://<input-bucket>-parquet/`. Query results: `s3://<input-bucket>-parquet-query-results/`
  (enforced by the workgroup)
- The programmatic access keys carry the Athena user's policy: `StartQueryExecution`/`StopQueryExecution` in the workgroup,
  `GetQueryExecution`/`GetQueryResults`, Glue `GetDatabase(s)`/`GetTable(s)`/`GetPartition(s)`, read the Parquet
  bucket. They can also write the buckets and run the Glue jobs, but this skill only reads
- The Athena user's keys (e.g. Grafana's) also work in a profile: Queries only, the query-only option for team members (S3 and canedge-lake-admin scripts stop with a clear message)
- Tables are created by the Glue job `<uniqueid>-map-tables` (trigger `<uniqueid>-map-tables-on-demand`).
  New Parquet files for existing tables are queryable at once; NEW devices/messages need a new job run

## Tables

| Table | Content |
|---|---|
| `tbl_<device>_<message>` | One per device + message folder, lower case: `12345678/CAN9_GnssSpeed` -> `tbl_12345678_can9_gnssspeed`. Multiplexed messages get one table per multiplexor value (suffix on the message name) |
| `tbl_<device>_messages` | Column `messagename`: List of the device's message tables (used by Grafana dropdowns) |
| `tbl_aggregations_devicemeta` | `deviceid`, `metaname` (`log_meta (DEVICEID)` from `device.json`), single partition `2024/01/01`: The device names shown by `lake_schema.py` and `devices()` |
| `tbl_aggregations_tripsummary` | Optional trip summaries (rarely deployed): `deviceid, message, signal, aggregation, signalvalue, signalcount, duration, tripstart, tripend, tripid, cluster` |
| `tbl_aggregations_events` | Optional event detection: `t, eventname, deviceid, eventid, message, signal, eventtype, eventvalue (1 start / 0 stop), signalvalue, latitude, longitude` |
| `tbl_aggregations_<name>` | Other custom aggregation/calculated tables |

Columns of message tables:
- `t` - timestamp (UTC, microseconds), the frame time
- One `double` column per DBC signal, lower case (Athena/Glue lower-cases identifiers). Values outside the
  DBC min/max are NULL; rows with all signals NULL are not written
- `date_created` - partition column, a STRING `'yyyy/MM/dd'` (from the S3 path `.../<yyyy>/<mm>/<dd>/`).
  Always filter on it: Athena prunes partitions only via this column, not via `t`

Device IDs in table names are lower case; in `deviceid` columns the case varies by deployment. Compare with
`upper(deviceid) = '12345678'`.

Schema discovery uses the Glue catalog (free, no Athena query): The same device, message and signal lists the Grafana
dropdowns get via SQL (`tbl_aggregations_devicemeta`, `tbl_<device>_messages`, `SHOW COLUMNS`).

## Costs + limits

- The printed cost is a rough scan-cost estimate. Athena reads every Parquet file of the selected partitions from
  S3, and those S3 GET requests are billed separately (not included); with many small files (one per message and
  log file) they can be the larger part. Fewer, broader queries over tight `date_created` windows keep both low
- Request guard (`summary`/`fleet()`): ~2 GETs per Parquet file ($0.0004 per 1,000) + 1 LIST per table and day,
  so roughly $1 per million files read. Files are counted by listing one message folder per sampled device (up to
  5; the profile's keys may list the Parquet bucket). Above $1 per run it stops for the user's OK
- Athena bills $5 per TB scanned (rounded up per MB, minimum 10 MB per query). Parquet is columnar: Only the
  selected columns of the selected partitions are scanned
- Result reuse: The same SQL within 24 h returns the stored result (0 MB scanned, free); `--no-reuse` /
  `connect(..., reuse_minutes=0)` when new data must be included (e.g. today's uploads)
- Max query string 262,144 bytes (relevant for generated UNION ALL over hundreds of devices: batch them)
- The skill never changes the data lake: No DDL/DML. Missing tables come from the Glue mapping job
  `<uniqueid>-map-tables`
- Grafana macros (`$__timeFilter`, `${device}`, ...) only work in Grafana, not in `lake_query.py` (all backends refuse them)
