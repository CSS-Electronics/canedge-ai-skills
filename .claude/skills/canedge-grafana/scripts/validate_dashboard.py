"""Validate a Grafana dashboard JSON for the CANedge Athena, BigQuery or Synapse (Microsoft SQL Server) data source
(static checks, no Grafana needed).

Checks: JSON validity + basic structure, unique panel ids, gridPos sanity (24-column grid, overlaps), datasource
references (uid conventions 'Amazon Athena' / 'Google BigQuery' / 'Microsoft SQL Server', ${DS_*} inputs,
'-- Dashboard --' panel links), target refIds/SQL presence, variables used vs defined, SQL macro sanity per backend
(unknown $__ macros, $__timeGroup without GROUP BY, missing $__timeFilter, Athena: date_created partition filter,
BigQuery: dataset prefix, no date_created on message tables; Synapse: OPENROWSET + filepath() folder pruning, no
LIMIT), device dashboards (single-select $device used in message tables) without the template's dynamic signal
panel, and unknown keys per Grafana's JSON schemas (assets/grafana-schema/,
refresh with update_schemas.py): Grafana silently ignores unknown keys, so a typo there never shows an error.

With a data source (--athena / --bigquery / --synapse / --project / --lake, + --env, as in canedge-lake-query): Every
message table and aggregated column in the panel SQL must exist in the lake (schema only, no data scanned). BigQuery
and Synapse message names are case sensitive: A wrong case is an error with the real name. A device dashboard without a
geomap while the lake has GPS positions (latitude + longitude) is a warning.

--summary prints panels, queries, variables, datasources and macros first (learn from a template, review a user
dashboard before/after editing it).

Usage:
  python validate_dashboard.py <dashboard.json> [--summary [--sql full|short|none]] [--json] [--strict]
                               [--athena | --bigquery | --synapse | --project P | --lake L] [--env NAME]

Exit code 1 if errors are found (--strict: also on warnings).
"""
from __future__ import annotations

import argparse
import difflib
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCHEMA_DIR = HERE.parent / "assets" / "grafana-schema"
LAKE_QUERY = HERE.parent.parent / "canedge-lake-query" / "scripts"
sys.path.insert(0, str(LAKE_QUERY))
from _backends import returns_raw_rows, strip_sql  # noqa: E402 (shared with lake-query's SQL checks)

BACKENDS = {"grafana-athena-datasource": "athena", "grafana-bigquery-datasource": "bigquery", "mssql": "synapse"}
DEFAULT_UID = {"athena": "Amazon Athena", "bigquery": "Google BigQuery", "synapse": "Microsoft SQL Server"}
# Plugin macros (athena-datasource pkg/athena/macros.go; google-bigquery-datasource pkg/bigquery/macros.go + macropro)
MACROS = {"athena": {"__timeFilter", "__timeFrom", "__timeTo", "__rawTimeFrom", "__rawTimeTo", "__timeGroup",
                     "__dateFilter", "__parseTime", "__unixEpochFilter", "__unixEpochGroup", "__table", "__column"},
          "bigquery": {"__timeFilter", "__timeFrom", "__timeTo", "__timeGroup", "__interval", "__interval_ms",
                       "__table", "__column"},
          # Grafana core Microsoft SQL Server data source (Synapse serverless SQL)
          "synapse": {"__time", "__timeEpoch", "__timeFilter", "__timeFrom", "__timeTo", "__timeGroup",
                      "__timeGroupAlias", "__unixEpochFilter", "__unixEpochNanoFilter", "__unixEpochNanoFrom",
                      "__unixEpochNanoTo", "__unixEpochGroup", "__unixEpochGroupAlias"}}
GLOBAL_VARS = {"__interval", "__interval_ms", "__range", "__range_s", "__range_ms", "__rate_interval", "__from",
               "__to", "__dashboard", "__user", "__org", "__name", "__all", "__auto", "__timezone", "__searchFilter",
               "__cell", "__value", "__field", "__series", "__data", "__url_time_range", "__url", "timeFilter"}
MACROS_WITH_ARGS = {"__timeFilter", "__timeGroup", "__rawTimeFrom", "__rawTimeTo", "__dateFilter", "__parseTime",
                    "__unixEpochFilter", "__unixEpochGroup", "__time", "__timeEpoch", "__timeGroupAlias",
                    "__unixEpochNanoFilter", "__unixEpochGroupAlias"}
