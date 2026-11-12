# CANedge data lake in Azure Synapse

Source: The CANedge Azure data lake deployment (canedge-azure-cloud-terraform: input container + output container
`<input>-parquet` in one storage account, the MF4-to-Parquet Azure Function, a Synapse workspace with a serverless
SQL pool and the table mapping Container App Job `synapse-map-tables-<id>`) and the official Grafana-Synapse
dashboard templates (CANedge Intro, Grafana-Synapse pages, in `canedge-knowledge`).

## Credentials

- A data lake profile `credentials/<name>.env` with `CANEDGE_CLOUD=azure`, created with the setup window
  `setup_env.py --cloud azure` (conventions section 7). It holds the Synapse SQL login only:
  `CANEDGE_SYNAPSE_ENDPOINT`, `CANEDGE_SYNAPSE_DATABASE`, `CANEDGE_SYNAPSE_USER`, `CANEDGE_SYNAPSE_PASSWORD`
- Where the user finds the values (the same ones Grafana uses): The end of the Synapse deployment output; Serverless
  SQL endpoint = Synapse workspace > Overview (`synapse-<id>-ondemand.sql.azuresynapse.net`); database + password =
  Container App Job `synapse-map-tables-<id>` (env `SYNAPSE_DATABASE`; secrets `synapse-reader-password` for the
  read-only login, `synapse-password` for the admin)
- Use the read-only login `sqlreaderuser` (like the Athena user on AWS): Synapse deployments from 2026-11 create it
  (the table mapping job creates/updates it at every run). It reads the Parquet data lake (OPENROWSET) and the mapped
  tables, and cannot create, change or drop anything. Older deployments: Deploy Synapse again (canedge-azure-cloud-
  terraform) and run the mapping job once. Until then the admin `sqladminuser` works: The skill only runs single
  SELECT statements (checked before and after the translation). The grants (ensure_reader_login in the job's
  synapse-map-tables.py, verified on a test lake): `ADMINISTER DATABASE BULK OPERATIONS` (OPENROWSET), `REFERENCES
  ON DATABASE SCOPED CREDENTIAL::my_credential`, `SELECT ON SCHEMA::dbo`. It cannot see `ParquetDataLake` in
  `sys.external_data_sources` (metadata visibility), but reads through it
- The input container (raw log files, control files) is a separate S3-compatible profile with the user's Flexify
  endpoint + keys (`setup_env.py --cloud s3`): canedge-s3 and canedge-s3-overview work on it like on any S3 bucket
- Connection: pymssql (TDS, port 1433, encrypted by the server), no ODBC driver. Never print the password, never ask
  for it in the chat

## Tables

| Table | Content |
|---|---|
| `tbl_<DEVICE>_<Message>` | One external table per device + message folder of the output container (`<DEVICE>/<Message>`), created by the mapping job. The skill uses the same lower-case names as Athena and maps them to the real-case folders |
| `tbl_<DEVICE>_messages` | `MessageName`: The device's message tables (written by the mapping job, Grafana dropdown) |
| `tbl_aggregations_devicemeta` | `DeviceId`, `MetaName`: The device names of `lake_schema.py` and `devices()` |
| `tbl_aggregations_tripsummary` / `_events` | Trip summary / event detection outputs: Not mapped by the job; the skill lists them when `aggregations/tripsummary` / `aggregations/events` hold data (columns from the first file, same as on Athena) |

- Columns: `t` (timestamp, UTC) + one float per signal; names are matched case-insensitively
- No `date_created` column: The skill derives it from the `yyyy/mm/dd` folders (`filepath()`, below), so the shared
  SQL works and filters on it prune whole folders
- Schema: `sys.external_tables` + `sys.columns` (one query, cached 24 h in `data/synapse-schema/`, `--refresh`)
- New devices and messages appear only after the mapping job has run again (Azure portal: Container App Job
  `synapse-map-tables-<id>` > Run now), also after backlog or aggregation runs. The user starts it; the skill cannot
- Message and folder names are case sensitive in Synapse: The skill takes them from the mapped tables, never type them

## SQL translation

Write the same Athena (Trino) SQL as for the other backends. `_synapse.py` translates it to T-SQL with sqlglot:

| Athena | Synapse (T-SQL) |
|---|---|
| `tbl_12345678_can1_eec1_s00` | `OPENROWSET(BULK '12345678/CAN1_EEC1_S00/*/*/*/*', DATA_SOURCE = 'ParquetDataLake', FORMAT = 'PARQUET') AS tbl_12345678_can1_eec1_s00` |
| `date_created` | `CONCAT(<alias>.filepath(1), '/', <alias>.filepath(2), '/', <alias>.filepath(3))` (the yyyy/mm/dd folders) |
| `from_unixtime(x)` / `to_unixtime(t)` | `DATEADD` (days + ms) / `(CAST(DATEDIFF(SECOND, '1970-01-01', t) AS BIGINT) * 1000 + DATEPART(MILLISECOND, t)) / 1000.0` (exact decimal buckets) |
| `date_trunc('hour', t)`, `date_diff('millisecond', a, b)` | `DATEADD(HOUR, DATEDIFF(HOUR, '1970-01-01', t), '1970-01-01')`, BIGINT seconds * 1000 + the ms parts (`DATEDIFF_BIG` fails on columns in serverless SQL: error 15816; `DATEDIFF(MILLISECOND)` overflows after 24.8 days) |
| `TIMESTAMP '2026-01-15 08:00:00'`, `LIMIT n`, `approx_distinct` | `CAST(... AS DATETIME2)`, `TOP n`, `APPROX_COUNT_DISTINCT` |
| `GROUP BY 1`, `ORDER BY` in subqueries, CTEs in subqueries, `IS DISTINCT FROM` | The expression, dropped, moved to the top level (unique names), expanded comparisons |
| `max_by` / `min_by`, `approx_percentile`, arrays / lambdas | Not available: Clear error (resample `--agg first/last` too). Percentiles: `hist` bins |

Why OPENROWSET: The mapped external tables only hold names (devices, messages, signals) for the Grafana dropdowns and the schema: A query on a message table returns no rows (their location `/<DEVICE>/<Message>/*` matches only the year folders, checked 2026-11). The official Grafana-Synapse dashboards read data with OPENROWSET and filter `filepath()`; the skill does the same.
The official Grafana-Synapse dashboards read with OPENROWSET and filter `filepath()`; the skill does the same. Always
filter `date_created` (Synapse skips the other folders) plus `t` for the exact window.

`--dry-run` prints the translated T-SQL; a failed query prints it too.

## Costs + limits

- Serverless SQL bills per TB of data processed (~$5/TB, minimum 10 MB per query; check the Azure pricing page for
  the region). The skill does not track the data processed: No cost estimate per query, no scan budget, no storage
  request guard. Cost control = the date window (`--stats`, `summary`, `episodes`, `hist` need `--start/--end`) +
  folder pruning + few, well-planned queries (`summary` once, then analyze the saved result)
- A hard limit is a Synapse setting the admin can set (daily/weekly/monthly TB): Synapse Studio > Manage > SQL pools
  > Built-in > Cost control, or `sp_set_data_processed_limit`
- Queries stop after 30 min (Synapse serverless limit) or `--timeout`
- Data lake admin (function logs, backlog/aggregation/mapping jobs) is not supported on Azure: The Azure portal
  (canedge-lake-admin says where)
