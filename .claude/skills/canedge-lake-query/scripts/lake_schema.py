"""Discover what is in a CANedge Parquet data lake: Local folder (DuckDB), Athena (Glue), BigQuery or Synapse.

Table naming (as the data lake's Glue mapping): One table per device + message, tbl_<device>_<message> in lower
case, e.g. tbl_12345678_can9_gnssspeed (message folder CAN9_GnssSpeed). Columns: t (UTC) + one lower-case column per
signal + date_created ('yyyy/MM/dd'). Helper tables: tbl_<device>_messages, tbl_aggregations_<name> (device meta,
optional trip summaries/events). Devices, messages and signals come from the Glue catalog (free, the same lists the
Grafana dropdowns query via SQL); the overview adds the device meta names (one tiny query, cached with the schema).

Usage (data source: --project projects/<name> | --lake <folder> | --athena | --bigquery | --synapse [--env NAME]):
  lake_schema.py --project p                         overview: devices, message tables, aggregation tables
  lake_schema.py --athena --tables [--device X] [--all]
  lake_schema.py --athena --describe tbl_12345678_can9_gnssspeed   (or 12345678_can9_gnssspeed)
  lake_schema.py --lake TEMP/lake --find speed [--regex] [--device X]
  lake_schema.py --project p --stats tbl_12345678_can1_eec1_s00 [--start .. --end ..]   (Athena: window required)
  lake_schema.py --athena --catalog --device X --dbc projects/p [--message bms] [--start .. --end ..]
  options: --refresh / --offline (cloud schema cache, 24 h in data/athena-schema/, data/bigquery-schema/ or
           data/synapse-schema/), --json
  --device takes a device ID or its meta name ('Truck 1').

DBC meaning (--dbc <project | folder | file>; default with --project: its dbc/ + dbc-groups.json): --catalog lists
the lake's message tables of a device with the DBC message (ID, comment) and per signal the unit, value table and
comment. Lake first: DBC messages the lake does not hold are never listed. With --start/--end it adds the values
per signal in that window (one summary scan) and marks signals without any value (always NULL). --describe and
--find annotate the same way; --find then also matches DBC units, comments and value table labels.
--stats runs one query: Rows, time span and per signal count, null share, min/max/mean, approx distinct values.
Athena: Re-run the data lake's Glue mapping job (AWS console) if new devices/messages are missing. Synapse: Re-run
the Container App Job synapse-map-tables-<id> (Azure portal, Run now) - also needed after backlog/aggregation runs.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _backends import LakeError, add_source_args, connect_args, signal_cols, table_name  # noqa: E402


def g6(x) -> str:
    if x is None:
        return "-"
    return f"{x:.6g}" if isinstance(x, float) else str(x)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_source_args(ap)
    g = ap.add_mutually_exclusive_group()
    g.add_argument("--tables", action="store_true", help="List message tables per device")
    g.add_argument("--describe", metavar="TABLE", help="Columns, types, partition keys + location of one table")
    g.add_argument("--find", metavar="SIGNAL", help="Which tables contain a signal (case-insensitive substring)")
    g.add_argument("--stats", metavar="TABLE", help="Signal statistics of one table (one query)")
    g.add_argument("--catalog", action="store_true", help="Lake tables + DBC meaning (+ values per signal with a window)")
    ap.add_argument("--regex", action="store_true", help="--find pattern is a regular expression")
    ap.add_argument("--device", action="append", help="Only these devices: ID or meta name (repeatable)")
    ap.add_argument("--message", help="--catalog: Only message tables containing this text")
    ap.add_argument("--dbc", type=Path, help="DBC files: A project folder, a folder of .dbc files or one file")
    ap.add_argument("--start", help="--stats / --catalog: UTC start (ISO; naive = UTC)")
    ap.add_argument("--end", help="--stats / --catalog: UTC end")
    ap.add_argument("--all", action="store_true", help="--tables: Do not truncate long lists")
    ap.add_argument("--refresh", action="store_true", help="Cloud: Re-read the schema (ignore the cache)")
    ap.add_argument("--offline", action="store_true", help="Cloud: Use the schema cache only")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        kw = {"refresh_schema": a.refresh, "offline_schema": a.offline} if (a.athena or a.bigquery or a.synapse) \
            else {}
        db = connect_args(a, **kw)
        return run(db, a)
    except LakeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


def dbc_index(a):
    """--dbc, else the project's DBCs (if it has any), else None."""
    path = a.dbc or (a.project if a.project and any((a.project / "dbc").glob("*.dbc")) else None)
    if not path:
        return None
    from _dbc import DbcIndex
    try:
        idx = DbcIndex(path)
    except FileNotFoundError as e:
        raise LakeError(str(e)) from None
    return idx


