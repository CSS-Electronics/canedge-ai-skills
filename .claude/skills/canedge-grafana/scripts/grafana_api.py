r"""Work in the user's Grafana directly: Import, export, check, list and delete the dashboards this skill builds.

  .venv\Scripts\python.exe .claude/skills/canedge-grafana/scripts/grafana_api.py <command> [--grafana-env NAME] [--json]

Commands:
  status                        URL, organization, Grafana version, what the token allows, data sources (+ whether
                                'Amazon Athena' / 'Google BigQuery' / 'Microsoft SQL Server' exists), dashboards
                                imported by this tool
  datasources                   Name, type, uid of each data source the token can see
  folders                       Folders (title, uid)
  search [--query TEXT]         Dashboards (title, uid, folder, tags, URL)
  import <dashboard.json> [--folder NAME] [--overwrite] [--datasource-uid UID]
                                Validate (validate_dashboard.py; errors stop it), point every Athena/BigQuery/Synapse
                                data source ref to the user's data source (the one named 'Amazon Athena' / 'Google
                                BigQuery' / 'Microsoft SQL Server' as the builder expects, else the only one of that
                                type, else --datasource-uid), upload it with its uid (a re-import updates it in place;
                                no uid in the file: one from the title) and print 'OPEN Grafana dashboard: <url>'.
                                Tags its dashboards 'canedge-ai'; refuses to replace a dashboard without that tag
                                (same uid, or same title in the folder) unless --overwrite. --folder: Created if
                                missing
  export <uid> -o FILE          Download a dashboard (the refs to 'Amazon Athena' / 'Google BigQuery' / 'Microsoft SQL
                                Server' are written back with these names, so validate_dashboard.py checks it as
                                usual). Edits: export, change, validate, import again (same uid)
  check <uid | dashboard.json> [--from ISO --to ISO] [--panel ID|TITLE ...] [--yes] [--datasource-uid UID]
                                Run every panel's queries through Grafana (POST /api/ds/query) over the dashboard's
                                time range (or --from/--to) with the variables resolved: Custom/constant/textbox/
                                interval = their current value, query variables = their query is run, then the current
                                value if it is among the results, else All (includeAll) or the first. Formats $var,
                                ${var}, ${var:csv|sqlstring|singlequote|doublequote|pipe|json|regex|glob|raw}, [[var]];
                                $__interval, $__interval_ms, $__range*, $__from, $__to are set as the Grafana UI does;
                                plugin macros ($__timeFilter, $__rawTimeFrom(...), ...) are left to the data source.
                                Per panel: OK + rows / NO DATA / ERROR + message. Athena/BigQuery/Synapse bill each
                                query: Above 20 queries it stops and asks for --yes (tell the user the number first)
  shot <uid> [--from ISO --to ISO] [--var name=value ...] [--full] -o FILE.png
                                Screenshot as the user sees it (1920x1080; --full: every panel) for the visual review
                                (units, cut titles, empty pies, colors). Headless Chromium, the token as a Bearer
                                header. Needs Playwright (optional, not in requirements.txt): pip install playwright +
                                python -m playwright install chromium in .venv. Loading runs the panels' queries
  delete <uid> --yes            Delete a dashboard (only when the user asks)

Profile: credentials/grafana/<name>.env from setup_env.py --grafana (the only one, or --grafana-env NAME). The token
is never printed. Exit codes: 0 OK, 1 failed (API error, a panel query failed), 2 refused (needs --yes/--overwrite,
invalid dashboard, data source not found).
"""
from __future__ import annotations

import argparse
import copy
import datetime as dt
import json
import re
import sys
import time
from pathlib import Path
from urllib.parse import quote

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "canedge-workspace" / "scripts")]
import validate_dashboard as vd  # noqa: E402
from env import grafana_profile  # noqa: E402
from setup_env import (GRAFANA_DS, GRAFANA_ROLES, grafana_client, grafana_datasources, grafana_error,  # noqa: E402
                       inspect_grafana)

MARKER_TAG = "canedge-ai"            # dashboards imported by this tool (may be updated without --overwrite)
SQL_DS_TYPES = {"mssql", "postgres", "grafana-postgresql-datasource", "mysql"}   # variable query = SQL text
MAX_QUERIES = 20                     # check: Ask for --yes above this many queries (Athena/BigQuery/Synapse cost money)
MAX_POINTS = 1000                    # check: maxDataPoints when the panel sets none (about a panel's pixel width)
MESSAGE = "Imported by canedge-grafana grafana_api.py"
CONVENTION = dict(vd.DEFAULT_UID)    # athena -> 'Amazon Athena', bigquery -> 'Google BigQuery'
PLUGIN = {be: t for t, be in vd.BACKENDS.items()}
SPECIAL = {"-- Grafana --", "-- Dashboard --", "-- Mixed --", "__expr__", "grafana"}
INTERVAL_STEPS = [10, 20, 50, 100, 200, 500, 1000, 2000, 5000, 10000, 15000, 20000, 30000, 60000, 120000, 300000,
                  600000, 900000, 1200000, 1800000, 3600000, 7200000, 10800000, 21600000, 43200000, 86400000,
                  604800000, 2592000000, 31536000000]
UNIT_MS = {"ms": 1, "s": 1000, "m": 60000, "h": 3600000, "d": 86400000, "w": 604800000, "M": 2592000000,
           "y": 31536000000}
VAR_RE = re.compile(r"\$\{(\w+)(?::([^}]+))?\}|\[\[(\w+)(?::(\w+))?\]\]|\$(\w+)")
CHECK_TAG = {True: "OK  ", False: "FAIL", None: "WARN"}
TOKEN = ""                           # set by connect(); say() masks it in every line printed


class Refused(Exception):
    """Not done on purpose (exit 2): Needs --yes / --overwrite, or the input is not usable."""


class Raw(str):
    """A value inserted as is, whatever the format (a variable's custom 'All' value)."""


def say(text: str = "") -> None:
    print(text.replace(TOKEN, "***") if TOKEN else text)


def connect(a, timeout: float = 30.0):
    global TOKEN
    prof = grafana_profile(a.grafana_env)
    TOKEN = prof["GRAFANA_TOKEN"]
    return grafana_client(prof["GRAFANA_URL"], TOKEN, timeout), prof


def dash_url(prof: dict, uid: str) -> str:
    return f"{prof['GRAFANA_URL'].rstrip('/')}/d/{quote(uid)}"


