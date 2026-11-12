# Dashboard spec for build_dashboard.py

Example: [../assets/examples/gps-fleet-spec.json](../assets/examples/gps-fleet-spec.json) (builds on both backends).

```json
{
  "title": "EV fleet - battery",
  "uid": "my-dashboard",
  "description": "Optional",
  "backend": "athena",
  "dataset": "BigQuery only, optional",
  "time": {"from": "2026-10-09T00:29:00Z", "to": "2026-10-09T00:54:00Z"},
  "timezone": "",
  "tags": ["athena", "canedge"],
  "datasource_uid": "Amazon Athena",
  "links": [{"title": "Fleet comparison", "dashboard": "other-uid", "tooltip": "Optional"}],
  "device_colors": {"12345678": "blue"},
  "variables": [...],
  "panels": [...]
}
```

Only `panels` is required. `backend`: `athena` (default), `bigquery` or `synapse`; `--backend`, `--athena`,
`--bigquery` or `--synapse` set it too. `uid` defaults to a slug of the title. `datasource_uid` (or `--datasource-uid`)
defaults to the template convention `Amazon Athena` / `Google BigQuery` / `Microsoft SQL Server` (the user's data
source must be named so). BigQuery adds a `$dataset`
variable: `dataset` from the spec, else from the lake (`--bigquery`), else `lakedataset1` (the template default).
`time`: Absolute ISO times for a trip dashboard (opens on that trip), the data period for a comparison, `now-7d` only
for standing monitoring. `links`: Dashboard links (`dashboard` = a uid, or `url`; optional `keep_time`,
`include_vars`). `device_colors`: Color per device on all fleet panels (default: blue, orange, green, red, ... in
order of first use).

## Variables