def dbc_errors(idx) -> None:
    for f, e in (idx.errors if idx else {}).items():
        print(f"WARNING: {f} could not be loaded ({e}) - skipped; run canedge-dbc dbc_lint.py", file=sys.stderr)


def run(db, a) -> int:
    tables = db.tables()
    source = getattr(db, "schema_source", None) or db.describe_source()
    devs = {db.device_id(d) for d in a.device or []}
    if devs:
        tables = [t for t in tables if t["device"] in devs or t["kind"] == "aggregation"]
    idx = dbc_index(a) if (a.describe or a.find or a.catalog) else None
    out = None

    if a.catalog:
        out = catalog(db, a, tables, idx, source)
    elif a.describe:
        t = db.table(a.describe)
        info = idx.match(t["device"], t["message"]) if idx and t["kind"] == "message" else None
        out = {**t, "dbc": info}
        if not a.json:
            from _dbc import short, values_text
            print(f"{t['name']} ({t['kind']}{', device ' + t['device'] if t['device'] else ''}) - {source}")
            print(f"  Location: {t['location']}")
            print(f"  Partition keys: {', '.join(f'{n} {ty}' for n, ty in t['partition_keys']) or '-'}")
            if info:
                print(f"  DBC: {info['message']} {info['id']} ({info['dbc']})"
                      + (f" - {short(info['comment'], 160)}" if info["comment"] else ""))
            elif idx:
                print("  DBC: No matching DBC message")
            print(f"  Columns ({len(t['columns'])}):")
            for n, ty in t["columns"]:
                s = (info or {}).get("signals", {}).get(n)
                extra = (f" {s['unit'] or '-':<8} " + (f"values {values_text(s['value_table'])} " if s["value_table"]
                                                       else "") + short(s["comment"], 70)) if s else ""
                print(f"    {n:<40} {ty:<10}{extra}")
    elif a.stats:
        out = db.stats(a.stats, a.start, a.end)
        if not a.json:
            print(f"{out['table']}: {out['rows']:,} rows, {(out['first_utc'] or '?')[:23]} -> "
                  f"{(out['last_utc'] or '?')[:23]} UTC - {db.describe_source()}")
            print(f"  {'signal':36s} {'count':>10} {'null%':>6} {'min':>12} {'max':>12} {'mean':>12} {'distinct~':>9}")
            for s in out["signals"]:
                print(f"  {s['signal']:36s} {s['count']:>10,} {100 * (s['null_share'] or 0):>5.1f}% {g6(s['min']):>12} "
                      f"{g6(s['max']):>12} {g6(s['mean']):>12} {g6(s['distinct_approx']):>9}")
            if hasattr(db, "budget"):
                print(f"  ({db.budget()})")
    elif a.find:
        out = find(db, a, tables, idx, devs, source)
    else:
        by_dev, aggs, other = defaultdict(list), [], []
        for t in tables:
            if t["kind"] == "message":
                by_dev[t["device"]].append(t)
            elif t["kind"] == "aggregation":
                aggs.append(t)
            elif t["kind"] == "other":
                other.append(t)
        labels = db.devices()   # meta names from tbl_aggregations_devicemeta (one tiny query, then cached)
        out = {"source": source, "devices": {d: [{"table": t["name"], "message": t["message"],
                                                  "signals": len(signal_cols(t))} for t in ts]
                                             for d, ts in sorted(by_dev.items())},
               "device_names": {d: labels.get(d) for d in sorted(by_dev) if labels.get(d)},
               "aggregation_tables": [{"table": t["name"], "columns": [c[0] for c in t["columns"]]} for t in aggs],
               "other_tables": [t["name"] for t in other]}
        if not a.json:
            n_msg = sum(len(v) for v in by_dev.values())
            print(f"{len(by_dev)} device(s), {n_msg} message tables, {len(aggs)} aggregation tables - {source}")
            for d, ts in sorted(by_dev.items()):
                ts = sorted(ts, key=lambda t: t["message"])
                d = f"{d} '{labels[d]}'" if labels.get(d) else d
                if a.tables:
                    lim = None if a.all else 40
                    print(f"  {d} ({len(ts)} messages):")
                    for t in ts[:lim]:
                        sig = [c[0] for c in signal_cols(t)]
                        print(f"    {t['name']}: {len(sig)} signals ({', '.join(sig[:6])}{', ...' if len(sig) > 6 else ''})")
                    if lim and len(ts) > lim:
                        print(f"    ... {len(ts) - lim} more (--all)")
                else:
                    names = [t["message"] for t in ts]
                    print(f"  {d}: {len(ts)} messages ({', '.join(names[:8])}{', ...' if len(names) > 8 else ''})")
            for t in aggs:
                print(f"  aggregation: {t['name']} ({', '.join(c[0] for c in t['columns'][:10])})")
            if other:
                print(f"  other: {', '.join(t['name'] for t in other[:10])}")
            if not by_dev:
                print("  No device message tables" + (" - has the Glue mapping job run since data arrived?"
                                                      if db.kind == "athena" else
                                                      " - has the Container App Job synapse-map-tables-<id> run since "
                                                      "data arrived?" if db.kind == "synapse" else ""))
    dbc_errors(idx)
    if a.json:
        print(json.dumps(out, indent=1, default=str))
    return 0


