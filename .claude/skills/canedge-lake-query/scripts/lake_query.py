"""Read-only SQL on a CANedge Parquet data lake: Local folder (DuckDB), Amazon Athena, Google BigQuery or Azure
Synapse.

The same SQL (Athena/Trino dialect) runs on all of them (BigQuery, Synapse: translated). Only aggregated results
should come back: Scan GBs, return KBs.

Usage (data source: --project projects/<name> | --lake <folder> | --athena | --bigquery | --synapse [--env NAME]):
  lake_query.py --athena sql "SELECT ..." | --file q.sql [--max-rows 1000] [--show 20] [--out r.csv|.json|.parquet]
                [--dry-run] [--timeout 600] [--json]
  lake_query.py --project p resample --device 12345678 --message can1_eec1_s00 --signals enginespeed
                --start 2026-01-15T08:00 --end 2026-01-15T12:00 [--agg avg,max] [--interval auto|1s|1min]
                [--points 1000] [--where "enginespeed > 0"] [--dry-run] [--out ..]
  lake_query.py --athena summary --signals enginespeed,wheelbasedvehiclespeed --start 2026-06-01 --end 2026-07-01
                [--by day|hour|total] [--device X] [--out cube.parquet] [--dry-run]
                (or --match "<regex>", or --all --device X: every signal of a device)
  lake_query.py --athena episodes --signal enginespeed --above 2000 --start .. --end .. [--min-duration 5]
                [--gap 10s] [--device X] [--out ep.parquet]          (or --below Y / --state V)
  lake_query.py --athena hist --signal enginespeed --bins 0,800,1500,2000 --start .. --end .. [--by day]
                [--gap 10s] [--out h.parquet]                        (or --states [--dbc p]: value table labels)
  --device takes a device ID or its meta name ('Truck 1', from tbl_aggregations_devicemeta), on every command.

episodes = periods where a signal is above/below a limit or equals a state, per device + table (start, end,
duration, rows, max, min). A per-day screen first, then the window query only on days with a match.
hist = time spent per value bin or state (each row holds until the next; intervals > --gap = no data), mergeable
seconds per device + period: Duty cycles, load spectra, exposure hours.

resample = the Grafana panel query with literal values: Buckets of --interval (auto = window / --points, snapped to
100 ms, 1 s, 10 s, 1 min, ...) with one aggregate per signal, partition filter date_created + exact t window. The
generated SQL is printed, so it can be reused in analysis scripts and Grafana panels.
summary = fleet statistics of exact signal names on every device/message table that has them (each table scanned
once, UNION ALL batches, also across tables with different signals): n, sum, min, max, avg, first/last time per
device + message + signal + period. Save it with --out and answer follow-up questions from the file (avg = sum / n)
instead of querying again. avg is per row (sample-weighted): Biased when the message rate varies (on-change logging,
prescalers, gaps).
Storage request guard (Athena/BigQuery, summary): Before the scan, the S3/GCS request cost of the files to read is
estimated from a few folder listings (very rough); above --max-request-usd (default $1) it stops: Ask the user.
summary --dry-run shows the estimate without running a query. The scan budget (--max-scan-gb, default 10) stops a
run that has scanned/billed more.
Local only: sql --start/--end limit the Parquet files read per table by their yyyy/mm/dd folders.
Athena/BigQuery: Prints the MB scanned/billed, a rough scan-cost estimate (storage request costs not included) and
the execution time. Athena reuses the result of the same SQL within 24 h (free, 0 MB; --no-reuse for fresh data).
Synapse: Data processed is not tracked (no cost estimate, no scan budget, no request guard); the date window and the
folder pruning (date_created -> filepath()) keep scans small. --dry-run prints the translated T-SQL.
Read-only: Only SELECT / WITH / SHOW / DESCRIBE / EXPLAIN / VALUES run, on every backend; the data lake is never
modified. Exit codes: 0 OK, 1 query failed, 2 refused (checks, configuration).
"""
from __future__ import annotations

import argparse
import csv
import json
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _backends import LakeError, _iso, add_source_args, check_sql, connect_args, parse_utc  # noqa: E402


def fmt(v) -> str:
    if v is None or (isinstance(v, float) and pd.isna(v)):
        return "NULL"
    if isinstance(v, float):
        return f"{v:.6g}"
    if isinstance(v, pd.Timestamp):
        return _iso(v)[:23]
    return str(v)


