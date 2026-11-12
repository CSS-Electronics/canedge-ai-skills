"""One interface for a CANedge Parquet data lake: Local folder (DuckDB), Amazon Athena, Google BigQuery or Azure
Synapse.

All backends expose the same table names (as the data lake's Glue mapping): tbl_<device>_<message> in lower case,
columns t (UTC) + one lower-case column per signal + date_created ('yyyy/MM/dd'), plus tbl_<device>_messages.
SQL is written in the Athena (Trino) dialect; the local backend adds DuckDB macros for the Trino functions DuckDB
lacks and the BigQuery and Synapse backends translate it, so the same SQL runs on all.

Files: _backends.py = the shared part (connect(), naming, SQL checks, the Lake base class with discovery, generated
queries, fleet batching and the storage request guard); _local.py = DuckDB on a Parquet folder; _athena.py = Athena +
Glue; _bigquery.py = BigQuery; _synapse.py = Azure Synapse serverless SQL (T-SQL via OPENROWSET). A new backend
is one more module with a Lake subclass (tables(), sql(), describe_source(), and list_files() for the request guard
on cloud storage).

Storage request guard (cloud): fleet() and summary() estimate the S3/GCS request cost of the files they will read
(one folder listing per sampled device) and stop above max_request_usd (default $1 per connect()). Very rough.

Module use (run from the repo root):
  import sys; sys.path.insert(0, ".claude/skills/canedge-lake-query/scripts")
  from _backends import connect
  db = connect(project="projects/<name>")          # local: projects/<name>/parquet
  db = connect(lake="TEMP/lake")                   # local: any folder with the data lake layout
  db = connect(athena=True, env=None)              # S3 data lake via Athena (profile credentials/<env>.env)
  db = connect(synapse=True, env="<name>")         # Azure data lake via Synapse (an azure profile)
  df = db.resample("12345678", "can1_eec1_s00", ["enginespeed"], "2026-01-15", "2026-01-16")  # Grafana-style query
  df = db.sql("SELECT ... FROM tbl_12345678_can1_eec1_s00 WHERE date_created = '2026/01/15' GROUP BY 1")
  hits = db.find("speed")                          # [{table, device, message, signal, type}]
  df = db.fleet("SELECT date_created AS day, max(speed) AS max_speed FROM {table} "
                "WHERE date_created BETWEEN '2026/06/01' AND '2026/06/30' GROUP BY 1", hits, "2026-06-01", "2026-07-01")
  est = db.request_estimate(hits, "2026-06-01", "2026-07-01")   # cloud: {files, usd, text, ...}; local: None
All returned datetime columns are tz-aware UTC.
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
from env import GET_USD, LIST_USD  # noqa: E402
from env import load_env, use_env as _use_env  # noqa: E402,F401 (load_env used by the backends)

FLEET_BATCH = 50                 # tables per UNION ALL query
MAX_QUERY_CHARS = 250_000        # Athena limit 262,144 bytes
AGGS = {"avg": "avg({c})", "min": "min({c})", "max": "max({c})", "sum": "sum({c})", "count": "count({c})",
        "first": "min_by({c}, t)", "last": "max_by({c}, t)"}
GETS_PER_FILE = 2                # Parquet footer + column data (rough)
SAMPLE_DEVICES = 5               # devices whose files are listed per estimate
FALLBACK_FILES_PER_DAY = 20      # per message table (one yyyy/mm/dd folder) and day, when listing fails
FALLBACK_DAYS = 365              # no start and nothing listed
MAX_REQUEST_USD = 1.0            # request guard per connect()
DATE_KEY_RE = re.compile(r"/(\d{4})/(\d{2})/(\d{2})/[^/]+$")
NICE_STEPS_S = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200,
                10800, 21600, 43200, 86400]


class LakeError(Exception):
    """Clean, user-facing error (no stack trace needed): Wrong name, budget reached, too many rows, ..."""




# ---------------------------------------------------------------- data lake profiles (credentials/<name>.env, cloud backends)
def use_env(name: str | None) -> None:
    """--env NAME: Use the profile credentials/NAME.env (canedge-workspace env.py), LakeError if it is missing."""
    _use_env(name, LakeError)


# ---------------------------------------------------------------- helpers
def parse_utc(s) -> dt.datetime | None:
    """ISO string / datetime / pandas Timestamp -> aware UTC datetime. Naive input is treated as UTC."""
    if s is None or s == "":
        return None
    if isinstance(s, str):
        s = s.strip().replace(" ", "T")
        if s.endswith("Z"):
            s = s[:-1] + "+00:00"
        s = dt.datetime.fromisoformat(s)
    if isinstance(s, pd.Timestamp):
        s = s.to_pydatetime()
    if s.tzinfo is None:
        s = s.replace(tzinfo=dt.timezone.utc)
    return s.astimezone(dt.timezone.utc)


def ts_literal(t: dt.datetime) -> str:
    """UTC datetime -> SQL literal valid in Trino and DuckDB."""
    return "TIMESTAMP '" + t.replace(tzinfo=None).isoformat(sep=" ", timespec="milliseconds") + "'"


def match_device(device: str, ids: list[str], names: dict[str, str | None], where: str = "") -> str:
    """A device ID or meta name -> the device ID, from the known IDs + ID -> meta name (Lake.device_id; also for
    saved summaries). A name resolves only if exactly one device has it (exact, case-insensitive, with or without the
    ' (<ID>)' suffix); a shared or partial name raises LakeError with the candidates."""
    x = str(device).strip()
    if x.upper() in ids:
        return x.upper()
    names = {d: n for d, n in names.items() if n}
    low = x.lower()
    bare = lambda n: re.sub(r"\s*\([0-9A-Fa-f]{8}\)\s*$", "", n).lower()   # noqa: E731 - 'Truck 1 (12345678)'
    hits = [d for d, n in names.items() if low in (n.lower(), bare(n))]
    cands = lambda ds: ", ".join(f"{d} '{names[d]}'" for d in ds[:15])   # noqa: E731
    if len(hits) == 1:
        print(f"NOTE: Device name '{x}' = {hits[0]}", file=sys.stderr)
        return hits[0]
    if hits:
        raise LakeError(f"Meta name '{x}' is not unique - {len(hits)} devices have it: {cands(hits)}. Ask the user "
                        "which one, then use the device ID")
    part = [d for d, n in names.items() if low in n.lower()]
    if part:
        raise LakeError(f"No device is named exactly '{x}'. Names containing it: {cands(part)}. Ask the user, "
                        "then use the device ID")
    listed = ", ".join(f"{d} '{names[d]}'" if d in names else d for d in sorted(ids)[:30])
    raise LakeError(f"No device '{x}'{' ' + where if where else ''}. Devices: {listed or '-'}"
                    + ("" if names else " (no device meta names: Use the device ID)"))


def table_name(device: str, message: str) -> str:
    """Athena-style table name: tbl_<device>_<message>, lower case (Glue lower-cases table names)."""
    return re.sub(r"[^a-z0-9_]", "_", f"tbl_{device}_{message}".lower())


def classify(name: str) -> dict:
    """tbl_<device>_<message> | tbl_<device>_messages | tbl_aggregations_<x> | other."""
    n = name.lower()
    m = re.fullmatch(r"tbl_([0-9a-f]{8})_(.+)", n)
    if m:
        if m.group(2) == "messages":
            return {"kind": "message_list", "device": m.group(1).upper(), "message": None}
        return {"kind": "message", "device": m.group(1).upper(), "message": m.group(2)}
    m = re.fullmatch(r"tbl_aggregations_(.+)", n)
    if m:
        return {"kind": "aggregation", "device": None, "message": m.group(1)}
    return {"kind": "other", "device": None, "message": None}


def table_names(tables) -> list[str]:
    """Unique lower-case table names from names, find() hits or tables() entries (order kept)."""
    names = []
    for x in tables:
        n = (x.get("table") or x["name"] if isinstance(x, dict) else str(x)).lower()
        if n not in names:
            names.append(n)
    return names


def usd_fmt(x: float) -> str:
    return f"${x:.2f}" if x >= 0.01 else f"${x:.4f}"


def val(s: str) -> str:
    """A signal column with NaN as NULL: Some lakes hold NaN for invalid/"not available" values (e.g. older decoder
    versions) where others hold NULL; count/min/max/avg and bins must treat both as no value."""
    return f'(CASE WHEN is_nan("{s}") THEN NULL ELSE "{s}" END)'


def signal_cols(t: dict) -> list[list[str]]:
    return [c for c in t["columns"] if c[0] not in ("t", "date_created")]


def nice_interval(seconds: float) -> float:
    """Smallest 'nice' bucket >= seconds (like Grafana's $__interval)."""
    return next((s for s in NICE_STEPS_S if s >= seconds), NICE_STEPS_S[-1])


def interval_s(spec: str) -> float:
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*(ms|s|sec|m|min|h|d)\s*", str(spec).lower())
    if not m:
        raise LakeError(f"Bad interval '{spec}' (use auto, or e.g. 100ms, 1s, 10s, 1min, 1h, 1d)")
    return float(m.group(1)) * {"ms": 0.001, "s": 1, "sec": 1, "m": 60, "min": 60, "h": 3600, "d": 86400}[m.group(2)]


def fmt_interval(s: float) -> str:
    for unit, n in (("d", 86400), ("h", 3600), ("min", 60), ("s", 1)):
        if s >= n and abs(s / n - round(s / n)) < 1e-9:
            return f"{round(s / n)}{unit}"
    return f"{round(s * 1000)}ms"


# ---------------------------------------------------------------- static SQL checks
READ_KEYWORDS = {"SELECT", "WITH", "SHOW", "DESCRIBE", "DESC", "EXPLAIN", "VALUES", "TABLE"}
# Matched case-insensitively after a word boundary, outside comments and string literals
WRITE_PATTERNS = {"INSERT": r"INSERT\s+(INTO|OVERWRITE)\b",
                  "CREATE": r"CREATE\s+(OR\s+REPLACE\s+)?(EXTERNAL\s+|TEMP\s+|TEMPORARY\s+)?(TABLE|VIEW|DATABASE|SCHEMA|FUNCTION|MACRO)\b",
                  "DROP": r"DROP\s+(TABLE|VIEW|DATABASE|SCHEMA|FUNCTION|MACRO)\b",
                  "ALTER": r"ALTER\s+(TABLE|VIEW|DATABASE|SCHEMA)\b", "UNLOAD": r"UNLOAD\s*\(",
                  "DELETE": r"DELETE\s+FROM\b", "UPDATE": r"UPDATE\s+\S+\s+SET\b", "MERGE": r"MERGE\s+INTO\b",
                  "MSCK": r"MSCK\b", "VACUUM": r"VACUUM\b", "OPTIMIZE": r"OPTIMIZE\s+\S+\s+REWRITE\b",
                  "TRUNCATE": r"TRUNCATE\s+(TABLE\s+)?\w", "GRANT": r"GRANT\s+\w+\s+ON\b", "REVOKE": r"REVOKE\s+\w+\s+ON\b",
                  "COPY": r"COPY\s+\S", "ATTACH": r"ATTACH\b", "EXPORT": r"EXPORT\s+DATABASE\b", "INSTALL": r"INSTALL\s+\w",
                  "LOAD": r"LOAD\s+\w", "SET": r"^\s*SET\s+\w", "PRAGMA": r"PRAGMA\b",
                  # T-SQL (Synapse): procedures, variables, SELECT ... INTO, logins/credentials/external objects
                  "EXEC": r"EXEC(UTE)?\s*[\w\[(@'\"]", "DECLARE": r"DECLARE\s+@", "INTO": r"INTO\s+[#@\[\w]",
                  "BULK INSERT": r"BULK\s+INSERT\b", "DENY": r"DENY\s+\w+\s+ON\b",
                  "CREATE/DROP/ALTER (T-SQL)": r"(CREATE|DROP|ALTER)\s+(EXTERNAL|LOGIN|USER|ROLE|CREDENTIAL|DATABASE\s+"
                                               r"SCOPED|MASTER\s+KEY|PROC|PROCEDURE|TRIGGER|STATISTICS|INDEX)\b"}


AGGREGATES = re.compile(
    r"\bGROUP\s+BY\b|\bLIMIT\b|\bOVER\s*\(|\b(COUNT|AVG|MIN|MAX|SUM|APPROX_PERCENTILE|MAX_BY|MIN_BY)\s*\(", re.I)


def returns_raw_rows(stripped_sql: str) -> bool:
    """No GROUP BY / LIMIT / window / aggregate: The query may return millions of raw rows (also used by
    canedge-grafana for panel queries)."""
    return not AGGREGATES.search(stripped_sql)


def strip_sql(sql: str) -> str:
    """Remove comments and string literals (for keyword checks only)."""
    s = re.sub(r"/\*.*?\*/", " ", sql, flags=re.S)
    s = re.sub(r"--[^\n]*", " ", s)
    return re.sub(r"'(?:[^']|'')*'", "''", s)


def check_sql(sql: str, backend: str = "athena") -> dict:
    """Static checks: Read-only statement, Grafana macros, and (Athena) partition filter + cost warnings."""
    s = strip_sql(sql)
    errors, warnings = [], []
    if len([x for x in s.split(";") if x.strip()]) > 1:
        errors.append("Multiple statements - run one statement per call")
    first = (re.match(r"\s*\(?\s*(\w+)", s) or [None, ""])[1].upper()
    is_read = first in READ_KEYWORDS
    writes = [k for k, p in WRITE_PATTERNS.items() if re.search(r"\b(?:" + p + ")", s, re.I | re.M)]
    if not is_read or writes:
        errors.append(f"Not a read-only statement (starts with '{first or '?'}'"
                      + (f", contains {', '.join(writes)}" if writes else "") + ")")
    if re.search(r"\$__\w+|\$\{\w+", s):
        errors.append("Grafana macros/variables ($__timeFilter, ${device}, ...) only work inside Grafana - replace "
                      "them with literal values (date_created BETWEEN '<yyyy/MM/dd>' AND ..., t >= TIMESTAMP '...')")
    tables = sorted({t.lower() for t in re.findall(r"\b(?:FROM|JOIN)\s+[\"`]?([A-Za-z_][\w]*)", s, re.I)})
    data_tables = [t for t in tables if t.startswith("tbl_") and not t.endswith("_messages")
                   and not t.startswith("tbl_aggregations_")]   # small (meta, lookup, trip tables)
    if backend == "athena":
        if first in ("SELECT", "WITH") and data_tables and not re.search(r"\bdate_created\b", s, re.I):
            warnings.append("No date_created filter: Athena scans ALL files of " + ", ".join(data_tables)
                            + " (slow + costly). Add e.g. WHERE date_created BETWEEN '2026/06/01' AND '2026/06/30'")
    if backend in ("athena", "bigquery", "synapse") and re.search(r"SELECT\s+\*", s, re.I) and data_tables:
        warnings.append("SELECT * reads every signal column - select only the columns you need (billing is per "
                        "column read)")
    if first in ("SELECT", "WITH") and data_tables and returns_raw_rows(s):
        warnings.append("No aggregation/LIMIT: The result may be millions of raw rows - resample on the fly "
                        "(GROUP BY a time bucket, see resample) and return only what answers the question")
    return {"statement": first, "read_only": is_read and not writes, "tables": tables, "errors": errors,
            "warnings": warnings}


# ---------------------------------------------------------------- shared query logic
class Lake:
    """Shared logic. A backend implements tables(), sql() and describe_source() and sets the attributes below."""
    kind = "?"
    needs_window = False   # True: generated stats/summary queries need start + end (partition filter, cost)
    workers = 1            # concurrent queries in fleet()
    meta_where = ""        # filter for the devicemeta query (Athena: its single partition)
    offline_schema = False
    max_request_usd: float | None = None   # cloud: storage request guard (None = local, free)
    request_usd_est = 0.0  # estimated storage request cost so far (guarded fleet()/summary() calls)
    request_files_est = 0

    def tables(self) -> list[dict]:
        raise NotImplementedError

    def sql(self, query: str, **kw) -> pd.DataFrame:
        raise NotImplementedError

    def describe_source(self) -> str:
        raise NotImplementedError

    def list_files(self, table: str, after: str = "") -> tuple[list[str], bool]:
        """Cloud: Up to 1000 Parquet file keys of a table (after the key suffix 'yyyy/mm/dd'), truncated flag."""
        raise NotImplementedError

    # -------------------------------------------------------- storage request guard (cloud)
    def request_estimate(self, tables, start=None, end=None) -> dict | None:
        """Very rough storage request cost of reading these tables in the window: ~GETS_PER_FILE GETs per Parquet
        file + one LIST per table and day with data. Files per table are counted by listing one message folder of up
        to SAMPLE_DEVICES devices (the devices with the most tables); other devices get the mean sampled count.
        No start = all data. None on the local backend (no request costs)."""
        if self.max_request_usd is None:
            return None
        by_dev: dict[str, list[str]] = {}
        for n in table_names(tables):
            c = classify(n)
            if c["kind"] == "message":
                by_dev.setdefault(c["device"], []).append(n)
        t0, t1 = parse_utc(start), parse_utc(end)
        d0, d1 = (t0.date() if t0 else None), (t1 or dt.datetime.now(dt.timezone.utc)).date()
        cache = self.__dict__.setdefault("_file_counts", {})
        sampled = {}   # device -> (files per table, days with data, first date, listing ok)
        for dev in sorted(by_dev, key=lambda d: -len(by_dev[d]))[:SAMPLE_DEVICES]:
            key = (by_dev[dev][0], d0, d1)
            if key not in cache:
                cache[key] = self._count_files(by_dev[dev][0], d0, d1)
            sampled[dev] = cache[key]
        n_s = max(len(sampled), 1)   # unsampled devices: The mean of the sampled ones
        top = (sum(x[0] for x in sampled.values()) / n_s, sum(x[1] for x in sampled.values()) / n_s, None, True)
        files = round(sum(len(ts) * sampled.get(d, top)[0] for d, ts in by_dev.items()))
        table_days = round(sum(len(ts) * sampled.get(d, top)[1] for d, ts in by_dev.items()))
        usd = files * GETS_PER_FILE * GET_USD + table_days * LIST_USD
        first = min((x[2] for x in sampled.values() if x[2]), default=None)
        window = f"{d0 or first or '?'} -> {d1}"
        n_tables = sum(len(ts) for ts in by_dev.values())
        basis = (f"files listed for {len(sampled)} of {len(by_dev)} devices" if all(x[3] for x in sampled.values())
                 else f"listing failed, {FALLBACK_FILES_PER_DAY} files/table/day assumed")
        return {"files": files, "usd": round(usd, 4), "tables": n_tables, "devices": len(by_dev),
                "window": window, "basis": basis,
                "text": f"~{usd_fmt(usd)} for ~{files:,} Parquet files ({n_tables} tables on {len(by_dev)} devices, "
                        f"{window}; {basis}; very rough)"}

    def _count_files(self, table: str, d0: dt.date | None, d1: dt.date) -> tuple[float, float, dt.date | None, bool]:
        """Files of one table between d0 and d1 (inclusive): Exact if the listing covers the window, else files per
        listed day x every day of the window (conservative). Returns (files, days with data, first date, ok)."""
        try:
            keys, truncated = self.list_files(table, d0.strftime("%Y/%m/%d") if d0 else "")
        except Exception:  # noqa: BLE001 - no list permission, network: fall back to an assumption
            days = (d1 - d0).days + 1 if d0 else FALLBACK_DAYS
            return FALLBACK_FILES_PER_DAY * days, days, None, False
        dates = [dt.date(int(m[1]), int(m[2]), int(m[3])) for m in map(DATE_KEY_RE.search, keys) if m]
        inside = [d for d in dates if (not d0 or d >= d0) and d <= d1]
        first = min(inside, default=None)
        if not truncated or (dates and max(dates) > d1):
            return len(inside), len(set(inside)), first, True
        days = (d1 - (d0 or first or d1)).days + 1
        return len(inside) / max(len(set(inside)), 1) * days, days, first, True

    def request_guard(self, tables, start=None, end=None) -> dict | None:
        """Add the request estimate to the running total; stop above max_request_usd (ask the user first)."""
        est = self.request_estimate(tables, start, end)
        if est is None:
            return None
        if self.request_usd_est + est["usd"] > self.max_request_usd:
            raise LakeError(f"Storage request guard: Estimated S3/GCS request cost {est['text']}"
                            + (f", {usd_fmt(self.request_usd_est)} so far" if self.request_usd_est else "")
                            + f" - above max_request_usd ${self.max_request_usd:g}. Tell the user the estimate, ask, "
                            "then re-run with a higher limit (--max-request-usd N / connect(..., max_request_usd=N)) "
                            "or narrow the devices/period")
        self.request_usd_est += est["usd"]
        self.request_files_est += est["files"]
        return est

    def request_text(self, storage: str) -> str:
        """For budget(): The guarded request estimate so far, or that request costs are not included."""
        if self.request_files_est:
            return (f" + est. {storage} requests ~{usd_fmt(self.request_usd_est)} for ~{self.request_files_est:,} files "
                    "(very rough)")
        return f", {storage} request costs not included"

    # -------------------------------------------------------- discovery
    def table(self, name: str) -> dict:
        name = name.lower()
        name = name if name.startswith("tbl_") else "tbl_" + name
        tabs = {t["name"].lower(): t for t in self.tables()}
        if name in tabs:
            return tabs[name]
        close = difflib.get_close_matches(name, list(tabs), n=5, cutoff=0.5)
        raise LakeError(f"Table {name} not found ({self.describe_source()}). Close: {', '.join(close) or '-'}")

    def _cached_names(self) -> dict[str, str] | None:
        """Cloud: Device names stored in the schema cache file (None = not cached; a schema refresh drops them)."""
        p = getattr(self, "_schema_cache", None)
        try:
            return json.loads(p.read_text(encoding="utf-8")).get("device_names") if p else None
        except (OSError, ValueError):
            return None

    def _store_names(self, names: dict[str, str]) -> None:
        p = getattr(self, "_schema_cache", None)
        try:
            if p:
                c = json.loads(p.read_text(encoding="utf-8"))
                c["device_names"] = names
                p.write_text(json.dumps(c), encoding="utf-8")
        except (OSError, ValueError):
            pass

    def device_ids(self) -> list[str]:
        return sorted({t["device"] for t in self.tables() if t["kind"] == "message"})

    def devices(self) -> dict[str, str | None]:
        """Device ID -> meta name from tbl_aggregations_devicemeta (as the Grafana device dropdown, e.g. 'Truck 1
        (12345678)'), None where the lake has none. One tiny query, then cached with the schema (cloud)."""
        if getattr(self, "_names", None) is None:
            ids = self.device_ids()   # loads the schema first (and with it the cache file of the names)
            found = self._cached_names()
            if found is None:
                found = {}
                if any(t["name"] == "tbl_aggregations_devicemeta" for t in self.tables()) and not self.offline_schema:
                    df = self.sql("SELECT deviceid, metaname FROM tbl_aggregations_devicemeta" + self.meta_where)
                    for d, m in zip(df["deviceid"].astype(str).str.upper(), df["metaname"]):
                        if m and str(m).strip().upper() != d:
                            found[d] = str(m).strip()
                    self._store_names(found)
            self._names = {d: found.get(d) for d in ids}
        return self._names

    def device_id(self, device: str) -> str:
        """A device ID or meta name -> the device ID. Device IDs are unique, meta names are NOT: A name resolves only
        if exactly one device has it (exact, case-insensitive, with or without the ' (<ID>)' suffix); a name shared by
        several devices or a partial name stops with the candidates (ask the user, then use the ID). Names are only
        looked up when the input is not a known ID; a resolved name is printed (stderr) so it can be confirmed."""
        ids = self.device_ids()
        if str(device).strip().upper() in ids:
            return str(device).strip().upper()
        return match_device(device, ids, self.devices(), f"with message tables ({self.describe_source()})")

    def resolve(self, device: str, message: str) -> dict:
        """Message table of a device: Exact message (any case, with or without tbl_<device>_) or a unique substring."""
        dev = self.device_id(device)
        msgs = [t for t in self.tables() if t["kind"] == "message" and t["device"] == dev]
        m = message.lower()
        m = m[len(f"tbl_{dev.lower()}_"):] if m.startswith(f"tbl_{dev.lower()}_") else m
        exact = [t for t in msgs if t["message"] == m]
        if exact:
            return exact[0]
        sub = [t for t in msgs if m in t["message"]]
        if len(sub) == 1:
            return sub[0]
        if sub:
            raise LakeError(f"'{message}' is ambiguous for {dev}: {', '.join(t['message'] for t in sub[:15])}")
        close = difflib.get_close_matches(m, [t["message"] for t in msgs], n=5, cutoff=0.4)
        raise LakeError(f"No message '{message}' for {dev}. Close: {', '.join(close) or '-'} (see lake_schema.py --tables)")

    def find(self, pattern: str, regex: bool = False, device: str | None = None) -> list[dict]:
        """Signal columns matching a pattern (case-insensitive substring, or regex): One hit per table + signal."""
        pat = re.compile(pattern if regex else re.escape(pattern), re.I)
        dev = self.device_id(device) if device else None
        out = []
        for t in self.tables():
            if t["kind"] not in ("message", "aggregation") or (dev and t["device"] != dev):
                continue
            for n, ty in signal_cols(t):
                if pat.search(n):
                    out.append({"table": t["name"], "device": t["device"], "message": t["message"], "signal": n,
                                "type": ty})
        return out

    # -------------------------------------------------------- generated queries
    def stats_sql(self, table: str, start=None, end=None) -> tuple[str, list[str]]:
        t = self.table(table)
        cols = [c[0] for c in signal_cols(t)]
        parts = ["count(*) AS n_rows", "min(t) AS first_t", "max(t) AS last_t"]
        for i, c in enumerate(cols):
            q = val(c)
            parts += [f"count({q}) AS c{i}_n", f"min({q}) AS c{i}_min", f"max({q}) AS c{i}_max",
                      f"avg({q}) AS c{i}_avg", f"approx_distinct({q}) AS c{i}_dist"]
        return f"SELECT {', '.join(parts)} FROM {t['name']}{self._where(start, end)}", cols

    def stats(self, table: str, start=None, end=None) -> dict:
        """Per signal: Non-null count, null share, min, max, mean, approx distinct values (one query)."""
        if self.needs_window and not (start and end):
            raise LakeError(f"{self.kind}: --stats needs --start and --end (the partition filter keeps the scan small)")
        q, cols = self.stats_sql(table, start, end)
        r = self.sql(q, start=start, end=end).iloc[0]
        total = int(r["n_rows"] or 0)
        sigs = []
        for i, c in enumerate(cols):
            n = int(r[f"c{i}_n"] or 0)
            sigs.append({"signal": c, "count": n, "null_share": round(1 - n / total, 4) if total else None,
                         "min": _num(r[f"c{i}_min"]), "max": _num(r[f"c{i}_max"]), "mean": _num(r[f"c{i}_avg"]),
                         "distinct_approx": _num(r[f"c{i}_dist"])})
        return {"table": self.table(table)["name"], "rows": total, "first_utc": _iso(r["first_t"]),
                "last_utc": _iso(r["last_t"]), "signals": sigs, "sql": q}

    @staticmethod
    def _where(start=None, end=None, extra: str | None = None) -> str:
        """date_created partition filter (whole days, prunes files) + exact t window, both in UTC."""
        t0, t1 = parse_utc(start), parse_utc(end)
        conds = []
        if t0 or t1:
            d0 = t0.strftime("%Y/%m/%d") if t0 else "0000/00/00"
            d1 = t1.strftime("%Y/%m/%d") if t1 else "9999/99/99"
            conds.append(f"date_created BETWEEN '{d0}' AND '{d1}'")
        if t0:
            conds.append(f"t >= {ts_literal(t0)}")
        if t1:
            conds.append(f"t < {ts_literal(t1)}")
        if extra:
            conds.append(f"({extra})")
        return (" WHERE " + " AND ".join(conds)) if conds else ""

    def resample_sql(self, device: str, message: str, signals: list[str] | str | None, start, end,
                     agg: list[str] | str = "avg", interval: str = "auto", points: int = 1000,
                     where: str | None = None) -> tuple[str, dict]:
        """The Grafana panel query with literals: $__timeGroup(t, $__interval) = from_unixtime(floor(to_unixtime(t)
        / N) * N), $__rawTimeFrom/To = date_created BETWEEN, $__timeFilter(t) = t window. Returns (sql, info)."""
        t = self.resolve(device, message)
        t0, t1 = parse_utc(start), parse_utc(end)
        if not (t0 and t1) or t1 <= t0:
            raise LakeError("resample needs --start and --end (end after start, UTC or with an offset)")
        avail = [c[0] for c in signal_cols(t)]
        if isinstance(signals, str):
            signals = [s.strip() for s in signals.split(",") if s.strip()]
        signals = [s.lower() for s in signals] if signals else avail
        bad = [s for s in signals if s not in avail]
        if bad:
            raise LakeError(f"Unknown signal(s) {bad} in {t['name']}. Available: {', '.join(avail)}")
        aggs = [a.strip().lower() for a in (agg.split(",") if isinstance(agg, str) else agg)]
        bad = [a for a in aggs if a not in AGGS]
        if bad:
            raise LakeError(f"Unknown aggregation(s) {bad}. Use: {', '.join(AGGS)}")
        window = (t1 - t0).total_seconds()
        n = nice_interval(window / max(points, 1)) if interval == "auto" else interval_s(interval)
        cols = [f'{AGGS[a].format(c=chr(34) + s + chr(34))} AS "{a}_{s}"' for s in signals for a in aggs]
        if n >= 1:
            bucket = f"from_unixtime(floor(to_unixtime(t) / {n:g}) * {n:g})"
        else:   # sub-second: integer milliseconds (t / 0.1 in floating point puts edge rows in the wrong bucket)
            ms = round(n * 1000)
            bucket = f"from_unixtime(floor(to_unixtime(t) * 1000 / {ms}) * {ms} / 1000)"
        q = (f"SELECT {bucket} AS time, " + ", ".join(cols) + f" FROM {t['name']}"
             + self._where(t0, t1, where) + " GROUP BY 1 ORDER BY 1")
        return q, {"table": t["name"], "interval": fmt_interval(n), "interval_s": n,
                   "expected_rows_max": int(window // n) + 1}

    def resample(self, device: str, message: str, signals=None, start=None, end=None, agg="avg",
                 interval: str = "auto", points: int = 1000, where: str | None = None) -> pd.DataFrame:
        """Signals of one message resampled on the fly (one row per time bucket, empty buckets dropped)."""
        q, info = self.resample_sql(device, message, signals, start, end, agg, interval, points, where)
        df = self.sql(q, start=start, end=end)
        df.attrs.update(sql=q, **info)
        return df

    def fleet(self, template, tables, start=None, end=None, batch: int = FLEET_BATCH,
              workers: int | None = None, guard: bool = True) -> pd.DataFrame:
        """Run one SQL template per table ({table}, {device}, {message}) as UNION ALL batches; adds a device
        column. template = a str, or a function table name -> SQL (all must return the same columns).
        tables = table names, find() hits or tables() entries. Batches run self.workers at a time.
        start/end = the template's time window: Used by the storage request guard (cloud; without them it
        assumes all data) and on the local backend to limit the files read."""
        names = table_names(tables)
        if not names:
            raise LakeError("fleet(): No tables given (did find() return nothing?)")
        est = self.request_guard(names, start, end) if guard else None
        parts = []
        for n in names:
            c = classify(n)
            body = template(n) if callable(template) else \
                template.format(table=n, device=c["device"] or "", message=c["message"] or "")
            parts.append(f"SELECT '{c['device'] or n}' AS device, q.* FROM ({body}) q")
        queries, cur = [], []
        for p in parts:
            if cur and (len(cur) >= batch or sum(len(x) for x in cur) + len(p) > MAX_QUERY_CHARS):
                queries.append(cur)
                cur = []
            cur.append(p)
        if cur:
            queries.append(cur)
        n_sql = [0]

        def run(q: list[str]) -> pd.DataFrame:
            """One UNION ALL batch; storage throttling (too many files at once): Split the batch in half, retry."""
            n_sql[0] += 1
            try:
                return self.sql("\nUNION ALL\n".join(q), start=start, end=end)
            except LakeError as e:
                if not re.search(r"THROTTL|reduce your request rate|SlowDown", str(e), re.I):
                    raise
                if len(q) == 1:
                    raise LakeError(f"{e}\nStorage throttling on one table: Split the period (e.g. one query per "
                                    "month)") from None
                print(f"Storage throttling: Retrying {len(q)} tables as 2 smaller queries", file=sys.stderr)
                time.sleep(2)
                return pd.concat([run(q[:len(q) // 2]), run(q[len(q) // 2:])], ignore_index=True)

        workers = min(workers or self.workers, len(queries))
        if workers > 1:
            with ThreadPoolExecutor(max_workers=workers) as ex:
                dfs = list(ex.map(run, queries))
        else:
            dfs = [run(q) for q in queries]
        df = pd.concat([d for d in dfs if not d.empty], ignore_index=True) if any(not d.empty for d in dfs) \
            else pd.DataFrame(columns=["device"])
        df.attrs.update(queries=n_sql[0], tables=len(names), request_estimate=est)
        return df

    def summary(self, hits, start, end, by: str = "day", keep_empty: bool = False) -> pd.DataFrame:
        """Mergeable statistics of signals across devices, each table scanned once. Long format: device, message,
        signal, period, n_rows, first_t, last_t, n, sum, min, max, avg. Save it, then answer follow-up questions by
        re-aggregating in pandas (avg = sum / n, max of max, sum of n) instead of querying again.
        hits = find() results (message tables only); by = 'day' (date_created) | 'hour' | 'total' (whole window).
        keep_empty: Keep signals without a value (n = 0, e.g. always outside the DBC range = NULL)."""
        if not (start and end):
            raise LakeError("summary needs a start and an end (UTC)")
        period = {"day": "date_created", "hour": "date_trunc('hour', t)", "total": "'total'"}.get(by)
        if period is None:
            raise LakeError(f"Unknown by='{by}' (use day, hour or total)")
        sigs: dict[str, list[str]] = {}
        for h in hits:
            if h.get("device") and h["signal"] not in sigs.setdefault(h["table"], []):
                sigs[h["table"]].append(h["signal"])
        sigs = {t: s for t, s in sigs.items() if s}
        if not sigs:
            raise LakeError("summary(): No signals of message tables in the hits (did find() match?)")
        est = self.request_guard(list(sigs), start, end)   # once for all tables, before any query
        where = self._where(start, end)

        def tpl(name: str, width: int) -> str:   # one table, its signals padded with NULL columns to the batch width
            ss = sigs[name]
            cols = [f"count({val(s)}) AS n{i}, CAST(sum({val(s)}) AS DOUBLE) AS s{i}, CAST(min({val(s)}) AS DOUBLE) AS "
                    f"lo{i}, CAST(max({val(s)}) AS DOUBLE) AS hi{i}" for i, s in enumerate(ss)]
            cols += [f"CAST(NULL AS BIGINT) AS n{i}, CAST(NULL AS DOUBLE) AS s{i}, CAST(NULL AS DOUBLE) AS lo{i}, "
                     f"CAST(NULL AS DOUBLE) AS hi{i}" for i in range(len(ss), width)]
            return (f"SELECT '{classify(name)['message']}' AS message, {period} AS period, count(*) AS n_rows, "
                    f"min(t) AS first_t, max(t) AS last_t, {', '.join(cols)} FROM {name}{where}"
                    + ("" if by == "total" else " GROUP BY 2"))

        # Tables sorted by signal count, so each UNION ALL batch pads to a similar width
        order = sorted(sigs, key=lambda n: len(sigs[n]))
        base = ["device", "message", "period", "n_rows", "first_t", "last_t"]
        frames, queries = [], 0
        for k in range(0, len(order), FLEET_BATCH):
            chunk = order[k:k + FLEET_BATCH]
            width = max(len(sigs[n]) for n in chunk)
            df = self.fleet(lambda n, w=width: tpl(n, w), chunk, start, end, guard=False)
            queries += df.attrs.get("queries", 0)
            if df.empty:
                continue
            for (dev, msg), g in df.groupby(["device", "message"], sort=False):
                for i, s in enumerate(sigs[table_name(dev, msg)]):
                    part = g[base].copy()
                    part.insert(2, "signal", s)
                    part["n"], part["sum"], part["min"], part["max"] = g[f"n{i}"], g[f"s{i}"], g[f"lo{i}"], g[f"hi{i}"]
                    frames.append(part)
        cols = ["device", "message", "signal", "period", "n_rows", "first_t", "last_t", "n", "sum", "min", "max"]
        out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)
        out["n"] = out["n"].fillna(0).astype("int64")
        if not keep_empty:
            out = out[out["n"] > 0].copy()   # drops devices/tables where the signal is all NULL
        out["avg"] = out["sum"] / out["n"].where(out["n"] > 0)
        out = out.sort_values(["signal", "device", "message", "period"]).reset_index(drop=True)
        out.attrs.update(by=by, start=_iso(parse_utc(start)), end=_iso(parse_utc(end)), queries=queries,
                         tables=len(sigs), request_estimate=est)
        return out

    # -------------------------------------------------------- episodes + time in bins (window functions per day)
    @staticmethod
    def _one_signal(hits) -> dict[str, str]:
        """Table -> signal from find() hits of message tables (one signal per table: the first hit)."""
        sigs: dict[str, str] = {}
        for h in hits:
            if h.get("device"):
                sigs.setdefault(h["table"], h["signal"])
        if not sigs:
            raise LakeError("No message table has the signal (did find() match?)")
        return sigs

    def episodes(self, hits, start, end, above=None, below=None, state=None, min_duration: float = 0,
                 gap: str = "10s") -> pd.DataFrame:
        """Periods where a signal is above / below a limit or equals a state, per device + table: start_t, end_t,
        duration_s (first to last matching row), n rows, max, min. Two stages: A cheap per-day count screen, then the
        run query (lag, PARTITION BY day) only on the days with a match. A data gap > gap ends an episode; episodes
        that cross midnight are merged again. hits = find() hits of one signal (e.g. every device that has it)."""
        if not (start and end):
            raise LakeError("episodes needs a start and an end (UTC)")
        given = [(op, v) for op, v in ((">", above), ("<", below), ("=", state)) if v is not None]
        if len(given) != 1:
            raise LakeError("episodes: Give exactly one of above, below, state")
        op, x = given[0][0], float(given[0][1])
        sigs = self._one_signal(hits)
        gap_ms = round(interval_s(gap) * 1000)
        pred = lambda s: f"{val(s)} {op} {x:g}"   # noqa: E731
        est = self.request_guard(list(sigs), start, end)
        screen = self.fleet(lambda n: (f"SELECT '{classify(n)['message']}' AS message, date_created AS day, "
                                       f"sum(CASE WHEN {pred(sigs[n])} THEN 1 ELSE 0 END) AS n_on FROM {n}"
                                       f"{self._where(start, end)} GROUP BY 2"), list(sigs), start, end, guard=False)
        flagged: dict[str, list[str]] = {}
        for r in screen.itertuples(index=False):
            if (r.n_on or 0) > 0:
                flagged.setdefault(table_name(r.device, r.message), []).append(str(r.day))
        queries = screen.attrs.get("queries", 0)
        cols = ["device", "message", "signal", "start_t", "end_t", "duration_s", "n", "max", "min"]
        frames = []
        if flagged:
            def run_tpl(n: str) -> str:
                s, days = sigs[n], ", ".join(f"'{d}'" for d in sorted(flagged[n]))
                w = "OVER (PARTITION BY date_created ORDER BY t)"
                flag = f"CASE WHEN {pred(s)} THEN 1 ELSE 0 END"   # 1/0, not boolean: Also valid T-SQL (Synapse)
                return (f"WITH s AS (SELECT t, {val(s)} AS v, date_created AS d, {flag} AS on_, lag(t) {w} AS prev_t, "
                        f"lag({flag}) {w} AS prev_on FROM {n}"
                        + self._where(start, end, f"date_created IN ({days}) AND {val(s)} IS NOT NULL")
                        + "), g AS (SELECT t, v, d, on_, sum(CASE WHEN prev_on IS NULL OR on_ <> prev_on OR "
                        f"date_diff('millisecond', prev_t, t) > {gap_ms} THEN 1 ELSE 0 END) "
                        "OVER (PARTITION BY d ORDER BY t) AS run FROM s) "
                        f"SELECT '{classify(n)['message']}' AS message, min(t) AS start_t, max(t) AS end_t, "
                        "count(*) AS n, max(v) AS max_v, min(v) AS min_v FROM g WHERE on_ = 1 GROUP BY d, run")
            df = self.fleet(run_tpl, list(flagged), start, end, guard=False)
            queries += df.attrs.get("queries", 0)
            if not df.empty:
                df["signal"] = [sigs[table_name(d, m)] for d, m in zip(df["device"], df["message"])]
                frames.append(df.rename(columns={"max_v": "max", "min_v": "min"}))
        out = pd.concat(frames, ignore_index=True) if frames else pd.DataFrame(columns=cols)
        out = _merge_midnight(utc_frame(out), gap_ms / 1000) if len(out) else out
        out["duration_s"] = (pd.to_datetime(out["end_t"]) - pd.to_datetime(out["start_t"])).dt.total_seconds() \
            if len(out) else pd.Series(dtype=float)
        out = out[out["duration_s"] >= min_duration] if len(out) else out
        out = out[cols].sort_values(["start_t", "device"]).reset_index(drop=True) if len(out) else out[cols]
        out.attrs.update(start=_iso(parse_utc(start)), end=_iso(parse_utc(end)), condition=f"{op} {x:g}", gap=gap,
                         queries=queries, tables=len(sigs), flagged_days=sum(len(v) for v in flagged.values()),
                         request_estimate=est)
        return out

    def hist(self, hits, start, end, bins: list[float] | None = None, states: bool = False, by: str = "day",
             gap: str = "10s") -> pd.DataFrame:
        """Time spent per value bin (bins = edges) or per state value (states=True), time-weighted: Each row holds
        its value until the next row (lead, PARTITION BY day); intervals longer than gap count as no data (logger
        off). Mergeable: Sum seconds over periods/devices for duty cycles, load spectra, exposure hours. Long format:
        device, message, signal, period, bin, bin_lo, bin_hi, label, seconds, n. Raise gap for on-change logging."""
        if not (start and end):
            raise LakeError("hist needs a start and an end (UTC)")
        if bool(bins) == bool(states):
            raise LakeError("hist: Give bins (edges) or states=True")
        period = {"day": "d", "hour": "date_trunc('hour', t)", "total": "'total'"}.get(by)
        if period is None:
            raise LakeError(f"Unknown by='{by}' (use day, hour or total)")
        edges = sorted(float(b) for b in bins) if bins else []
        bin_expr = ("CASE " + " ".join(f"WHEN v < {b:g} THEN {i}" for i, b in enumerate(edges))
                    + f" ELSE {len(edges)} END") if edges else "CAST(v AS DOUBLE)"
        sigs = self._one_signal(hits)
        gap_ms = round(interval_s(gap) * 1000)
        est = self.request_guard(list(sigs), start, end)

        def tpl(n: str) -> str:
            s = sigs[n]
            return (f"WITH s AS (SELECT t, {val(s)} AS v, date_created AS d, "
                    f"lead(t) OVER (PARTITION BY date_created ORDER BY t) AS next_t FROM {n}"
                    + self._where(start, end, f"{val(s)} IS NOT NULL")
                    + f") SELECT '{classify(n)['message']}' AS message, {period} AS period, {bin_expr} AS bin, "
                    "sum(date_diff('millisecond', t, next_t)) / 1000.0 AS seconds, count(*) AS n FROM s "
                    f"WHERE next_t IS NOT NULL AND date_diff('millisecond', t, next_t) <= {gap_ms} "
                    + ("GROUP BY 3" if by == "total" else "GROUP BY 2, 3"))
        df = self.fleet(tpl, list(sigs), start, end, guard=False)
        cols = ["device", "message", "signal", "period", "bin", "bin_lo", "bin_hi", "label", "seconds", "n"]
        if df.empty:
            out = pd.DataFrame(columns=cols)
        else:
            df["signal"] = [sigs[table_name(d, m)] for d, m in zip(df["device"], df["message"])]
            if edges:
                b = df["bin"].astype(int)
                lo = [float("-inf")] + edges
                hi = edges + [float("inf")]
                df["bin_lo"], df["bin_hi"] = [lo[i] for i in b], [hi[i] for i in b]
                df["label"] = [f"< {edges[0]:g}" if i == 0 else f">= {edges[-1]:g}" if i == len(edges)
                               else f"{edges[i - 1]:g} - {edges[i]:g}" for i in b]
            else:
                df["bin_lo"] = df["bin_hi"] = df["bin"].astype(float)
                df["label"] = [f"{v:g}" for v in df["bin"].astype(float)]
            df["seconds"] = df["seconds"].astype(float)
            out = df[cols].sort_values(["device", "message", "period", "bin"]).reset_index(drop=True)
        out.attrs.update(by=by, start=_iso(parse_utc(start)), end=_iso(parse_utc(end)), gap=gap,
                         bins=edges or "states", queries=df.attrs.get("queries", 0), tables=len(sigs),
                         request_estimate=est)
        return out


def _merge_midnight(df: pd.DataFrame, gap_s: float) -> pd.DataFrame:
    """Episodes are found per day (PARTITION BY day): Join an episode that ends just before midnight with the next
    one of the same table that starts within gap_s after it."""
    df = df.sort_values(["device", "message", "start_t"]).reset_index(drop=True)
    rows = []
    for r in df.to_dict("records"):
        p = rows[-1] if rows else None
        if (p and p["device"] == r["device"] and p["message"] == r["message"]
                and p["end_t"].date() != r["start_t"].date()
                and 0 <= (r["start_t"] - p["end_t"]).total_seconds() <= gap_s):
            p["end_t"], p["n"] = r["end_t"], p["n"] + r["n"]
            p["max"], p["min"] = max(p["max"], r["max"]), min(p["min"], r["min"])
        else:
            rows.append(r)
    return pd.DataFrame(rows)


def _num(x):
    if x is None or (isinstance(x, float) and pd.isna(x)):
        return None
    try:
        return float(x)
    except (TypeError, ValueError):
        return x


def _iso(x) -> str | None:
    if x is None or x is pd.NaT or (isinstance(x, float) and pd.isna(x)):
        return None
    ts = pd.Timestamp(x)
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return ts.isoformat().replace("+00:00", "Z")


def utc_frame(df: pd.DataFrame) -> pd.DataFrame:
    """Make naive datetime columns tz-aware UTC (all lake times are UTC)."""
    for c in df.columns:
        if pd.api.types.is_datetime64_any_dtype(df[c]) and getattr(df[c].dt, "tz", None) is None:
            df[c] = df[c].dt.tz_localize("UTC")
    return df


# ---------------------------------------------------------------- entry point
def connect(lake: str | Path | None = None, project: str | Path | None = None, athena: bool = False,
            bigquery: bool = False, env: str | None = None, synapse: bool = False, **kw) -> Lake:
    """Exactly one of: lake=<folder> | project=<projects/name> (local DuckDB) | athena=True | bigquery=True |
    synapse=True (+ env=<name> for the data lake profile credentials/<name>.env)."""
    if sum(bool(x) for x in (lake, project, athena, bigquery, synapse)) != 1:
        raise LakeError("Give exactly one data source: --project <p> / --lake <folder> (local), --athena (AWS), "
                        "--bigquery (Google) or --synapse (Azure)")
    # backends are imported on use: boto3 only for Athena, google-cloud-bigquery only for BigQuery, DuckDB only local
    if athena:
        from _athena import AthenaLake
        return AthenaLake(env=env, **kw)
    if bigquery:
        from _bigquery import BigQueryLake
        return BigQueryLake(env=env, **kw)
    if synapse:
        from _synapse import SynapseLake
        return SynapseLake(env=env, **kw)
    from _local import LocalLake
    return LocalLake(Path(project) / "parquet" if project else Path(lake))


def add_source_args(ap, sub: bool = False) -> None:
    """CLI flags shared by lake_schema.py and lake_query.py. sub=True: The copy on a subcommand (no defaults, so the
    flags work before and after the subcommand)."""
    g = ap.add_argument_group("data source (one of)")
    d = {"default": argparse.SUPPRESS} if sub else {}
    g.add_argument("--project", type=Path, help="Local: projects/<name> (uses <project>/parquet)", **d)
    g.add_argument("--lake", type=Path, help="Local: any folder with the data lake layout", **d)
    g.add_argument("--athena", action="store_true", help="AWS data lake via Amazon Athena (data lake profile)", **d)
    g.add_argument("--bigquery", action="store_true", help="Google data lake via BigQuery (key file in credentials/)", **d)
    g.add_argument("--synapse", action="store_true", help="Azure data lake via Synapse (azure data lake profile)", **d)
    g.add_argument("--env", metavar="NAME", help="Athena/BigQuery/Synapse: Data lake profile credentials/NAME.env "
                                                 "(default: the only one)", **d)


def connect_args(a, **kw) -> Lake:
    synapse = getattr(a, "synapse", False)
    if a.env and not (a.athena or a.bigquery or synapse):
        raise LakeError("--env only applies to --athena / --bigquery / --synapse")
    return connect(lake=a.lake, project=a.project, athena=a.athena, bigquery=a.bigquery, synapse=synapse, env=a.env,
                   **kw)