def find(db, a, tables, idx, devs, source) -> dict:
    """Lake columns whose name matches; with DBCs also columns whose unit/comment/value labels match, and message
    tables whose DBC message comment matches. Only lake columns are returned."""
    from _dbc import short
    pat = re.compile(a.find if a.regex else re.escape(a.find), re.I)
    hits, msg_hits = [], []
    for t in tables:
        if t["kind"] not in ("message", "aggregation"):
            continue
        info = idx.match(t["device"], t["message"]) if idx and t["kind"] == "message" else None
        if info and info["comment"] and pat.search(info["comment"]):
            msg_hits.append({"table": t["name"], "device": t["device"], "dbc_message": info["message"],
                             "comment": info["comment"]})
        for n, ty in signal_cols(t):
            where = "name" if pat.search(n) else None
            if not where and info:
                from _dbc import text_hit
                where = text_hit(pat, info, n)
            if where:
                s = (info or {}).get("signals", {}).get(n) or {}
                hits.append({"table": t["name"], "device": t["device"], "message": t["message"], "signal": n,
                             "type": ty, "matched": where, **({"unit": s.get("unit"), "comment": s.get("comment"),
                                                               "value_table": s.get("value_table")} if s else {})})
    out = {"pattern": a.find, "hits": hits, "message_hits": msg_hits}
    if a.json:
        return out
    by_sig, by_text = defaultdict(list), defaultdict(list)
    for h in hits:
        (by_sig if h["matched"] == "name" else by_text)[h["signal"]].append(h)
    dbc = lambda h: (f" [{h.get('unit') or '-'}: {short(h.get('comment'), 60)}]"   # noqa: E731
                     if h.get("unit") or h.get("comment") else "")
    print(f"Signals matching '{a.find}': {len(by_sig)} column name(s) in {sum(map(len, by_sig.values()))} table(s)"
          + (f", {len(by_text)} more via the DBC" if idx else "") + f" - {source}")
    for n, hs in sorted(by_sig.items()):
        msgs = sorted({h["message"] for h in hs})
        devices = sorted({h["device"] for h in hs if h["device"]})
        print(f"  {n}: {len(hs)} table(s), message(s) {', '.join(msgs)}, devices {', '.join(devices) or '-'}{dbc(hs[0])}")
        for h in hs[:5]:
            print(f"      {h['table']} ({h['type']})")
        if len(hs) > 5:
            print(f"      ... {len(hs) - 5} more (--json for all)")
    if by_text:
        print(f"  Matching in the DBC (unit / comment / value labels), {len(by_text)} column(s):")
        for n, hs in sorted(by_text.items())[:30]:
            print(f"    {n} ({hs[0]['matched']}; {', '.join(sorted({h['message'] for h in hs}))}){dbc(hs[0])}")
        if len(by_text) > 30:
            print(f"    ... {len(by_text) - 30} more (--json for all)")
    for m in msg_hits[:20]:
        print(f"  Message comment: {m['table']} ({m['dbc_message']}): {short(m['comment'], 100)}")
    if not hits and not msg_hits:
        print("  None - try a shorter pattern, --regex, --dbc (searches DBC comments), or --tables")
    elif hits:
        print("  Schema only: A column can be all NULL (not available) - check --stats / --catalog with a window "
              "before reporting it")
    return out


