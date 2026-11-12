"""Build a Grafana dashboard JSON for the CANedge Athena, BigQuery or Synapse data source from a compact spec, then
validate it.

Mirrors the official CANedge templates (assets/templates/ + the README links):
  - Athena: Datasource {type grafana-athena-datasource, uid 'Amazon Athena'}, tables tbl_${device:csv}_<message>
    (lower case), panel SQL with $__timeGroup(t, $__interval) + date_created BETWEEN $__rawTimeFrom('yyyy/MM/dd')
    AND $__rawTimeTo('yyyy/MM/dd') AND $__timeFilter(t)
  - BigQuery: Datasource {type grafana-bigquery-datasource, uid 'Google BigQuery'}, a $dataset variable, tables
    ${dataset}.tbl_${device:csv}_<Message> (real case, e.g. CAN9_GnssSpeed), panel SQL with
    TIMESTAMP_MILLIS(DIV(UNIX_MILLIS(t), $__interval_ms) * $__interval_ms) as time + $__timeFilter(t)
  - Synapse: Datasource {type mssql, uid 'Microsoft SQL Server'} (Grafana's core Microsoft SQL Server data source),
    tables read as OPENROWSET(BULK '${device:csv}/<Message>/*/*/*/*', DATA_SOURCE = 'ParquetDataLake', FORMAT =
    'PARQUET') AS r (real case), panel SQL with the template's filepath() folder filter, $__timeFilter(t) and
    $__interval_ms time bucket (SYN_BUCKET)
  - All: The device dropdown from tbl_aggregations_devicemeta (meta names shown, IDs as values)
  - Device dashboards: The $device variable and the 'dynamic' panel ($message > $signal > $signal_value >
    $aggregation dropdowns) are copied from the bundled dynamic template, so every dashboard selects devices and
    signals the same way
Spec format: references/dashboard-spec.md.

With a data source (--athena / --bigquery / --synapse / --project / --lake, + --env, as in canedge-lake-query) the
build resolves BigQuery/Synapse message case and the BigQuery dataset from the lake, then checks every table and
column (schema only).

Usage:
  python build_dashboard.py --spec spec.json --out dashboard.json [--backend athena|bigquery|synapse]
                            [--datasource-uid UID] [--athena | --bigquery | --synapse [--env NAME]]

Import in Grafana: Dashboards > New > Import > upload dashboard.json > Load.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import validate_dashboard  # noqa: E402

GRID_W = 24
DEFAULT_SIZE = {"timeseries": (12, 8), "stat": (4, 4), "table": (24, 8), "geomap": (12, 11), "bargauge": (8, 8),
                "barchart": (12, 8), "piechart": (6, 8), "state-timeline": (12, 6), "text": (6, 4), "row": (24, 1),
                "dynamic": (10, 7)}
# CAN signals hold their value until the next frame: Stepped lines, never linear interpolation
STEP_STYLE = {"lineInterpolation": "stepAfter", "lineWidth": 2, "showPoints": "auto", "pointSize": 1,
              "gradientMode": "opacity", "fillOpacity": 40}
# Grafana units that switch scale (1,200 km -> "1.2 Mm", 30 h -> "1.3 day"): Fixed suffixes in stats, bars, pies, tables
FIXED_UNIT = {"lengthkm": "suffix: km", "lengthm": "suffix: m", "kwatth": "suffix: kWh", "watth": "suffix: Wh",
              "h": "suffix: h"}
UNIT_TEXT = {"volt": "V", "amp": "A", "watt": "W", "kwatt": "kW", "watth": "Wh", "kwatth": "kWh", "celsius": "°C",
             "fahrenheit": "°F", "percent": "%", "degree": "°", "litre": "L", "velocitykmh": "km/h", "velocityms": "m/s",
             "velocitymph": "mph", "rotrpm": "rpm", "lengthm": "m", "lengthkm": "km", "s": "s", "ms": "ms", "h": "h",
             "pressurebar": "bar", "pressurekpa": "kPa", "pressurepsi": "psi", "hertz": "Hz", "accMS2": "m/s²"}
DEVICE_COLORS = ["blue", "orange", "green", "red", "purple", "yellow", "dark-blue", "dark-orange", "dark-green",
                 "dark-red"]
DASH_DS = {"type": "datasource", "uid": "-- Dashboard --"}
TEMPLATES = Path(__file__).resolve().parent.parent / "assets" / "templates"
DYNAMIC_VARS = ("message", "signal", "signal_value", "aggregation")   # the template chain after $device
# Route color scale of the official GPS templates (speed in km/h)
SPEED_KMH_STEPS = [(None, "dark-green"), (20, "green"), (30, "light-green"), (40, "super-light-green"),
                   (50, "super-light-yellow"), (60, "yellow"), (70, "semi-dark-yellow"), (80, "super-light-orange"),
                   (90, "light-orange"), (100, "orange"), (110, "semi-dark-orange"), (120, "dark-orange"),
                   (130, "light-red"), (140, "red"), (150, "dark-red")]
COLOR = "#3d85c6"
STATE_COLORS = ["green", "blue", "orange", "red", "purple", "yellow", "dark-green", "dark-blue", "dark-orange", "dark-red"]
CONN = {"catalog": "__default", "database": "__default", "region": "__default",
        "resultReuseEnabled": True, "resultReuseMaxAgeInMinutes": 60}
# Synapse time bucket, as in the official Grafana-Synapse templates: Below 1 s the millisecond part per $__interval_ms,
# from 1 s the whole seconds per interval (Grafana's intervals from 1 s are whole seconds; CEILING = 1 below 1 s)
SYN_BUCKET = ("DATEADD(MILLISECOND, (DATEPART(MILLISECOND, t) / $__interval_ms) * $__interval_ms, DATEADD(SECOND, "
              "FLOOR(DATEDIFF(SECOND, '2020', t) / CEILING($__interval_ms / 1000.0)) * CEILING($__interval_ms / "
              "1000.0), '2020'))")
SYN_GROUP = ("FLOOR(DATEDIFF(SECOND, '2020', t) / CEILING($__interval_ms / 1000.0)), DATEPART(MILLISECOND, t) / "
             "$__interval_ms")
SYN_DATA_SOURCE = "ParquetDataLake"
BACKENDS = {
    "athena": {"type": "grafana-athena-datasource", "uid": "Amazon Athena", "bucket": "$__timeGroup(t, $__interval)",
               "group": "$__timeGroup(t, $__interval)",
               "where": "date_created BETWEEN $__rawTimeFrom('yyyy/MM/dd') AND $__rawTimeTo('yyyy/MM/dd')"
                        "\n    AND\n    $__timeFilter(t)"},
    "bigquery": {"type": "grafana-bigquery-datasource", "uid": "Google BigQuery",
                 "bucket": "TIMESTAMP_MILLIS(DIV(UNIX_MILLIS(t), $__interval_ms) * $__interval_ms)", "group": "time",
                 "where": "$__timeFilter(t)"},
    "synapse": {"type": "mssql", "uid": "Microsoft SQL Server", "bucket": SYN_BUCKET, "group": SYN_GROUP,
                "where": "CONCAT(r.filepath(1), '-', r.filepath(2), '-', r.filepath(3)) BETWEEN CONVERT(date, "
                         "$__timeFrom()) AND CONVERT(date, $__timeTo())\n    AND\n    $__timeFilter(t)"},
}


class SpecError(Exception):
    pass


def ds(ctx: dict) -> dict:
    return {"type": ctx["type"], "uid": ctx["uid"]}


def template_parts(ctx: dict) -> tuple[dict, dict]:
    """Variables (by name) + the panel of the bundled dynamic template of the backend, datasource set to ctx."""
    path = TEMPLATES / f"{ctx['name']}-dynamic-dashboard-template.json"
    d = json.loads(path.read_text(encoding="utf-8"))

    def fix_ds(o):
        if isinstance(o, dict):
            if isinstance(o.get("datasource"), dict):
                o["datasource"] = ds(ctx)
            for v in o.values():
                fix_ds(v)
        elif isinstance(o, list):
            for v in o:
                fix_ds(v)
    fix_ds(d)
    return {v["name"]: v for v in d["templating"]["list"]}, d["panels"][0]


def build_dynamic(p: dict, ctx: dict) -> dict:
    """The template's dynamic signal panel (any message + signals of the selected device, AVG/MIN/MAX dropdown)."""
    panel = copy.deepcopy(template_parts(ctx)[1])
    for k in ("gridPos", "id"):
        panel.pop(k, None)
    panel["title"] = p.get("title", "Message: ${message:csv} | Signal: ${signal}")
    panel["description"] = p.get("description", "Pick any message and signal(s) of the selected device in the "
                                                "dropdowns above (aggregation: AVG / MIN / MAX)")
    panel["options"]["tooltip"] = {"mode": "multi", "sort": "none"}   # as in the GPS templates
    panel["fieldConfig"]["defaults"].setdefault("custom", {}).update(STEP_STYLE)   # CAN signals: stepped lines
    return panel