# Table references: tbl_<device ID | ${device} | $device | aggregations>_<message> (Athena lower case, BigQuery real case)
TABLE_REF = re.compile(r"\btbl_(\$\{device(?::\w+)?\}|\$device\b|[0-9A-Fa-f]{8}|aggregations)_(\$\{?\w+(?::\w+)?\}?|\w+)")
AGG_COL = re.compile(r"\b(?:AVG|MIN|MAX|SUM|COUNT|STDDEV|STDDEV_POP|VARIANCE|ARBITRARY|ANY_VALUE|APPROX_PERCENTILE|"
                     r"MAX_BY|MIN_BY|LAST_VALUE|FIRST_VALUE)\s*\(\s*(?:DISTINCT\s+)?[\"`]?([A-Za-z_]\w*)\b[\"`]?(?!\s*\()", re.I)  # not a function call
# Synapse: OPENROWSET(BULK '<device>/<message>/*/*/*/*', ...) = the table of that device + message folder
BULK_REF = re.compile(r"\bBULK\s+N?'/?(\$\{device(?::\w+)?\}|\$device\b|[0-9A-Fa-f]{8}|aggregations)/"
                      r"(\$\{?\w+(?::\w+)?\}?|\w+)/", re.I)
SQL_FORMATS = {0, 1, 2, None, "time_series", "table", "logs"}   # Athena/BigQuery: numbers; mssql: names
# The Synapse time bucket of the Grafana-Synapse templates before 2026-11: Never wider than 1 s (one row per second
# with data on long time ranges); current form: build_dashboard.SYN_BUCKET / SYN_GROUP
OLD_SYNAPSE_GROUP = "DATEDIFF(SECOND, '2020', t), DATEPART(MILLISECOND, t) / $__interval_ms"
NO_QUERY_PANELS = {"row", "text", "news", "dashlist", "welcome"}
# Unknown keys the official templates carry, each harmless: key -> reason
SCHEMA_IGNORE = {"style": "legacy dashboard key (theme, Grafana < 10), dropped on import",
                 "queryValue": "written by the Grafana UI on custom variables"}


# ---------------------------------------------------------------- helpers
def iter_panels(panels: list, container: str = "top") -> list[tuple[dict, str]]:
    """Flatten panels incl. panels nested in collapsed rows: (panel, container)."""
    out = []
    for p in panels or []:
        out.append((p, container))
        if p.get("type") == "row" and p.get("panels"):
            out += iter_panels(p["panels"], f"row {p.get('id')}")
    return out


def unwrap(d: dict) -> dict:
    """API export wrapper {"dashboard": {...}, "meta": {...}} -> the dashboard."""
    return d["dashboard"] if "dashboard" in d and "panels" not in d else d


def target_sql(t: dict, backend: str | None = None) -> str | None:
    """The query the plugin runs: Athena reads rawSQL, BigQuery + Microsoft SQL Server rawSql. A variable query of
    the SQL data sources (mssql) is the SQL text itself."""
    if isinstance(t, str):
        return t if backend == "synapse" else None
    if backend in ("bigquery", "synapse"):
        return t.get("rawSql")
    if backend == "athena":
        return t.get("rawSQL")
    return t.get("rawSQL") or t.get("rawSql")


def backend_of(ds) -> str | None:
    """athena / bigquery from a datasource ref. Grafana resolves the data source by uid, so a template uid of the
    other backend wins over the type (an official BigQuery template has type athena + uid 'Google BigQuery')."""
    if not isinstance(ds, dict):
        return None
    by_uid = {u: b for b, u in DEFAULT_UID.items()}.get(ds.get("uid"))
    return by_uid or BACKENDS.get(ds.get("type"))


def type_mismatch(ds) -> bool:
    return isinstance(ds, dict) and backend_of(ds) not in (None, BACKENDS.get(ds.get("type")))


def message_tables(s: str) -> list[tuple[str, str]]:
    """(device part, message part) of the CANedge tables in SQL (tbl_ names, Synapse OPENROWSET folders), without the
    helper tables."""
    return [(d, m) for d, m in TABLE_REF.findall(s) + BULK_REF.findall(s)
            if m.lower() != "messages" and m.lower() != "devicemeta"]


def panel_sqls(d: dict) -> list[str]:
    return [sql for p, _ in iter_panels(d.get("panels", [])) if p.get("type") not in NO_QUERY_PANELS
            for t in p.get("targets") or [] if (sql := target_sql(t))]


