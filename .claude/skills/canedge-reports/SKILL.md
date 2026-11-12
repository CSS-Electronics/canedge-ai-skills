---
name: canedge-reports
description: >
  Shareable reports and the shared page kit of the canedge-* skills: Self-contained HTML + JSON with data
  provenance, the dashboard kit (filters, tabs, tables, KPIs, uPlot charts, GNSS map, config/DBC diffs) and
  local-time helpers. Use when a result should be shared ("report", "summary for my manager", "something I
  can send") or a page is built.
---

# canedge-reports

Turns a `report_spec.json` (+ chart SVGs) into:

- `report.html` - one self-contained file (charts embedded), opens in any browser, works offline
- `report.json` - the spec incl. its `data` block, so you (or another agent) can answer follow-up
  questions without re-processing the data

Why HTML/JSON (not a hosted artifact): It works with any AI agent, keeps OEM data local, and
can be emailed or put on SharePoint/Confluence as-is. Asked for a PDF: The user prints the page from the
browser (Print > Save as PDF; print styles show every table row and hide the hovers).

## Workflow

1. Do the analysis in a script (e.g. `projects/<name>/reports/<yyyymmdd>-<slug>/analysis.py`) that
   writes the chart SVGs and `report_spec.json` into its own folder
2. Make charts with the shared style: matplotlib `charts.py` (any static plot; matplotlib is in
   requirements.txt, never install other chart packages), time axes in local time via `display_time.py`:
   ```python
   import sys; sys.path.insert(0, ".claude/skills/canedge-reports/scripts")
   from charts import new_figure, save, COLORS, mark_event, local_time_axis
   from display_time import DisplayTime
   ```
   Interactive charts with hover and zoom belong in a dashboard page (the kit below), not in a static report
3. Build the report:
   ```
   .venv\Scripts\python.exe .claude/skills/canedge-reports/scripts/build_report.py --spec <folder>/report_spec.json
   ```
4. Summarize the findings in chat and end with the bold **Open report** link from the printed `OPEN report` line
   (canedge-workspace "Replies to the user"). In a project: Add one findings-log line to `project.md`

## The spec

See [references/report-spec.md](references/report-spec.md) for the format and the content
guidelines. In short:

- **Fixed frame** (always): Title, data used (an (i) hover after the subtitle). Optional: Method, caveats, reproduce (closed accordions); conclusion
  reports add 2-4 summary bullets + confidence
- **Insights first**: Per-section key findings (`insights`), generic explanations in hover (i) icons,
  every chart and table titled, long tables cut at 15 rows with "+ X more", in-cell bars for comparisons
  (see the interactive options in the reference)
- **Flexible middle**: One key-finding figure + 2-4 context figures that let the reader verify the
  conclusion - not more, not less
- **"Data used" is mandatory** (spec key `provenance`): Device IDs, log files/S3 keys/SQL, time
  range (local + UTC), DBC files per device, tool versions

## Interactive dashboards (dashboard kit)

Overview pages are dashboards, not text reports: One self-contained `report.html` (+ a JSON for the agent), no
summary bullets. Visuals in this order: The kit's panels > a custom panel on the kit (uPlot `SERIES`,
`stackedBars`, `TRACK`; e.g. canedge-lake-analysis `custom.js`) > a static matplotlib image via `charts.py`. Nothing
to install. The shared kit keeps them consistent (raw overview, S3 overview):

- `scripts/dashboard.py`: `build_html(title, app_js, data)` pastes the template, fonts, report CSS, uPlot, the kit and
  the app's JS + data into one HTML (no CDN, no npm, works offline)
- `assets/dashboard.html` / `dashboard.css` / `dashboard.js` (`DASH`): Filter bar (device multi-select, period
  inputs YY/MM/DD HH:MM, activity brush), tabs with (i) hovers + URL hash, KPI groups (`kgroups`: 4 groups in the
  visual identity colors), tables (`table`: sort, search, paging for 100s of rows, toggle filter, in-cell bars,
  Meta column, badges), tooltips, time formatting (`fmt`: YY/MM/DD everywhere, local time with the zone in the
  subtitle), series colors (`colorOf(key)`: one color per device or series name on every chart of a page; register
  the devices first, in device order)