def catalog(db, a, tables, idx, source) -> dict:
    """Lake first: Each message table of the device(s) with its DBC message + per signal unit, value table, comment;
    with --start/--end the values per signal in that window (one summary scan, keep_empty)."""
    from _dbc import short, values_text
    tabs = sorted((t for t in tables if t["kind"] == "message"
                   and (not a.message or a.message.lower() in t["message"])), key=lambda t: (t["device"], t["message"]))
    if not tabs:
        raise LakeError("No message tables" + (f" containing '{a.message}'" if a.message else "")
                        + " for these devices (lake_schema.py --tables)")
    devs = sorted({t["device"] for t in tabs})
    if len(devs) > 5 and not a.device:
        raise LakeError(f"--catalog covers {len(devs)} devices: Pick one or a few with --device (DBC groups usually "
                        "match per vehicle type, so one device per type is enough)")
    avail = {}
    if a.start and a.end:
        hits = [{"table": t["name"], "device": t["device"], "message": t["message"], "signal": c}
                for t in tabs for c, _ in signal_cols(t)]
        df = db.summary(hits, a.start, a.end, by="total", keep_empty=True)
        for r in df.itertuples(index=False):
            avail[(table_name(r.device, r.message), r.signal)] = (int(r.n), int(r.n_rows or 0))
    elif db.needs_window:
        print("NOTE: No --start/--end: DBC meaning only. Add a window to see which signals have values "
              "(one summary scan)", file=sys.stderr)
    names = db.devices()
    out = {"source": source, "dbc": {"dir": str(idx.dir), "errors": idx.errors} if idx else None,
           "window": [a.start, a.end] if avail else None, "devices": {}}
    for d in devs:
        dt_ = []
        for t in (t for t in tabs if t["device"] == d):
            info = idx.match(d, t["message"]) if idx else None
            rows = None
            sigs = []
            for c, ty in signal_cols(t):
                s = (info or {}).get("signals", {}).get(c) or {}
                n, rows = avail.get((t["name"], c), (None, rows))
                sigs.append({"signal": c, "type": ty, "unit": s.get("unit"), "comment": s.get("comment"),
                             "value_table": s.get("value_table"), "min": s.get("min"), "max": s.get("max"),
                             "in_dbc": bool(s), "n": n, "has_data": None if n is None else n > 0})
            dt_.append({"table": t["name"], "message": t["message"], "rows": rows,
                        "dbc": {k: info[k] for k in ("dbc", "message", "id", "comment", "suffix")} if info else None,
                        "signals": sigs})
        out["devices"][d] = {"name": names.get(d), "tables": dt_}
    if a.json:
        return out
    win = f", values {a.start} -> {a.end}" if avail else ""
    print(f"Catalog: {len(tabs)} message table(s) on {len(devs)} device(s){win} - {source}"
          + (f" | DBC: {idx.dir}" if idx else " | no DBC (--dbc)"))
    for d, dv in out["devices"].items():
        print(f"{d}" + (f" '{dv['name']}'" if dv["name"] else "") + f": {len(dv['tables'])} message tables")
        unmatched = [t["table"] for t in dv["tables"] if idx and not t["dbc"]]
        for t in dv["tables"]:
            if idx and not t["dbc"]:
                continue
            b = t["dbc"]
            rows = "" if t["rows"] is None else f", {t['rows']:,} rows"
            print(f"  {t['table']}" + (f" = {b['message']} {b['id']} ({b['dbc']})" if b else "") + rows
                  + (f" - {short(b['comment'], 120)}" if b and b["comment"] else ""))
            empty = [s["signal"] for s in t["signals"] if s["has_data"] is False]
            for s in t["signals"]:
                if s["has_data"] is False:
                    continue
                n = "" if s["n"] is None else f" n={s['n']:,}"
                vt = f" values {values_text(s['value_table'])}" if s["value_table"] else ""
                print(f"    {s['signal']:<34} {s['unit'] or '-':<8}{n}{vt}"
                      + (f" - {short(s['comment'], 80)}" if s["comment"] else ""))
            if t["rows"] == 0:
                print("    no data in this window (find days with data: lake_query.py summary --by day)")
            elif empty:
                print(f"    no values in the window (always NULL, e.g. not available / outside the DBC range): "
                      f"{', '.join(empty)}")
        if unmatched:
            print(f"  No DBC message for: {', '.join(unmatched[:20])}{' ...' if len(unmatched) > 20 else ''}")
    if hasattr(db, "budget") and avail:
        print(f"({db.budget()})")
    return out


if __name__ == "__main__":
    sys.exit(main())
