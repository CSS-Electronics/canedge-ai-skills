---
name: canedge-lake-query
description: >
  SQL on a CANedge Parquet data lake (DBC-decoded signals): Local via DuckDB, or the cloud lake via Amazon
  Athena, Google BigQuery or Azure Synapse, with the same tables and SQL. Schema + DBC meaning per device,
  resampled signals (the Grafana panel query), stats, episodes, duty cycles and fleet summaries with cost guards.
  Use for "average/max of signal X in period Y", "what happened at <time>", "which signals are available", "query
  my data lake", "Athena", "BigQuery", "Synapse", "Azure data lake". Analysis pages: canedge-lake-analysis.
---

# canedge-lake-query

One query layer for every place a CANedge Parquet data lake lives: A local folder (DuckDB), the AWS data lake
(Athena), the Google data lake (BigQuery) and the Azure data lake (Synapse serverless SQL). The table names match
(`tbl_<device>_<message>`), and the SQL is written in the Athena dialect, so a query tested locally runs on Athena,
on BigQuery and Synapse (translated) and in Grafana panels. The point is to move the compute to the data: Scan GBs,
return KBs. Shared rules:
[conventions.md](../canedge-workspace/references/conventions.md) (projects, credentials, UTC).

Run from the repo root with `.venv\Scripts\python.exe` (`.venv/bin/python` on macOS/Linux). Data source (one of):
`--project projects/<name>` or `--lake <folder>` (local), `--athena`, `--bigquery` or `--synapse` (cloud, `--env
<name>` for the data lake profile `credentials/<name>.env`; default: the only profile). `--signals` / `--signal` take column names
or `message.signal`, the message with its channel prefix (e.g. `can1_eec1.enginespeed`).

## Scripts