def is_device_dashboard(d: dict) -> bool:
    """Per-device dashboard: A single-select $device variable that panels use in message tables (tbl_${device}_x)."""
    dev = next((v for v in (d.get("templating") or {}).get("list", []) or [] if v.get("name") == "device"), None)
    if not dev or dev.get("multi"):
        return False
    return any("$" in dv for s in panel_sqls(d) for dv, _ in message_tables(strip_sql(s)))


def has_dynamic_panel(d: dict) -> bool:
    return any("${signal_value" in s for s in panel_sqls(d))


def has_geomap(d: dict) -> bool:
    return any(p.get("type") == "geomap" for p, _ in iter_panels(d.get("panels", [])))


# ---------------------------------------------------------------- SQL checks
def check_sql(sql: str, where: str, errors: list, warnings: list, backend: str, is_variable: bool = False) -> None:
    """Grafana panel/variable SQL (lake-query's check_sql refuses the macros, so only its helpers are shared)."""
    s = strip_sql(sql)
    found = []
    for m in re.finditer(r"\$(__\w+)", s):
        name = m.group(1)
        if name not in MACROS[backend] and name not in GLOBAL_VARS:
            found.append(f"{where}: Unknown {backend} macro ${name} ({', '.join('$' + x for x in sorted(MACROS[backend]))})")
        if name in MACROS_WITH_ARGS and not re.match(r"\s*\(", s[m.end():]):
            found.append(f"{where}: ${name} needs arguments, e.g. ${name}(t)")
    errors += list(dict.fromkeys(found))
    if s.count("(") != s.count(")"):
        errors.append(f"{where}: Unbalanced parentheses in SQL ({s.count('(')} '(' vs {s.count(')')} ')')")
    if "$__timeGroup" in s and not re.search(r"\bGROUP\s+BY\b", s, re.I):
        errors.append(f"{where}: $__timeGroup used without GROUP BY")
    if not re.match(r"\s*\(?\s*(SELECT|WITH)\b", s, re.I):
        return
    tables = message_tables(s)
    data = [x for x in tables if x[0] != "aggregations"]
    if data and not is_variable and not any(m in s for m in ("$__timeFilter", "$__unixEpochFilter", "$__dateFilter")):
        warnings.append(f"{where}: No $__timeFilter(t) - the panel ignores the dashboard time range and scans all files")
    if data and not is_variable and returns_raw_rows(s):
        warnings.append(f"{where}: No time bucket / GROUP BY - the panel returns raw rows (slow, costly); aggregate "
                        "per $__interval as the templates do")
    if backend == "athena" and data and not re.search(r"\bdate_created\b", s, re.I):
        warnings.append(f"{where}: No date_created filter - Athena scans all partitions (use date_created "
                        "BETWEEN $__rawTimeFrom('yyyy/MM/dd') AND $__rawTimeTo('yyyy/MM/dd'))")
    if backend == "synapse" and not is_variable:
        if OLD_SYNAPSE_GROUP in re.sub(r"\s+", " ", sql):
            warnings.append(f"{where}: Old Synapse template time bucket - never wider than 1 s, so long time ranges "
                            "return one row per second: Use the current template form (FLOOR(DATEDIFF(SECOND, '2020', "
                            "t) / CEILING($__interval_ms / 1000.0)), see build_dashboard.py SYN_BUCKET / SYN_GROUP)")
        if re.search(r"\bLIMIT\s+\d", s, re.I):
            errors.append(f"{where}: T-SQL has no LIMIT - use SELECT TOP n")
        if re.search(r"\btbl_(?!aggregations_devicemeta)\w+", s) and not BULK_REF.search(s):
            warnings.append(f"{where}: Mapped Synapse table - returns no data rows (names only): Read the folder with "
                            "OPENROWSET(BULK '<device>/<message>/*/*/*/*', DATA_SOURCE = 'ParquetDataLake', FORMAT = "
                            "'PARQUET') AS r + the filepath() filter (as the templates)")
        elif data and not re.search(r"\.filepath\s*\(", s, re.I):
            warnings.append(f"{where}: No filepath() filter - Synapse reads every yyyy/mm/dd folder: Add "
                            "CONCAT(r.filepath(1), '-', r.filepath(2), '-', r.filepath(3)) BETWEEN CONVERT(date, "
                            "$__timeFrom()) AND CONVERT(date, $__timeTo())")
    if backend == "bigquery":
        if data and len(data) == len(tables) and re.search(r"\bdate_created\b", s, re.I):
            errors.append(f"{where}: BigQuery message tables have no date_created column - filter $__timeFilter(t) only")
        if re.search(r"(?<![.\w])`?tbl_", s):
            warnings.append(f"{where}: BigQuery table without dataset - use ${{dataset}}.tbl_... (as the templates)")