def slug(title: str) -> str:
    return re.sub(r"[^a-z0-9-]+", "-", (title or "dashboard").lower()).strip("-")[:40]


def get_dashboard(api, uid: str) -> dict | None:
    """{"dashboard", "meta"} or None if there is no such uid."""
    from grafana_client.client import GrafanaClientError
    try:
        return api.dashboard.get_dashboard(uid)
    except GrafanaClientError as e:
        if e.status_code == 404:
            return None
        raise


def load_json(path: str) -> dict:
    try:
        return vd.unwrap(json.loads(Path(path).read_text(encoding="utf-8")))
    except (OSError, ValueError) as e:
        raise Refused(f"Cannot read {path}: {e}") from e


# ---------------------------------------------------------------- data source refs
class DatasourceMap:
    """Points the dashboard's data source refs to the user's data sources: A real uid stays; an Athena/BigQuery/
    Synapse ref (template uid 'Amazon Athena' / 'Google BigQuery' / 'Microsoft SQL Server' or the plugin type) goes
    to --datasource-uid, else the data source with that name, else the only one of that plugin type."""

    def __init__(self, dss: list[dict], override: str | None = None):
        self.dss = dss
        self.by_uid = {d["uid"]: d for d in dss}
        self.by_name = {d["name"]: d for d in dss}
        self.override = None
        if override:
            self.override = self.by_uid.get(override) or self.by_name.get(override)
            if not self.override:
                raise Refused(f"--datasource-uid {override}: No such data source (Grafana has: {self.listing()})")
        self.mapped: dict[str, str] = {}    # ref uid in the file -> 'name (uid)' used
        self.missing: dict[str, str] = {}   # ref uid in the file -> backend
        self.inputs: dict[str, str] = {}    # ${DS_X} __inputs -> plugin id
        self.used: dict[str, dict] = {}     # uid -> data source the dashboard ends up using

    def listing(self) -> str:
        return ", ".join(f"{d['name']} ({d['type']}, uid {d['uid']})" for d in self.dss) or "none the token can see"

    def target(self, be: str) -> dict | None:
        if self.override:
            if self.override["type"] != PLUGIN[be]:
                raise Refused(f"--datasource-uid {self.override['uid']} is a {self.override['type']} data source, the "
                              f"dashboard queries {be}")
            return self.override
        same = [d for d in self.dss if d["type"] == PLUGIN[be]]
        return self.by_name.get(CONVENTION[be]) or (same[0] if len(same) == 1 else None)

    def resolve(self, ref):
        if ref is None or (isinstance(ref, str) and (ref in SPECIAL or ref.startswith("$") and ref[2:-1] not in
                                                      self.inputs)):
            return ref
        if isinstance(ref, str):
            ref = {"uid": ref}
        if not isinstance(ref, dict):
            return ref
        uid = ref.get("uid")
        if uid in SPECIAL or (isinstance(uid, str) and uid.startswith("$") and uid.strip("${}") not in self.inputs):
            return ref
        be = vd.backend_of(ref)
        if isinstance(uid, str) and uid.strip("${}") in self.inputs:
            be = vd.BACKENDS.get(self.inputs[uid.strip("${}")])
        d = None
        if be and self.override:
            d = self.target(be)
        elif uid in self.by_uid:
            d = self.by_uid[uid]
        elif uid in self.by_name:
            d = self.by_name[uid]
        elif be:
            d = self.target(be)
        if not d:
            if be or uid is not None:
                self.missing[str(uid or ref.get("type"))] = be or "other"
            return ref
        if uid != d["uid"]:
            self.mapped[str(uid or ref.get("type"))] = f"{d['name']} (uid {d['uid']})"
        self.used[d["uid"]] = d
        return {"type": d["type"], "uid": d["uid"]}

    def apply(self, dash: dict) -> dict:
        """The dashboard with every 'datasource' ref resolved (in place)."""
        self.inputs = {i["name"]: i.get("pluginId", "") for i in dash.get("__inputs") or []
                       if i.get("type") == "datasource" and i.get("name")}

        def walk(o):
            if isinstance(o, dict):
                for k, v in o.items():
                    if k == "datasource":
                        o[k] = self.resolve(v)
                    else:
                        walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)
        walk(dash)
        return dash

    def problem(self) -> str | None:
        """Refs that point to no data source in Grafana (None = all found)."""
        if not self.missing:
            return None
        names = [CONVENTION.get(be, ref) for ref, be in self.missing.items()]
        return (f"No data source in Grafana for {', '.join(sorted(set(names)))} (Grafana has: {self.listing()}). "
                "Name the data source so in Grafana, or pass --datasource-uid <uid>")


def to_convention(dash: dict, dss: list[dict]) -> int:
    """Export: Refs to a data source named 'Amazon Athena' / 'Google BigQuery' / 'Microsoft SQL Server' get that
    name as uid again (the builder's and validator's convention). Returns the number of refs changed."""
    names = {d["uid"]: d["name"] for d in dss if d["name"] in CONVENTION.values()}
    n = 0

    def walk(o):
        nonlocal n
        if isinstance(o, dict):
            for k, v in o.items():
                if k == "datasource" and isinstance(v, dict) and v.get("uid") in names:
                    o[k] = {"type": v.get("type"), "uid": names[v["uid"]]}
                    n += 1
                else:
                    walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)
    walk(dash)
    return n


# ---------------------------------------------------------------- variables + time
def fmt_value(vals, fmt: str | None) -> str:
    """One variable's value(s) in a Grafana format (multi-value default: comma separated, as ${var:csv})."""
    if isinstance(vals, Raw):
        return str(vals)
    vals = [str(x) for x in (vals if isinstance(vals, list) else [vals])]
    fmt = (fmt or "").split(":")[0]
    if fmt == "sqlstring":
        return ",".join("'" + x.replace("'", "''") + "'" for x in vals)
    if fmt == "singlequote":
        return ",".join("'" + x.replace("'", "\\'") + "'" for x in vals)
    if fmt == "doublequote":
        return ",".join('"' + x.replace('"', '\\"') + '"' for x in vals)
    if fmt == "pipe":
        return "|".join(vals)
    if fmt == "json":
        return json.dumps(vals if len(vals) != 1 else vals[0])
    if fmt == "regex":
        return re.escape(vals[0]) if len(vals) == 1 else "(" + "|".join(map(re.escape, vals)) + ")"
    if fmt == "glob":
        return vals[0] if len(vals) == 1 else "{" + ",".join(vals) + "}"
    if fmt in ("percentencode", "queryparam"):
        return ",".join(quote(x, safe="") for x in vals)
    return ",".join(vals)   # csv, raw, text, none


