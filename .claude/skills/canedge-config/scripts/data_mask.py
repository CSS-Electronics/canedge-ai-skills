"""Data prescaler mask for "log this message only when signal(s) X change": The bytes the signals cover, the mask hex,
the other signals in those bytes (they trigger the prescaler too) and a ready filter line.

The device's data prescaler works per byte (CANedge Docs "Message prescaling"): Mask bit i = data byte i (rightmost bit
= byte 0), e.g. 0B = bytes 0, 1 and 3; empty = all bytes. It cannot watch single bits, so every other signal in a
masked byte also triggers a logged frame (a counter or checksum there = the prescaler logs almost every frame).

Usage (from the repo root):
  data_mask.py --dbc <file.dbc> --message <name or ID (hex, e.g. 0x0CF00400)> --signals <a,b> [--json]
Then confirm the effect with simulate_config.py --html (Explore tab: "Watch signals" shows the same bytes).
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path[:0] = [str(HERE), str(HERE.parents[1] / "canedge-workspace" / "scripts")]
from cfg_common import j1939_pgn, signal_bytes  # noqa: E402
from dbc_groups import fmt_can_id, load_dbc, message_layouts  # noqa: E402


def mask_hex(byts: list[int]) -> str:
    """Mask hex as in the config (bit i = byte i), uppercase, even length (same as the Explore tab)."""
    h = format(sum(1 << b for b in set(byts)), "X")
    return h.zfill(len(h) + len(h) % 2)


def byte_text(byts: list[int]) -> str:
    """'byte 3' or 'bytes 3-4, 7'."""
    return f"byte{'s' if len(set(byts)) > 1 else ''} {ranges(byts)}"


def ranges(byts: list[int]) -> str:
    out, run = [], []
    for b in sorted(set(byts)):
        if run and b == run[-1] + 1:
            run.append(b)
        else:
            if run:
                out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
            run = [b]
    if run:
        out.append(f"{run[0]}-{run[-1]}" if len(run) > 1 else str(run[0]))
    return ", ".join(out)


def find_message(db, key: str):
    k = key.strip()
    for m in db.messages:
        if m.name.lower() == k.lower():
            return m
    try:
        fid = int(k, 16)   # CAN IDs as hex, with or without 0x
    except ValueError:
        return None
    return next((m for m in db.messages if m.frame_id == fid), None)


def filter_line(m, j1939: bool, mask: str) -> dict:
    """Acceptance filter for this one message with the data prescaler (J1939: PGN mask, any source address)."""
    ext = m.is_extended_frame
    if j1939:
        pgn = j1939_pgn(m.frame_id)
        pdu2 = (pgn >> 8) & 0xFF >= 0xF0
        f1, f2, meth = format(pgn << 8, "X"), ("3FFFF00" if pdu2 else "3FF0000"), 1
    else:
        f1 = f2 = format(m.frame_id, "X")
        meth = 0
    return {"name": m.name[:16], "state": 1, "type": 0, "id_format": int(ext), "method": meth, "f1": f1, "f2": f2,
            "prescaler_type": 3, "prescaler_value": mask}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dbc", required=True, type=Path, help="DBC file with the message")
    ap.add_argument("--message", required=True, help="Message name or CAN ID (hex)")
    ap.add_argument("--signals", required=True, help="Comma-separated signal names whose changes should be logged")
    ap.add_argument("--json", action="store_true", help="Print JSON for the agent")
    args = ap.parse_args()

    db = load_dbc(args.dbc)
    m = find_message(db, args.message)
    if m is None:
        sys.exit(f"No message '{args.message}' in {args.dbc.name}")
    by_name = {s.name.lower(): s for s in m.signals}
    want = [w.strip() for w in args.signals.split(",") if w.strip()]
    missing = [w for w in want if w.lower() not in by_name]
    if missing:
        sys.exit(f"Not in {m.name}: {', '.join(missing)}. Signals: {', '.join(s.name for s in m.signals)}")
    span = lambda s: signal_bytes(s.start, s.length, 1 if s.byte_order == "little_endian" else 0)  # noqa: E731
    picked = [by_name[w.lower()] for w in want]
    byts = sorted({b for s in picked for b in span(s)})
    mask = mask_hex(byts)
    others = [{"signal": s.name, "bytes": sorted(set(span(s)) & set(byts)), "multiplexed": s.multiplexer_ids is not None}
              for s in m.signals if s not in picked and set(span(s)) & set(byts)]
    j1939 = bool(message_layouts(args.dbc).get((m.is_extended_frame, m.frame_id), {}).get("j1939"))
    out = {"message": m.name, "id": fmt_can_id(m.frame_id, m.is_extended_frame), "length": m.length,
           "fd": m.length > 8, "j1939_pgn": f"{j1939_pgn(m.frame_id):04X}" if j1939 else None,
           "signals": [{"signal": s.name, "bytes": span(s)} for s in picked], "bytes": byts, "mask": mask,
           "also_trigger": others, "filter": filter_line(m, j1939, mask)}
    if args.json:
        print(json.dumps(out, indent=1))
        return 0
    pgn = f", J1939 PGN {out['j1939_pgn']}" if j1939 else ""
    print(f"Message {m.name} ({out['id']}, {m.length} bytes{', CAN FD' if out['fd'] else ''}{pgn}) in {args.dbc.name}")
    for s in out["signals"]:
        print(f"  Watch {s['signal']}: {byte_text(s['bytes'])}")
    print(f"Mask: {mask} ({byte_text(byts)}; bit i = byte i)")
    if others:
        print("Also in these bytes, so they trigger a logged frame too: "
              + ", ".join(f"{o['signal']} ({byte_text(o['bytes'])})" for o in others))
        print("  A counter or checksum among them = the prescaler logs almost every frame: Check the changes per "
              "signal in the Explore tab")
    else:
        print("No other signals in these bytes")
    print(f"Filter (above broader filters; the config editor's filter builder uses the same fields):\n  {json.dumps(out['filter'])}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