# ---------------------------------------------------------------- Grafana JSON schemas (unknown keys only)
_schemas: dict[str, dict | None] = {}


def _schema(name: str) -> dict | None:
    if name not in _schemas:
        p = SCHEMA_DIR / f"{name}.json"
        _schemas[name] = json.loads(p.read_text(encoding="utf-8")) if p.exists() else None
    return _schemas[name]


def _unknown_keys(name: str, ref: str | None, inst, where: str, out: list) -> None:
    """Unknown keys only: The schemas describe what Grafana saves (defaults filled in, some types stricter than
    Grafana itself), so 'required' and type errors are noise; an unknown key is a silently ignored typo."""
    s = _schema(name)
    if s is None or (ref and ref not in s.get("definitions", {})):
        return
    import jsonschema
    s = {**s, "$ref": f"#/definitions/{ref}"} if ref else s
    for e in jsonschema.Draft7Validator(s).iter_errors(inst):
        if e.validator != "additionalProperties" or not isinstance(e.instance, dict):
            continue
        known = set(e.schema.get("properties", {}))
        keys = sorted(k for k in e.instance if k not in known and k not in SCHEMA_IGNORE)
        if keys:
            path = "/".join(map(str, e.absolute_path))
            out.append(f"{where}{' ' + path if path else ''}: Unknown key(s) {', '.join(keys)} - Grafana ignores them")


def schema_warnings(d: dict) -> list[str]:
    if not SCHEMA_DIR.exists():
        return []
    try:
        import jsonschema  # noqa: F401
    except ImportError:
        return []
    out: list[str] = []
    top = {k: v for k, v in d.items() if not k.startswith("__")}
    top.update(panels=[], templating={"list": []})
    _unknown_keys("dashboard", "Dashboard", top, "dashboard", out)
    for v in (d.get("templating") or {}).get("list", []) or []:
        _unknown_keys("dashboard", "VariableModel", v, f"variable ${v.get('name')}", out)
    for p, _ in iter_panels(d.get("panels", [])):
        where = f"panel {p.get('id')} '{p.get('title', '')}'"
        if p.get("type") == "row":
            _unknown_keys("dashboard", "RowPanel", {**p, "panels": []}, where, out)
            continue
        core = {k: v for k, v in p.items() if k not in ("targets", "options")}
        fc = json.loads(json.dumps(p.get("fieldConfig") or {}))
        custom = (fc.get("defaults") or {}).pop("custom", None)
        if "fieldConfig" in p:
            core["fieldConfig"] = fc
        _unknown_keys("dashboard", "Panel", core, where, out)
        plugin = str(p.get("type", "")).replace("-", "")   # state-timeline -> statetimeline.json
        if isinstance(p.get("options"), dict) and plugin:
            _unknown_keys(plugin, "Options", p["options"], where + " options", out)
        if isinstance(custom, dict) and plugin:
            _unknown_keys(plugin, "FieldConfig", custom, where + " fieldConfig.defaults.custom", out)
        for t in p.get("targets") or []:
            ds = t.get("datasource") or p.get("datasource")
            if backend_of(ds) and not type_mismatch(ds):   # a type mismatch is reported by validate() already
                _unknown_keys(backend_of(ds), None, t, f"{where} [{t.get('refId')}]", out)
    return out