def interpolate(text: str, values: dict) -> str:
    """$var, ${var}, ${var:format}, [[var]], [[var:format]]; unknown names ($__timeFilter, ...) stay."""
    def rep(m):
        name, fmt = (m.group(1), m.group(2)) if m.group(1) else (m.group(3), m.group(4)) if m.group(3) else \
            (m.group(5), None)
        return fmt_value(values[name], fmt) if name in values else m.group(0)
    return VAR_RE.sub(rep, text)


def interpolate_obj(o, values: dict):
    if isinstance(o, str):
        return interpolate(o, values)
    if isinstance(o, dict):
        return {k: (v if k in ("refId", "datasource") else interpolate_obj(v, values)) for k, v in o.items()}
    if isinstance(o, list):
        return [interpolate_obj(v, values) for v in o]
    return o


def parse_time(v, now_ms: int, end: bool = False) -> int:
    """Epoch ms from epoch ms, ISO (UTC if no zone) or Grafana relative time (now-7d, now/d, now-1d/d)."""
    s = str(v).strip()
    if re.fullmatch(r"\d{10,}", s):
        return int(s)
    m = re.fullmatch(r"now(?:([+-])(\d+)(ms|[smhdwMy]))?(?:/([smhdwMy]))?", s)
    if m:
        t = now_ms + (1 if m[1] == "+" else -1) * int(m[2] or 0) * UNIT_MS[m[3] or "s"]
        if m[4]:
            unit = UNIT_MS[m[4]] if m[4] in "smhd" else UNIT_MS["d"]   # w/M/y: rounded to the day
            t = t // unit * unit + (unit - 1 if end else 0)
        return t
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError as e:
        raise Refused(f"Time '{s}': Use ISO (2026-06-25 or 2026-06-25T08:00:00Z) or Grafana time (now-7d)") from e
    if d.tzinfo is None:
        d = d.replace(tzinfo=dt.timezone.utc)
    return int(d.timestamp() * 1000)


def time_range(dash: dict, t_from: str | None, t_to: str | None) -> tuple[int, int]:
    now = int(time.time() * 1000)
    rng = dash.get("time") or {}
    t0 = parse_time(t_from or rng.get("from", "now-7d"), now)
    t1 = parse_time(t_to or rng.get("to", "now"), now, end=True)
    if t1 <= t0:
        raise Refused(f"Empty time range ({iso(t0)} -> {iso(t1)})")
    return t0, t1


def iso(ms: int) -> str:
    return dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")


def fmt_interval(ms: int) -> str:
    for u in ("d", "h", "m", "s"):
        if ms >= UNIT_MS[u] and ms % UNIT_MS[u] == 0:
            return f"{ms // UNIT_MS[u]}{u}"
    return f"{ms}ms"


def parse_interval(s) -> int:
    m = re.fullmatch(r"\s*>?\s*(\d+)(ms|[smhdwMy])\s*", str(s or ""))
    return int(m[1]) * UNIT_MS[m[2]] if m else 0


