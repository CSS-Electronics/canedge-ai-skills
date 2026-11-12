# SQL patterns for the CANedge data lake (local DuckDB, Athena, BigQuery, Synapse)

All patterns are Athena (Trino) SQL and run unchanged on every backend: The local backend adds DuckDB macros for the
Trino functions DuckDB lacks ([local.md](local.md)), the BigQuery and Synapse backends translate the SQL
([bigquery.md](bigquery.md), [synapse.md](synapse.md)). Synapse exceptions: `max_by`/`min_by` (2) and
`approx_percentile` (3) do not exist there; drop them or use `hist` (the other patterns run). Placeholders: `tbl_dev_msg` = a message table (`tbl_<device>_<message>`),
`sig`/`state` = signal columns (lower case). Find real names with `lake_schema.py --find <signal>`. Test on one
device + one day first.

## 1. Partition-pruned time filter (always)

```sql
SELECT t, sig
FROM tbl_dev_msg
WHERE date_created BETWEEN '2026/01/15' AND '2026/01/16'          -- prunes partitions (cost + speed)
  AND t >= TIMESTAMP '2026-01-15 01:00:00' AND t < TIMESTAMP '2026-01-16 03:00:00'   -- exact window, UTC
LIMIT 100
```
- `date_created` is a `'yyyy/MM/dd'` string: Compare as a string, include both boundary days
- `t` is UTC. Convert to local time for display only, in Python (not with `AT TIME ZONE`, which differs between
  Athena and DuckDB)

## 2. Resample on the fly (the Grafana panel query)

The core extraction query: Scan the raw rows in the lake, return one row per time bucket. `lake_query.py resample`
generates it (and prints it); the bucket size adapts to the window like Grafana's `$__interval`.

```sql
SELECT from_unixtime(floor(to_unixtime(t) / 60) * 60) AS time,
       avg(sig) AS avg_sig, max(sig) AS max_sig, max_by(sig, t) AS last_sig
FROM tbl_dev_msg
WHERE date_created BETWEEN '2026/01/15' AND '2026/01/15'
  AND t >= TIMESTAMP '2026-01-15 08:00:00' AND t < TIMESTAMP '2026-01-15 12:00:00'
GROUP BY 1 ORDER BY 1
```

| Grafana (Athena data source) | Literal SQL (here) |
|---|---|
| `$__timeGroup(t, $__interval)` | `from_unixtime(floor(to_unixtime(t) / N) * N)`, N = bucket in seconds |
| `date_created BETWEEN $__rawTimeFrom('yyyy/MM/dd') AND $__rawTimeTo('yyyy/MM/dd')` | `date_created BETWEEN '2026/01/15' AND '2026/01/15'` |
| `$__timeFilter(t)` | `t BETWEEN TIMESTAMP '...' AND TIMESTAMP '...'` (resample uses `>=` / `<`: no double-counted rows at window edges) |
| `tbl_${device:csv}_mymessage` | `tbl_12345678_mymessage` (device ID lower case) |

- Pick the aggregate per signal: `avg` (continuous), `max_by(sig, t)` = last value (states, counters, SOC; not on
  Synapse), `max`/`min` (peaks, alarms), `count` (message rate, data gaps)
- Bucket size: ~1000 buckets per window is plenty for a chart; 1 day at 1 s = 86,400 rows (too many)
- Sub-second buckets: Use integer milliseconds, `from_unixtime(floor(to_unixtime(t) * 1000 / 100) * 100 / 1000)`
  for 100 ms (`/ 0.1` in floating point puts rows at bucket edges into the wrong bucket); `resample` does this
- Calendar buckets: `date_trunc('minute', t)` (`'hour'`, `'day'`)

## 3. Per-day / per-device statistics