# ---------------------------------------------------------------- validation
def validate(d: dict) -> dict:
    errors, warnings = [], []
    d = unwrap(d)
    for k in ("title", "panels"):
        if k not in d:
            errors.append(f"Missing top-level '{k}'")
    if not isinstance(d.get("panels", []), list):
        errors.append("'panels' must be a list")
        return {"errors": errors, "warnings": warnings, "schema": []}
    if not d.get("uid"):
        warnings.append("No uid - Grafana generates one on import (links to this dashboard will change)")
    if d.get("schemaVersion") is None:
        warnings.append("No schemaVersion")
    inputs = {i.get("name") for i in d.get("__inputs", []) or []}
    tvars = (d.get("templating") or {}).get("list", []) or []
    var_names = {v.get("name") for v in tvars}
    panels = iter_panels(d.get("panels", []))
    ids = [p.get("id") for p, _ in panels]
    dup = sorted({i for i in ids if ids.count(i) > 1 and i is not None}, key=str)
    if dup:
        errors.append(f"Duplicate panel ids: {dup}")
    if any(i is None for i in ids):
        errors.append("Panel(s) without id")
    id_set = set(ids)

    # gridPos
    boxes: dict[str, list] = {}
    for p, cont in panels:
        where = f"panel {p.get('id')} '{p.get('title', '')}'"
        g = p.get("gridPos")
        if not isinstance(g, dict) or not all(isinstance(g.get(k), int) for k in ("h", "w", "x", "y")):
            errors.append(f"{where}: gridPos must have integer h, w, x, y")
            continue
        if not (1 <= g["w"] <= 24 and 0 <= g["x"] <= 23 and g["x"] + g["w"] <= 24 and g["h"] >= 1 and g["y"] >= 0):
            errors.append(f"{where}: gridPos out of the 24-column grid {g}")
        for q, gq in boxes.get(cont, []):
            if g["x"] < gq["x"] + gq["w"] and gq["x"] < g["x"] + g["w"] and g["y"] < gq["y"] + gq["h"] and gq["y"] < g["y"] + g["h"]:
                warnings.append(f"{where}: Overlaps panel {q.get('id')} (Grafana will push panels down)")
        boxes.setdefault(cont, []).append((p, g))

    # datasources + targets
    used_backends: set[str] = set()
    mismatched: list[str] = []

    def check_ds(ds, where):
        if ds is None:
            return None
        if isinstance(ds, str):
            if ds.startswith("${") and ds[2:-1] not in inputs:
                errors.append(f"{where}: Datasource {ds} not defined in __inputs")
            return None
        typ, uid = ds.get("type"), ds.get("uid")
        if isinstance(uid, str) and uid.startswith("${") and uid[2:-1] not in inputs | var_names:
            errors.append(f"{where}: Datasource uid {uid} is neither an __input nor a variable")
        be = backend_of(ds)
        if type_mismatch(ds):
            mismatched.append(where)
        if be and uid != DEFAULT_UID[be] and not (isinstance(uid, str) and uid.startswith("${")):
            warnings.append(f"{where}: {be} uid '{uid}' differs from the template convention '{DEFAULT_UID[be]}' - "
                            "it must match the uid of the user's data source")
        if not be and typ not in ("datasource", None) and not str(typ).startswith("${"):
            warnings.append(f"{where}: Datasource type '{typ}' is neither Athena, BigQuery nor Synapse (SQL not "
                            "checked)")
        if be:
            used_backends.add(be)
        return be

    for p, _ in panels:
        where = f"panel {p.get('id')} '{p.get('title', '')}'"
        if p.get("type") == "row":
            continue
        check_ds(p.get("datasource"), where)
        refids = []
        targets = p.get("targets") or []
        if not targets and p.get("type") not in NO_QUERY_PANELS:
            warnings.append(f"{where}: No queries")
        for t in targets:
            refids.append(t.get("refId"))
            tw = f"{where} [{t.get('refId')}]"
            ds = t.get("datasource") or p.get("datasource") or {}
            if isinstance(ds, dict) and ds.get("uid") == "-- Dashboard --":
                if t.get("panelId") not in id_set:
                    errors.append(f"{tw}: '-- Dashboard --' query points to missing panelId {t.get('panelId')}")
                continue
            be = check_ds(t.get("datasource"), tw) if t.get("datasource") else backend_of(p.get("datasource"))
            if be:
                sql = target_sql(t, be)
                if not sql or not str(sql).strip():
                    errors.append(f"{tw}: {be} target without {'rawSQL' if be == 'athena' else 'rawSql'}")
                else:
                    check_sql(sql, tw, errors, warnings, be)
                if t.get("format") not in SQL_FORMATS:
                    warnings.append(f"{tw}: Unexpected format {t.get('format')} (0 / time_series, 1 / table, 2 / logs)")
        dups = sorted({r for r in refids if refids.count(r) > 1}, key=str)
        if dups:
            errors.append(f"{where}: Duplicate refIds {dups}")

    # variables
    defined = [v.get("name") for v in tvars]
    for n in sorted({n for n in defined if defined.count(n) > 1}):
        errors.append(f"Variable ${n} defined twice")
    used = set()
    for p, _ in panels:
        used |= used_vars(json.dumps(p))
    for v in tvars:
        q = v.get("query")
        used |= used_vars(json.dumps(q) if q is not None else "")
        be = check_ds(v.get("datasource"), f"variable ${v.get('name')}")
        sql = target_sql(q, be) if isinstance(q, (dict, str)) else None
        if v.get("type") == "query" and be and sql:
            check_sql(sql, f"variable ${v.get('name')}", errors, warnings, be, is_variable=True)
    used |= used_vars(json.dumps(d.get("links", [])))
    for n in sorted(used - set(defined) - inputs):
        errors.append(f"Variable ${n} is used but not defined in templating")
    for n in sorted(set(defined) - used):
        warnings.append(f"Variable ${n} is defined but not used")
    if mismatched:
        warnings.append(f"{len(mismatched)} datasource ref(s) name the uid of one backend but the type of the other "
                        f"(e.g. {mismatched[0]}): Grafana uses the uid; set the matching type")
    if len(used_backends) > 1:
        warnings.append(f"Mixes {' and '.join(sorted(used_backends))} queries - a dashboard normally uses one data "
                        "lake")
    if is_device_dashboard(d) and not has_dynamic_panel(d):
        warnings.append("Device dashboard without the dynamic signal panel ($message > $signal > $aggregation "
                        "dropdowns of the official templates): Add a {\"type\": \"dynamic\"} panel to the spec")
    return {"title": d.get("title"), "panels": len([p for p, _ in panels if p.get("type") != "row"]),
            "variables": defined, "backends": sorted(used_backends), "errors": errors, "warnings": warnings,
            "schema": schema_warnings(d)}