def builtins(t0: int, t1: int, panel: dict | None = None) -> tuple[dict, int, int]:
    """The global variables the Grafana UI sets per panel query, + intervalMs and maxDataPoints."""
    panel = panel or {}
    points = int(panel.get("maxDataPoints") or MAX_POINTS)
    raw = max((t1 - t0) / points, parse_interval(panel.get("interval")))
    iv = next((s for s in INTERVAL_STEPS if s >= raw), INTERVAL_STEPS[-1])
    rng = t1 - t0
    vals = {"__interval": fmt_interval(iv), "__interval_ms": str(iv), "__rate_interval": fmt_interval(4 * iv),
            "__range": fmt_interval(rng // 1000 * 1000 or 1000), "__range_s": str(rng // 1000),
            "__range_ms": str(rng), "__from": str(t0), "__to": str(t1)}
    return {k: [v] for k, v in vals.items()}, iv, points


def frame_values(frames: list) -> list[str]:
    """Values of a variable query result: The 'value' / '__value' field, else 'text' / '__text', else the first."""
    out = []
    for f in frames or []:
        fields = [(x.get("name") or "").lower() for x in (f.get("schema") or {}).get("fields", [])]
        cols = (f.get("data") or {}).get("values") or []
        if not cols:
            continue
        i = next((fields.index(k) for k in ("__value", "value", "__text", "text") if k in fields), 0)
        out += [str(x) for x in cols[i] if x is not None]
    return list(dict.fromkeys(out))


def apply_regex(values: list[str], regex: str) -> list[str]:
    """A variable's regex (/.../flags, JavaScript): Keep matches; a capture group (or (?<value>..)) is the value."""
    m = re.fullmatch(r"/(.*)/([gimsuy]*)", regex or "", re.S)
    if not m:
        return values
    rx = re.compile(m[1].replace("(?<", "(?P<").replace("(?P<=", "(?<=").replace("(?P<!", "(?<!"),
                    re.I if "i" in m[2] else 0)
    out = []
    for v in values:
        hit = rx.search(v)
        if hit:
            out.append(hit.groupdict().get("value") or (hit.group(1) if rx.groups else v))
    return list(dict.fromkeys(out))


def variable_options(v: dict) -> list[str]:
    opts = [str(o.get("value")) for o in v.get("options") or [] if o.get("value") not in (None, "$__all")]
    if not opts and v.get("type") == "custom" and isinstance(v.get("query"), str):
        opts = [x.split(" : ")[-1].strip() for x in re.split(r"(?<!\\),", v["query"]) if x.strip()]
    return opts


def resolve_variables(dash: dict, run, t0: int, t1: int, mapper: DatasourceMap | None = None) -> tuple[dict, list]:
    """{name: values} for all variables in order (later ones may use earlier ones) + how each was chosen.
    run(queries) -> {refId: {"frames", "error"}}: Runs a query variable's query (None: offline, current values)."""
    values, info = builtins(t0, t1)[0], []
    for v in (dash.get("templating") or {}).get("list", []) or []:
        name, typ = v.get("name"), v.get("type")
        cur = (v.get("current") or {}).get("value")
        cur = [] if cur in (None, "", []) else [str(x) for x in (cur if isinstance(cur, list) else [cur])]
        want_all = cur == ["$__all"] or (not cur and bool(v.get("includeAll")))
        how, opts = "current", None
        if typ == "constant":
            vals = [str(v.get("query", ""))]
        elif typ == "query" and run is not None:
            q = v.get("query")
            ds = v.get("datasource")
            if isinstance(q, dict):
                q = copy.deepcopy(q)
            elif isinstance(ds, dict) and ds.get("type") in SQL_DS_TYPES:   # Synapse (mssql): the SQL text itself
                q = {"rawSql": q, "rawQuery": True, "editorMode": "code", "format": "table"}
            else:
                q = {"rawSQL": q, "rawSql": q, "query": q}
            q = interpolate_obj(q, values)
            q.update(refId="tempvar", datasource=mapper.resolve(ds) if mapper else ds)
            res = run([q]).get("tempvar") or {"error": "no result"}
            if res.get("error"):
                vals = [] if want_all else cur
                how = f"query failed: {res['error']}" + ("; current value used" if vals else "")
            else:
                # Grafana fills in variables in the regex first (e.g. /^(?:${cluster:pipe})\|([^|]+)/)
                opts = apply_regex(frame_values(res.get("frames")), interpolate(v.get("regex") or "", values))
                vals, how = select(cur, opts, want_all)
        elif typ in ("custom", "textbox", "interval", "datasource", "query"):
            opts = variable_options(v)
            if typ == "textbox" and not cur:
                cur = [str(v.get("query", ""))]
            vals, how = select(cur, opts, want_all) if opts else (cur, "current")
        else:
            continue   # adhoc / groupby filters: not part of the query text
        if want_all and v.get("includeAll") and v.get("allValue") and how.startswith("All"):
            vals = Raw(v["allValue"])
        if v.get("type") == "datasource" and vals and mapper:
            d = mapper.by_name.get(vals[0]) or mapper.by_uid.get(vals[0])
            vals = [d["uid"]] if d else vals
        values[name] = vals
        info.append({"name": name, "type": typ, "values": list(vals) if not isinstance(vals, Raw) else [str(vals)],
                     "how": how, "options": len(opts) if opts is not None else None})
    return values, info


def select(cur: list[str], opts: list[str], want_all: bool) -> tuple[list[str], str]:
    """Grafana's choice on load: The current value if still an option, All if selected/default, else the first."""
    if want_all:
        return opts, f"All ({len(opts)})"
    if cur and all(c in opts for c in cur):
        return cur, "current"
    if not opts:
        return cur, "no options" + ("; current value used" if cur else "")
    return opts[:1], f"first of {len(opts)}" + (f" (current {','.join(cur)} not among them)" if cur else "")


# ---------------------------------------------------------------- query runs
def ds_query(api, queries: list[dict], t0: int, t1: int) -> dict:
    """POST /api/ds/query: {refId: {"rows", "fields", "frames", "error"}}."""
    from grafana_client.client import GrafanaException
    try:
        r = api.client.POST("/ds/query", json={"queries": queries, "from": str(t0), "to": str(t1)})
    except GrafanaException as e:
        if isinstance(e.response, dict) and isinstance(e.response.get("results"), dict):
            r = e.response   # a failing query answers 4xx/5xx with the per-query results
        else:
            return {q["refId"]: {"rows": 0, "fields": [], "frames": [], "error": grafana_error(e)} for q in queries}
    out = {}
    for q in queries:
        res = (r.get("results") or {}).get(q["refId"]) or {}
        frames = res.get("frames") or []
        rows = sum(len(((f.get("data") or {}).get("values") or [[]])[0] or []) for f in frames)
        fields = [x.get("name") for f in frames for x in (f.get("schema") or {}).get("fields", [])]
        err = res.get("error") or (None if res else "no result for this query")
        out[q["refId"]] = {"rows": rows, "fields": fields, "frames": frames,
                           "error": " ".join(str(err).split())[:400] if err else None}
    return out


def panel_plan(dash: dict, only: list[str] | None) -> tuple[list, list]:
    """(panels with queries to run [(panel, targets)], skipped [(panel, reason)])."""
    run, skipped = [], []
    for p, _ in vd.iter_panels(dash.get("panels", [])):
        if p.get("type") in vd.NO_QUERY_PANELS or p.get("type") == "row":
            continue
        if only and str(p.get("id")) not in only and p.get("title") not in only:
            continue
        pds = p.get("datasource")
        targets = [t for t in p.get("targets") or [] if not t.get("hide")]
        if isinstance(pds, dict) and pds.get("uid") == "-- Dashboard --":
            skipped.append((p, "reuses another panel's queries"))
        elif not targets:
            skipped.append((p, "library panel (its queries live in the library)" if p.get("libraryPanel") else
                            "no queries"))
        else:
            run.append((p, targets))
    return run, skipped


def is_expression(t: dict, p: dict) -> bool:
    ds = t.get("datasource") or p.get("datasource")
    return isinstance(ds, dict) and ds.get("uid") in ("__expr__", "-- Grafana --") or ds == "__expr__"


def check_dashboard(api, dash: dict, mapper: DatasourceMap, t0: int, t1: int, only: list[str] | None,
                    yes: bool) -> dict:
    run, skipped = panel_plan(dash, only)
    var_queries = sum(1 for v in (dash.get("templating") or {}).get("list", []) or [] if v.get("type") == "query")
    n_panel = sum(1 for p, ts in run for t in ts if not is_expression(t, p))
    n = var_queries + n_panel
    if n > MAX_QUERIES and not yes:
        raise Refused(f"This check runs {n} queries ({var_queries} variable + {n_panel} panel queries) over "
                      f"{iso(t0)} -> {iso(t1)}. Athena/BigQuery/Synapse bill each query by the data it scans: Tell the "
                      "user the number, ask, then re-run with --yes (or fewer panels with --panel, or a shorter "
                      "--from/--to on a day with data)")
    default = next((d for d in mapper.dss if d.get("default")), None)
    values, var_info = resolve_variables(dash, lambda qs: ds_query(api, qs, t0, t1), t0, t1, mapper)
    panels = []
    for p, targets in run:
        local, iv, points = builtins(t0, t1, p)
        local = {**values, **local}
        note = None
        rep = p.get("repeat")
        if rep and isinstance(local.get(rep), list) and len(local[rep]) > 1:
            local[rep] = local[rep][:1]
            note = f"repeated per ${rep}: checked with {local[rep][0]}"
        queries = []
        for i, t in enumerate(targets):
            q = interpolate_obj(copy.deepcopy(t), local)
            ds = t.get("datasource") or p.get("datasource")
            ds = mapper.resolve(interpolate_obj(ds, local)) if ds else (
                {"type": default["type"], "uid": default["uid"]} if default else None)
            q.update(datasource=ds, refId=t.get("refId") or chr(65 + i), intervalMs=iv, maxDataPoints=points)
            queries.append(q)
        res = ds_query(api, queries, t0, t1)
        errs = [f"[{r}] {x['error']}" for r, x in res.items() if x["error"]]
        rows = sum(x["rows"] for x in res.values())
        panels.append({"id": p.get("id"), "title": interpolate(p.get("title", ""), local), "status": "error" if errs else
                       "ok" if rows else "no data", "rows": rows, "errors": errs, "note": note,
                       "queries": {r: {"rows": x["rows"], "fields": x["fields"][:12], "error": x["error"]}
                                   for r, x in res.items()}})
    count = {s: sum(1 for x in panels if x["status"] == s) for s in ("ok", "no data", "error")}
    return {"title": dash.get("title"), "uid": dash.get("uid"), "from": iso(t0), "to": iso(t1),
            "queries": n, "variables": var_info, "panels": panels,
            "skipped": [{"id": p.get("id"), "title": p.get("title", ""), "reason": r} for p, r in skipped],
            "summary": count, "data_sources": mapper.mapped}


# ---------------------------------------------------------------- commands
def cmd_status(a) -> int:
    api, prof = connect(a)
    res = inspect_grafana({"url": prof["GRAFANA_URL"], "token": TOKEN})
    tagged = None
    if res["ok"]:
        try:
            tagged = len(api.search.search_dashboards(tag=MARKER_TAG, type_="dash-db", limit=5000))
        except Exception:  # noqa: BLE001 - informational only
            pass
    dss = res["datasources"]
    out = {"profile": prof["path"], "url": res["url"], "organization": res["org"], "grafana_version": res["version"],
           "role": res["role"], "token_allows": GRAFANA_ROLES.get(res["role"]) if res["role"] else None,
           "access": res["access"], "data_sources": dss,
           "expected_data_sources": [n for n in GRAFANA_DS.values() if any(d["name"] == n for d in dss or [])],
           "dashboards_imported_by_this_tool": tagged,
           "checks": [f"{CHECK_TAG[ok]} {n}" + (f": {d}" if d else "") for n, ok, d in res["checks"]]}
    if a.json:
        say(json.dumps(out, indent=1))
    else:
        say(f"Grafana profile {prof['name']} ({prof['path']}): {res['url']}")
        say(f"  Organization {res['org'] or '-'}, Grafana {res['version'] or '-'}, role {res['role'] or '-'}")
        for c in out["checks"]:
            say(f"  {c}")
        if dss:
            say("  Data sources: " + ", ".join(f"{d['name']} ({d['type']})" for d in dss))
        if tagged is not None:
            say(f"  Dashboards imported by this tool (tag {MARKER_TAG}): {tagged}")
    return 0 if res["ok"] else 1


def cmd_datasources(a) -> int:
    api, _ = connect(a)
    dss = grafana_datasources(api)
    if a.json:
        say(json.dumps(dss, indent=1))
        return 0
    say(f"{len(dss)} data source(s):")
    for d in dss:
        exp = " <- the name the dashboards expect" if d["name"] in CONVENTION.values() else ""
        say(f"  {d['name']} | {d['type']} | uid {d['uid']}" + (" | default" if d["default"] else "") + exp)
    return 0


def search(api, **kw) -> list[dict]:
    return api.search.search_dashboards(limit=5000, **kw)


def cmd_folders(a) -> int:
    api, _ = connect(a)
    rows = [{"title": h.get("title"), "uid": h.get("uid"), "parent": h.get("folderTitle")}
            for h in search(api, type_="dash-folder")]
    if a.json:
        say(json.dumps(rows, indent=1))
        return 0
    say(f"{len(rows)} folder(s):")
    for r in rows:
        say(f"  {r['title']} | uid {r['uid']}" + (f" | in {r['parent']}" if r["parent"] else ""))
    return 0


def cmd_search(a) -> int:
    api, prof = connect(a)
    rows = [{"title": h.get("title"), "uid": h.get("uid"), "folder": h.get("folderTitle") or "General",
             "tags": h.get("tags") or [], "url": dash_url(prof, h.get("uid"))}
            for h in search(api, type_="dash-db", **({"query": a.query} if a.query else {}))]
    if a.json:
        say(json.dumps(rows, indent=1))
        return 0
    say(f"{len(rows)} dashboard(s)" + (f" matching '{a.query}'" if a.query else "") + ":")
    for r in rows:
        say(f"  {r['title']} | uid {r['uid']} | {r['folder']}" + (f" | tags {', '.join(r['tags'])}" if r["tags"]
                                                                  else "") + f" | {r['url']}")
    return 0


def find_folder(api, name: str) -> dict | None:
    hits = [h for h in search(api, type_="dash-folder", query=name) if (h.get("title") or "").lower() == name.lower()]
    if len(hits) > 1:
        raise Refused(f"Several folders named '{name}' (uids {', '.join(h['uid'] for h in hits)}): Rename one in Grafana")
    return hits[0] if hits else None


def prepare_import(d: dict, dss: list[dict], override: str | None) -> tuple[dict, DatasourceMap, dict]:
    """Offline part of import: Validate, map the data sources, uid + marker tag. Returns (dashboard, map, validation)."""
    res = vd.validate(d)
    if res["errors"]:
        raise Refused(f"validate_dashboard.py finds {len(res['errors'])} error(s), fix them first: "
                      + "; ".join(res["errors"][:5]))
    m = DatasourceMap(dss, override)
    dash = m.apply(copy.deepcopy(d))
    if m.problem():
        raise Refused(m.problem())
    dash["uid"] = dash.get("uid") or slug(dash.get("title"))
    dash["id"] = None
    dash["tags"] = list(dict.fromkeys([*(dash.get("tags") or []), MARKER_TAG]))
    return dash, m, res


def lake_database(env_name: str | None) -> tuple[str | None, str]:
    """The Athena database of the workspace's data lake profile (--env, else the only one): (database, note)."""
    import env as ws_env
    try:
        ws_env.use_env(env_name, exc=RuntimeError)
        if not ws_env.profile_path():
            return None, ws_env.profile_problem() or "no data lake profile"
        info = ws_env.lake()
        if info.get("cloud") != "aws" or not info.get("lake_id"):
            return None, "the data lake profile is not an AWS data lake"
        return info.get("athena_database") or ws_env.athena_database(info["lake_id"]), ""
    except Exception as e:  # noqa: BLE001 - the comparison is a safety check, never a blocker by itself
        return None, str(e)[:150]


def synapse_target(env_name: str | None) -> tuple[tuple[str, str] | None, str]:
    """(serverless SQL endpoint, database) of the workspace's Azure data lake profile, else (None, note)."""
    import env as ws_env
    try:
        ws_env.use_env(env_name, exc=RuntimeError)
        if not ws_env.profile_path():
            return None, ws_env.profile_problem() or "no data lake profile"
        cfg = ws_env.synapse_config()
        if ws_env.lake().get("cloud") != "azure" or not (cfg["endpoint"] and cfg["database"]):
            return None, "the data lake profile is not an Azure data lake"
        return (cfg["endpoint"], cfg["database"]), ""
    except Exception as e:  # noqa: BLE001 - a safety check, never a blocker by itself
        return None, str(e)[:150]


def synapse_mismatches(api, m: DatasourceMap, env_name: str | None) -> tuple[list[str], str]:
    """Microsoft SQL Server data sources the dashboard uses that point to another Synapse endpoint or database than
    the Azure profile (same reason as for Athena)."""
    used = {uid: d for uid, d in m.used.items() if d.get("type") == PLUGIN.get("synapse")}
    if not used:
        return [], ""
    want, note = synapse_target(env_name)
    if not want:
        return [], f"data source not compared with the lake ({note})"
    out = []
    for uid, d in used.items():
        try:
            full = api.datasource.get_datasource_by_uid(uid)
        except Exception:  # noqa: BLE001 - not readable with this token
            out.append(f"'{d['name']}': settings not readable with this token, check it points to {want[0]} / "
                       f"{want[1]}")
            continue
        host = (full.get("url") or "").split("://")[-1].split(",")[0].split(":")[0].lower()
        db = (full.get("jsonData") or {}).get("database") or full.get("database")
        if host and host != want[0]:
            out.append(f"'{d['name']}' (uid {uid}) points to {host}, the data lake is {want[0]}")
        elif db and db.lower() != want[1].lower():
            out.append(f"'{d['name']}' (uid {uid}) queries database {db}, the data lake is {want[1]}")
    return out, ""


def database_mismatches(api, m: DatasourceMap, env_name: str | None) -> tuple[list[str], str]:
    """Athena data sources the dashboard uses whose default database differs from the lake's: A dashboard mapped to
    'Amazon Athena' of another lake imports fine, but every panel then queries the wrong lake (or fails). Synapse:
    The Microsoft SQL Server data source's host + database vs the Azure profile."""
    if any(d.get("type") == PLUGIN.get("synapse") for d in m.used.values()):
        return synapse_mismatches(api, m, env_name)
    want, note = lake_database(env_name)
    if not want:
        return [], f"data source database not compared with the lake ({note})"
    out = []
    for uid, d in m.used.items():
        if d.get("type") != PLUGIN.get("athena"):
            continue
        try:
            db = (api.datasource.get_datasource_by_uid(uid).get("jsonData") or {}).get("database")
        except Exception:  # noqa: BLE001 - not readable with this token: say so instead of guessing
            out.append(f"'{d['name']}': database not readable with this token, check it points to {want}")
            continue
        if db and db != want:
            out.append(f"'{d['name']}' (uid {uid}) queries Athena database {db}, the data lake is {want}")
    return out, ""


def cmd_import(a) -> int:
    d = load_json(a.dashboard)
    api, prof = connect(a)
    dash, m, val = prepare_import(d, grafana_datasources(api), a.datasource_uid)
    bad, note = database_mismatches(api, m, a.env)
    if bad and not a.any_database:
        raise Refused("Wrong lake: " + "; ".join(bad) + ". Pick the data source of this lake with --datasource-uid "
                      "(datasources lists them; the user adds one if none fits), or --any-database if intended")
    uid, title = dash["uid"], dash.get("title", "")
    existing = get_dashboard(api, uid)
    if existing and MARKER_TAG not in (existing["dashboard"].get("tags") or []) and not a.overwrite:
        raise Refused(f"Grafana already has a dashboard with uid {uid} ('{existing['dashboard'].get('title')}', not "
                      "imported by this tool): Ask the user, then re-run with --overwrite (replaces it), or change "
                      "the uid in the file")
    folder, created = None, False
    if a.folder:
        folder = find_folder(api, a.folder)
        if not folder:
            folder = api.folder.create_folder(a.folder)
            created = True
    folder_uid = folder["uid"] if folder else (existing["meta"].get("folderUid") or None) if existing else None
    clash = [h for h in search(api, type_="dash-db", query=title) if (h.get("title") or "").lower() == title.lower()
             and h.get("uid") != uid and (h.get("folderUid") or None) == folder_uid]
    if clash and not a.overwrite:
        raise Refused(f"The folder already has a dashboard '{title}' (uid {clash[0]['uid']}): Change the title, use "
                      "--folder, or ask the user and re-run with --overwrite (replaces that dashboard)")
    payload = {"dashboard": dash, "overwrite": bool(existing) or a.overwrite, "message": MESSAGE}
    if folder_uid:
        payload["folderUid"] = folder_uid
    r = api.dashboard.update_dashboard(payload)
    url = dash_url(prof, r.get("uid", uid))
    out = {"title": title, "uid": r.get("uid", uid), "version": r.get("version"), "url": url,
           "updated": bool(existing), "folder": (folder or {}).get("title") or (
               existing["meta"].get("folderTitle") if existing else None) or "General", "folder_created": created,
           "data_sources": m.mapped, "validation_warnings": len(val["warnings"]) + len(val.get("schema", []))}
    if a.json:
        say(json.dumps(out, indent=1))
    else:
        say(f"{'Updated' if existing else 'Imported'} '{title}' (uid {out['uid']}, version {out['version']}) in "
            f"folder {out['folder']}" + (" (created)" if created else ""))
        for ref, ds in m.mapped.items():
            say(f"  Data source: {ref} -> {ds}")
        for b in bad:
            say(f"  WARNING: {b} (--any-database)")
        if note:
            say(f"  Note: {note}")
        if out["validation_warnings"]:
            say(f"  {out['validation_warnings']} validation warning(s): validate_dashboard.py lists them")
        say(f"OPEN Grafana dashboard: {url}")
    return 0


def cmd_export(a) -> int:
    api, _ = connect(a)
    r = get_dashboard(api, a.uid)
    if not r:
        raise Refused(f"No dashboard with uid {a.uid} (search lists them)")
    dash = r["dashboard"]
    dash["id"] = None
    try:
        n = to_convention(dash, grafana_datasources(api))
    except Exception:  # noqa: BLE001 - keep the real uids if data sources are not readable
        n = 0
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(dash, indent=2), encoding="utf-8")
    meta = r.get("meta") or {}
    info = {"file": out.as_posix(), "title": dash.get("title"), "uid": dash.get("uid"), "version": meta.get("version"),
            "folder": meta.get("folderTitle") or "General", "refs_to_convention_names": n,
            "imported_by_this_tool": MARKER_TAG in (dash.get("tags") or [])}
    if a.json:
        say(json.dumps(info, indent=1))
    else:
        say(f"Wrote {info['file']} ('{info['title']}', uid {info['uid']}, version {info['version']}, folder "
            f"{info['folder']})")
        if n:
            say(f"  {n} data source ref(s) written as 'Amazon Athena' / 'Google BigQuery' / 'Microsoft SQL Server' "
                "(import maps them again)")
        say("  Edit it, check it with validate_dashboard.py, then import it again (same uid = update in place"
            + ("" if info["imported_by_this_tool"] else "; not imported by this tool: needs --overwrite") + ")")
    return 0


