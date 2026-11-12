# results.json: The analysis page spec

`build_analysis.py` turns `results.json` (written by the analysis script, usually with the helpers in
`build_analysis.py`) into one self-contained `report.html` on the shared dashboard kit (canedge-reports): The filter
bar (devices with meta names, period, activity brush), tabs, KPI groups, tables, uPlot charts, a GNSS map. The page
never queries the lake: Everything it shows is in the file.

```json
{
  "schema": "canedge-lake-analysis/1",
  "title": "Duty cycle - 4 vehicles",
  "question": "The user's question, verbatim (shown above the conclusion)",
  "summary": ["**All three trucks drive alike**: About 1 in 10 driving hours is above 1,800 rpm", "..."],
  "confidence": "Medium - what it rests on (not shown)",
  "tz": "Europe/Berlin",
  "devices": {"12345678": "Truck 1 (12345678)", "AABBCCDD": null},
  "window": ["2026-01-01T00:00:00Z", "2026-04-01T00:00:00Z"],
  "step": 86400,
  "activity": "daily",
  "facts": {"daily": {"columns": ["device", "t", "signal", "n", "sum", "max"], "rows": [["12345678", 1767225600, "enginespeed", 8100, 9.2e6, 2210]]}},
  "findings": [{"title": "Time above 1,800 rpm", "value": "2.1x", "note": "Truck 1 vs the fleet", "devices": ["12345678"],
                "from": "2026-02-10T00:00:00Z", "to": "2026-02-12T00:00:00Z", "tab": "Context", "tip": "..."}],
  "overview": [{"type": "bars", "layout": "share", "...": "..."}],
  "tabs": [{"title": "Duty cycle", "tip": "Time per engine speed band", "panels": [{"type": "bars", "...": "..."}]}],
  "context": {"signals": [], "tracks": []},
  "provenance": {"devices": [], "time_range_utc": [], "sources": ["Athena database ...", "SQL ..."], "dbc_files": {},
                 "extra": {"Scan": "1.2 GB, est. $0.006 (rough)"}}
}
```

- `devices`: ID -> meta name (or null). Results are keyed by device ID; names are labels (not unique)
- `window`, `step`: The period the filter bar covers, and its granularity (s). `step` = the facts' period (86400 for
  `summary --by day`, 3600 for `--by hour`)
- `activity`: The facts table whose rows (device + `t`) draw the brush (data per device and step)
- `provenance`: "Data used" (the canedge-reports report spec fields), shown in the (i) hover of the subtitle
- `question`: One muted line on the Overview. `summary`: The executive summary under it (see Overview).
  `confidence`: Not shown (for the chat and follow-ups)

## Facts (what the filters act on)

`facts.<name>` = a table with `columns` + `rows`. Columns `device` and `t` (epoch seconds, the period start) make it
filterable: The page keeps the rows of the selected devices with `from <= t < to`, then aggregates. Only mergeable
values belong here: Counts, sums, min, max, seconds per bin, episode rows. Helpers: `facts_summary()`,
`facts_hist()`, `facts_episodes()`.

## Panels

Every panel: `type`, `title`, optional `tip` ((i) hover), `mode` (`filtered`, the default, or `fixed`).

- `fixed`: Values that do not merge (percentiles, compare scores, correlations, model output). Give `computed`:
  `{"devices": [...], "from": iso, "to": iso}`. The panel always shows those values, with a "computed for ..." line,
  and is dimmed when the selection differs from it and from the default view. Fixed tables carry their `columns`
  (strings or `{label, unit, bar, tip, decimals}`) + `rows` inline

Aggregates (`agg` on a facts column `col`, or `cols` for ratio): `sum`, `max`, `min`, `count` (rows), `mean`,
`ratio` (`sum(cols[0]) / sum(cols[1])`, e.g. the average from summary sum / n), `distinct` (e.g. devices).
`where` = `{"column": value}` (or a list of values) filters the rows first. `scale` multiplies the value (e.g.
1/3600 for hours), `unit` and `decimals` format it.