def used_vars(text: str) -> set[str]:
    names = set(re.findall(r"\$\{(\w+)(?::[^}]*)?\}", text)) | set(re.findall(r"\[\[(\w+)(?::\w+)?\]\]", text))
    names |= set(re.findall(r"\$([A-Za-z_]\w*)", text))
    return {n for n in names if not n.startswith("__") and n not in GLOBAL_VARS}


# ---------------------------------------------------------------- lake check (canedge-lake-query schema)
def lake_check(d: dict, lake) -> tuple[list[str], list[str]]:
    """Message tables + aggregated columns of every panel query vs the lake schema. ${device} tables: The message
    must exist for at least one device; literal device IDs: The table must exist. BigQuery/Synapse: Exact message
    case."""
    errors, warnings = [], []
    tabs = {t["name"].lower(): t for t in lake.tables()}
    by_msg: dict[str, list[dict]] = {}
    for t in tabs.values():
        if t["kind"] == "message":
            by_msg.setdefault(t["message"], []).append(t)
    all_dev = {t["device"] for t in tabs.values() if t["kind"] == "message"}
    partial: dict[str, set] = {}   # ${device} messages that only some devices have
    queries = []
    for p, _ in iter_panels(unwrap(d).get("panels", [])):
        if p.get("type") in NO_QUERY_PANELS:   # leftover targets (e.g. a text panel in a template) never run
            continue
        for t in p.get("targets") or []:
            sql = target_sql(t, backend_of(t.get("datasource") or p.get("datasource")))
            if sql:
                queries.append((f"panel {p.get('id')} '{p.get('title', '')}' [{t.get('refId')}]", sql))
    for v in (unwrap(d).get("templating") or {}).get("list", []) or []:
        sql = target_sql(v["query"], backend_of(v.get("datasource"))) if isinstance(v.get("query"), (dict, str)) \
            else None
        if sql:
            queries.append((f"variable ${v.get('name')}", sql))
    for where, sql in queries:
        s = strip_sql(sql)
        hits = []
        for dev, msg in message_tables(s):
            if "$" in msg:
                continue   # dynamic message (${message}): nothing to check
            if dev == "aggregations":
                if f"tbl_aggregations_{msg.lower()}" not in tabs:
                    errors.append(f"{where}: tbl_aggregations_{msg} is not in the lake (that aggregation is not deployed)")
                continue
            cands = [lake_t for lake_t in by_msg.get(msg.lower(), [])
                     if "$" in dev or lake_t["device"] == dev.upper()]
            if not cands:
                if "$" in dev or not by_msg.get(msg.lower()):
                    close = difflib.get_close_matches(msg.lower(), list(by_msg), n=4, cutoff=0.5)
                    errors.append(f"{where}: Message {msg} is not in the lake. Close: {', '.join(close) or '-'}")
                else:
                    errors.append(f"{where}: Device {dev.upper()} has no table for {msg}")
                continue
            if "$" in dev and len(cands) < len(all_dev):
                partial[msg.lower()] = {c["device"] for c in cands}
            real = (cands[0].get("bq_name") or "")
            real_msg = real[len("tbl_") + 9:] if real else (cands[0].get("folder") or "").partition("/")[2] or None
            if real_msg and msg != real_msg:
                errors.append(f"{where}: {'BigQuery table' if real else 'Synapse folder'} names are case sensitive - "
                              f"use {real_msg}, not {msg}")
            hits.append(cands)
        if len(hits) == 1:   # one message table: The aggregated columns must be its signals
            cols = {c[0] for t in hits[0] for c in t["columns"]}
            for col in sorted({c.lower() for c in AGG_COL.findall(s)} - {"t", "distinct", "case"}):
                if col not in cols:
                    close = difflib.get_close_matches(col, sorted(cols), n=4, cutoff=0.5)
                    errors.append(f"{where}: Column {col} is not in {hits[0][0]['message']}. Close: {', '.join(close) or '-'}")
    if is_device_dashboard(unwrap(d)) and not has_geomap(unwrap(d)):
        gps = sorted({t["message"] for t in tabs.values() if t["kind"] == "message"
                      and {"latitude", "longitude"} <= {c[0].lower() for c in t["columns"]}})
        if gps:
            warnings.append(f"The lake has GPS positions ({', '.join(gps)}) but the dashboard has no geomap: Add a "
                            "'geomap' panel (route colored by speed, as in the GPS templates)")
    if partial:
        full = sorted(set.intersection(*partial.values()))
        warnings.append(f"Only some of the {len(all_dev)} devices have these messages (their panels stay empty for "
                        f"the others): {', '.join(f'{m} {len(d)}' for m, d in sorted(partial.items()))}. Devices with "
                        f"all of them: {', '.join(full[:12]) or '-'}{' ...' if len(full) > 12 else ''}. Fine for a mixed "
                        'fleet; else a fixed dropdown: {"type": "device", "devices": [...]}')
    return errors, warnings


