"""DBC coverage on real log files: Which DBC messages are on the bus, and why some of them decode to nothing.

For each device + channel it compares the DBC messages with the raw frames (IDs + data lengths from the per-file raw
stats cache of canedge-raw-overview, data/raw-stats/; missing stats are computed with its raw_stats.py, which has
its own size guard). No DBC decoding is needed. It reports every DBC message that IS on the bus but gives no or
partial output in the CSS decoders. What counts is whether the SIGNALS fit in the frames, not the DBC length (a
DBC length > the frame length alone decodes fine):

  NOT DECODED  no signal fits in the (shortest) frame
  AT RISK      some signals end beyond the shortest frame: The decoder drops those signals, or the whole message
               (both seen with decoder 25.12.15); with mixed frame lengths ("mixed_lengths") it skips the short
               frames and can write corrupt Parquet files: Split the message (decoder-technical.md section 4)
  NO SIGNALS   message without signals (no output by design)

plus counts of DBC messages not on the bus and bus IDs no DBC matches (top IDs by frames). Matching as the decoder
does it: Exact 11/29-bit ID, or the PGN for 29-bit IDs with ProtocolType J1939. Multi-frame messages (DBC length > 8
on classic frames, J1939 TP / ISO-TP) are not length-checked.

Usage:
  dbc_coverage.py --log <files/folders> --project projects/<name>     DBCs per device from the project (dbc-groups.json)
  dbc_coverage.py --log <files/folders> --dbc can1-x.dbc can2-y.dbc    loose DBCs (channel from the can1-/can2- prefix,
                                                                       no prefix = CAN1 + CAN2), applied to every device
  Options: --max-gb (raw stats size guard, default 1), --yes, --top 5, --json

It only reads the per-file raw stats (no decoding): If they are cached (canedge-raw-overview ran), pass the whole
data folder; that takes seconds and also finds IDs and frame lengths that occur in only some sessions (e.g. a
message that switches from 6 to 8 bytes). Otherwise pick a few files with typical traffic (e.g. one per session):
Uncached files are converted first (size guard --max-gb). CANmod.router: With the router DBC among the DBCs the raw
stats are demuxed (CAN11-14, ...), so can11- DBCs are checked against the router channels.
Exit codes: 0 no issues, 1 messages on the bus that do not (fully) decode, 2 usage error, 3 raw stats size guard.
"""
from __future__ import annotations

import argparse
import json
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
sys.path.insert(0, str(HERE.parents[2] / "canedge-workspace" / "scripts"))
sys.path.insert(0, str(HERE.parents[2] / "canedge-raw-overview" / "scripts"))
from dbc_groups import by_channel, dbcs_for_device, fmt_can_id, light_layouts, pgn_of  # noqa: E402
from mf4_files import describe, find_log_files  # noqa: E402
import muxtp  # noqa: E402
from tools import password_file, passwords_refusal  # noqa: E402

RAW_STATS = HERE.parents[2] / "canedge-raw-overview" / "scripts" / "raw_stats.py"
FD_LEN = {9: 12, 10: 16, 11: 20, 12: 24, 13: 32, 14: 48, 15: 64}


def data_len(dlc: int, fd: bool) -> int:
    return FD_LEN.get(dlc, 64) if fd and dlc > 8 else min(dlc, 8)


def stats_file(meta: dict) -> Path:
    from raw_stats import stats_path  # heavy import (pandas), only when needed
    return stats_path(meta)


def bus_ids(metas: list[dict]) -> dict[str, dict[str, dict[tuple[bool, int], dict]]]:
    """device -> channel -> (extended, id) -> {frames, lengths} from the cached raw stats."""
    out: dict = {}
    for m in metas:
        p = stats_file(m)
        if not p.exists():
            continue
        for r in json.loads(p.read_text(encoding="utf-8")).get("ids", []):
            key = (bool(r.get("ide")), int(r["id"], 16))
            e = out.setdefault(m["device"], {}).setdefault(r["channel"], {}).setdefault(key, {"frames": 0, "lengths": set()})
            e["frames"] += r["frames"]
            e["lengths"] |= {data_len(d, bool(r.get("fd"))) for d in r.get("dlc", [])}
    return out


def dbc_messages(paths: list[Path]) -> dict[tuple[bool, int], dict]:
    msgs = {}
    for p in paths:
        for key, d in light_layouts(p.read_text(encoding="latin-1")).items():
            if d["name"].startswith("VECTOR__INDEPENDENT"):  # pseudo message 0xC0000000
                continue
            msgs[key] = {"name": d["name"], "length": d["layout"][0], "signals": len(d["columns"]), "j1939": d["j1939"],
                         "dbc": p.name, "bytes_needed": {s[0]: last_byte(s[1], s[2], s[3]) + 1 for s in d["layout"][1]}}
    return msgs


