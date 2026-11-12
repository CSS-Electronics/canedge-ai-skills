"""Compact overview of a DBC file for the agent (cantools, strict=False).

Usage:
  python dbc_info.py <file.dbc>                       # message table + nodes + totals
  python dbc_info.py <file.dbc> --message <name|id>   # signal details of one message (id as 0x1F0, 496 or 0x98FEF1FE)
  python dbc_info.py <file.dbc> --find <text>         # signals whose name/comment/unit contains <text> (case-insensitive)
  add --json for machine-readable output

The message table shows: ID (hex), name, DLC, extended (X) / CAN FD (FD), cycle time (GenMsgCycleTime) in ms,
signal count, multiplexed flag, senders. Run dbc_lint.py for validation - this script only describes the file.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
from dbc_groups import fmt_can_id, load_dbc as load  # noqa: E402,F401 (load re-exported for canedge-lake-query _dbc.py)


def hex_id(m) -> str:
    return fmt_can_id(m.frame_id, m.is_extended_frame)


def msg_row(m) -> dict:
    return {"id": hex_id(m), "name": m.name, "dlc": m.length, "extended": m.is_extended_frame, "fd": m.is_fd,
            "cycle_time_ms": m.cycle_time, "signals": len(m.signals), "multiplexed": m.is_multiplexed(),
            "senders": list(m.senders or [])}


def sig_row(s) -> dict:
    mux = ""
    if s.is_multiplexer:
        mux = "M"
    if s.multiplexer_ids:
        ids = s.multiplexer_ids
        mux += f"m{ids[0]}" if len(ids) == 1 else f"m[{','.join(map(str, ids[:6]))}{',...' if len(ids) > 6 else ''}]"
        if s.multiplexer_signal:
            mux += f" ({s.multiplexer_signal})"
    choices = {int(k): str(v) for k, v in (s.choices or {}).items()}
    return {"name": s.name, "start": s.start, "length": s.length,
            "byte_order": "little" if s.byte_order == "little_endian" else "big",
            "signed": s.is_signed, "float": s.is_float, "factor": s.scale, "offset": s.offset,
            "min": s.minimum, "max": s.maximum, "unit": s.unit or "", "mux": mux,
            "value_table": choices, "comment": s.comment or ""}


def find_message(db, key: str):
    for m in db.messages:
        if m.name == key:
            return m
    try:
        v = int(key, 0)
    except ValueError:
        low = key.lower()
        hits = [m for m in db.messages if low in m.name.lower()]
        return hits[0] if len(hits) == 1 else None
    v29 = v & 0x1FFFFFFF if v & 0x80000000 else v
    hits = [m for m in db.messages if m.frame_id == v29]
    return hits[0] if hits else None


def fmt(v) -> str:
    if v is None:
        return "-"
    if isinstance(v, float):
        return f"{v:.6g}"
    return str(v)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dbc", type=Path)
    ap.add_argument("--message", help="message name or CAN ID (hex 0x.. or decimal; DBC IDs with bit 31 also work)")
    ap.add_argument("--find", help="search signal names, units and comments")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args()
    if not args.dbc.is_file():
        print(f"Not found: {args.dbc}", file=sys.stderr)
        return 2
    try:
        db = load(args.dbc)
    except Exception as e:  # noqa: BLE001
        print(f"cantools cannot load the file: {e}\nRun dbc_lint.py for line-level details.", file=sys.stderr)
        return 2
    msgs = [m for m in db.messages if m.name != "VECTOR__INDEPENDENT_SIG_MSG"]

    if args.message:
        m = find_message(db, args.message)
        if m is None:
            print(f"No (unique) message matches {args.message!r}", file=sys.stderr)
            return 1
        out = {"message": msg_row(m), "comment": m.comment or "", "signals": [sig_row(s) for s in m.signals]}
        if args.json:
            print(json.dumps(out, indent=1, default=str))
            return 0
        r = out["message"]
        print(f"{r['id']} {r['name']}  DLC {r['dlc']}{'  extended' if r['extended'] else ''}{'  CAN FD' if r['fd'] else ''}"
              f"  cycle {fmt(r['cycle_time_ms'])} ms  senders {','.join(r['senders']) or '-'}")
        if out["comment"]:
            print(f"  comment: {out['comment'][:200]}")
        hdr = f"  {'signal':<32} {'start':>5} {'len':>3} {'order':<6} {'s/f':<3} {'factor':>10} {'offset':>10} {'min':>10} {'max':>10} {'unit':<8} mux"
        print(hdr)
        for s in out["signals"]:
            sf = ("f" if s["float"] else "") + ("s" if s["signed"] else "u")
            print(f"  {s['name']:<32} {s['start']:>5} {s['length']:>3} {s['byte_order']:<6} {sf:<3} {fmt(s['factor']):>10} "
                  f"{fmt(s['offset']):>10} {fmt(s['min']):>10} {fmt(s['max']):>10} {s['unit'][:8]:<8} {s['mux']}")
            if s["value_table"]:
                items = list(s["value_table"].items())
                vt = ", ".join(f"{k}={v}" for k, v in items[:8]) + (f", ... ({len(items)} values)" if len(items) > 8 else "")
                print(f"  {'':<32} values: {vt}")
        return 0

    if args.find:
        q = args.find.lower()
        hits = []
        for m in msgs:
            for s in m.signals:
                if q in s.name.lower() or q in (s.unit or "").lower() or q in (s.comment or "").lower():
                    hits.append({"message": m.name, "id": hex_id(m), **sig_row(s)})
        if args.json:
            print(json.dumps(hits, indent=1, default=str))
        else:
            print(f"{len(hits)} signal(s) matching {args.find!r}")
            for h in hits[:200]:
                print(f"  {h['id']:<11} {h['message']:<32} {h['name']:<32} {h['length']:>2}b x{fmt(h['factor'])} "
                      f"+{fmt(h['offset'])} [{fmt(h['min'])}|{fmt(h['max'])}] {h['unit']}")
        return 0

    rows = [msg_row(m) for m in sorted(msgs, key=lambda m: (m.is_extended_frame, m.frame_id))]
    nodes = [n.name for n in db.nodes]
    totals = {"messages": len(rows), "signals": sum(r["signals"] for r in rows),
              "extended": sum(r["extended"] for r in rows), "fd": sum(r["fd"] for r in rows),
              "multiplexed": sum(r["multiplexed"] for r in rows),
              "with_cycle_time": sum(1 for r in rows if r["cycle_time_ms"])}
    if args.json:
        print(json.dumps({"file": str(args.dbc), "totals": totals, "nodes": nodes, "messages": rows}, indent=1))
        return 0
    print(f"{args.dbc}: {totals['messages']} messages ({totals['extended']} extended, {totals['fd']} CAN FD, "
          f"{totals['multiplexed']} multiplexed), {totals['signals']} signals, {totals['with_cycle_time']} with cycle time")
    print(f"Nodes: {', '.join(nodes) if nodes else '-'}")
    print(f"  {'ID':<11} {'name':<36} {'DLC':>4} {'flags':<5} {'cycle':>6} {'sigs':>4}  senders")
    for r in rows:
        flags = ("X" if r["extended"] else "") + ("FD" if r["fd"] else "") + ("M" if r["multiplexed"] else "")
        print(f"  {r['id']:<11} {r['name'][:36]:<36} {r['dlc']:>4} {flags:<5} {fmt(r['cycle_time_ms']):>6} {r['signals']:>4}  "
              f"{','.join(r['senders'])[:30]}")
    print("Flags: X = extended (29-bit), FD = CAN FD, M = multiplexed. Details: --message <name|id>")
    return 0


if __name__ == "__main__":
    sys.exit(main())