def cmd_check(a) -> int:
    api, prof = connect(a, timeout=180.0)
    from_file = a.target.lower().endswith(".json") or Path(a.target).is_file()
    if from_file:
        dash = load_json(a.target)
    else:
        r = get_dashboard(api, a.target)
        if not r:
            raise Refused(f"No dashboard with uid {a.target} (search lists them)")
        dash = r["dashboard"]
    mapper = DatasourceMap(grafana_datasources(api), a.datasource_uid)
    dash = mapper.apply(copy.deepcopy(dash))
    if mapper.problem():
        raise Refused(mapper.problem())
    bad, _ = database_mismatches(api, mapper, a.env)
    for b in bad:
        say(f"WARNING: {b}: The panels below query that database")
    t0, t1 = time_range(dash, a.from_, a.to)
    res = check_dashboard(api, dash, mapper, t0, t1, a.panel, a.yes)
    if a.json:
        say(json.dumps(res, indent=1))
    else:
        say(f"Checked '{res['title']}' (uid {res['uid']}) over {res['from']} -> {res['to']}: {res['queries']} queries")
        seen: dict[str, str] = {}   # the same error (e.g. data source credentials) is printed once

        def short(e: str, where: str) -> str:
            ref = re.match(r"\[\w+\] ", e)
            key = re.sub(r"(RequestID|request id|Job ID)[:=]?\s*[\w-]+,?\s*", "", e[ref.end() if ref else 0:], flags=re.I)
            if key in seen:
                return (ref.group(0) if ref else "") + f"same error as {seen[key]}"
            seen[key] = where
            return e
        if res["variables"]:
            say("  Variables: " + "; ".join(f"${v['name']} = {','.join(v['values'])[:60] or '(empty)'} ("
                                            f"{short(v['how'], '$' + v['name']) if v['how'].startswith('query failed') else v['how']})"
                                            for v in res["variables"]))
        for p in res["panels"]:
            tag = {"ok": "OK      ", "no data": "NO DATA ", "error": "ERROR   "}[p["status"]]
            say(f"  {tag} panel {p['id']} '{p['title']}': {p['rows']} rows" + (f" ({p['note']})" if p["note"] else ""))
            for e in p["errors"]:
                say(f"           {short(e, 'panel ' + str(p['id']))}")
        for s in res["skipped"]:
            say(f"  SKIPPED  panel {s['id']} '{s['title']}': {s['reason']}")
        c = res["summary"]
        say(f"  -> {c['ok']} OK, {c['no data']} no data, {c['error']} error(s)"
            + (" (no data: Is the time range where the data is? --from/--to)" if c["no data"] else ""))
        if not from_file:   # a local file's uid may not be in Grafana: No link
            say(f"OPEN Grafana dashboard: {dash_url(prof, res['uid'])}")
    return 1 if res["summary"]["error"] else 0