def print_table(cols: list[str], rows: list[list], show: int) -> None:
    shown = [[fmt(v) for v in r] for r in rows[:show]]
    widths = [min(40, max([len(c)] + [len(r[i]) for r in shown])) for i, c in enumerate(cols)]
    print("  " + " | ".join(c[:w].ljust(w) for c, w in zip(cols, widths)))
    print("  " + "-+-".join("-" * w for w in widths))
    for r in shown:
        print("  " + " | ".join(v[:w].ljust(w) for v, w in zip(r, widths)))
    if len(rows) > show:
        print(f"  ... {len(rows) - show:,} more rows (use --out to save all)")


def write_out(df: pd.DataFrame, out: Path, sql: str) -> None:
    out.parent.mkdir(parents=True, exist_ok=True)
    d = df.copy()
    if out.suffix.lower() == ".parquet":
        d.to_parquet(out, index=False)
        return
    for c in d.columns:
        if pd.api.types.is_datetime64_any_dtype(d[c]):
            d[c] = d[c].map(_iso)
    if out.suffix.lower() == ".json":
        out.write_text(json.dumps({"sql": sql, "columns": list(d.columns),
                                   "rows": json.loads(d.to_json(orient="values"))}, indent=1), encoding="utf-8")
    else:
        d.to_csv(out, index=False, quoting=csv.QUOTE_MINIMAL)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_source_args(ap)
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--max-rows", type=int, default=1000, help="Max rows to fetch (Athena, default 1000)")
    common.add_argument("--show", type=int, default=20, help="Rows to print (default 20)")
    common.add_argument("--out", type=Path, help="Save the result as .csv, .json or .parquet")
    common.add_argument("--dry-run", action="store_true", help="Checks + generated SQL only (no query)")
    common.add_argument("--timeout", type=float, default=600, help="Athena/Synapse: Stop the query after N seconds")
    common.add_argument("--json", action="store_true", help="JSON result (summary + first --show rows)")
    common.add_argument("--no-reuse", action="store_true", help="Athena: Do not reuse results of the same SQL (< 24 h)")
    common.add_argument("--max-scan-gb", type=float, default=10.0, help="Cloud: Scan budget per run (default 10)")
    common.add_argument("--max-request-usd", type=float, default=1.0,
                        help="Cloud: Storage request guard for summary in USD (default 1, very rough estimate)")
    add_source_args(common, sub=True)   # the source flags also work after the subcommand
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("sql", parents=[common], help="Run a SQL statement")
    s.add_argument("query", nargs="?", help="SQL statement (or use --file)")
    s.add_argument("--file", type=Path, help="Read the SQL from this file")
    s.add_argument("--start", help="Local: Only read Parquet files of days from this UTC time")
    s.add_argument("--end", help="Local: Only read Parquet files of days up to this UTC time")
    r = sub.add_parser("resample", parents=[common], help="Grafana-style resampled signal query")
    r.add_argument("--device", required=True, help="Device ID or meta name")
    r.add_argument("--message", required=True, help="Message (table suffix, any case) or a unique substring")
    r.add_argument("--signals", help="Comma separated (default: all signals of the message)")
    r.add_argument("--start", required=True, help="UTC start (ISO; naive = UTC, offsets are converted)")
    r.add_argument("--end", required=True, help="UTC end")
    r.add_argument("--agg", default="avg", help="avg,min,max,sum,count,first,last (comma separated; default avg)")
    r.add_argument("--interval", default="auto", help="auto (default) or e.g. 100ms, 1s, 10s, 1min, 1h")
    r.add_argument("--points", type=int, default=1000, help="auto interval: Target number of buckets (default 1000)")
    r.add_argument("--where", help="Extra SQL condition, e.g. \"enginespeed > 0\"")
    f = sub.add_parser("summary", parents=[common], help="Fleet statistics of signals across devices")
    fs = f.add_mutually_exclusive_group(required=True)
    fs.add_argument("--signals", help="Exact signal column names, comma separated (lake_schema.py --find); "
                                      "message.signal = only that table")
    fs.add_argument("--match", metavar="REGEX", help="Every signal whose name matches, e.g. \"^cell_?\\d+$\"")
    fs.add_argument("--all", action="store_true", help="Every signal of the --device(s) (triage, catalog)")
    f.add_argument("--start", required=True, help="UTC start")
    f.add_argument("--end", required=True, help="UTC end")
    f.add_argument("--by", default="day", choices=["day", "hour", "total"], help="Period per row (default day)")
    f.add_argument("--device", action="append", help="Only these devices: ID or meta name (repeatable)")
    f.add_argument("--keep-empty", action="store_true", help="Keep signals without any value (n = 0)")
    e = sub.add_parser("episodes", parents=[common], help="Periods where a signal is above/below a limit or a state")
    h = sub.add_parser("hist", parents=[common], help="Time spent per value bin / state (duty cycle, exposure)")
    for p in (e, h):
        p.add_argument("--signal", required=True, help="Exact signal column name (lake_schema.py --find), or "
                                                       "message.signal (one table)")
        p.add_argument("--start", required=True, help="UTC start")
        p.add_argument("--end", required=True, help="UTC end")
        p.add_argument("--device", action="append", help="Only these devices: ID or meta name (repeatable)")
        p.add_argument("--gap", default="10s", help="A data gap longer than this ends an episode / counts as no data "
                                                     "(default 10s; raise it for on-change logging)")
    ec = e.add_mutually_exclusive_group(required=True)
    ec.add_argument("--above", type=float)
    ec.add_argument("--below", type=float)
    ec.add_argument("--state", type=float, help="Signal equals this value (e.g. a value table entry)")
    e.add_argument("--min-duration", type=float, default=0, help="Seconds (default 0 = also single rows)")
    hc = h.add_mutually_exclusive_group(required=True)
    hc.add_argument("--bins", help="Bin edges, comma separated, e.g. 0,500,1000,1500")
    hc.add_argument("--states", action="store_true", help="One bin per value (states, modes, flags)")
    h.add_argument("--by", default="total", choices=["day", "hour", "total"], help="Period per row (default total)")
    h.add_argument("--dbc", type=Path, help="--states: Value table labels from these DBCs (project, folder or file; "
                                            "default with --project: its DBCs)")
    a = ap.parse_args()
    try:
        return run(a)
    except LakeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