| Script | Use it for |
|---|---|
| `scripts/lake_schema.py` | Overview of devices (with their meta names), message tables and aggregation tables (cloud: schema cached 24 h) |
| `scripts/lake_schema.py --catalog --device X [--dbc <p>] [--message M] [--start --end]` | What a device has + what it means: Its lake tables with the DBC message (ID, comment) and per signal unit, value table, comment; with a window the values per signal (all-NULL signals marked) |
| `scripts/lake_schema.py --find <signal>` / `--describe <table>` / `--tables [--device X]` | Which tables/devices contain a signal (with `--dbc` also via DBC comments, units, value labels); columns of a table; all tables per device |
| `scripts/lake_schema.py --stats <table> [--start --end]` | Rows, time span and per signal count, NULL share, min/max/mean, distinct values (one query; Athena needs a window) |
| `scripts/lake_query.py resample --device X --message M --signals a,b --start .. --end ..` | The core extraction: Signals resampled on the fly (`--agg avg,max,last`, `--interval auto`), prints the SQL |
| `scripts/lake_query.py summary --signals a,b \| --match <regex> \| --all --device X --start .. --end .. [--by day\|hour\|total] --out f.parquet` | Fleet statistics: Every device/message with these signals (or all of a device's), each table scanned once; mergeable n/sum/min/max per period |
| `scripts/lake_query.py episodes --signal s --above Y \| --below Y \| --state V --start .. --end .. [--min-duration] [--gap]` | When was a signal above/below a limit or in a state: Start, end, duration, peak per device; day screen first, so long periods stay cheap |
| `scripts/lake_query.py hist --signal s --bins a,b,c \| --states --start .. --end .. [--by day] [--gap]` | Time spent per value bin or state (time-weighted, mergeable seconds): Duty cycles, load spectra, exposure hours |
| `scripts/lake_query.py sql "SQL" [--dry-run] [--out r.csv]` | Any read-only SQL; `--dry-run` checks it without running (BigQuery/Synapse: shows the translated SQL) |
| `scripts/_backends.py` (`connect()`) | Python module for analysis scripts: `resample()`, `summary()`, `episodes()`, `hist()`, `fleet()`, `sql()`, `find()`, `devices()`, `device_id()`, `stats()` return DataFrames |

Every script has `--help`; `--json` gives compact machine output. `--device` takes a device ID or its meta name
(`'Truck 1'`, from `tbl_aggregations_devicemeta`). IDs are unique, meta names are not: A name works only if exactly
one device has it; a shared or partial name stops with the candidates (ask the user, then use the ID). Key all
results by device ID, the name is a label. `--signal(s)` also take `message.signal`: A device can hold the same
signal in several tables (per source address, two DBC versions), one result row each: Never add them up blindly. Code: `_backends.py` (shared logic), `_local.py`, `_athena.py`,
`_bigquery.py`, `_synapse.py` (one module per backend), `_dbc.py` (DBC meaning, reuses canedge-dbc + canedge-workspace).

## Workflow

1. **Schema first**: `lake_schema.py` (devices + meta names), then the logged period per device: `summary --signals
   <one signal> --start 2000-01-01 --end <today> --by total` (first/last time, one column = cheap; never assume
   the user's period has data). Then `--catalog --device X --start .. --end ..` on one day with data of one device
   per vehicle type (what exists, has values and means), or `--find <signal>` / `--describe <table>`. Never guess
   table or column names (lower case). The decoder splits messages per J1939
   source address (`can1_eec1_s00`) and per multiplexor value. Missing devices or messages in the cloud lake: The
   data lake's table mapping (Athena: Glue job; Synapse: Container App Job `synapse-map-tables-<id>`) must be
   re-run by the user
2. **Simple questions**: `lake_query.py resample` for a signal over a period, `lake_schema.py --stats` for ranges and NULLs, or one
   SQL from [references/sql-patterns.md](references/sql-patterns.md) (`--dry-run` first)
3. **Small test, then scale** (cloud): One device, one day; check rows + MB, then widen. Tell the user before
   anything above ~10 GB
4. **Fleet questions** (many devices): Plan the signals and period, run `summary` once into the project
   (`--out projects/<name>/data/...parquet`, one file per month for long periods), then answer the question and the
   follow-ups from that file in pandas (sql-patterns.md 6). Custom per-table SQL across devices: `fleet()`
5. **Analyses** (many queries, a page to explore or share, question types like "what is wrong with X", duty
   cycles, outliers): `canedge-lake-analysis` (recipes, `compare.py`, the analysis page). Static reports:
   `canedge-reports`. Either way the data source, window and the MB/cost from `budget()` go into the provenance

## Rules

- Aggregate, don't download: Every query returns only what answers the question (resampled buckets, per-day or
  per-device stats, events). Never pull raw rows out of the cloud data lake to process them locally; `sql()` stops
  above 100,000 rows. Never load raw rows into the conversation
- Read-only: The skill never modifies a data lake. Only SELECT/WITH/SHOW/DESCRIBE/EXPLAIN run (checked before every
  query, also locally); there is no way to run DDL/DML. Table changes are the data lake automation's job
- Athena: Every query on a message table filters `date_created` (string `'yyyy/MM/dd'`). BigQuery: Filter `t`
  (as the Grafana template); `date_created` works there too (derived from `t`). Synapse: Filter `date_created` +
  `t` (`date_created` = the yyyy/mm/dd folders: The filter skips whole folders, as in the Grafana-Synapse template)
- `connect()` stops at a scan budget (default 10 GB per script run): Ask the user before raising it. Synapse: Data
  processed is not tracked (no budget, no cost estimate): Keep windows small, test on one device + day first
- Synapse lacks `max_by`/`min_by` (resample `--agg first,last`), `approx_percentile` and arrays: Use avg/min/max,
  `hist` for distributions
- Costs: The printed scan cost is a rough estimate (Athena/BigQuery pricing). Storage requests (S3/GCS reads of each
  Parquet file) come on top and can dominate when a query touches many small files. Say this when you quote a cost,
  and prefer one `summary` over many small queries. Select only the columns you need (billed per column)
- Storage request guard: `summary` and `fleet()` estimate the request cost first (a few folder listings, very rough)
  and stop above $1 per run. Then tell the user the estimate and ask before re-running with `--max-request-usd N`
  (`connect(..., max_request_usd=N)`). Large fleet analyses: `summary ... --dry-run` first shows the estimate
- DBC files: Project `dbc/` (+ `dbc-groups.json`) > the input bucket root via `canedge-s3 s3_object.py ls
  --folders` / `get` (same profile; Google: S3-compatible endpoint), after asking the user; copy them into the project
  `dbc/` > ask the user. Read the message + signal comments and value tables before interpreting a signal, and give
  units with every result
- The lake decides what is available, not the DBC: A DBC often has far more messages than the lake holds, and some
  signals of a decoded message are always NULL (e.g. "not available" values outside the DBC range). Only report
  signals that have values (`--catalog` with a window, `--stats`)
- Protocol specifics (J1939 source addresses + DM1 DTCs, multi-frame data, OBD2/UDS responses, NMEA 2000): Read the
  protocol's "Log ... data" section in the CANedge Intro (`canedge-knowledge`) before interpreting such signals.
  DM1 from the CSS J1939-73 DBCs: Query the combined `<CANx>_CALC_DM01` table, not one table per source address
  (canedge-grafana Rules)
- `summary` `avg` is per row (sample-weighted): Biased when the message rate varies (on-change logging, prescalers,
  gaps). Compare `n_rows` with the DBC cycle time; time-weighted values: `hist`
- Invalid values are NULL or NaN depending on the lake (decoder version): `summary`, `--stats`, `episodes` and `hist`
  treat NaN as NULL; hand-written SQL must do the same (sql-patterns.md 4)
- Times are UTC (`t`); show local time using the project's `display_tz` (canedge-reports `display_time.py`)
- Generate fleet SQL from `find()` hits, never hand-written table lists
- Credentials: The profile from `setup_env.py` (canedge-workspace conventions section 7). Athena = the data lake
  stack's programmatic access keys or the Athena user's keys (database + workgroup derived from the lake ID);
  BigQuery = the data lake's programmatic access key file or the BigQuery user's key file (`setup_env.py --key-file
  <path> --bucket <input bucket>`); Synapse = the Synapse SQL login of an `azure` profile (`setup_env.py --cloud
  azure`). The Athena / BigQuery user's keys (Grafana's) are query-only. Never open or print key files, never ask
  for keys or passwords in the chat