SHOT_BUSY = '[aria-label="Panel loading bar"], [data-testid*="loading"], .panel-loading'


def cmd_shot(a) -> int:
    """PNG of a dashboard as the user sees it (1920x1080; --full: every panel), for the visual review. Headless
    Chromium via Playwright (optional package), the token sent as a Bearer header (never printed or saved)."""
    try:
        from playwright.sync_api import Error as PwError, sync_playwright
    except ImportError as e:
        raise Refused("shot needs Playwright (optional, not in requirements.txt; ~150 MB with Chromium): "
                      ".venv\\Scripts\\pip install playwright, then .venv\\Scripts\\python -m playwright install "
                      "chromium (macOS/Linux: .venv/bin/...). Ask the user first") from e
    _, prof = connect(a)
    q = {"orgId": "1"}
    now = int(time.time() * 1000)
    if a.from_:
        q["from"] = str(parse_time(a.from_, now))
    if a.to:
        q["to"] = str(parse_time(a.to, now, end=True))
    for kv in a.var or []:
        k, _, v = kv.partition("=")
        q[f"var-{k}"] = v
    url = dash_url(prof, a.uid) + "?" + "&".join(f"{k}={quote(v)}" for k, v in q.items())
    out = Path(a.out)
    out.parent.mkdir(parents=True, exist_ok=True)

    def wait_loaded(page) -> bool:   # no panel loading for 3 s in a row (Athena/BigQuery/Synapse queries take seconds)
        t0, quiet = time.time(), 0
        while time.time() - t0 < a.wait:
            quiet = quiet + 1 if page.locator(SHOT_BUSY).count() == 0 else 0
            if quiet >= 3 and time.time() - t0 > 8:
                return True
            time.sleep(1)
        return False

    with sync_playwright() as pw:
        try:
            browser = pw.chromium.launch()
        except PwError as e:
            raise Refused("No Chromium for Playwright: .venv\\Scripts\\python -m playwright install chromium") from e
        try:
            ctx = browser.new_context(viewport={"width": 1920, "height": a.height},
                                      extra_http_headers={"Authorization": f"Bearer {TOKEN}"})
            page = ctx.new_page()
            page.goto(url, wait_until="domcontentloaded")
            loaded = wait_loaded(page)
            if "/login" in page.url:
                raise Refused("Grafana showed its login page: The token was not accepted for the UI")
            if a.full:   # Grafana only queries the panels in view: A tall viewport loads all of them
                page.set_viewport_size({"width": 1920, "height": 3000})
                loaded = wait_loaded(page) and loaded
            page.screenshot(path=str(out), full_page=a.full)
            nodata = page.get_by_text("No data", exact=True).count()
            errors = page.locator('[data-testid*="Panel status error"]').count()
        finally:
            browser.close()
    res = {"file": out.resolve().as_posix(), "loaded": loaded, "no_data_panels": nodata, "error_panels": errors}
    if a.json:
        say(json.dumps(res))
    else:
        say(f"Saved {res['file']} ({'all panels loaded' if loaded else f'still loading after {a.wait} s'}; "
            f"'No data' panels: {nodata}, panels with an error: {errors})")
        say("  Review the PNG (Read it): Units, cut titles, empty pies, colors, legends. Click links in the browser")
    return 0