def run(a) -> int:
    cloud = a.athena or a.bigquery or a.synapse
    kw = {"timeout": a.timeout, "reuse_minutes": 0 if a.no_reuse else 1440, "max_scan_gb": a.max_scan_gb,
          "max_request_usd": a.max_request_usd} if cloud else {}
    db = connect_args(a, **kw)
    backend = db.kind
    if a.cmd == "summary":
        return run_summary(db, a)
    if a.cmd in ("episodes", "hist"):
        return run_window(db, a)
    if a.cmd == "resample":
        sql, info = db.resample_sql(a.device, a.message, a.signals, a.start, a.end, a.agg, a.interval, a.points,
                                    a.where)
    else:
        if bool(a.query) == bool(a.file):
            raise LakeError("give the SQL as an argument or via --file (one of them)")
        sql = (a.file.read_text(encoding="utf-8") if a.file else a.query).strip().rstrip(";").strip()
        info = {}
    chk = check_sql(sql, backend)

    if a.dry_run:
        cfg, note = None, None
        if a.athena:
            from _athena import athena_config
            cfg = athena_config()
            note = cfg["problem"]
        elif a.bigquery:
            from _bigquery import bigquery_config, creds_error
            cfg = bigquery_config()
            note = creds_error() if cfg["missing"] else None
            if not note and not chk["errors"]:
                info["sql_bigquery"] = db.translate(sql)
        elif a.synapse:
            from _synapse import creds_error as synapse_creds_error
            cfg = dict(db.cfg)
            note = synapse_creds_error(cfg) if cfg["missing"] else None
            if not note and not chk["errors"]:
                info["sql_synapse"] = db.translate(sql)
        res = {"dry_run": True, "backend": backend, "sql": sql, **info, **chk}
        if cfg:
            res["config"] = cfg
        if a.json:
            print(json.dumps(res, indent=1))
        else:
            print("SQL:\n" + "\n".join("  " + x for x in sql.splitlines()))
            if info.get("sql_bigquery"):
                print("BigQuery SQL (translated):\n  " + info["sql_bigquery"])
            if info.get("sql_synapse"):
                print("Synapse T-SQL (translated):\n  " + info["sql_synapse"])
            if info.get("interval"):
                print(f"Interval: {info['interval']} (max {info['expected_rows_max']:,} rows)")
            if cfg:
                print("Config: " + " ".join(f"{k}={v}" for k, v in cfg.items() if k != "missing"))
            else:
                print(f"Source: {db.describe_source()}")
            print(f"Statement: {chk['statement']} | tables: {', '.join(chk['tables']) or '-'}")
            for e in chk["errors"]:
                print(f"ERROR: {e}")
            for w in chk["warnings"]:
                print(f"WARNING: {w}")
            if note:
                print("NOTE: " + note)
            if not chk["errors"] and not chk["warnings"]:
                print("OK: No issues found")
        return 1 if chk["errors"] else 0

    if chk["errors"]:
        for e in chk["errors"]:
            print(f"REFUSED: {e}", file=sys.stderr)
        return 2
    for w in chk["warnings"]:
        print(f"WARNING: {w}", file=sys.stderr)

    if cloud:
        try:
            res = db.run(sql, a.max_rows)
        except KeyboardInterrupt:
            print("Interrupted - query stopped", file=sys.stderr)
            return 130
        if res["state"] != "SUCCEEDED":
            print(f"{res['state']}: {res['error']} (query {res['query_execution_id']})", file=sys.stderr)
            if res.get("sql_bigquery"):
                print(f"BigQuery SQL (translated): {res['sql_bigquery']}", file=sys.stderr)
            if res.get("sql_synapse"):
                print(f"Synapse T-SQL (translated): {res['sql_synapse']}", file=sys.stderr)
            return 1
        df = db.frame(res)
        head = (f"{res['state']} in {res['execution_s']} s (engine {res['engine_s']} s) | "
                + ("reused a previous result (free) " if res["reused"] else
                   "data processed not tracked (Synapse)" if res["data_scanned_mb"] is None else
                   f"{'billed' if 'billed_mb' in res else 'scanned'} "
                   f"{res.get('billed_mb', res['data_scanned_mb']):.1f} MB | est. scan cost "
                   f"${res['cost_usd_est']:.5f} (rough)")
                + f" | {len(df):,} rows"
                + (f" (truncated at --max-rows {a.max_rows})" if res["truncated"] else "")
                + f" | id {res['query_execution_id']}")
        meta = {k: v for k, v in res.items() if k not in ("rows", "columns", "column_types")}
    else:
        import time
        t0 = time.time()
        df = db.sql(sql, start=getattr(a, "start", None), end=getattr(a, "end", None))
        head = f"OK in {time.time() - t0:.2f} s (DuckDB, {db.describe_source()}) | {len(df):,} rows"
        meta = {"backend": "local", "seconds": round(time.time() - t0, 2)}

    if a.out:
        write_out(df, a.out, sql)
    if a.json:
        prev = df.head(a.show).copy()
        for c in prev.columns:
            if pd.api.types.is_datetime64_any_dtype(prev[c]):
                prev[c] = prev[c].map(_iso)
        print(json.dumps({**meta, **info, "sql": sql, "columns": list(df.columns), "rows_fetched": len(df),
                          "rows_preview": json.loads(prev.to_json(orient="values")),
                          "out": str(a.out) if a.out else None}, indent=1, default=str))
        return 0
    if a.cmd == "resample":
        print("SQL: " + sql)
        print(f"Interval: {info['interval']}")
    print(head)
    if len(df.columns):
        print_table(list(df.columns), df.values.tolist(), a.show)
    if a.out:
        print(f"Saved: {a.out}")
    return 0


