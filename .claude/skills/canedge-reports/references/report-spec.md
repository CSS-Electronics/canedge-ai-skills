# Report spec (report_spec.json)

`build_report.py` turns this JSON into `report.html` (self-contained) and `report.json`.
Text fields support `**bold**` and `` `code` `` only. Figure paths are relative to the spec file.

```json
{
  "title": "Charging analysis - 12345678 - 15/01/2026",
  "subtitle": "Optional one-liner",
  "question": "The user's question, verbatim (stored in report.json only, not shown)",
  "summary": ["2-4 bullets: The conclusion first", "..."],
  "confidence": "High - what the confidence rests on, and the key caveat",
  "kpis": [
    {"label": "Log files", "value": "887", "note": "45.3 GB"},
    {"label": "Bus active", "value": "6.9 h", "note": "7% of recorded time", "warn": false}
  ],
  "provenance": {
    "devices": ["12345678"],
    "time_range_utc": ["2026-01-15T01:00:00Z", "2026-01-15T02:30:00Z"],
    "time_range_local": ["2026-01-15 02:00:00", "2026-01-15 03:30:00"],
    "display_tz": "Europe/Berlin (UTC+01:00)",
    "sources": ["D:/LOG/12345678/00000001/00000001.MF4"],
    "files": 1,
    "dbc_files": {"12345678": ["can1-vehicle.dbc"]},
    "tools": {"mdf2parquet_decode": "25.12.15"},
    "extra": {"Project": "projects/2026-01-15-charging-fault"}
  },
  "sections": [
    {
      "heading": "Key finding",
      "text": ["Paragraphs..."],
      "bullets": ["Optional bullets"],
      "figures": [{"file": "fig-timeline.svg", "caption": "What the reader should see", "source": "12345678 CAN1 BMS.PackCurrent"}],
      "table": {"columns": ["ID", "Frames"], "rows": [["0x351", 12345]], "caption": "Optional"}
    }
  ],
  "method": ["How the result was derived, step by step"],
  "caveats": ["Known data/DBC limitations, what was NOT checked"],
  "reproduce": {"script": "projects/.../analysis.py", "command": ".venv\\Scripts\\python.exe projects/.../analysis.py", "sql": "Athena SQL if used"},
  "data": {"any": "machine-readable numbers behind the charts - written to report.json only"}
}
```

## Interactive options (all optional)

| Key | Where | Effect |
|---|---|---|
| `compact: true` | top level | Figure captions/sources and table captions move into hover (i) icons - the page shows findings first |
| `kpi_groups` | top level | Instead of `kpis`: `[{"title": "Traffic", "tip": "...", "kpis": [{"label", "value", "note", "warn", "tip"}]}]`. Use 3-4 MECE groups of 2-3 numbers (e.g. scope / recording / traffic / content) |
| `insights` | section | Highlighted key findings, shown first. For agent-written reports; programmatic reports show their key numbers in tables/charts instead of generated bullet text |
| `info` | section | (i) icon next to the heading: The generic "how to read this" text |
| `tables` | section | Several tables (in addition to `table`) |
| `bars` | table | Column indexes that get in-cell bars scaled to the column maximum (numeric cells) |
| `column_tips` | table | `{"Column": "tooltip"}` (i) icons in the header |
| `title` (+ `title_tip`) | table, figure | Short bold title above the element. Every table and chart should have one (several charts of one kind: title on the first). On a compact figure, the caption + source go into the title's (i) |
| `visible_rows` | table | Rows shown before a "+ X more" button (default 15); printing shows all rows. No accordions for tables |
| `decimals` | table | `{"<col index>": n}`. Each column uses ONE number format: Whole numbers stay whole; a column with any decimal value shows all its numbers with the same decimals (default 1). Round values like FPS or ms to integers before passing them |
| `{"cells": [...], "class": "sub"}` | table row | Lightly shaded sub-row, e.g. the channel rows below a per-device total |
| `group_first_col: true` | table | Darker 1 px separator line where the first-column value changes (e.g. a new device); the value is shown only on the group's first row |
| `tables_first: true` | section | Show the section's tables above its figures |
| `{"stack": [{"pct", "color"}], "tip"}` | table cell | Stacked 100% bar (e.g. time split per state) |
| `data-tip="..."` | any SVG/HTML element | Hover tooltip (text only), e.g. on elements of a hand-written SVG |

"Data used" is an (i) hover after the subtitle. Method, Caveats and Reproduce are optional closed accordions.

## Fixed frame vs flexible middle

| Part | Fixed? | Notes |
|---|---|---|
| title | Always | |
| question | Optional | Kept in `report.json` for follow-ups; not rendered (the title says what the report covers) |
| summary, confidence | Optional | For conclusion-type reports (e.g. a fault deep dive). Skip them in programmatic overview reports - their findings go into section `insights` |
| provenance | Always | Rendered as the "Data used" hover after the subtitle. Local time first (`time_range_local` + `display_tz` from `display_time.py`), UTC in brackets. The footer names the data source from `data_source` / `sources` / `tools` (Amazon Athena, Google BigQuery, the MF4 tools on log files, a local Parquet lake): Name it there (e.g. "S3 data lake via Amazon Athena, database ..."). `processing` (one sentence) replaces the derived footer text |
| kpis / kpi_groups | Optional | Headline numbers; `warn: true` colors a value red. Labels must fit one line (~14 characters) |
| sections | Flexible | See guidelines below |
| method, caveats | Optional | Short, factual. Leave out of programmatic overview reports (their rules live in the skill) |
| reproduce | Optional | Script path + command (and SQL for Athena); can go into `data` instead |
| data | Recommended | The numbers behind the charts, for follow-up questions |

## Guidelines for the flexible part ("a 10x engineer reporting to their manager")

- Key insights first, generic explanations hidden (`info`, `column_tips`, `compact`); every chart and table titled
- Every figure must support a claim in the summary or the section insights. No chart "because the data exists"
- One **key finding** figure that proves the conclusion (e.g. annotated state timeline)
- 2-4 **context** figures that let the reader verify it - the trigger, the response, and the
  conditions that rule out alternative explanations (e.g. supply voltage + temperature)
- Same time window and the same event markers (`charts.mark_event`) across related plots; shade episodes
  (`ax.axvspan(start, end, color=COLORS["red"], alpha=0.15)`)
- One message per chart, units in the axis labels, local time axes (`charts.local_time_axis(ax, dt_, first, last)`)
- Every figure has a caption (what to look for) and a source (device + message.signal)
- Put the numbers behind the charts (episode list, segment table) into `data`
- Get the data in `analysis.py` from canedge-lake-query (`from _backends import connect`; resampled or aggregated
  queries, local lake or Athena) and note the data source + SQL in `provenance` / `reproduce`
- Prefer a compact table over a chart when the reader needs exact values
- Keep it as short as the question allows - not more, not less
