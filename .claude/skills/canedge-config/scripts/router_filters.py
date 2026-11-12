"""Write CANmod.router S1-S4 filters from a keep list: Exact IDs, J1939 PGNs or the messages of a DBC, each with an
optional prescaler. Router filters are accept-only ID + mask filters (max 32 per channel, first match wins), so
hand-written ones easily drop or shadow IDs: Use this script, then simulate_config.py + check_config.py.

Usage (from the repo root):
  router_filters.py --config <project>/config/router-original/config-01.02.json
                    --out <project>/config/router-<variant>/config-01.02.json
                    --set S1 "103:time=100,104:data,18BD0506,pgn:FEF1:time=1000"
                    --set S2 "dbc:<project>/dbc/can12-engine.dbc"  --set S3 all  --set S4 none
                    [--mode replace|top|bottom]

--set <S1-S4 or CAN11-14> <items>, comma-separated items:
  <ID>[:<prescaler>]        exact ID, hex (3 digits / <= 7FF = 11-bit, else 29-bit), e.g. 103, 18FEF100
  pgn:<PGN>[:<prescaler>]   J1939 PGN mask (any source address; PDU1: any destination), e.g. pgn:FEF1
  mask:<ID>/<MASK>[:<p>]    ID + mask, e.g. mask:100/7F0 = 0x100-0x10F (11-bit if both <= 7FF, else 29-bit)
  dbc:<file>[:<prescaler>]  every message of the DBC (J1939 DBC: by PGN; others: exact IDs)
  all                       accept all 11-bit + 29-bit IDs (the default filters)
  none                      no enabled filter = nothing forwarded from this channel (asks for confirmation: --yes)
Prescaler: time=<ms> | count=<N> (1/N) | data[=<hex byte mask>] (data: log on change; mask bit i = data byte i)
CAN11-14 = S1-S4 assume the default tunnel 0x10 (the config's tunnel ID decides: 0x12 = CAN15-18).

--mode: replace (default) = the channel's filters become exactly the keep list; top / bottom = add them before /
after the existing filters (duplicates removed; note: an earlier accept-all filter shadows filters added below it).
Channels without --set stay as they are. The input config is never modified.
"""
from __future__ import annotations

import argparse
import copy
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cfg_common import (  # noqa: E402
    ROUTER_MAX_FILTERS, ROUTER_SECTIONS, g, is_router, load_config, router_channel_map, router_filters,
)
from dbc_groups import pgn_of, protocol_type  # noqa: E402  (canedge-workspace, via cfg_common)

ALL = [{"name": "AllStandardID", "state": 1, "id_format": 0, "frame_format": 2, "f1": "7FF", "f2": "0", "prescaler_type": 0},
       {"name": "AllExtendedID", "state": 1, "id_format": 1, "frame_format": 2, "f1": "1FFFFFFF", "f2": "0",
        "prescaler_type": 0}]


def prescaler(tokens: list[str]) -> dict:
    if not tokens or not tokens[0]:
        return {"prescaler_type": 0}
    k, _, v = tokens[0].partition("=")
    k = k.lower()
    if k == "time":
        n = int(v)
        if not 1 <= n <= 4194304:
            raise ValueError(f"time prescaler {n} ms outside 1-4194304")
        return {"prescaler_type": 2, "prescaler_value": n}
    if k == "count":
        n = int(v)
        if not 1 <= n <= 256:
            raise ValueError(f"count prescaler {n} outside 1-256")
        return {"prescaler_type": 1, "prescaler_value": n}
    if k == "data":
        if v and not re.fullmatch(r"[0-9A-Fa-f]{1,16}", v):
            raise ValueError(f"data mask {v!r}: up to 16 hex digits (bit i = data byte i)")
        return {"prescaler_type": 3, "prescaler_value": v.upper()}
    raise ValueError(f"unknown prescaler {tokens[0]!r}: time=<ms>, count=<N> or data[=<mask>]")


def exact(can_id: int, ext: bool, name: str, pre: dict) -> dict:
    return {"name": name[:16], "state": 1, "id_format": int(ext), "frame_format": 2,
            "f1": f"{can_id:X}", "f2": "1FFFFFFF" if ext else "7FF", **pre}


def pgn_filter(pgn: int, name: str, pre: dict) -> dict:
    """Mask filter for a J1939 PGN (the config editor's filter builder): PDU1 ignores the destination address."""
    pdu1 = ((pgn >> 8) & 0xFF) < 240
    f1, f2 = ((pgn & 0x3FF00) << 8, 0x3FF0000) if pdu1 else (pgn << 8, 0x3FFFF00)
    return {"name": name[:16], "state": 1, "id_format": 1, "frame_format": 2, "f1": f"{f1:X}", "f2": f"{f2:X}", **pre}


def dbc_filters(path: Path, pre: dict) -> list[dict]:
    text = path.read_text(encoding="latin-1")
    j1939 = protocol_type(text).upper() == "J1939"
    out, pgns = [], set()
    for m in re.finditer(r"^BO_\s+(\d+)\s+(\w+)\s*:", text, re.M):
        raw, name = int(m.group(1)), m.group(2)
        if raw == 0xC0000000:  # VECTOR__INDEPENDENT_SIG_MSG
            continue
        ext, fid = bool(raw & 0x80000000), raw & 0x1FFFFFFF
        if ext and j1939:
            if pgn_of(fid) not in pgns:
                pgns.add(pgn_of(fid))
                out.append(pgn_filter(pgn_of(fid), name, pre))
        else:
            out.append(exact(fid, ext, name, pre))
    if not out:
        raise ValueError(f"{path}: no messages")
    return out