| type | Fields | Shows |
|---|---|---|
| `kpis` | `groups: [{title, color (blue, green, orange, red), items: [{label, facts, agg, col/cols, where, scale, unit, decimals, note, tip}]}]`; an item without `facts` shows its `value` (a computed number or text) | Stat panels (`DASH.kgroups`) |
| `table` | `facts, group: [cols], columns: [{label, facts, agg, col/cols, where, scale, unit, decimals, bar, tip}], sort: [i, -1]` (fixed: `columns` + `rows`); a column's own `facts` = values from other facts with the same group columns; `agg: "span"` = first to last period (YY/MM, `days: true` for YY/MM/DD) | One row per group (`DASH.table`: search, sort, in-cell bars). `device` in `group` adds the meta name. Compare entities: One row per car, one column per measure, one unit per column; short labels (unit in the header, the rest in `tip`), bars only where comparing the rows matters |
| `series` | `facts, y: {agg, col/cols, where, scale, unit, label}, split: "device"`; or inline `data: {t: [], series: [{label, values}]}` + `unit` (fixed, e.g. a resample result or a computed series) | Time series, a line per device over the periods (`series.js`), gaps where no data |
| `bars` | `facts, stack: "label", value: "seconds", where, scale, unit, order: [labels], palette: sequential \| categorical, layout: time \| share \| hist`; `layout: category` + `group` (default device), `value: {agg, col/cols, unit, decimals}`, `top` (or inline `items: [{label, value}]`) | time: Stacked bars per period (`stacked-bars.js`); share: One 100% bar per device; hist: One column per bin (histogram, `xlabel`); category: One horizontal bar per group, largest first, own scale per panel (compare devices per bin in a table, not in two panels). `stack: "device"`: Meta names, one colour per device |
| `episodes` | `facts` (from `facts_episodes()`), `value` (column for the peak), `unit` | One timeline row per device + a table; "show" zooms the page to the episode |
| `context` | `signals: [keys]` (default all), `map: true`, `markers: "<facts>"` (e.g. the episodes: a point on the route where each started) | Synced charts of the context signals (+ GNSS map with the route colored by the first signal) for the selected devices and period |
| `aligned` | `data: {t: [s from onset], events: [{label, role: problem \| reference, values}]}`, `unit`, `decimals`, `roles: {problem: "With fault", reference: "Same moment, no fault"}`, `xlabel`, `height` (from `events.py align --trace`; `mode: fixed`) | Event-aligned traces: A thin line per event, a bold median per role, dashed line at the onset; hover = the medians. The evidence visual for "what happens before / at / after a fault onset vs normal" |
| `custom` | `fn` (a function in the `--custom-js` file: `ANALYSIS.panel("fn", (el, view, R) => ...)`), `data` | The unique visual. `view` = `{devs: Set, from, to}`; use the kit (`SERIES`, `stackedBars`, `TRACK`, `DASH.table`) and `DASH.colorOf(deviceId)` for device colors |
| `note` | `text` | One short line, only where a hover cannot hold it (prefer `tip`) |

Colors: Each device keeps one color on every chart of the page (`DASH.colorOf`, by device ID); a series named
after a device (meta name or ID) takes its color, other series names keep one color across charts.

## Overview: Answer first

The first tab answers the question the way a decision maker reads it: The question (one muted line, `question`),
the takeaway as the page title (`answer.headline`: one sentence that answers it, with the number), the executive
summary, one hero card, the evidence visual, then max 3 supporting cards. Explanations go into `tip`
hovers, the detailed reasoning into the chat.

**Executive summary** (`summary`, 3-5 bullets, the only text block): Written like a consulting executive summary for a
manager who reads only this. The question above it is shown at the same size, a line separates it from the cards.
- Ranked by importance. Bullet 1 gives the **evidence for the headline**: What was compared and how close or far
  apart it is ("All three cars spend about the same share of time at each speed, within 4 percentage points").
  A number only belongs in a bullet if it supports the claim of that bullet: "1 in 10 hours above 90 km/h" says
  how one car drives, not that the cars drive alike
- Then what backs it up, the one real difference (with a likely reason, marked as such: "possibly"), and the caveat
  that changes how to read the page ("Truck 2 accounts for half of all driving hours, so fleet totals mostly reflect it")