def last_byte(start: int, length: int, order: str) -> int:
    """Index of the last payload byte a signal touches (DBC start bit; big endian = Motorola, start = MSB)."""
    if order == "little_endian":
        return (start + length - 1) // 8
    msb = (start // 8) * 8 + (7 - start % 8)  # linear (MSB-first) bit index
    return (msb + length - 1) // 8


def check_channel(msgs: dict, bus: dict, top: int) -> dict:
    pgn_idx: dict[int, tuple] = {pgn_of(fid): (ext, fid) for (ext, fid), d in msgs.items() if ext and d["j1939"]}
    matched: dict[tuple, list[tuple]] = {}
    unmatched = []
    for key, b in bus.items():
        ext, fid = key
        k = key if key in msgs else (pgn_idx.get(pgn_of(fid)) if ext else None)
        if k:
            matched.setdefault(k, []).append(key)
        else:
            unmatched.append((key, b["frames"]))
    issues, ok = [], 0
    for k, ids in sorted(matched.items(), key=lambda kv: kv[0][1]):
        d = msgs[k]
        lengths = set().union(*(bus[i]["lengths"] for i in ids))
        frames = sum(bus[i]["frames"] for i in ids)
        row = {"id": fmt_can_id(k[1], k[0]), "message": d["name"], "dbc": d["dbc"], "dbc_length": d["length"],
               "frame_lengths": sorted(lengths), "frames": frames}
        beyond = sorted(s for s, n in d["bytes_needed"].items() if n > min(lengths))
        if d["signals"] == 0:
            issues.append({**row, "status": "NO SIGNALS"})
        elif d["length"] > 8 and max(lengths) <= 8:
            ok += 1  # multi-frame message (TP): reassembled by the decoder, not length-checked
        elif len(beyond) == d["signals"]:
            issues.append({**row, "status": "NOT DECODED", "signals_beyond_frame": beyond})
        elif beyond:
            issues.append({**row, "status": "AT RISK", "signals_beyond_frame": beyond, "mixed_lengths": len(lengths) > 1})
        else:
            ok += 1  # every signal fits in the shortest frame (a longer DBC length alone is fine)
    unmatched.sort(key=lambda x: -x[1])
    return {"dbc_messages": len(msgs), "on_bus": len(matched), "decodable": ok, "issues": issues,
            "dbc_not_on_bus": len(msgs) - len(matched), "bus_ids_without_dbc": len(unmatched),
            "top_bus_ids_without_dbc": [{"id": fmt_can_id(fid, ext), "frames": n} for (ext, fid), n in unmatched[:top]]}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--log", type=Path, nargs="+", required=True, help="Log files/folders (CANedge layout)")
    src = ap.add_mutually_exclusive_group(required=True)
    src.add_argument("--project", type=Path, help="projects/<name>: DBCs per device from dbc/ (+ dbc-groups.json)")
    src.add_argument("--dbc", type=Path, nargs="+", help="Loose DBC files (applied to every device in --log)")
    ap.add_argument("--max-gb", type=float, default=1.0, help="Raw stats size guard for uncached files (default 1)")
    ap.add_argument("--yes", action="store_true", help="Proceed beyond the raw stats size guard (user confirmed)")
    ap.add_argument("--top", type=int, default=5, help="Bus IDs without DBC to list per channel (default 5)")
    ap.add_argument("--passwords", help="passwords.json for encrypted files (MFE/MFM; default <project>/passwords.json)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    inputs = [p for p in a.log if p.exists()]
    files = find_log_files(inputs)
    if not files:
        ap.error("no log files found in --log")
    # Pass the inputs as given (folders, not every file path): Windows caps a command line at ~32k characters
    cmd = [sys.executable, str(RAW_STATS), *map(str, inputs), "--max-gb", str(a.max_gb)] + (["--yes"] if a.yes else [])
    # CANmod.router: Raw stats of the demuxed channels (CAN11+), mapping from the router DBC among the DBCs
    mx = muxtp.for_project(a.project) if a.project else muxtp.from_dbcs([p.resolve() for p in a.dbc])
    cmd += [f"--muxtp={muxtp.spec(e)}" for e in mx]
    pw = password_file(a.passwords, a.project)
    refusal = passwords_refusal(files, pw, a.project)
    if refusal:
        print(refusal)
        return 2
    if pw:
        cmd += ["--passwords", str(pw.resolve())]
    r = subprocess.run(cmd, capture_output=True, text=True)
    if r.returncode == 3:
        print(r.stdout.strip().splitlines()[-1])
        return 3
    if r.returncode not in (0, 1):
        print(r.stdout + r.stderr)
        return 2
    metas = [describe(f) for f in files]
    bus = bus_ids(metas)

    report = {}
    for dev, chans in sorted(bus.items()):
        if a.project:
            mapping, note = dbcs_for_device(a.project, dev)
            if mapping is None:
                report[dev] = {"skipped": note}
                continue
        else:
            mapping = by_channel([p.resolve() for p in a.dbc])
        report[dev] = {ch: check_channel(dbc_messages(mapping[ch]), chans.get(ch, {}), a.top)
                       for ch in sorted(mapping)}

    if a.json:
        print(json.dumps({"files": len(files), "devices": report}, indent=1))
    else:
        print(f"{len(files)} log file(s)")
        for dev, chans in report.items():
            if "skipped" in chans:
                print(f"{dev}: SKIPPED - {chans['skipped']}")
                continue
            for ch, c in chans.items():
                print(f"{dev} {ch}: {c['on_bus']} of {c['dbc_messages']} DBC messages on the bus, {c['decodable']} "
                      f"decodable, {len(c['issues'])} issue(s); {c['bus_ids_without_dbc']} bus IDs without DBC"
                      + (" (top: " + ", ".join(f"{x['id']} {x['frames']:,}" for x in c["top_bus_ids_without_dbc"])
                         + ")" if c["top_bus_ids_without_dbc"] else ""))
                for i in c["issues"]:
                    print(f"  {i['status']:11s} {i['id']:>10} {i['message']} ({i['dbc']}): DBC length {i['dbc_length']}, "
                          f"frames {'/'.join(map(str, i['frame_lengths']))} bytes, {i['frames']:,} frames"
                          + (f"; beyond the frame: {', '.join(i['signals_beyond_frame'])}"
                             if i.get("signals_beyond_frame") else "")
                          + ("\n              MIXED LENGTHS: Corrupt Parquet risk - split the message "
                             "(canedge-mdf2parquet decoder-technical.md section 4)" if i.get("mixed_lengths") else ""))
    bad = any(i["status"] in ("NOT DECODED", "AT RISK") for chans in report.values() if "skipped" not in chans
              for c in chans.values() for i in c["issues"])
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
