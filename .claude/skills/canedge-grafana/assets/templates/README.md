# Official CANedge Grafana dashboard templates

From the CANedge Intro (Grafana-Athena / Grafana-BigQuery / Grafana-Synapse > Customize dashboard). Three are bundled
unmodified: The dynamic templates, the source the builder copies the `$device` variable, the dynamic panel and its
`$message` > `$signal` > `$signal_value` > `$aggregation` dropdowns from (`{"type": "device"}` + `{"type":
"dynamic"}`), so every device dashboard matches them. The others are linked: Download one (HTTP GET) into
`projects/<name>/grafana/` when the user wants it as a base, read it with `validate_dashboard.py --summary`, then edit.

| Template | Athena | BigQuery | Synapse |
|---|---|---|---|
| dynamic (bundled) | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-athena/templates/athena-dynamic-dashboard-template.json | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-bigquery/templates/bigquery-dynamic-dashboard-template.json | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-synapse/templates/synapse-dynamic-dashboard-template.json |
| internal-gps | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-athena/templates/athena-internal-gps-dashboard-template.json | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-bigquery/templates/bigquery-internal-gps-dashboard-template.json | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-synapse/templates/synapse-internal-gps-dashboard-template.json |
| canmod-gps | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-athena/templates/athena-canmod-gps-dashboard-template.json | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-bigquery/templates/bigquery-canmod-gps-dashboard-template.json | - |
| trip-summary | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-athena/templates/athena-trip-summary-template.json | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-bigquery/templates/bigquery-trip-summary-template.json | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-synapse/templates/synapse-trip-summary-template.json |
| event-summary | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-athena/templates/athena-event-summary-template.json | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-bigquery/templates/bigquery-event-summary-template.json | https://canlogger.csselectronics.com/canedge-getting-started/ce3/_static/files/log-file-tools/grafana-synapse/templates/synapse-event-summary-template.json |

| Template | Use |
|---|---|
| dynamic | Any CAN/LIN data: `$device` > `$message` > `$signal` dropdowns, one time series panel |
| internal-gps / canmod-gps | CANedge GNSS/IMU (CAN9) or CANmod.gps: Route geomap colored by speed, stats, attitude, IMU + a compact dynamic panel. The layout reference for device dashboards (the builder's `geomap` + `dynamic` panels reproduce both; `assets/examples/gps-fleet-spec.json` rebuilds the layout) |
| trip-summary | Fleet KPIs from `tbl_aggregations_tripsummary`: Only if `lake_schema.py` lists that table (trip summary aggregation deployed) |
| event-summary | Events from `tbl_aggregations_events`: Only if `lake_schema.py` lists that table (event detection deployed) |

Notes (checked 2026-11-03; Synapse 2026-11-12):
- The bundled files match the live ones byte for byte. All official templates validate with 0 errors and 0 warnings
- Synapse templates read the folders with OPENROWSET (`<device>/<message>/*/*/*/*`, `aggregations/<name>/*/*/*/*`)
  and the `filepath()` folder filter; the mapped tables only feed the dropdowns. Time bucket (fixed 2026-11-12 in
  the dynamic + internal-gps templates; before, it never got wider than 1 s): `FLOOR(DATEDIFF(SECOND, '2020', t) /
  CEILING($__interval_ms / 1000.0))` + the millisecond part, = `build_dashboard.py` `SYN_BUCKET` / `SYN_GROUP`.
  `validate_dashboard.py` warns about the old form in a user's dashboard
- Refresh: Re-download the bundled files, then run `scripts/update_schemas.py`, which also re-checks every template
  listed here against the Grafana schemas