```sql
SELECT date_created AS day, count(*) AS frames, avg(sig) AS avg_sig, max(sig) AS max_sig,
       approx_percentile(sig, 0.95) AS p95_sig, min(t) AS first_t, max(t) AS last_t
FROM tbl_dev_msg
WHERE date_created BETWEEN '2026/01/01' AND '2026/01/31'
GROUP BY 1 ORDER BY 1
```
Histogram: `SELECT floor(sig / 10) * 10 AS bin, count(*) AS n FROM ... GROUP BY 1 ORDER BY 1`.

## 4. Events with window functions

State transitions:
```sql
WITH s AS (
  SELECT t, state, lag(state) OVER (ORDER BY t) AS prev_state
  FROM tbl_dev_msg
  WHERE date_created BETWEEN '2026/01/15' AND '2026/01/15'
)
SELECT t, prev_state, state FROM s
WHERE state IS DISTINCT FROM prev_state AND prev_state IS NOT NULL
ORDER BY t
```

Threshold crossings (rising edge above 400):
```sql
WITH s AS (SELECT t, sig, lag(sig) OVER (ORDER BY t) AS prev FROM tbl_dev_msg WHERE date_created = '2026/01/15')
SELECT t, prev, sig FROM s WHERE sig > 400 AND prev <= 400 ORDER BY t
```

Time-weighted average + total of a rate (fuel L/h -> L, power kW -> kWh, current A -> Ah). Each row holds until
the next; intervals > 10 s = no data (as `hist --gap`):
```sql
WITH s AS (
  SELECT sig, date_diff('millisecond', t, lead(t) OVER (PARTITION BY date_created ORDER BY t)) AS dt_ms
  FROM tbl_dev_msg WHERE date_created BETWEEN '2026/01/15' AND '2026/01/16'
)
SELECT sum(sig * dt_ms) / sum(dt_ms) AS avg_time_weighted, sum(sig * dt_ms) / 3600000.0 AS total_rate_x_hours,
       sum(dt_ms) / 3600000.0 AS hours_with_data
FROM s WHERE dt_ms <= 10000 AND NOT is_nan(sig)
```

Generated, fleet-wide and cheaper than hand-written SQL (`lake_query.py` / `db.episodes()`, `db.hist()`):
- **Episodes** (periods above/below a limit or in a state): `episodes --signal sig --above 80 [--min-duration 5]`.
  A per-day count screen first, then `lag()` runs only on the days with a match; a data gap > `--gap` (10 s) ends
  an episode. Spikes: `--min-duration 0` (single rows count)
- **Time per bin or state** (duty cycle, load spectrum, exposure hours): `hist --signal sig --bins 0,500,1000` or
  `--states` (value table labels with `--dbc`). Each row holds its value until the next (`lead(t)`), intervals >
  `--gap` = no data. Seconds are mergeable (sum over days/devices). On-change logging (data prescaler): Raise `--gap`
  to the longest expected unchanged period

Hand-written window SQL:
- `OVER (PARTITION BY date_created ORDER BY t)`: Without a partition, a long period sorts all rows in one place
  (slow, may fail on Athena); events across midnight then need a merge (as `episodes` does)
- Several devices in one query: `PARTITION BY device` inside `OVER (...)`
- Large time gaps inside an episode mean missing data (logger off, filter), not a long event
- NaN: Some lakes hold NaN instead of NULL for invalid values; the generated queries treat it as NULL
  (`CASE WHEN is_nan(sig) THEN NULL ELSE sig END`, `is_nan` works on every backend)

## 5. Join two messages on time (via buckets)

```sql
WITH a AS (
  SELECT date_trunc('second', t) AS ts, avg(sig_a) AS sig_a
  FROM tbl_dev_msg_a WHERE date_created = '2026/01/15' GROUP BY 1
), b AS (
  SELECT date_trunc('second', t) AS ts, avg(sig_b) AS sig_b
  FROM tbl_dev_msg_b WHERE date_created = '2026/01/15' GROUP BY 1
)
SELECT a.ts, a.sig_a, b.sig_b, a.sig_a * b.sig_b / 1000 AS product
FROM a JOIN b ON a.ts = b.ts
ORDER BY a.ts
```
- Choose the bucket >= the slower message's period. `FULL OUTER JOIN` keeps unmatched buckets
- Joins are much slower than separate resampled queries: For dashboards prefer two queries + a Grafana merge