def spec_hits(db, specs: list[str], devs: set) -> tuple[list[dict], list[str]]:
    """'signal' or 'message.signal' specs -> (find() hits of message tables, specs without a hit)."""
    import re
    parts = [s.strip().lower().rpartition(".")[::2] for s in specs if s.strip()]   # (message or '', signal)
    names = sorted({s for _, s in parts})
    hits = [h for h in db.find("^(" + "|".join(re.escape(n) for n in names) + ")$", regex=True)
            if h["device"] and (not devs or h["device"] in devs)]
    ok = lambda h, m, s: h["signal"] == s and (not m or h["message"] == m)   # noqa: E731
    missing = [f"{m}.{s}" if m else s for m, s in parts if not any(ok(h, m, s) for h in hits)]
    return [h for h in hits if any(ok(h, m, s) for m, s in parts)], missing


def warn_multi(hits: list[dict]) -> None:
    """A device with the same signal in several tables: Separate rows that must not simply be added up."""
    by: dict = {}
    for h in hits:
        by.setdefault((h["device"], h["signal"]), []).append(h["message"])
    for (d, s), ms in by.items():
        if len(ms) > 1:
            print(f"NOTE: {d} has {s} in {len(ms)} tables ({', '.join(ms)}): One row each, do not add them up "
                  f"(the same data, e.g. two DBC versions, or different sources: bus, source ID, a copy that never "
                  f"sets). Check each (lake_schema.py --stats), then pick one with message.signal", file=sys.stderr)