# Athena SHOW COLUMNS pads the names with spaces: The capture group trims them (clean legend), date_created + t out
ATHENA_SIGNAL_REGEX = r"/^(?!(?:date_created|t)\s*$)(\S+)\s*$/"


def dynamic_variables(ctx: dict, spec_panel: dict) -> list[dict]:
    """$message, $signal, $signal_value (hidden), $aggregation from the template; optional default message + signal
    (one signal, not All)."""
    tv = template_parts(ctx)[0]
    out = [copy.deepcopy(tv[n]) for n in DYNAMIC_VARS]
    if ctx["name"] == "athena":
        out[1]["regex"] = ATHENA_SIGNAL_REGEX
    if spec_panel.get("message"):
        m = spec_panel["message"]
        out[0]["current"] = {"selected": True, "text": m, "value": m}
    if spec_panel.get("signal"):
        sig = spec_panel["signal"]
        sig = [x.lower() if ctx["name"] == "athena" else x for x in (sig if isinstance(sig, list) else [sig])]
        out[1]["current"] = {"selected": True, "text": sig, "value": sig}
    return out


def target(sql: str, ctx: dict, ref: str = "A", fmt: int = 0) -> dict:
    if ctx["name"] == "synapse":   # Grafana's Microsoft SQL Server data source
        return {"datasource": ds(ctx), "editorMode": "code", "format": "table" if fmt == 1 else "time_series",
                "rawQuery": True, "rawSql": sql, "refId": ref}
    if ctx["name"] == "bigquery":
        return {"datasource": ds(ctx), "editorMode": "code", "format": fmt, "location": "", "project": "",
                "rawQuery": True, "rawSql": sql, "refId": ref}
    return {"connectionArgs": CONN, "datasource": ds(ctx), "format": fmt, "rawSQL": sql, "refId": ref}