- Plain, natural sentences, one line each (max 100 characters; `build_analysis.py` warns). Not every bullet in the
  form "label: detail". No jargon: No signal names, SQL or statistics terms; "half of all driving hours", not "51%
  of moving seconds"
- `**bold**` on the key phrase only, at most once per bullet; not every bullet needs one
- Each bullet adds something; one that only repeats a card's number, or that the reader cannot act on, is cut
- Write the bullets from the computed numbers in the script (so they stay true when the data changes), and phrase a
  claim the data does not prove as a lead
- `answer.headline`: The conclusion in words, one line (max 68 characters, also checked); the numbers go into the
  bullets

```json
"answer": {"headline": "Truck 1 runs 2.1x more time above 1,800 rpm than the other trucks",
           "title": "Truck 1: time above 1,800 rpm",
           "value": {"facts": "bands", "agg": "share", "col": "seconds", "num": {"label": [">= 1800"]}, "devices": ["12345678"],
                     "scale": 100, "unit": "%", "decimals": 0},
           "ref": {"label": "other trucks", "value": {"...same...": "", "devices": "others"}},
           "delta": "ratio", "good": "lower", "tip": "...",
           "devices": ["12345678"], "from": "...", "to": "...", "tab": "Duty cycle",
           "visual": {"type": "bars", "layout": "share", "...": "..."}},
"findings": [{"title": "Periods above 2,000 rpm", "value": {"facts": "high", "agg": "count"}, "note": "...", "tab": "..."}]
```

- `value` / `ref.value`: A literal or a facts spec (as a KPI item; `devices` limits it, `"others"` = the
  selected devices without the card's own). A number literal takes the card's `unit`, `decimals` and `scale` (the
  ref: its own, else the card's), or give `{"value": 309, "unit": "Wh/km", "decimals": 0}`. Facts values follow the
  filters (the dynamic part of the page). A delta needs numbers on both sides (a text literal like "10%" shows no
  delta); a value of 0 shows a `diff`
- `agg: "share"`: `sum(col)` of the rows matching `num` / `sum(col)` of all rows after `where`
- `delta`: `ratio` (2.1x), `pct` (+12%) or `diff` (+1.5 unit; shares: points) vs the reference; `good`: `higher`
  / `lower` colors it green / red (default neutral)
- `findings`: The supporting cards, same fields (no headline / visual). `overview: [panels]` adds more visuals
- Cards: Equal width, fixed rows (title, value, delta, note), each one line. Title max 22 characters, note max 34
  (`build_analysis.py` warns; longer text is cut off with "..."). With `devices` / `from` / `to` / `tab`, the whole
  card is clickable and jumps there (arrow in the title row)

## Context (signals + GNSS around the findings)

```json
"context": {
  "signals": [{"key": "12345678|can1_eec1_s00|enginespeed", "device": "12345678", "label": "Engine speed",
               "unit": "rpm", "values": {"0": "Off"},
               "tiers": [{"interval": 600, "from": 1767225600, "to": 1775001600, "t": [], "v": []},
                         {"interval": 1, "from": 1770681600, "to": 1770685200, "t": [], "v": []}]}],
  "tracks": [{"device": "12345678", "color_by": "12345678|can1_eec1_s00|enginespeed",
              "tiers": [{"interval": 60, "from": 0, "to": 0, "t": [], "lat": [], "lon": []}]}]
}
```

`"trips": [{"device", "color_by", "label", "unit", "trips": [{"t", "end", "lat", "lon", "v"}]}]`: Trips from the data
(1 min buckets, split at pauses over 5 min): Start, end, first position, average of the first context signal.

The map shows a route line only from detailed data (60 s per point or finer, e.g. the detail tier around a
finding), broken at real pauses; otherwise one dot per trip start of every selected car, colored by the trip's
average (positions averaged over minutes or hours, joined by lines, are meaningless). Charts break at pauses too.
Context charts: Lines at 5 min per point or finer (broken at pauses), otherwise one bar per car and day (week
for selections over 120 days), labeled "daily average".
`context.py` builds it (resample per tier, point budget, trips). The page draws the finest tier that covers the selected
period; when the selection is finer than any tier, it says so: Re-run `context.py` for that window, rebuild.