def run_summary(db, a) -> int:
    import time
    devs = {db.device_id(d) for d in a.device or []}
    if a.all and not devs:
        raise LakeError("summary --all needs --device (every signal of the whole fleet is rarely what you want: "
                        "use --signals / --match)")
    if a.signals:
        hits, missing = spec_hits(db, a.signals.split(","), devs)
        names = sorted({h["signal"] for h in hits})
        warn_multi(hits)
    else:
        hits = [h for h in db.find(a.match or "", regex=bool(a.match))
                if h["device"] and (not devs or h["device"] in devs)]
        names, missing = sorted({h["signal"] for h in hits}), []
    what = a.signals or (f"match {a.match}" if a.match else "all signals")
    if not hits:
        raise LakeError(f"No message table has the signal(s) {what} - check the names with lake_schema.py --find")
    tables = {h["table"] for h in hits}
    if a.dry_run:
        est = db.request_estimate(hits, a.start, a.end)
        res = {"dry_run": True, "backend": db.kind, "signals": names, "missing_signals": missing,
               "tables": len(tables), "devices": len({h["device"] for h in hits}), "request_estimate": est,
               "max_request_usd": db.max_request_usd,
               "over_guard": bool(est and est["usd"] > db.max_request_usd)}
        if a.json:
            print(json.dumps(res, indent=1, default=str))
            return 0
        print(f"Dry run: {len(names)} signal(s) in {res['tables']} table(s) on {res['devices']} device(s), "
              f"{a.start} -> {a.end}, by {a.by} (no query run)")
        if missing:
            print(f"WARNING: No table has {', '.join(missing)}")
        if est:
            print(f"Storage requests: {est['text']}")
            print(f"{'ABOVE' if res['over_guard'] else 'Below'} the request guard (${db.max_request_usd:g})"
                  + (": Tell the user and ask before running with --max-request-usd" if res["over_guard"] else ""))
        return 0
    t0 = time.time()
    df = db.summary(hits, a.start, a.end, a.by, keep_empty=a.keep_empty)
    labels = db.devices()
    df.insert(1, "device_name", df["device"].map(lambda d: labels.get(d)))
    head = (f"OK in {time.time() - t0:.1f} s | {len(names)} signal(s) in {len(tables)} table(s) on "
            f"{len({h['device'] for h in hits})} device(s), {df.attrs.get('queries')} queries | {len(df):,} rows"
            + (f" | {db.budget()}" if hasattr(db, "budget") else ""))
    if a.out:
        write_out(df, a.out, f"summary {what} {a.start} -> {a.end} by {a.by}")
    if a.json:
        prev = df.head(a.show).copy()
        for c in prev.columns:
            if pd.api.types.is_datetime64_any_dtype(prev[c]):
                prev[c] = prev[c].map(_iso)
        print(json.dumps({"signals": names, "missing_signals": missing, "tables": len(tables),
                          "queries": df.attrs.get("queries"), "request_estimate": df.attrs.get("request_estimate"),
                          "budget": db.budget() if hasattr(db, "budget") else None,
                          "columns": list(df.columns), "rows": len(df), "rows_preview":
                          json.loads(prev.to_json(orient="values")), "out": str(a.out) if a.out else None},
                         indent=1, default=str))
        return 0
    print(head)
    if missing:
        print(f"WARNING: No table has {', '.join(missing)}")
    show = df.drop(columns=["sum"] + (["first_t", "last_t"] if a.by != "total" else []))
    for c in ("first_t", "last_t"):   # by total: the device's logged period in the window
        if c in show.columns:
            show[c] = show[c].map(lambda x: x.strftime("%Y-%m-%d %H:%M") if pd.notna(x) else "-")
    print_table(list(show.columns), show.values.tolist(), a.show)
    if a.out:
        print(f"Saved: {a.out}")
    return 0