## 6. Fleet analyses: Query once, analyze the saved result

Each device has its own tables, so a fleet question means one scan per device table. The expensive part is the
scan, so do it once at a granularity that answers the follow-up questions too, save it, and do the rest in pandas:

```
lake_query.py --athena summary --signals enginespeed,wheelbasedvehiclespeed --start 2026-01-01 --end 2026-02-01 \
              --by day --out projects/<name>/data/summary-2026-01.parquet
```
`summary` (= `db.summary(hits, start, end, by)`) finds every table with the exact signal names (`--match <regex>`:
e.g. all cell voltages; `--all --device X`: every signal of a device, for triage), scans each table once (all its
requested signals together, `UNION ALL` batches of ~50 tables, also across tables with different signals) and returns per device +
message + signal + period (`day`, `hour` or `total`): `n_rows, first_t, last_t, n, sum, min, max, avg`, with
`device_name` from the device meta table. These statistics merge: Fleet or weekly average = `sum(sum) / sum(n)`,
overall max = `max(max)`, data coverage = `n_rows`, `first_t`/`last_t`. Rankings, outliers, trends and
"which devices never reached X" then need no further query.

- Pick the signals and the period up front (one scan per table either way; extra signals of the same message are
  nearly free on the local lake, billed per column on Athena/BigQuery)
- Long periods: One file per month (`--start/--end` per month); finished months never need a re-query
- Percentiles and distributions do not merge: Add a histogram via `fleet()` if needed (pattern 3, `floor(sig / 10)`)
- `avg` = per row: With a varying message rate (on-change logging, prescalers, gaps) use time-weighted values
  (pattern 4, `lead(t)` with a gap cap)
- Devices with different DBCs have different signal names: Check `lake_schema.py --find` before concluding a device
  "has no data". Only devices with the table appear (never logged, or the Glue job has not run)
- Optional trip summary / event tables (`tbl_aggregations_*`) are rarely deployed; use them only if they exist and
  match the question exactly

Custom per-table SQL (events, histograms, conditions) runs across the fleet with `fleet()`. Never hand-write the
table list:

```python
import sys; sys.path.insert(0, ".claude/skills/canedge-lake-query/scripts")
from _backends import connect
db = connect(athena=True)                     # or connect(project="projects/<name>"), connect(bigquery=True)
hits = [h for h in db.find("speed") if h["signal"] == "speed"]       # one table per device with that signal
print(db.request_estimate(hits, "2026-01-01", "2026-02-01")["text"])   # cloud: storage request cost, very rough
df = db.fleet("SELECT date_created AS day, count_if(speed > 90) AS n_over_90, count(*) AS n FROM {table} "
              "WHERE date_created BETWEEN '2026/01/01' AND '2026/01/31' GROUP BY 1", hits, "2026-01-01", "2026-02-01")
names = db.devices()                          # device ID -> meta name (label the results)
print(db.budget())                            # Athena/BigQuery: queries, MB, cost so far
```
- `{table}`, `{device}`, `{message}` are filled per table; the template must not end with `ORDER BY` (sort in pandas)
- Pass the template's window as `start`/`end`: The storage request guard (cloud) estimates the files to read and
  stops above $1 per `connect()`; without a window it assumes all data
- The scan budget (default 10 GB per `connect()`) and the request guard stop a script: Ask the user before raising
  them. Athena reuses the result of the same SQL within 24 h for free, so re-running an `analysis.py` while
  developing it costs nothing

## 7. Data availability (cheap)

```sql
SELECT date_created AS day, count(*) AS n_rows, min(t) AS first_t, max(t) AS last_t
FROM tbl_dev_msg WHERE date_created BETWEEN '2026/01/01' AND '2026/01/31'
GROUP BY 1 ORDER BY 1
```