References: [sql-patterns.md](references/sql-patterns.md) (patterns, Grafana macro mapping, fleet analyses),
[athena.md](references/athena.md) (stack outputs, tables, costs, limits), [bigquery.md](references/bigquery.md)
(key file, tables, SQL translation, costs), [synapse.md](references/synapse.md) (SQL login, tables, T-SQL
translation, folder pruning, costs), [local.md](references/local.md) (local views, dialect differences, DuckDB-only
extras).

## Related skills

- `canedge-mdf2parquet` - build a local lake from log files (no data lake yet, ad hoc DBC decoding)
- `canedge-grafana` - turn findings into Grafana dashboards (same tables; the resample SQL maps to the panel macros)
- `canedge-lake-analysis` - question-type recipes, `compare.py` (what deviates), the interactive analysis page
- `canedge-reports` - the report frame; `canedge-workspace` - projects, conventions
- `canedge-s3` - DBC files from the bucket root; raw log files when the data lake lacks a signal (no DBC for it)
- `canedge-dbc` - lint/fix the DBCs (a DBC that fails to load is skipped by `--catalog` with a warning)
- `canedge-knowledge` - how the data lake automation and table mapping work (CANedge Intro: Parquet data lake pages)
- User tool: The free [Parquet viewer](https://www.csselectronics.com/pages/parquet-viewer-can-bus-data-lake) lets the
  user plot and inspect a local lake folder, a zip or single Parquet files (e.g. downloaded from the output bucket)
  in the browser. Suggest it when the user wants to look at signals or raw rows themselves; for the cloud lake at
  scale, Grafana