def openrowset(folder: str) -> str:
    """Synapse: The official read of one table folder (filepath(1..3) = its yyyy/mm/dd folders)."""
    return (f"OPENROWSET(\n        BULK '{folder}/*/*/*/*',\n        DATA_SOURCE = '{SYN_DATA_SOURCE}',\n"
            "        FORMAT = 'PARQUET'\n    ) AS r")


def panel_table(message: str, device: str | None, ctx: dict) -> str:
    """Athena: lower case. BigQuery: ${dataset}. + device ID upper case + the message as given (real case).
    Synapse: OPENROWSET over <DEVICE>/<Message> (real case; aggregations/<name> for tbl_aggregations_<name>)."""
    if ctx["name"] == "synapse":
        m = re.fullmatch(r"tbl_(aggregations|[0-9A-Fa-f]{8})_(\w+)", message, re.I)
        if m and m.group(1).lower() == "aggregations":
            name = m.group(2).lower()
            return "tbl_aggregations_devicemeta" if name == "devicemeta" else openrowset(f"aggregations/{name}")
        if m:
            return openrowset(f"{m.group(1).upper()}/{m.group(2)}")
        return openrowset(f"{device.upper() if device else '${device:csv}'}/{message}")
    bq = ctx["name"] == "bigquery"
    pre = "${dataset}." if bq else ""
    if message.lower().startswith("tbl_"):
        return pre + message.lower()
    msg = message if bq else message.lower()
    dev = (device.upper() if bq else device.lower()) if device else "${device:csv}"
    return f"{pre}tbl_{dev}_{msg}"


def aggs(p: dict) -> list[str]:
    a = p.get("agg", "MAX" if p.get("type") == "state-timeline" else "AVG")
    return [x.upper() for x in (a if isinstance(a, list) else [a])]


def bucket_sql(cols: str, table: str, ctx: dict, where: str | None = None) -> str:
    """The template query: Resampled aggregates of one message table, without ORDER BY."""
    return (f"SELECT\n    {ctx['bucket']} as time,\n    {cols}\nFROM\n    {table}\nWHERE\n    {ctx['where']}"
            + (f"\n    AND\n    ({where})" if where else "") + f"\nGROUP BY\n    {ctx['group']}")


def signal_sql(p: dict, device: str | None, ctx: dict, extra_cols: str = "") -> str:
    if not p.get("signals"):
        raise SpecError(f"panel '{p.get('title')}': 'signals' is required with 'message'")
    cols = extra_cols + ", ".join(f"{a}({s.lower()}) as {a}_{s.lower()}" for s in p["signals"] for a in aggs(p))
    return bucket_sql(cols, panel_table(p["message"], device, ctx), ctx, p.get("where"))


def value_mappings(vm: dict) -> list[dict]:
    """{"0": "Off", "1": {"text": "On", "color": "green"}} -> one Grafana value mapping (colors assigned in order)."""
    opts = {}
    for i, (k, v) in enumerate(vm.items()):
        v = v if isinstance(v, dict) else {"text": str(v)}
        opts[str(k)] = {"index": i, "text": v["text"], "color": v.get("color", STATE_COLORS[i % len(STATE_COLORS)])}
    return [{"type": "value", "options": opts}]


def field_config(p: dict, mode: str = "palette-classic") -> dict:
    d = {"color": {"mode": mode} if mode != "fixed" else {"mode": "fixed", "fixedColor": COLOR},
         "mappings": value_mappings(p["value_map"]) if p.get("value_map") else []}
    for k in ("unit", "min", "max", "decimals"):
        if k in p:
            d[k] = p[k]
    steps = [{"color": "green", "value": None}]
    for v, c in p.get("thresholds", []):
        steps.append({"color": c, "value": v})
    d["thresholds"] = {"mode": "absolute", "steps": steps}
    return d