def cmd_delete(a) -> int:
    api, _ = connect(a)
    r = get_dashboard(api, a.uid)
    if not r:
        raise Refused(f"No dashboard with uid {a.uid}")
    title = r["dashboard"].get("title")
    folder = (r.get("meta") or {}).get("folderTitle") or "General"
    if not a.yes:
        raise Refused(f"This deletes '{title}' (uid {a.uid}, folder {folder}). Only when the user asked: Re-run with "
                      "--yes")
    api.dashboard.delete_dashboard(a.uid)
    say(json.dumps({"deleted": a.uid, "title": title, "folder": folder}) if a.json else
        f"Deleted '{title}' (uid {a.uid}, folder {folder})")
    return 0


def main() -> int:
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--grafana-env", metavar="NAME", help="Grafana profile credentials/grafana/NAME.env (default: "
                                                              "the only one)")
    common.add_argument("--json", action="store_true", help="JSON output")
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("status", parents=[common], help="URL, version, what the token allows, data sources")
    sub.add_parser("datasources", parents=[common], help="Data sources (name, type, uid)")
    sub.add_parser("folders", parents=[common], help="Folders (title, uid)")
    p = sub.add_parser("search", parents=[common], help="Dashboards (title, uid, folder, URL)")
    p.add_argument("--query", help="Text in the title")
    p = sub.add_parser("import", parents=[common], help="Upload a dashboard JSON (same uid = update in place)")
    p.add_argument("dashboard", help="Dashboard JSON file")
    p.add_argument("--folder", help="Folder title (created if missing; default: General, or the current folder)")
    p.add_argument("--overwrite", action="store_true", help="Replace a dashboard this tool did not import (same uid "
                                                            "or same title in the folder): Only when the user agrees")
    p.add_argument("--datasource-uid", metavar="UID", help="The user's Athena/BigQuery/Synapse data source (uid or "
                                                           "name) when none is named 'Amazon Athena' / 'Google "
                                                           "BigQuery' / 'Microsoft SQL Server'")
    p.add_argument("--env", metavar="NAME", help="Data lake profile (credentials/NAME.env) whose Athena database the "
                                                 "data source must query (default: the only one)")
    p.add_argument("--any-database", action="store_true", help="Import although the Athena data source queries "
                                                               "another database than the data lake's")
    p = sub.add_parser("export", parents=[common], help="Download a dashboard JSON")
    p.add_argument("uid")
    p.add_argument("-o", "--out", required=True, help="Output JSON file")
    p = sub.add_parser("check", parents=[common], help="Run every panel's queries, report rows / no data / errors")
    p.add_argument("target", help="Dashboard uid in Grafana, or a dashboard JSON file")
    p.add_argument("--from", dest="from_", metavar="ISO", help="Start (ISO, UTC if no zone; or now-7d). Default: the "
                                                               "dashboard's time range")
    p.add_argument("--to", metavar="ISO", help="End (ISO or now)")
    p.add_argument("--panel", nargs="+", metavar="ID|TITLE", help="Only these panels")
    p.add_argument("--yes", action="store_true", help=f"Run more than {MAX_QUERIES} queries (the user agreed)")
    p.add_argument("--datasource-uid", metavar="UID", help="As for import")
    p.add_argument("--env", metavar="NAME", help="As for import (warns if the data source queries another database)")
    p = sub.add_parser("shot", parents=[common], help="PNG of a dashboard in the browser (needs Playwright)")
    p.add_argument("uid")
    p.add_argument("-o", "--out", required=True, help="Output PNG file")
    p.add_argument("--from", dest="from_", metavar="ISO", help="Start (ISO, UTC if no zone; or now-7d). Default: the "
                                                               "dashboard's time range")
    p.add_argument("--to", metavar="ISO", help="End (ISO or now)")
    p.add_argument("--var", nargs="+", metavar="NAME=VALUE", help="Variable values (e.g. device=2F6913DB)")
    p.add_argument("--full", action="store_true", help="The whole dashboard (tall viewport), not the first screen")
    p.add_argument("--height", type=int, default=1080, help="Viewport height (default 1080)")
    p.add_argument("--wait", type=int, default=90, help="Max seconds to wait for the panels to load (default 90)")
    p = sub.add_parser("delete", parents=[common], help="Delete a dashboard (only when the user asks)")
    p.add_argument("uid")
    p.add_argument("--yes", action="store_true", help="Really delete it")
    a = ap.parse_args()
    from grafana_client.client import GrafanaException
    try:
        return {"status": cmd_status, "datasources": cmd_datasources, "folders": cmd_folders, "search": cmd_search,
                "import": cmd_import, "export": cmd_export, "check": cmd_check, "shot": cmd_shot,
                "delete": cmd_delete}[a.cmd](a)
    except Refused as e:
        say(json.dumps({"refused": str(e)}) if a.json else f"REFUSED: {e}")
        return 2
    except GrafanaException as e:
        msg = grafana_error(e) + (" - the token's role does not allow this (status shows what it allows; import, "
                                  "export and delete need Editor)" if e.status_code == 403 else "")
        say(json.dumps({"error": msg}) if a.json else f"ERROR: Grafana API: {msg}")
        return 1
    except Exception as e:  # noqa: BLE001 - connection problems etc., shown plainly (the token is masked)
        say(json.dumps({"error": grafana_error(e)}) if a.json else f"ERROR: {grafana_error(e)}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
