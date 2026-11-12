# Local backend (DuckDB on a Parquet folder)

## Source + views

- A folder with the data lake layout `<device>/<CANx_Message>/<yyyy>/<mm>/<dd>/*.parquet`: A project lake built by
  canedge-mdf2parquet (`--project projects/<name>` = `projects/<name>/parquet`), or any folder (`--lake`), e.g. a
  copy of the S3 Parquet bucket or the output of the MF4 decoders
- Views are created per query for every `tbl_*` name in the SQL, like the Glue mapping: `tbl_<device>_<message>`
  lower case (non-alphanumerics -> `_`), signal columns lower-cased, `date_created` (`'yyyy/mm/dd'`) from the
  folder, plus `tbl_<device>_messages` (column `messagename`)
- `lake_query.py sql --start/--end` (and `resample`, `fleet(start=, end=)`) only read the files of the days in the
  window; without them a view reads all files of the table. Still filter `date_created`/`t` in the SQL for exact
  bounds (same SQL as on Athena)
- `t` is read as a naive TIMESTAMP holding UTC; the session time zone is UTC and results are returned tz-aware.
  Compare with `TIMESTAMP '2026-01-15 04:00:00'` (UTC)
- Read-only: The same statement check as on Athena (no COPY, ATTACH, CREATE, SET, ...)

## Dialect: Athena (Trino) vs DuckDB

The reference dialect is Athena, so SQL ports to the cloud lakes and Grafana (BigQuery translates it, see
[bigquery.md](bigquery.md)). The local backend defines DuckDB macros for the Trino functions DuckDB lacks:

| Trino (Athena) | DuckDB macro |
|---|---|
| `approx_percentile(x, p)` | `approx_quantile(x, p)` |
| `approx_distinct(x)` | `approx_count_distinct(x)` |
| `from_unixtime(s)` | `make_timestamp(round(s * 1e6))` (naive UTC TIMESTAMP) |
| `to_unixtime(t)` | `epoch(t)` |
| `array_join(a, sep)` | `array_to_string(a, sep)` |

Same in both: `date_trunc`, `date_diff('millisecond', a, b)`, `max_by`/`min_by`, `lag`/`lead`/`sum() OVER`,
`IS DISTINCT FROM`, `TIMESTAMP '...'` literals, string comparison on `date_created`, `UNION ALL`, `FULL OUTER JOIN`.

Precision: Athena reads `t` as a millisecond timestamp (truncated: `.849200` -> `.849`); locally `t` keeps the full
microseconds (CANedge resolution 50 us). Resampled results match exactly for buckets >= 1 ms (verified on the same
device/day on both backends); sub-millisecond timing analyses need the local lake.

Differences to avoid in shared SQL:
- `AT TIME ZONE`: Different result types; convert to local time in Python for display
- Trino lambdas (`transform(arr, x -> ...)`) vs DuckDB `list_transform`: Only used in Grafana variable queries
- Grafana macros (`$__timeGroup`, `${device}`): Grafana only; all backends refuse them (see sql-patterns.md 2)

DuckDB-only conveniences (fine in local analysis scripts, never port them to Athena/BigQuery):
- `ASOF JOIN`: Each left row gets the latest right row, e.g.
  `FROM tbl_a a ASOF JOIN tbl_b b ON a.t >= b.t WHERE a.t - b.t <= INTERVAL 1 SECOND`
- `time_bucket(INTERVAL '10 seconds', t)`