def timeseries_custom() -> dict:
    return {"axisBorderShow": False, "axisCenteredZero": False, "axisColorMode": "text", "axisLabel": "",
            "axisPlacement": "auto", "barAlignment": 0, "drawStyle": "line",
            "hideFrom": {"legend": False, "tooltip": False, "viz": False}, "insertNulls": 300000,
            "lineStyle": {"fill": "solid"}, "scaleDistribution": {"type": "linear"}, "spanNulls": False,
            "stacking": {"group": "A", "mode": "none"}, "thresholdsStyle": {"mode": "off"}, **STEP_STYLE}


def soft_limits(fc: dict, custom: dict) -> None:
    """Never a fixed axis limit that hides values: max -> axisSoftMax, a min other than 0 -> axisSoftMin."""
    if "max" in fc:
        custom["axisSoftMax"] = fc.pop("max")
    if fc.get("min", 0) != 0:
        custom["axisSoftMin"] = fc.pop("min")


def unit_once(fc: dict, title: str) -> tuple[str, str]:
    """Bars, bar gauges, pies: Plain numbers on the bars/slices (unit 'none'), the unit once in the title (or axis
    label). Returns (title, unit text)."""
    unit = fc.pop("unit", None)
    fc["unit"] = "none"
    text = UNIT_TEXT.get(unit, unit.split(":", 1)[1].strip() if unit and ":" in unit else unit or "")
    if text and f"({text}" not in title and f"{text})" not in title:
        title = f"{title} ({text})" if title else text
    return title, text


def color_overrides(colors: dict, regex: bool = False) -> list[dict]:
    """One fixed color per series name ({"EV01": "blue"}); regex: per device ID inside the series name."""
    return [{"matcher": {"id": "byRegexp", "options": f".*{n}.*"} if regex else {"id": "byName", "options": n},
             "properties": [{"id": "color", "value": {"mode": "fixed", "fixedColor": c}}]} for n, c in colors.items()]


def build_geomap(p: dict, panel: dict, ctx: dict, device: str | None) -> dict:
    title = p.get("title", "")
    pos = p.get("position_message")
    if not pos:
        raise SpecError(f"panel '{title}': geomap needs 'position_message' (e.g. can9_gnsspos)")
    lat, lon = p.get("lat", "latitude"), p.get("lon", "longitude")
    valid = p.get("valid_signal", "positionvalid")
    pos_cols = f"AVG({lon}) as AVG_{lon}, AVG({lat}) as AVG_{lat}" + (f", AVG({valid}) as AVG_{valid}" if valid else "")
    targets = [target(bucket_sql(pos_cols, panel_table(pos, device, ctx), ctx) + "\nORDER BY\n    time ASC", ctx, "A", 0)]
    filters = [{"config": {"id": "isNull", "options": {}}, "fieldName": f"AVG_{lon}"},
               {"config": {"id": "isNull", "options": {}}, "fieldName": f"AVG_{lat}"}]
    if valid:
        filters.append({"config": {"id": "equal", "options": {"value": 0}}, "fieldName": f"AVG_{valid}"})
    transformations, color_field = [], None
    if p.get("color_message") and p.get("color_signal"):
        cs = p["color_signal"].lower()
        sql_b = bucket_sql(f"AVG({cs}) as AVG_{cs}", panel_table(p["color_message"], device, ctx), ctx)
        targets.append(target(sql_b + "\nORDER BY\n    time ASC", ctx, "B", 1))
        transformations.append({"id": "merge", "options": {}})
        color_field = f"AVG_{cs}"
        if p.get("color_scale"):
            color_field = f"AVG_{cs}_scaled"
            transformations.append({"id": "calculateField", "options": {
                "alias": color_field, "binary": {"left": f"AVG_{cs}", "operator": "*", "right": str(p["color_scale"])},
                "mode": "binary", "reduce": {"reducer": "sum"}}})
        filters.append({"config": {"id": "isNull", "options": {}}, "fieldName": f"AVG_{cs}"})
    transformations.append({"id": "filterByValue", "options": {"filters": filters, "match": "any", "type": "exclude"}})
    panel.update({
        "fieldConfig": {"defaults": {**field_config(p, "thresholds"), "custom": {"hideFrom": {"legend": False, "tooltip": False, "viz": False}}},
                        "overrides": []},
        "options": {"basemap": {"config": {}, "name": "Layer 0", "opacity": 0.6, "tooltip": True, "type": "osm-standard"},
                    "controls": {"mouseWheelZoom": True, "showAttribution": False, "showDebug": False,
                                 "showMeasure": False, "showScale": False, "showZoom": True},
                    "layers": [{"config": {"arrow": 0, "style": {
                        "color": {"field": color_field, "fixed": "blue"} if color_field else {"fixed": "blue"},
                        "lineWidth": 2, "opacity": 1, "size": {"fixed": 3, "max": 15, "min": 2},
                        "symbol": {"fixed": "img/icons/marker/circle.svg", "mode": "fixed"}}},
                        "location": {"latitude": f"AVG_{lat}", "longitude": f"AVG_{lon}", "mode": "coords"},
                        "name": "Route", "opacity": 1, "tooltip": True, "type": "route"}],
                    "tooltip": {"mode": "details"},
                    "view": {"allLayers": True, "id": "fit", "lat": 0, "lon": 0, "padding": 10, "shared": True, "zoom": 15}},
        "targets": targets})
    if p.get("thresholds") is None:   # color by value: The GPS templates' km/h speed scale, else plain blue
        steps = SPEED_KMH_STEPS if color_field else [(None, "blue")]
        panel["fieldConfig"]["defaults"]["thresholds"]["steps"] = [{"color": c, "value": v} for v, c in steps]
    panel["transformations"] = transformations + p.get("transformations", [])
    return panel