# ---------------------------------------------------------------- summary (panels, queries, variables)
def ds_ref(ds) -> str:
    if ds is None:
        return "-"
    if isinstance(ds, str):
        return ds
    return f"{ds.get('type', '?')}:{ds.get('uid', '?')}"


def summarize(d: dict) -> dict:
    d = unwrap(d)
    panels = []
    for p, _ in iter_panels(d.get("panels", [])):
        targets = [{"refId": t.get("refId"), "datasource": ds_ref(t.get("datasource")), "format": t.get("format"),
                    "sql": target_sql(t, backend_of(t.get("datasource") or p.get("datasource"))) or t.get("query")}
                   for t in p.get("targets", []) or []]
        panels.append({"id": p.get("id"), "type": p.get("type"), "title": p.get("title"),
                       "gridPos": p.get("gridPos"), "datasource": ds_ref(p.get("datasource")),
                       "transformations": [x.get("id") for x in p.get("transformations", []) or []],
                       "targets": targets})
    variables = []
    for v in (d.get("templating") or {}).get("list", []):
        q = v.get("query")
        variables.append({"name": v.get("name"), "type": v.get("type"), "multi": v.get("multi"),
                          "includeAll": v.get("includeAll"), "hide": v.get("hide"),
                          "datasource": ds_ref(v.get("datasource")),
                          "query": target_sql(q, backend_of(v.get("datasource"))) if isinstance(q, dict) else q,
                          "regex": v.get("regex") or None})
    all_ds = sorted(({t["datasource"] for p in panels for t in p["targets"]} | {p["datasource"] for p in panels}
                     | {v["datasource"] for v in variables}) - {"-"})
    return {"title": d.get("title"), "uid": d.get("uid"), "description": d.get("description"),
            "schemaVersion": d.get("schemaVersion"), "time": d.get("time"), "tags": d.get("tags"),
            "inputs": [i.get("name") for i in d.get("__inputs", [])], "datasources": all_ds,
            "macros": sorted(set(re.findall(r"\$__[A-Za-z_]+", json.dumps(d)))), "variables": variables,
            "panels": panels}