- An app (`<skill>/assets/app.js`) adds its data model, tab contents and charts, then calls `DASH.app({...}).start()`.
  Pages without the filter bar (canedge-config impact page) use their own template with fixed panels and
  `DASH.page(tabs)` + `DASH.table` (also grouped headers, filter buttons, per-column bar colors, one bar scale)
  Pattern: Pre-aggregated additive facts, summed in the browser for the selected devices and period
- Style: Definitions and context in (i) hovers, not in visible sub texts; table titles + column hovers instead of
  captions; dates as YY/MM/DD (HH:MM); sizes in KB/MB/GB; at most ~20 rows in charts/heatmaps (tables list all);
  tags (badges) short and lower case; one unit per column and columns that do not change with the filters; stat
  cards of equal size with one-line rows (nothing wraps or shifts); no chart or route line across data gaps.
  Check every page opened from disk (file://), as the user opens it

`assets/fonts/` holds the website fonts (JetBrains Mono for titles, Open Sans for text and tabs, OFL; see its README).
`assets/uplot/` holds a pinned copy of uPlot 1.6.31 (MIT, `LICENSE`): A small, fast chart library with zoom and hover.
`assets/stacked-bars.js` is the shared stacked bar chart (white separators, synced cursor): Raw overview (FPS tab),
S3 overview (per day) and canedge-config `simulate_config.py` (timeline). Optional kit modules, inlined only where a
page asks for them (`build_html(..., kit=["series", "track"])`): `assets/series.js` (`SERIES`: one zoom + cursor
for several charts, line charts (also stepped: hold each value), grouped bars per period, on/off state timelines, local-time axes;
canedge-config timeline + Explore tab, canedge-lake-analysis) and `assets/track.js` (`TRACK`: GNSS route colored by a signal (only from detailed data, broken at time gaps), one dot per trip, cursor marker, point markers,
on an Esri light gray basemap online, also from pages opened from disk, and a plain route offline; another provider via `o.tiles` / `CFG.tiles`) with a pinned copy of Leaflet 1.9.4 (`assets/leaflet/`,
BSD-2, `LICENSE`). The generic analysis page on all of this: canedge-lake-analysis. `assets/favicon.ico`
(CSS Electronics logo) is the browser tab icon of every kit page. `assets/diff2html/` is a pinned copy of diff2html 3.1.6
(MIT, `LICENSE.md`), the library the CANedge config editor uses for its "Review changes" diff: The Diff tabs of
`simulate_config.py` (canedge-config) and `dbc_diff.py --html` (canedge-dbc) use `dashboard.line_diff()` (the text) +
`dashboard.diff_parts()` (inlines diff2html + `assets/diff.js`, `DIFF.html`).

## Time zones

Data stays in UTC; readers see local time. `scripts/display_time.py` resolves the zone (the calling script's
`--tz` > project `display_tz` > the PC's zone) and formats/converts timestamps for text and chart axes
(`DisplayTime.from_project(cfg).fmt(t)` / `.local(t)` / `.label(first, last)`). Kit pages format times in the browser:
Pass `DisplayTime.from_project(cfg, args.tz).page_tz()` as `CFG.tz` (None = the viewer's PC zone).

## Rules

- Never put secrets, credentials or customer-identifying details that the user has not approved
  into a report
- Time axes and text in local time via `display_time.py`, with the zone in the axis label; mark the
  same events on all related plots (`charts.mark_event`)
- Every figure needs a caption and a source (device + message.signal)
- Text (summary bullets, captions, hovers) follows the writing style in canedge-workspace "Replies to the user":
  No em/en dashes, capital after a colon
- One line per bullet (`summary`, `insights`, `bullets`): Max ~90 characters. Longer: Split it
  into two bullets, or move the detail into a table, caption or (i) hover
- Keep the `data` block compact (aggregates, not raw rows)