def build_panel(p: dict, ctx: dict, has_device_var: bool, notes: list | None = None,
                dev_colors: dict | None = None) -> dict:
    notes = notes if notes is not None else []
    typ = p.get("type", "timeseries")
    title = p.get("title", "")
    device = p.get("device")
    if p.get("message") and not device and not has_device_var and not p.get("devices") \
            and not p["message"].lower().startswith("tbl_"):
        raise SpecError(f"panel '{title}': Needs 'device', 'devices' or a {{\"type\": \"device\"}} variable")
    panel = {"datasource": ds(ctx), "description": p.get("description", ""), "title": title, "type": typ}
    transformations = []

    if typ == "row":
        return {"collapsed": False, "panels": [], "title": title, "type": "row"}
    if typ == "text":
        return {"type": "text", "title": title, "options": {"mode": "markdown", "content": p.get("content", "")}}
    if typ == "geomap":
        return build_geomap(p, panel, ctx, device)
    if typ == "dynamic":
        if not has_device_var:
            raise SpecError(f"panel '{title}': The dynamic panel needs a {{\"type\": \"device\"}} variable")
        return build_dynamic(p, ctx)

    # timeseries / stat / table / bargauge / barchart / piechart / state-timeline
    fmt = 0
    if p.get("from_panel"):
        sql = None   # the rows of another panel ('-- Dashboard --' data source), resolved in build()
    elif p.get("sql"):
        sql = p["sql"]
        fmt = p.get("format", 1 if typ in ("table", "piechart") else 0)
    elif p.get("message") and p.get("devices"):
        # Fleet panel: One series per device via UNION ALL + partitionByValues (as in the trip summary template)
        parts = [signal_sql(p, dv, ctx, extra_cols=f"'{dv.upper()}' as device, ") for dv in p["devices"]]
        sql = "\nUNION ALL\n".join(parts) + "\nORDER BY\n    time asc"
        fmt = 1
        transformations.append({"id": "partitionByValues", "options": {"fields": ["device"]}})
    elif p.get("message"):
        sql = signal_sql(p, device, ctx) + "\nORDER BY\n    time asc"
        fmt = 1 if typ == "table" else 0
    else:
        raise SpecError(f"panel '{title}': Needs 'message' + 'signals', 'sql' or 'from_panel'")
    if sql is not None:
        panel["targets"] = [target(sql, ctx, "A", fmt)]
    if p.get("fields"):   # keep only these columns (e.g. of a shared query)
        transformations.append({"id": "filterFieldsByName", "options": {"include": {"names": list(p["fields"])}}})
    legend = {"calcs": [], "displayMode": "list", "placement": "bottom", "showLegend": True}
    fc = field_config(p, "thresholds" if typ == "stat" and p.get("thresholds") else
                      "fixed" if typ in ("stat", "bargauge") else "palette-classic")
    if typ != "timeseries" and fc.get("unit") in FIXED_UNIT:
        fc["unit"] = FIXED_UNIT[fc["unit"]]
    reduce = {"calcs": [p.get("reduce", "lastNotNull")], "values": bool(p.get("rows")),
              "fields": f"/^{p['field']}$/" if p.get("field") else ""}
    if typ == "timeseries":
        custom = timeseries_custom()
        soft_limits(fc, custom)
        panel["fieldConfig"] = {"defaults": {**fc, "custom": custom}, "overrides": []}
        panel["options"] = {"legend": legend, "tooltip": {"mode": "multi", "sort": "none"}}
    elif typ == "stat":
        # One large value per panel (or one per device / row), background color (white text), no sparkline
        if p.get("message") and not p.get("devices") and len(p.get("signals", [])) * len(aggs(p)) > 1:
            notes.append(f"panel '{title}': A stat shows one value: One signal + aggregation per stat panel")
        panel["fieldConfig"] = {"defaults": fc, "overrides": []}
        panel["options"] = {"colorMode": "background", "graphMode": "none", "justifyMode": "center",
                            "orientation": "auto", "reduceOptions": reduce, "textMode": "auto",
                            "text": {"titleSize": 16, "valueSize": 40} if int(p.get("w", 4)) >= 4 else {}}
    elif typ == "table":
        panel["fieldConfig"] = {"defaults": {**fc, "custom": {"align": "auto", "filterable": True}}, "overrides": []}
        panel["options"] = {"showHeader": True, "cellHeight": "sm", "footer": {"show": False, "reducer": ["sum"]}}
    elif typ == "bargauge":
        panel["title"], _ = unit_once(fc, title)
        panel["fieldConfig"] = {"defaults": fc, "overrides": []}
        panel["options"] = {"displayMode": "basic", "orientation": "horizontal", "showUnfilled": True,
                            "valueMode": "color", "namePlacement": "top",
                            "reduceOptions": {**reduce, "calcs": [p.get("reduce", "max")]}}
    elif typ == "barchart":
        _, text = unit_once(fc, title)
        custom = {"axisLabel": p.get("axis_label", text), "axisPlacement": "auto", "axisColorMode": "text",
                  "axisCenteredZero": False, "fillOpacity": 85, "gradientMode": "none", "lineWidth": 1,
                  "scaleDistribution": {"type": "linear"}, "thresholdsStyle": {"mode": "off"},
                  "hideFrom": {"legend": False, "tooltip": False, "viz": False}}
        soft_limits(fc, custom)
        panel["fieldConfig"] = {"defaults": {**fc, "custom": custom}, "overrides": []}
        panel["options"] = {"orientation": "auto", "showValue": "auto", "stacking": "none", "xTickLabelRotation": 0,
                            "text": {"valueSize": 14}, "legend": legend, "tooltip": {"mode": "multi", "sort": "none"},
                            **({"xField": p["x_field"]} if p.get("x_field") else {})}
    elif typ == "piechart":
        # One slice per row (sql: a label column + a value column) or per series; plain numbers, unit in the title
        panel["title"], _ = unit_once(fc, title)
        fc.setdefault("decimals", 1)
        panel["fieldConfig"] = {"defaults": {**fc, "custom": {"hideFrom": {"legend": False, "tooltip": False,
                                                                          "viz": False}}}, "overrides": []}
        rows = p.get("rows", fmt == 1 or bool(p.get("from_panel")))
        panel["options"] = {"pieType": p.get("pie_type", "donut"), "displayLabels": ["percent"],
                            "reduceOptions": {**reduce, "values": bool(rows)},
                            "legend": {"showLegend": True, "displayMode": "table", "placement": "bottom",
                                       "values": ["value", "percent"]},
                            "tooltip": {"mode": "single", "sort": "none"}}
    elif typ == "state-timeline":
        # One row per field; value_map (from the DBC value table) gives each state a name + color
        panel["fieldConfig"] = {"defaults": {**field_config(p, "thresholds"), "custom": {
            "fillOpacity": 80, "hideFrom": {"legend": False, "tooltip": False, "viz": False}, "insertNulls": 300000,
            "lineWidth": 0, "spanNulls": False}}, "overrides": []}
        panel["options"] = {"alignValue": "left", "legend": {**legend, "showLegend": bool(p.get("value_map"))},
                            "mergeValues": True, "rowHeight": 0.9, "showValue": "auto",
                            "tooltip": {"mode": "single", "sort": "none"}}
    else:
        raise SpecError(f"panel '{title}': Unsupported type '{typ}' (timeseries, stat, table, bargauge, barchart, "
                        "piechart, state-timeline, geomap, dynamic, text, row)")
    ov = panel["fieldConfig"]["overrides"]
    if p.get("devices") and dev_colors:   # the same color per device on every panel
        ov += color_overrides({d.upper(): dev_colors[d.upper()] for d in p["devices"] if d.upper() in dev_colors}, True)
    ov += color_overrides(p.get("colors") or {})
    panel["transformations"] = transformations + p.get("transformations", [])
    return panel