def one_line(s: str | None, n: int) -> str:
    if not s:
        return ""
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[:n] + " ..."


def print_summary(s: dict, sql: str) -> None:
    print(f"Dashboard: {s['title']} (uid {s['uid']}, schemaVersion {s['schemaVersion']}, time {s['time']})")
    if s["description"]:
        print(f"  {s['description']}")
    print(f"Datasources: {', '.join(s['datasources']) or '-'}" + (f" | __inputs: {s['inputs']}" if s["inputs"] else ""))
    print(f"Macros used: {', '.join(s['macros']) or '-'}")
    print(f"\nVariables ({len(s['variables'])}):")
    for v in s["variables"]:
        flags = ",".join(f for f, on in (("multi", v["multi"]), ("all", v["includeAll"]), (f"hide={v['hide']}", v["hide"])) if on)
        print(f"  ${v['name']} [{v['type']}{', ' + flags if flags else ''}]")
        if sql != "none" and v["query"]:
            print(f"      {v['query'] if sql == 'full' else one_line(str(v['query']), 160)}")
    print(f"\nPanels ({len(s['panels'])}):")
    for p in s["panels"]:
        g = p["gridPos"] or {}
        tr = f" | transforms: {', '.join(p['transformations'])}" if p["transformations"] else ""
        print(f"  #{p['id']} {p['type']} '{p['title']}' at x={g.get('x')} y={g.get('y')} w={g.get('w')} h={g.get('h')}{tr}")
        if sql == "none":
            continue
        for t in p["targets"]:
            if t["sql"]:
                print(f"      [{t['refId']}] {t['sql'] if sql == 'full' else one_line(t['sql'], 160)}")
            elif "dashboard" in t["datasource"].lower():
                print(f"      [{t['refId']}] (reuses another panel's query)")
    print()


def report(res: dict, label: str) -> None:
    print(f"{label}: '{res.get('title')}', {res.get('panels')} panels, variables "
          f"{', '.join('$' + str(v) for v in res.get('variables', [])) or '-'}"
          + (f", {'/'.join(res['backends'])}" if res.get("backends") else ""))
    for e in res["errors"]:
        print(f"  ERROR: {e}")
    for w in res["warnings"]:
        print(f"  WARNING: {w}")
    for w in res.get("schema", []):
        print(f"  WARNING (schema): {w}")
    print(f"  -> {len(res['errors'])} error(s), {len(res['warnings']) + len(res.get('schema', []))} warning(s)")


def add_lake_args(ap) -> None:
    from _backends import add_source_args
    add_source_args(ap)


def run_lake_check(a, d: dict, res: dict) -> None:
    """Optional: Names vs the lake (only when a data source flag is given)."""
    if not (a.athena or a.bigquery or a.synapse or a.project or a.lake):
        return
    from _backends import LakeError, connect_args
    try:
        errs, warns = lake_check(d, connect_args(a))
    except LakeError as e:
        res["errors"].append(f"Lake check failed: {e}")
        return
    res["errors"] += errs
    res["warnings"] += warns


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dashboard", type=Path)
    ap.add_argument("--summary", action="store_true", help="Print panels, queries and variables first")
    ap.add_argument("--sql", choices=["full", "short", "none"], default="short", help="SQL in --summary")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--strict", action="store_true", help="Exit 1 on warnings too")
    add_lake_args(ap)
    a = ap.parse_args()
    try:
        d = unwrap(json.loads(a.dashboard.read_text(encoding="utf-8")))
    except json.JSONDecodeError as e:
        print(json.dumps({"errors": [f"Invalid JSON: {e}"]}) if a.json else f"ERROR: Invalid JSON: {e}")
        return 1
    except OSError as e:
        print(f"ERROR: Cannot read {a.dashboard}: {e}", file=sys.stderr)
        return 1
    res = validate(d)
    run_lake_check(a, d, res)
    if a.json:
        print(json.dumps({**({"summary": summarize(d)} if a.summary else {}), **res}, indent=1))
    else:
        if a.summary:
            print_summary(summarize(d), a.sql)
        report(res, str(a.dashboard))
    warn = res["warnings"] + res["schema"]
    return 1 if res["errors"] or (a.strict and warn) else 0


if __name__ == "__main__":
    sys.exit(main())