| Spec | Result |
|---|---|
| `{"type": "device"}` | `$device` dropdown from `tbl_aggregations_devicemeta` (meta names shown, IDs as values), copied from the bundled dynamic template |
| `{"type": "device", "devices": ["12345678", "11223344"]}` | `$device` as a fixed custom list (no devicemeta table needed) |
| `{"type": "custom", "name": "limit", "options": [300, 400], "default": 400, "multi": false}` | Custom dropdown |
| `{"type": "textbox", "name": "limit", "default": "400"}` | Free text box |
| `{"type": "query", "name": "trip", "sql": "SELECT ...", "multi": true, "include_all": true}` | Query variable (SQL in the backend's dialect) |

Use variables in SQL as `${name}`, `${name:csv}` or `${name:singlequote}` (multi-value `IN (...)`). A `dynamic`
panel adds `$message`, `$signal`, `$signal_value` (hidden) and `$aggregation` after `$device` (names reserved).

## Panels

Common keys: `type`, `title`, `description`, `w` (1-24), `h`, optional `x`/`y` (else auto layout in spec order),
`unit` (Grafana unit id, table below), `min`, `max`, `decimals`, `thresholds` (`[[value, color], ...]`),
`value_map` (DBC value table: `{"0": "Off", "1": {"text": "On", "color": "green"}}`, colors assigned in order if
omitted), `colors` (fixed color per series name, e.g. `{"EV01": "blue"}`: the same on every panel), `fields`
(keep only these columns), `transformations` (raw Grafana transformations, appended).

Style defaults (SKILL.md "Dashboard design + style"): Time series stepped (`stepAfter`, width 2, points auto 1, fill
40); `max` and a `min` other than 0 become soft axis limits on time series and bar charts. Stats: Background color,
no sparkline, value size 40 from `w` 4. Stats, bars, pies, tables: `lengthkm`, `lengthm`, `kwatth`, `watth`, `h`
become fixed suffixes (no "1 Mm" or "1.4 day"). Bars, bar gauges, pies: Plain numbers, the unit once in the title
(bar charts: the axis label, `axis_label` to set it).

| Key | Panels | Use |
|---|---|---|
| `from_panel` | any query panel | The rows of the panel with this title (`-- Dashboard --` data source): One query, several panels |
| `field` | stat, piechart, bargauge | The column to show (`reduceOptions.fields`) |
| `rows: true` | stat, piechart, bargauge | One value per row, named by its text column (e.g. one per device); pies from `sql` / `from_panel`: on by default |
| `reduce` | stat, bargauge, piechart | Reducer (`lastNotNull`, `max`, `mean`, ...) |
| `x_field` | barchart | The category column (e.g. `setting`, `speed band`) |
| `pie_type` | piechart | `donut` (default) or `pie` |

| Type | Keys | Generated query |
|---|---|---|
| `timeseries` / `stat` / `bargauge` / `barchart` / `state-timeline` | `message`, `signals`, `agg` (`AVG`/`MIN`/`MAX`/`SUM`/`COUNT` or a list; default `AVG`, state-timeline `MAX`), optional `device` (fixed, else `${device:csv}`), `where` (extra SQL condition), `reduce` (stat/bargauge: `lastNotNull`, `max`, `mean`, ...) | One resampled aggregate per signal (below) |
| same, fleet | `message`, `signals`, `agg`, `devices: [...]` | `UNION ALL` of one query per device with a `device` column + `partitionByValues` (one series per device) |
| any of the above + `table` | `sql` (full custom SQL with macros, in the backend's dialect), optional `format` (0 time series, 1 table) | As given |
| `geomap` | `position_message` (e.g. `can9_gnsspos`), `lat`/`lon` (default `latitude`/`longitude`), `valid_signal` (default `positionvalid`, `null` to skip), optional `color_message` + `color_signal` + `color_scale` (3.6 = m/s to km/h); without `thresholds` a colored route uses the templates' km/h speed scale (0-150) | Route layer as in the GPS templates: Two queries merged, invalid/NULL positions filtered |
| `piechart` | `sql` (a label column + a value column, one slice per row) or `from_panel` + `field`; or `message` + `signals` (one slice per series) | Donut, labels in %, legend table with value + percent |
| `dynamic` | Optional `message` + `signal` (preselected: one signal, not All; Athena lower case), `title`, `description`; needs a `device` variable; one per dashboard; default w 10, h 7 | The dynamic template's panel + its dropdowns: Any message + signals of `$device`, AVG/MIN/MAX via `$aggregation` (stepped lines; Athena: names trimmed) |
| `text` | `content` (markdown) | - |
| `row` | `title` | Full-width section header |

Message names are the table suffix (`can9_gnssspeed`, from `canedge-lake-query/scripts/lake_schema.py`). Write them in
lower case: BigQuery and Synapse need the real case (`CAN9_GnssSpeed`), which `--bigquery` / `--synapse` take from
the lake. A `message`
starting with `tbl_` is used as the full table name (e.g. `tbl_aggregations_tripsummary` with `sql`).

## Generated query per backend

| | Athena | BigQuery | Synapse |
|---|---|---|---|
| Time bucket | `$__timeGroup(t, $__interval) as time` | `TIMESTAMP_MILLIS(DIV(UNIX_MILLIS(t), $__interval_ms) * $__interval_ms) as time` | `DATEADD(MILLISECOND, (DATEPART(MILLISECOND, t) / $__interval_ms) * $__interval_ms, DATEADD(SECOND, FLOOR(DATEDIFF(SECOND, '2020', t) / CEILING($__interval_ms / 1000.0)) * CEILING($__interval_ms / 1000.0), '2020')) AS time` (the template's; `SYN_BUCKET`) |
| Table | `tbl_${device:csv}_can9_gnssspeed` | `${dataset}.tbl_${device:csv}_CAN9_GnssSpeed` | `OPENROWSET(BULK '${device:csv}/CAN9_GnssSpeed/*/*/*/*', DATA_SOURCE = 'ParquetDataLake', FORMAT = 'PARQUET') AS r` |
| Filter | `date_created BETWEEN $__rawTimeFrom('yyyy/MM/dd') AND $__rawTimeTo('yyyy/MM/dd') AND $__timeFilter(t)` | `$__timeFilter(t)` | `CONCAT(r.filepath(1), '-', r.filepath(2), '-', r.filepath(3)) BETWEEN CONVERT(date, $__timeFrom()) AND CONVERT(date, $__timeTo()) AND $__timeFilter(t)` |
| Group | `GROUP BY $__timeGroup(t, $__interval)` | `GROUP BY time` | `GROUP BY FLOOR(DATEDIFF(SECOND, '2020', t) / CEILING($__interval_ms / 1000.0)), DATEPART(MILLISECOND, t) / $__interval_ms` (`SYN_GROUP`) |
| Data source | `grafana-athena-datasource`, query in `rawSQL` | `grafana-bigquery-datasource`, query in `rawSql` | `mssql` (core Microsoft SQL Server), query in `rawSql`, format `time_series` / `table`; variable queries are plain SQL strings |

## Custom SQL rules (validated)

- Message tables: Keep the backend's filter from the table above; `$__timeFilter(t)` on both (else the panel
  ignores the time range and scans every file)
- Return aggregated rows: The time bucket + `GROUP BY` (Grafana picks the interval from the panel width and time
  range), not raw frames
- Time series panels need a time column (`as time`); tables can return anything
- Cross-message math: Prefer two queries + a Grafana `merge` + `calculateField` transformation over SQL JOINs
- BigQuery: No `date_created` on message tables (aggregation tables have it), no `$__rawTimeFrom`; tables with
  `${dataset}.`
- Synapse: T-SQL (`TOP n`, no `LIMIT`; no GROUP BY ordinals or aliases). Read every data folder with OPENROWSET +
  the filepath() filter (trip summary / events: `BULK 'aggregations/tripsummary/*/*/*/*'`); the mapped `tbl_` tables
  return no data rows and only feed the dropdowns (`tbl_aggregations_devicemeta`, `INFORMATION_SCHEMA.COLUMNS`).
  Message folders and signal names are case sensitive

## DBC unit -> Grafana unit id

| DBC unit | `unit` | DBC unit | `unit` |
|---|---|---|---|
| V | `volt` | km/h | `velocitykmh` |
| A | `amp` | m/s | `velocityms` |
| W / kW | `watt` / `kwatt` | mph | `velocitymph` |
| Wh / kWh | `watth` / `kwatth` | rpm | `rotrpm` |
| degC | `celsius` | m / km | `lengthm` / `lengthkm` (stats etc.: `suffix: km`) |
| degF | `fahrenheit` | s / ms / h | `s` / `ms` / `h` |
| % | `percent` (0-100) | bar / kPa / psi | `pressurebar` / `pressurekpa` / `pressurepsi` |
| deg | `degree` | Hz | `hertz` |
| L | `litre` | m/s² | `accMS2` |
| | | other (e.g. Nm) | `suffix: Nm` |
