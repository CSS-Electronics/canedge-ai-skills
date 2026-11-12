"""Find control signal candidates without a DBC: Bytes/bits whose value separates busy from idle bus periods.

For a user who wants to stop logging while e.g. the vehicle is parked, but has no DBC with a suitable signal:
  1. Every second of the log file is labelled busy or idle by its CAN frames/s (CAN1/CAN2): --busy-fps, or the
     Otsu split of the frames/s values (two clusters). Without a clear split there is nothing to separate
  2. IDs sent in most busy AND most idle seconds are candidates (a trigger must be on the bus in both states)
  3. Each data byte (unsigned, 8-bit) and each bit is scored by the best threshold "busy if value >= t" (or the
     inverse): Balanced accuracy over busy and idle frames (1.0 = the value alone tells busy from idle)
  4. The best candidates are printed with ready-to-use control fields (Intel byte order, factor 1, offset 0 =
     the raw byte/bit value); verify them with simulate_config.py on the same and another log file

Usage (from the repo root):
  trigger_candidates.py <log file> [--busy-fps 800] [--top 10] [--json] [--tool <mdf2csv>] [--passwords ...]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

sys.path[:0] = [str(Path(__file__).resolve().parent), str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts")]
from simulate_config import read_frames  # noqa: E402
from tools import passwords_refusal, resolve  # noqa: E402

CHN = {"CAN9": 0, "CAN1": 1, "CAN2": 2}


def otsu(x: np.ndarray) -> tuple[float, float]:
    """(threshold, separation 0..1) of the best two-cluster split of x."""
    v = np.sort(x)
    best, thr = -1.0, float(v[0])
    var = v.var() or 1.0
    for k in range(1, len(v)):
        if v[k] == v[k - 1]:
            continue
        a, b = v[:k], v[k:]
        between = len(a) * len(b) / len(v) ** 2 * (a.mean() - b.mean()) ** 2
        if between > best:
            best, thr = between, (v[k - 1] + v[k]) / 2
    return thr, best / var


def best_split(busy: np.ndarray, idle: np.ndarray, nvals: int) -> tuple[float, int, bool]:
    """(balanced accuracy, threshold t, busy_high) for 'busy if value >= t' (busy_high) or 'busy if value < t'."""
    hb = np.bincount(busy, minlength=nvals) / max(len(busy), 1)
    hi = np.bincount(idle, minlength=nvals) / max(len(idle), 1)
    ge_b = np.cumsum(hb[::-1])[::-1]  # share of busy frames with value >= t
    lt_i = np.concatenate([[0], np.cumsum(hi)[:-1]])  # share of idle frames with value < t
    acc_hi = (ge_b + lt_i) / 2  # busy high
    acc_lo = 1 - acc_hi  # busy low (complement)
    t_hi, t_lo = int(np.argmax(acc_hi[1:]) + 1), int(np.argmax(acc_lo[1:]) + 1)
    return (float(acc_hi[t_hi]), t_hi, True) if acc_hi[t_hi] >= acc_lo[t_lo] else (float(acc_lo[t_lo]), t_lo, False)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("log", type=Path, help="One log file with both busy and idle periods")
    ap.add_argument("--busy-fps", type=float, help="Frames/s (CAN1+CAN2) from which a second counts as busy (default: Otsu)")
    ap.add_argument("--top", type=int, default=10, help="Candidates to list (default 10)")
    ap.add_argument("--json", action="store_true", help="Print JSON for the agent")
    ap.add_argument("--tool", help="mdf2csv path (default: canedge-mdf2csv/bin, or CANEDGE_MDF2CSV)")
    ap.add_argument("--passwords", help="passwords.json for encrypted files (MFE/MFM)")
    a = ap.parse_args()

    refusal = passwords_refusal([a.log], a.passwords)
    if refusal:
        sys.exit(refusal)
    F = read_frames(a.log, resolve("mdf2csv", a.tool), a.passwords)
    sec = ((F["ts"] - F["ts"].min()) // 1000).astype(int)
    bus = np.isin(F["chlabel"], ["CAN1", "CAN2"])
    fps = np.bincount(sec[bus], minlength=sec.max() + 1).astype(float)
    if a.busy_fps is not None:
        thr, sep = a.busy_fps, None
    else:
        thr, sep = otsu(fps)
    busy_sec = fps >= thr
    nb, ni = int(busy_sec.sum()), int((~busy_sec).sum())
    head = {"seconds": len(fps), "busy_seconds": nb, "idle_seconds": ni, "busy_fps_threshold": round(float(thr), 1),
            "busy_fps_median": round(float(np.median(fps[busy_sec])), 1) if nb else None,
            "idle_fps_median": round(float(np.median(fps[~busy_sec])), 1) if ni else None,
            "split_quality": round(sep, 2) if sep is not None else None}
    if nb < 10 or ni < 10:
        print(json.dumps({**head, "candidates": [], "note": "No clear busy/idle periods in this file (need >= 10 s of each)"}, indent=1))
        return 1
    fb = busy_sec[sec]
    out = []
    for (ch, ide, cid), idx in zip(F["gkeys"], F["gidx"]):
        if ch not in CHN:
            continue
        s = sec[idx]
        cover_b = len(np.unique(s[busy_sec[s]])) / nb
        cover_i = len(np.unique(s[~busy_sec[s]])) / ni
        if cover_b < 0.5 or cover_i < 0.5:
            continue  # a trigger must be on the bus in most busy and most idle seconds
        hexes = F["data"][idx]
        n = min(len(h) for h in hexes) // 2
        if not n:
            continue
        raw = np.frombuffer(bytes.fromhex("".join(h[:2 * n] for h in hexes)), dtype=np.uint8).reshape(len(hexes), n)
        isb = fb[idx]
        for b in range(n):
            v = raw[:, b].astype(np.int64)
            acc, t, high = best_split(v[isb], v[~isb], 256)
            cand = [(acc, "byte", b * 8, 8, t, high, 255)]
            for k in range(8):
                bit = (v >> k) & 1
                acc_b, t_b, high_b = best_split(bit[isb], bit[~isb], 2)
                cand.append((acc_b, "bit", b * 8 + k, 1, t_b, high_b, 1))
            for acc, kind, bitpos, length, t, high, vmax in cand:
                if acc < 0.8 or (kind == "byte" and len(np.unique(v)) <= 2):
                    continue  # weak, or a byte that is really a bit (listed as bit)
                sv = v if kind == "byte" else (v >> (bitpos % 8)) & 1
                # Switches the trigger would cause (in the frame sequence): Close to the busy/idle switches = clean
                switches = int(np.count_nonzero(np.diff((sv >= t) == high)))
                start, stop = ([t, vmax], [0, t - 1]) if high else ([0, t - 1], [t, vmax])
                out.append({"channel": ch, "id": f"0x{cid:08X}" if ide else f"0x{cid:03X}", "kind": kind, "byte": bitpos // 8,
                            "score": round(acc, 3), "switches": switches,
                            "rule": f"busy when {'>=' if high else '<'} {t}" if kind == "byte" else f"busy when {1 if (high and t == 1) or (not high and t == 0) else 0}",
                            "control": {"message": {"chn": CHN[ch], "id_format": ide, "id": f"{cid:X}",
                                                    "id_mask": "1FFFFFFF" if ide else "7FF"},
                                        "signal": {"type": 0, "byteorder": 1, "bitpos": bitpos, "length": length,
                                                   "factor": 1, "offset": 0},
                                        "start": start, "stop": stop}})
    # Best first (score in steps of 0.01, then fewest switches); per ID + byte keep the best entry (byte or bit)
    out.sort(key=lambda c: (-round(c["score"], 2), c["switches"], c["kind"] != "bit"))
    seen, best = set(), []
    for c in out:
        k = (c["channel"], c["id"], c["byte"])
        if k not in seen:
            seen.add(k)
            best.append(c)
    res = {**head, "candidates": best[: a.top]}
    if a.json:
        print(json.dumps(res, indent=1))
        return 0
    print(f"{a.log.name}: {nb} busy / {ni} idle seconds (busy >= {head['busy_fps_threshold']} frames/s; medians "
          f"{head['busy_fps_median']} vs {head['idle_fps_median']}{'' if sep is None else f', split quality {sep:.2f}'})")
    if not best:
        print("No byte or bit separates busy from idle (score >= 0.8). Use another signal source (e.g. GNSS speed)")
        return 1
    trans = int(np.count_nonzero(np.diff(busy_sec.astype(int))))
    print(f"Busy/idle switches (per second): {trans}. A clean trigger has a similar number of switches")
    print(f"{'score':>6} {'switches':>8} {'ch':5} {'id':11} {'signal':22} {'rule':16} start / stop range")
    for c in best[: a.top]:
        s = c["control"]["signal"]
        print(f"{c['score']:>6} {c['switches']:>8} {c['channel']:5} {c['id']:11} {c['kind'] + ' bitpos ' + str(s['bitpos']) + ' len ' + str(s['length']):22} "
              f"{c['rule']:16} {c['control']['start']} / {c['control']['stop']}")
    print("Control fields (Intel, factor 1, offset 0 = raw value) are in --json. Verify with simulate_config.py")
    return 0


if __name__ == "__main__":
    sys.exit(main())