def build_variable(v: dict, ctx: dict) -> dict:
    typ = v.get("type")
    if typ == "device":
        if v.get("devices"):
            vals = [d.upper() for d in v["devices"]]
            return {"current": {"selected": True, "text": vals[0], "value": vals[0]}, "hide": 0, "includeAll": False,
                    "multi": False, "name": "device", **({"label": v["label"]} if v.get("label") else {}),
                    "options": [{"selected": i == 0, "text": d, "value": d} for i, d in enumerate(vals)],
                    "query": ",".join(vals), "skipUrlSync": False, "type": "custom"}
        return copy.deepcopy(template_parts(ctx)[0]["device"])   # as in every official template
    if typ == "custom":
        return custom_variable(v["name"], [str(o) for o in v.get("options", [])], v.get("default"), v.get("label"),
                               bool(v.get("multi")))
    if typ == "textbox":
        cur = str(v.get("default", ""))
        return {"current": {"selected": False, "text": cur, "value": cur}, "hide": 0, "name": v["name"],
                "label": v.get("label", v["name"]), "options": [{"selected": True, "text": cur, "value": cur}],
                "query": cur, "skipUrlSync": False, "type": "textbox"}
    if typ == "query":
        return {"current": {"selected": True}, "datasource": ds(ctx), "definition": "", "hide": 0,
                "includeAll": bool(v.get("include_all")), "multi": bool(v.get("multi")), "name": v["name"],
                "label": v.get("label", v["name"]), "options": [],
                "query": v["sql"] if ctx["name"] == "synapse" else target(v["sql"], ctx, "tempvar", 1),
                "refresh": 2 if v.get("refresh_on_time_change") else 1, "regex": v.get("regex", ""),
                "skipUrlSync": False, "sort": 0, "type": "query"}
    raise SpecError(f"variable {v}: type must be device, custom, textbox or query")


