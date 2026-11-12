# CANedge data lake in Google BigQuery

Source: The CANedge Google data lake deployment (Parquet bucket in Cloud Storage + a BigQuery dataset) and the
official Grafana-BigQuery dashboard template (see the Parquet data lake pages of the CANedge Intro in
`canedge-knowledge`).

## Credentials

- The data lake deployment creates a BigQuery service account (the one Grafana uses) with a JSON key. The user
  copies that key file into `credentials/` (gitignored). One service account key there is used automatically;
  several: Set `CANEDGE_GOOGLE_KEY_FILE` in a data lake profile `credentials/<name>.env` (`--env <name>`). Normally
  `setup_env.py --key-file <key file> --bucket <input bucket>` does this (conventions section 7)
- Project = the key's `project_id` (override: `CANEDGE_BIGQUERY_PROJECT`). Dataset = the project's only dataset
  (several: `CANEDGE_BIGQUERY_DATASET`, the `dataset` variable of the Grafana dashboard)
- Never open the key file with a read tool, never print it. `lake_query.py --bigquery sql "..." --dry-run` shows
  whether a key was found (path only)

## Tables

| Table | Content |
|---|---|
| `tbl_<DEVICE>_<Message>` | One external table per device + message folder (`gs://<bucket>-parquet/<DEVICE>/<Message>/*`). Names keep their case in BigQuery; the skill uses the same lower-case names as Athena and maps them |
| `tbl_<DEVICE>_messages` | Column `MessageName`: The device's message tables (Grafana dropdown) |
| `tbl_aggregations_devicemeta` | `DeviceId`, `MetaName` (no partition): The device names of `lake_schema.py` and `devices()` |
| `tbl_aggregations_tripsummary` / `_events` | Optional trip summary / event detection outputs (same columns as on Athena) |

- Columns: `t` (TIMESTAMP, UTC, microseconds) + one FLOAT64 per signal; column names are case-insensitive
- No `date_created` column (no hive partitions). The skill derives it as `FORMAT_TIMESTAMP('%Y/%m/%d', t)`, so the
  shared SQL works; the time filter that matters is the one on `t`, as in the Grafana template
- Schema: `INFORMATION_SCHEMA.COLUMNS` (one query, cached 24 h in `data/bigquery-schema/`, `--refresh`)

## SQL translation

Write the same Athena (Trino) SQL as for the other backends. `_bigquery.py` translates it with sqlglot and fixes:

| Athena | BigQuery |
|---|---|
| `from_unixtime(x)` / `to_unixtime(t)` | `TIMESTAMP_MICROS(CAST(ROUND(x * 1e6) AS INT64))` / `UNIX_MICROS(t) / 1e6` (sub-second buckets stay exact) |
| `TIMESTAMP '2026-01-15 08:00:00'` | `TIMESTAMP('2026-01-15 08:00:00')` (UTC, comparable with `t`) |
| `date_created` | `FORMAT_TIMESTAMP('%Y/%m/%d', t)` |
| `tbl_12345678_can1_eec1_s00` | `` `<dataset>.tbl_12345678_CAN1_EEC1_S00` `` (real case) |
| `approx_percentile(x, 0.95)`, `date_trunc('hour', t)`, `date_diff('millisecond', a, b)`, `max_by` | `APPROX_QUANTILES(x, 20)[19]`, `TIMESTAMP_TRUNC(t, HOUR)`, `DATE_DIFF(b, a, MILLISECOND)`, `MAX_BY` (sqlglot) |

`--dry-run` prints the translated SQL; a failed query prints it too (also the way to get BigQuery SQL for a Grafana
panel: `canedge-grafana` builds the standard panels itself and checks names + case against the lake). Verified against Athena on the same data: All
sql-patterns.md blocks run, and resample results (1 min, 1 s, 100 ms buckets) are identical.

## Costs + limits

- On-demand pricing: $6.25 per TiB billed, minimum 10 MB per table and query (a `fleet()` batch over 50 tables
  bills at least 500 MB). Only the selected columns are read
- The printed cost is a rough estimate from the billed bytes. Cloud Storage reads of the Parquet files are billed
  separately and are not included; with many small files they can be the larger part. Prefer few, well-planned
  queries (`summary` once, then analyze the saved result)
- Request guard (`summary`/`fleet()`): As on Athena (~$1 per million files read, Cloud Storage class B reads),
  files counted by listing the external tables' source folders with the same key; above $1 per run it stops for
  the user's OK
- The scan budget (default 10 GB per `connect()`) is passed as `maximum_bytes_billed`: A query that would exceed it
  fails without cost
- Duplicate Parquet files in the bucket (e.g. a re-uploaded backlog) show up as duplicate rows. If counts look too
  high, compare `count(*)` with `count(DISTINCT t)`