def parse_items(items: str) -> list[dict] | None:
    """Filters of one --set value; None = 'none' (no enabled filter)."""
    out = []
    for tok in [t.strip() for t in items.split(",") if t.strip()]:
        parts = tok.split(":")
        head = parts[0].lower()
        if head == "all":
            out += copy.deepcopy(ALL)
        elif head == "none":
            return None
        elif head == "mask":
            fid, _, msk = parts[1].partition("/")
            f1, f2 = int(fid, 16), int(msk, 16)
            ext = f1 > 0x7FF or f2 > 0x7FF or len(fid) > 3
            out.append({"name": f"{f1:X}/{f2:X}"[:16], "state": 1, "id_format": int(ext), "frame_format": 2,
                        "f1": f"{f1:X}", "f2": f"{f2:X}", **prescaler(parts[2:])})
        elif head == "pgn":
            pgn = int(parts[1], 16)
            out.append(pgn_filter(pgn, f"PGN {pgn:X}", prescaler(parts[2:])))
        elif head == "dbc":
            # Windows paths contain ':' (C:/...): the prescaler is the part after the last ':' if it looks like one
            rest = tok[4:]
            m = re.match(r"^(.*?):((?:time|count|data)(?:=[0-9A-Fa-f]*)?)$", rest, re.I)
            path, pre = (m.group(1), [m.group(2)]) if m else (rest, [])
            out += dbc_filters(Path(path), prescaler(pre))
        else:
            sid = head.removeprefix("0x")
            can_id = int(sid, 16)
            ext = len(sid) > 3 or can_id > 0x7FF
            out.append(exact(can_id, ext, f"0x{can_id:X}", prescaler(parts[1:])))
    return out


def same(a: dict, b: dict) -> bool:
    keys = ("state", "id_format", "frame_format", "f1", "f2", "prescaler_type", "prescaler_value")
    return all(str(a.get(k, "")).upper() == str(b.get(k, "")).upper() for k in keys)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", type=Path, required=True, help="CANmod.router config to start from (not modified)")
    ap.add_argument("--out", type=Path, required=True, help="New config file (config-VV.VV.json in its own folder)")
    ap.add_argument("--set", nargs=2, action="append", required=True, metavar=("CHANNEL", "ITEMS"),
                    help="S1-S4 (or CAN11-14 / the demuxed channels) and the comma-separated keep items")
    ap.add_argument("--mode", choices=("replace", "top", "bottom"), default="replace")
    ap.add_argument("--yes", action="store_true", help="Allow 'none' (a channel that forwards nothing)")
    a = ap.parse_args()

    cfg = load_config(a.config)[0]
    if not is_router(cfg):
        sys.exit(f"{a.config} is not a CANmod.router config (no phy.can.route)")
    if a.out.resolve() == a.config.resolve():
        sys.exit("--out must be a new file: The input config is never modified")
    chmap = router_channel_map(cfg) or {}
    new = copy.deepcopy(cfg)
    report = []
    for ch, items in a.set:
        c = ch.upper()
        sec = f"can_s{c[1:]}" if re.fullmatch(r"S[1-4]", c) else chmap.get(c)
        if sec not in ROUTER_SECTIONS:
            sys.exit(f"Unknown channel {ch}: Use S1-S4" + (f" or {', '.join(chmap)}" if chmap else ""))
        try:
            flt = parse_items(items)
        except (ValueError, IndexError, OSError) as e:
            sys.exit(f"--set {ch}: {e}")
        old = g(new, "phy", sec, "filter") or []
        s = sec.replace("can_s", "S")
        if flt is None:
            if not a.yes:
                sys.exit(f"--set {ch} none: {s} would forward nothing. Confirm with the user, then add --yes")
            res = [dict(f, state=0) for f in old] or [dict(ALL[0], state=0)]   # schema: min 1 filter
        elif a.mode == "replace":
            res = flt
        else:
            res = flt + old if a.mode == "top" else old + flt
        dedup = []
        for f in res:
            if not any(same(f, d) for d in dedup):
                dedup.append(f)
        if len(dedup) > ROUTER_MAX_FILTERS:
            sys.exit(f"{s}: {len(dedup)} filters, the router allows {ROUTER_MAX_FILTERS}. Group IDs: J1939 by PGN "
                     f"(pgn:), neighbouring IDs with one mask filter, or keep accept-all ('all') and prescale only the "
                     f"largest IDs first")
        new["phy"][sec]["filter"] = dedup
        en = router_filters(new, sec)
        report.append(f"{s} ({sec}): {len(dedup)} filters ({len(en)} enabled)" +
                      (f", {len(res) - len(dedup)} duplicates removed" if len(res) > len(dedup) else ""))
    a.out.parent.mkdir(parents=True, exist_ok=True)
    a.out.write_text(json.dumps(new, indent=2), encoding="utf-8")
    for line in report:
        print(line)
    print(f"Wrote {a.out}. Next: simulate_config.py <log> --config {a.out} --baseline {a.config} --project <p> --html "
          f"..., then check_config.py {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