def custom_variable(name: str, opts: list[str], default=None, label: str | None = None, multi: bool = False) -> dict:
    cur = str(default if default is not None else (opts[0] if opts else ""))
    return {"current": {"selected": True, "text": cur, "value": cur}, "hide": 0, "includeAll": False, "multi": multi,
            "name": name, "label": label or name, "options": [{"selected": o == cur, "text": o, "value": o} for o in opts],
            "query": ",".join(opts), "skipUrlSync": False, "type": "custom"}


def layout(panels: list[dict], specs: list[dict]) -> None:
    """Skyline layout on the 24-column grid in spec order: Each panel goes to the highest free spot (left first),
    like Grafana's own vertical compaction. Rows span the full width below everything. Explicit x/y win."""
    col_h = [0] * GRID_W
    for p, s in zip(panels, specs):
        w, h = DEFAULT_SIZE.get(p["type"], (12, 8))
        w, h = min(GRID_W, int(s.get("w", w))), int(s.get("h", h))
        if p["type"] == "row":
            w = GRID_W
        if "x" in s and "y" in s:
            x, y = int(s["x"]), int(s["y"])
        elif p["type"] == "row":
            x, y = 0, max(col_h)
        else:
            x, y = min(((max(col_h[x:x + w]), x) for x in range(GRID_W - w + 1)), key=lambda c: (c[0], c[1]))[::-1]
        p["gridPos"] = {"h": h, "w": w, "x": x, "y": y}
        for c in range(x, min(GRID_W, x + w)):
            col_h[c] = max(col_h[c], y + h)
        if p["type"] == "row":
            col_h = [y + h] * GRID_W


def dash_link(x: dict) -> dict:
    """Dashboard link: {"title", "dashboard": uid | "url", "tooltip", "keep_time", "include_vars"}, e.g. a comparison
    dashboard <-> its trip dashboard."""
    url = f"/d/{x['dashboard']}" if x.get("dashboard") else x.get("url")
    if not url or not x.get("title"):
        raise SpecError(f"link {x}: needs 'title' + 'dashboard' (uid) or 'url'")
    return {"asDropdown": False, "icon": "dashboard" if x.get("dashboard") else "external link",
            "includeVars": bool(x.get("include_vars")), "keepTime": bool(x.get("keep_time")), "tags": [],
            "targetBlank": False, "title": x["title"], "tooltip": x.get("tooltip", ""), "type": "link", "url": url}


def build(spec: dict, uid_override: str | None = None, notes: list | None = None) -> dict:
    notes = notes if notes is not None else []
    name = spec.get("backend", "athena")
    if name not in BACKENDS:
        raise SpecError(f"backend '{name}': Use athena, bigquery or synapse")
    ctx = {"name": name, **BACKENDS[name]}
    ctx["uid"] = uid_override or spec.get("datasource_uid") or ctx["uid"]
    variables = [build_variable(v, ctx) for v in spec.get("variables", [])]
    if name == "bigquery":
        dataset = spec.get("dataset")
        if not dataset:
            dataset = "lakedataset1"
            if notes is not None:
                notes.append("No BigQuery dataset in the spec or from --bigquery: $dataset = 'lakedataset1' (the "
                             "template default) - the user picks the real one in the dashboard's dataset field")
        variables.insert(0, custom_variable("dataset", [dataset]))
    has_dev = any(v["name"] == "device" for v in variables)
    pspecs = spec.get("panels", [])
    if not pspecs:
        raise SpecError("spec has no panels")
    dyn = [p for p in pspecs if p.get("type") == "dynamic"]
    if len(dyn) > 1:
        raise SpecError("Only one 'dynamic' panel per dashboard (all would show the same dropdown selection)")
    if dyn and has_dev:
        taken = {v["name"] for v in variables} & set(DYNAMIC_VARS)
        if taken:
            raise SpecError(f"The dynamic panel adds ${', $'.join(DYNAMIC_VARS)}: Rename the spec variable(s) "
                            f"{', '.join(sorted(taken))}")
        at = next(i for i, v in enumerate(variables) if v["name"] == "device") + 1
        variables[at:at] = dynamic_variables(ctx, dyn[0])
        if not dyn[0].get("signal"):
            notes.append("The dynamic panel opens on All signals: Set its 'message' + 'signal' (one signal)")
    # the same color per device on every fleet panel: spec device_colors, else in order of first use
    dev_colors = {k.upper(): v for k, v in (spec.get("device_colors") or {}).items()}
    for d in (d.upper() for p in pspecs for d in p.get("devices") or []):
        dev_colors.setdefault(d, DEVICE_COLORS[len(dev_colors) % len(DEVICE_COLORS)])
    panels = [build_panel(p, ctx, has_dev, notes, dev_colors) for p in pspecs]
    layout(panels, pspecs)
    for i, p in enumerate(panels, 1):
        p["id"] = i
    by_title = {s.get("title"): p["id"] for s, p in zip(pspecs, panels) if s.get("title")}
    for s, p in zip(pspecs, panels):   # one query, several panels: Reuse its rows ('-- Dashboard --' data source)
        if s.get("from_panel"):
            if s["from_panel"] not in by_title:
                raise SpecError(f"panel '{s.get('title')}': from_panel '{s['from_panel']}' is no panel title")
            p["datasource"] = DASH_DS
            p["targets"] = [{"datasource": DASH_DS, "panelId": by_title[s["from_panel"]], "refId": "A"}]
    title = spec.get("title") or "CANedge dashboard"
    dash_uid = spec.get("uid") or re.sub(r"[^a-z0-9-]+", "-", title.lower()).strip("-")[:40]
    return {
        "annotations": {"list": [{"builtIn": 1, "datasource": {"type": "grafana", "uid": "-- Grafana --"},
                                  "enable": True, "hide": True, "iconColor": "rgba(0, 211, 255, 1)",
                                  "name": "Annotations & Alerts", "type": "dashboard"}]},
        "description": spec.get("description", ""), "editable": True, "fiscalYearStartMonth": 0, "graphTooltip": 1,
        "id": None, "links": [dash_link(x) for x in spec.get("links", [])], "liveNow": False, "panels": panels, "refresh": spec.get("refresh", ""),
        "schemaVersion": 38, "tags": spec.get("tags", [name, "canedge"]),
        "templating": {"list": variables}, "time": spec.get("time", {"from": "now-7d", "to": "now"}),
        "timepicker": {}, "timezone": spec.get("timezone", ""), "title": title, "uid": dash_uid, "version": 1,
        "weekStart": ""}