def run_window(db, a) -> int:
    """episodes / hist: One signal on every device (or --device) that has it."""
    import time
    devs = {db.device_id(d) for d in a.device or []}
    hits, _ = spec_hits(db, [a.signal], devs)
    if not hits:
        raise LakeError(f"No message table has the signal {a.signal} - check the name with lake_schema.py --find")
    sig = hits[0]["signal"]
    warn_multi(hits)
    if a.dry_run:
        est = db.request_estimate(hits, a.start, a.end)
        print(f"Dry run: {sig} in {len(hits)} table(s) on {len({h['device'] for h in hits})} device(s) (no query run)"
              + (f"\nStorage requests: {est['text']}" if est else ""))
        return 0
    t0 = time.time()
    if a.cmd == "episodes":
        df = db.episodes(hits, a.start, a.end, a.above, a.below, a.state, a.min_duration, a.gap)
        what = f"episodes {sig} {df.attrs['condition']}"
    else:
        bins = [float(x) for x in a.bins.split(",")] if a.bins else None
        df = db.hist(hits, a.start, a.end, bins, a.states, a.by, a.gap)
        what = f"hist {sig} {a.bins or 'states'} by {a.by}"
        if a.states and len(df):
            labels = state_labels(a, df, sig)
            if labels:
                df["label"] = [labels.get((d, m), {}).get(int(v), lab) if float(v).is_integer() else lab
                               for d, m, v, lab in zip(df["device"], df["message"], df["bin"], df["label"])]
    names = db.devices()
    df.insert(1, "device_name", df["device"].map(lambda d: names.get(d)))
    head = (f"OK in {time.time() - t0:.1f} s | {what} | {len(hits)} table(s), {df.attrs.get('queries')} queries"
            + (f", {df.attrs['flagged_days']} day(s) with a match" if a.cmd == "episodes" else "")
            + f" | {len(df):,} rows" + (f" | {db.budget()}" if hasattr(db, "budget") else ""))
    if a.out:
        write_out(df, a.out, f"{what} {a.start} -> {a.end} gap {a.gap}")
    if a.json:
        prev = df.head(a.show).copy()
        for c in prev.columns:
            if pd.api.types.is_datetime64_any_dtype(prev[c]):
                prev[c] = prev[c].map(_iso)
        print(json.dumps({"what": what, "rows": len(df), "queries": df.attrs.get("queries"),
                          "budget": db.budget() if hasattr(db, "budget") else None, "columns": list(df.columns),
                          "rows_preview": json.loads(prev.to_json(orient="values")),
                          "out": str(a.out) if a.out else None}, indent=1, default=str))
        return 0
    print(head)
    if a.cmd == "hist" and len(df):   # time with data vs the window: Gaps (logger off, > --gap) are not in the bins
        win_h = (parse_utc(a.end) - parse_utc(a.start)).total_seconds() / 3600
        cov = df.groupby(["device", "message"])["seconds"].sum() / 3600
        print(f"Time with data (window {win_h:,.1f} h): " + ", ".join(
            f"{d} {m} {h:,.2f} h" for (d, m), h in list(cov.items())[:10]) + (" ..." if len(cov) > 10 else ""))
    show = df.drop(columns=[c for c in ("bin_lo", "bin_hi") if c in df.columns])
    print_table(list(show.columns), show.values.tolist(), a.show)
    if a.out:
        print(f"Saved: {a.out}")
    return 0


def state_labels(a, df, sig) -> dict:
    """(device, message) -> {value: label} from the DBC value tables (hist --states)."""
    path = a.dbc or (a.project if a.project and any((a.project / "dbc").glob("*.dbc")) else None)
    if not path:
        return {}
    from _dbc import DbcIndex
    idx = DbcIndex(path)
    out = {}
    for d, m in set(zip(df["device"], df["message"])):
        info = idx.match(d, m)
        vt = ((info or {}).get("signals", {}).get(sig) or {}).get("value_table") or {}
        out[(d, m)] = {int(k): v for k, v in vt.items()}
    return out


if __name__ == "__main__":
    sys.exit(main())