def resolve_from_lake(spec: dict, lake) -> None:
    """BigQuery/Synapse: Message names in their real case (CAN9_GnssSpeed) from the lake schema; BigQuery: the
    dataset."""
    if spec.get("backend") not in ("bigquery", "synapse"):
        return
    real = {}
    for t in lake.tables():
        if t["kind"] == "message" and t.get("bq_name"):
            real.setdefault(t["message"], t["bq_name"][len("tbl_") + 9:])
        elif t["kind"] == "message" and t.get("folder"):   # Synapse: <DEVICE>/<Message>
            real.setdefault(t["message"], t["folder"].partition("/")[2])
    for p in spec.get("panels", []):
        for k in ("message", "position_message", "color_message"):
            if p.get(k) and not p[k].lower().startswith("tbl_"):
                p[k] = real.get(p[k].lower(), p[k])
    if not spec.get("dataset") and hasattr(lake, "dataset"):
        spec["dataset"] = lake.dataset()


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--backend", choices=list(BACKENDS), help="Data source (default: spec backend, else the lake "
                                                               "flag, else athena)")
    ap.add_argument("--datasource-uid", help="Data source uid (default: spec datasource_uid, else 'Amazon Athena' / "
                                             "'Google BigQuery' / 'Microsoft SQL Server')")
    validate_dashboard.add_lake_args(ap)
    a = ap.parse_args()
    lake_flag = "athena" if a.athena else "bigquery" if a.bigquery else "synapse" if a.synapse else None
    notes: list[str] = []
    try:
        spec = copy.deepcopy(json.loads(a.spec.read_text(encoding="utf-8")))
        spec["backend"] = a.backend or spec.get("backend") or lake_flag or "athena"
        if lake_flag and lake_flag != spec["backend"]:
            raise SpecError(f"--{lake_flag} does not match backend '{spec['backend']}'")
        lake = None
        if a.athena or a.bigquery or a.synapse or a.project or a.lake:
            from _backends import LakeError, connect_args
            try:
                lake = connect_args(a)
                resolve_from_lake(spec, lake)
            except LakeError as e:
                raise SpecError(f"Lake: {e}") from e
        elif spec["backend"] in ("bigquery", "synapse"):
            notes.append(f"{'BigQuery table' if spec['backend'] == 'bigquery' else 'Synapse folder'} names are case "
                         f"sensitive (CAN9_GnssSpeed): Build with --{spec['backend']} to take them from the lake, or "
                         "write each message in its real case")
        dash = build(spec, a.datasource_uid, notes)
    except (OSError, json.JSONDecodeError, SpecError, KeyError) as e:
        print(f"ERROR: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 1
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(dash, indent=2), encoding="utf-8")
    res = validate_dashboard.validate(dash)
    if lake is not None:
        errs, warns = validate_dashboard.lake_check(dash, lake)
        res["errors"] += errs
        res["warnings"] += warns
    res["warnings"] = notes + res["warnings"]
    validate_dashboard.report(res, f"Wrote {a.out} (uid {dash['uid']})")
    print("  Import: Grafana > Dashboards > New > Import > upload the JSON > Load (or with a Grafana profile: "
          "grafana_api.py import)")
    return 1 if res["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
