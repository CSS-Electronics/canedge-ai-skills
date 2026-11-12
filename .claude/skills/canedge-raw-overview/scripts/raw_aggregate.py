"""Aggregate cached per-file raw stats (data/raw-stats) into one compact stats.json for the agent.

Reads the canedge-raw-stats/1 files written by raw_stats.py (it never converts log files itself) and
merges them per device: files, sessions, size, firmware/hardware/config CRC32 changes, recording spans
+ periods without data, bus activity per minute (silent / active) with trips (active periods), per-channel
totals (frames, frames/s, timeline) and per-ID totals (rate, period, DLC, unique
payloads, storage share, presence). With >1 device it adds a cross-device ID comparison. Schema
canedge-raw-aggregate/2, fields in references/raw-stats.md.

Select the files by device (all cached stats of that device), by log file paths/folders (only the
files found there; missing stats are listed) or by project (project.json sources + devices).

Usage:
  python raw_aggregate.py --device A1B2C3D4 [--device ...] --out TEMP/raw/stats.json
  python raw_aggregate.py <file/folder> [...] --out <path> [--start 2026-01-15T00:00Z --end ...]
  python raw_aggregate.py --project projects/<name>      (default out: projects/<name>/reports/<yyyymmdd>-raw-overview/stats.json)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent))
from raw_stats import STATIC_MAX_CHANGES_PCT, STATS_ROOT, describe, display_id, est_storage, find_log_files, project_paths, stats_path  # noqa: E402
from paths import report_dir  # noqa: E402  (canedge-workspace, on sys.path via raw_stats)

SCHEMA = "canedge-raw-aggregate/2"
def file_name(f: dict) -> str:
    """CANedge-style log file name: <device>/<session>/<split>.<ext>"""
    return f"{f['device']}/{f.get('session') or 0:08d}/{f.get('split') or 0:08d}.{f.get('format') or 'MF4'}"


MIN_RATE_COVER_S = 1.0  # per-minute FPS = frames / max(recorded s, 1): All statistics use the same minutes, < 1 s cannot spike
MAX_INTERVAL_S = 100_000 * 60  # longer spans indicate bad timestamps (same limit as the per-file bins)
def ch_key(c: str) -> tuple:
    """Natural channel order: CAN1, CAN2, ..., CAN9, CAN10, ..., then LIN1, ... (any number of channels)"""
    m = re.match(r"(\D*)(\d+)$", c)
    return (m.group(1), int(m.group(2))) if m else (c, 0)


STATE_NAMES = {0: "silent", 1: "active"}  # per minute: Silent = recording, no frames on any bus; active = frames on a bus


# ---------------------------------------------------------------------------------------------- helpers
def ts(s: str) -> float:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00")).timestamp()


def iso(t: float, ms: bool = False) -> str:
    s = dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")
    return (s[:-3] if ms else s[:-7]) + "Z"


def rnd(x, sig: int = 3):
    """Round to `sig` significant digits (ints stay ints)."""
    if x is None or isinstance(x, int):
        return x
    x = float(x)
    if x == 0 or not np.isfinite(x):
        return 0.0 if x == 0 else None
    return float(f"{x:.{sig}g}")


def weighted_median(values: list[float], weights: list[float]) -> float | None:
    pairs = sorted((v, w) for v, w in zip(values, weights) if v is not None)
    if not pairs:
        return None
    tot = sum(w for _, w in pairs)
    acc = 0.0
    for v, w in pairs:
        acc += w
        if acc >= tot / 2:
            return v
    return pairs[-1][0]


def compact_sessions(sessions: set) -> list | dict:
    s = sorted(x for x in sessions if x is not None)
    return s if len(s) <= 10 else {"count": len(s), "first": s[0], "last": s[-1]}


def has_bins(st: dict) -> bool:
    return any(c.get("bins") for c in st["channels"].values())


def recording_intervals(stats: list[dict]) -> list[tuple[float, float]]:
    """Time the logger was recording: Each file's first..last frame, plus the time between consecutive splits of
    the same session (a new session starts at every power-on, so the logger was on in between, with a silent bus).
    Files without 60 s bins (timestamp jumps) are skipped."""
    ok = [s for s in stats if has_bins(s)]
    iv = [(ts(s["time"]["first_utc"]), ts(s["time"]["last_utc"])) for s in ok]
    order = sorted(ok, key=lambda s: (s["file"].get("session") or -1, s["file"].get("split") or -1))
    for a, b in zip(order, order[1:]):
        fa, fb = a["file"], b["file"]
        if (fa.get("session") is not None and fa["session"] == fb.get("session") and fa.get("split") is not None
                and fb.get("split") == fa["split"] + 1):
            t0, t1 = ts(a["time"]["last_utc"]), ts(b["time"]["first_utc"])
            if 0 < t1 - t0 <= MAX_INTERVAL_S:
                iv.append((t0, t1))
    return iv


def minute_cover(intervals: list[tuple[float, float]]) -> tuple[np.ndarray, np.ndarray]:
    """(minute index = epoch // 60, covered seconds 0-60) for every minute touched by a recording interval."""
    ms, cs = [], []
    for t0, t1 in intervals:
        m0, m1 = int(t0 // 60), int(t1 // 60)
        m = np.arange(m0, m1 + 1)
        c = np.clip(np.minimum(m * 60.0 + 60, t1) - np.maximum(m * 60.0, t0), 0, 60)
        if t1 == t0:
            c[:] = 1.0  # single-frame file
        ms.append(m)
        cs.append(c)
    if not ms:
        return np.zeros(0, dtype=np.int64), np.zeros(0)
    m, c = np.concatenate(ms), np.concatenate(cs)
    minutes, inv = np.unique(m, return_inverse=True)
    cov = np.minimum(np.bincount(inv, weights=c), 60.0)
    keep = cov > 0
    return minutes[keep], cov[keep]


def load_stats(files: list[Path]) -> list[dict]:
    out = []
    for p in files:
        try:
            d = json.loads(p.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            print(f"WARNING: Unreadable stats file {p}", file=sys.stderr)
            continue
        if d.get("schema") == "canedge-raw-stats/1" and d.get("frames_total"):
            d["_stats_path"] = str(p)
            out.append(d)
    return out


def select(paths: list[Path], devices: set[str], project: Path | None, root: Path = STATS_ROOT) -> dict:
    """Return {"stats": [...], "inputs": [...], "missing": [log paths without (current) stats]}."""
    inputs = [str(p) for p in paths]
    devs = set(devices)
    if project:
        pp, pdev = project_paths(project)
        paths = list(paths) + pp
        inputs += [str(p) for p in pp]
        if not devices:
            devs |= set(pdev)  # project device filter applies to its sources
    stats_files: list[Path] = []
    missing: list[str] = []
    if paths:
        for f in find_log_files([p for p in paths if p.exists()]):
            m = describe(f)
            if devs and m["device"] not in devs:
                continue
            sp = stats_path(m, root)
            ok = False
            if sp.exists():
                try:
                    fm = json.loads(sp.read_text(encoding="utf-8"))["file"]
                    ok = fm["size_bytes"] == m["size_bytes"] and abs(fm["mtime"] - m["mtime"]) < 1e-2
                except (OSError, ValueError, KeyError):
                    ok = False
            (stats_files.append(sp) if ok else missing.append(m["path"]))
    else:
        for d in sorted(devs):
            stats_files += sorted((root / d).rglob("*.json"))
            inputs.append(f"{root}/{d}")
    return {"stats": load_stats(sorted(set(stats_files))), "inputs": inputs, "missing": missing}


# ---------------------------------------------------------------------------------------------- aggregation
def merge_spans(spans: list[tuple[float, float]], tol: float) -> list[list[float]]:
    out: list[list[float]] = []
    for s, e in sorted(spans):
        if out and s - out[-1][1] <= tol:
            out[-1][1] = max(out[-1][1], e)
        else:
            out.append([s, e])
    return out


def header_value(f: dict, key: str) -> str:
    """A header field for display: "unknown" if the header is not readable (MFC/MFE/MFM), "(not set)" for an empty meta."""
    v = f.get(key)
    if v:
        return v
    if f.get("header") == "unsupported":
        return "unknown"
    return "(not set)" if key == "meta" else "unknown"


def value_history(stats: list[dict], key: str) -> dict:
    vals: dict = {}
    for st in sorted(stats, key=lambda s: s["time"]["first_utc"]):
        v = header_value(st["file"], key)
        h = vals.setdefault(v, {"value": v, "files": 0, "first_utc": st["time"]["first_utc"][:19] + "Z"})
        h["files"] += 1
        h["last_utc"] = st["time"]["last_utc"][:19] + "Z"
    return {"changed": len(vals) > 1, "values": list(vals.values())}


def config_periods(stats: list[dict]) -> list[dict]:
    """Consecutive runs of log files (by first frame) with the same meta name, config CRC32 and firmware. `changed`
    lists what differs from the previous period (a meta change always comes with a config change). Files with an
    unreadable header (MFC/MFE/MFM) form their own periods and never count as a change."""
    out: list[dict] = []
    for st in sorted(stats, key=lambda s: s["time"]["first_utc"]):
        f = st["file"]
        key = (f.get("meta"), f.get("config_crc32"), f.get("firmware")) if f.get("header") != "unsupported" else None
        if out and out[-1]["_key"] == key:
            p = out[-1]
            p["files"] += 1
            p["to_utc"] = max(p["to_utc"], st["time"]["last_utc"][:19] + "Z")
            p["_sess"].add(f.get("session"))
            continue
        prev = next((q["_key"] for q in reversed(out) if q["_key"] is not None), None)
        changed = [] if key is None or prev is None else [n for n, a, b in zip(("meta", "config", "fw"), prev, key) if a != b]
        out.append({"_key": key, "_sess": {f.get("session")}, "from_utc": st["time"]["first_utc"][:19] + "Z",
                    "to_utc": st["time"]["last_utc"][:19] + "Z", "files": 1, "meta": header_value(f, "meta"),
                    "config_crc32": header_value(f, "config_crc32"), "firmware": header_value(f, "firmware"),
                    "changed": changed})
    for p in out:
        s = sorted(x for x in p.pop("_sess") if x is not None)
        p["sessions"] = len(s)  # distinct sessions
        p["session_range"] = (f"{s[0]}" if s[0] == s[-1] else f"{s[0]}-{s[-1]}") if s else None
        p.pop("_key")
    return out


def device_model(stats: list[dict], o: argparse.Namespace) -> dict:
    """The per-minute model of one device, shared by stats.json (aggregate_device) and the dashboard facts
    (facts.py): Recording minutes + covered seconds, frames per minute per channel, activity state per minute
    (0 silent, 1 active; --activity-ignore channels do not count) and the trips (active periods)."""
    stats = sorted(stats, key=lambda s: s["time"]["first_utc"])
    rec_iv = recording_intervals(stats)
    minutes, cov = minute_cover(rec_iv)

    # ---- frames per minute per channel (aligned to the recording minutes)
    chans = sorted({c for s in stats for c in s["channels"]}, key=ch_key)
    fr = {ch: np.zeros(len(minutes)) for ch in chans}
    for s in stats:
        for ch, c in s["channels"].items():
            if not c.get("bins") or not len(minutes):
                continue
            b = np.asarray(c["bins"]["frames"], dtype=float)
            m = int(ts(c["bins"]["start_utc"]) // 60) + np.arange(len(b))
            k = np.clip(np.searchsorted(minutes, m), 0, len(minutes) - 1)
            ok = minutes[k] == m
            np.add.at(fr[ch], k[ok], b[ok])

    # ---- minute states: silent (recording, no frames on any bus) / active. Averages use active time only
    bus = sum((fr[ch] for ch in chans), np.zeros(len(minutes)))  # all channels (incl. CAN9 GNSS/IMU)
    per_min = lambda x: x / np.maximum(cov, MIN_RATE_COVER_S)  # noqa: E731  per-minute FPS (medians, maxima)
    rate = per_min(bus)
    # Activity: All channels by default. --activity-ignore (e.g. CAN9) leaves channels out of the active test, e.g. when
    # the logger's own CAN-internal messages (a 1 Hz time heartbeat) keep a parked vehicle 'active' (endless trips)
    ign = {c.upper() for c in (getattr(o, "activity_ignore", None) or [])}
    act = sum((fr[ch] for ch in chans if ch not in ign), np.zeros(len(minutes))) > 0
    code = act.astype(int)

    # ---- trips: active periods, short interruptions (silent or no data) merged
    segs_t: list[list[int]] = []
    for i in np.nonzero(act)[0].tolist():
        if segs_t and minutes[i] - minutes[segs_t[-1][1]] <= o.segment_merge_min + 1:
            segs_t[-1][1] = i
        else:
            segs_t.append([i, i])
    file_spans = [(ts(s["time"]["first_utc"]), ts(s["time"]["last_utc"]),
                   file_name(s["file"])) for s in stats]
    segs_t = [g for g in segs_t if minutes[g[1]] - minutes[g[0]] + 1 >= o.trip_min_min]  # shorter = e.g. a wake-up, not a trip
    segments = []
    for i0, i1 in segs_t:
        t0, t1 = float(minutes[i0] * 60), float(minutes[i1] * 60 + 60)
        sl = slice(i0, i1 + 1)
        files = [f for a, b, f in file_spans if a < t1 and b >= t0]
        segments.append({"start_utc": iso(t0), "end_utc": iso(t1), "minutes": int(minutes[i1] - minutes[i0] + 1),
                         "fps_mean": rnd(bus[sl].sum() / max(cov[sl].sum(), 1e-9)), "files": files[:10],
                         **({"files_more": len(files) - 10} if len(files) > 10 else {})})
    return {"stats": stats, "rec_iv": rec_iv, "minutes": minutes, "cov": cov, "chans": chans, "fr": fr, "bus": bus,
            "rate": rate, "act": act, "per_min": per_min, "code": code, "segments": segments}


def router_info(stats: list[dict]) -> dict:
    """CANmod.router context of a device's stats: demux mappings used, channel labels (CAN11 -> 'router 0x010 S1'),
    and the files whose CAN2 looks like un-demuxed router tunnel frames (raw_stats 'router_hint'). {} = none."""
    keys = sorted({s["demux"]["muxtp"] for s in stats if s.get("demux")})
    labels: dict = {}
    for s in stats:
        labels.update((s.get("demux") or {}).get("labels") or {})
    hint = [s for s in stats if s.get("router_hint")]
    out: dict = {}
    if keys:
        out.update(demux=keys, labels=labels, not_demuxed_files=sum(not s.get("demux") for s in stats))
    if hint:
        out["router_hint"] = {"files": len(hint), "ids": sorted({f"{h['channel']} {display_id(h['id'])}"
                                                                 for s in hint for h in s["router_hint"]})}
    return out


def aggregate_device(dev: str, stats: list[dict], o: argparse.Namespace) -> tuple[dict, dict]:
    m = device_model(stats, o)
    stats, rec_iv, minutes, cov, chans, fr, bus, rate, act, per_min, code, segments = (
        m[k] for k in ("stats", "rec_iv", "minutes", "cov", "chans", "fr", "bus", "rate", "act", "per_min", "code",
                       "segments"))
    spans_raw = [(ts(s["time"]["first_utc"]), ts(s["time"]["last_utc"])) for s in stats]
    t_first, t_last = min(s for s, _ in spans_raw), max(e for _, e in spans_raw)
    est_total = sum(est_storage(i["frames"], i["payload_bytes"]) for s in stats for i in s["ids"])
    size_total = sum(s["file"]["size_bytes"] for s in stats)

    # ---- recording time + periods without data
    spans = merge_spans(rec_iv or spans_raw, o.merge_s)
    no_data = [{"from_utc": iso(a[1]), "to_utc": iso(b[0]), "hours": rnd((b[0] - a[1]) / 3600)}
               for a, b in zip(spans, spans[1:]) if b[0] - a[1] > o.gap_h * 3600]
    recorded_s = float(cov.sum())
    active_s = float(cov[act].sum())
    states = {}
    for c_, nm in STATE_NAMES.items():
        sel = code == c_
        states[nm] = {"h": rnd(cov[sel].sum() / 3600), "pct": rnd(100 * cov[sel].sum() / max(recorded_s, 1e-9))}
    states["active"]["fps_median"] = rnd(float(np.median(rate[act]))) if act.any() else None
    fps = {"fps_active_mean": rnd(bus.sum() / max(active_s, 1e-9)),  # all buses, over active time only
           "fps_max": rnd(float(rate.max())) if len(rate) else None}  # busiest minute, all buses

    # trip summary over ALL segments (the list below is capped at --max-spans)
    durs = [g["minutes"] for g in segments]
    days = int(len(np.unique(minutes // 1440))) if len(minutes) else 0
    trips = {"count": len(durs), "total_h": rnd(sum(durs) / 60), "median_min": rnd(float(np.median(durs))) if durs else None,
             "longest_min": max(durs) if durs else None, "days_with_data": days,
             "per_day": rnd(len(durs) / days) if days else None,
             "last_start_utc": segments[-1]["start_utc"] if segments else None}

    # ---- frames/s timeline for charts: recording minutes in time order, merged into <= o.points points
    # (periods without data are compressed out; "break_before" marks points preceded by one > gap threshold)
    chunk = max(1, int(np.ceil(len(minutes) / o.points)))
    blocks: list[list[int]] = []
    for i, m in enumerate(minutes.tolist()):
        if not blocks or (m - minutes[blocks[-1][-1]]) * 60 > o.gap_h * 3600:
            blocks.append([])
        blocks[-1].append(i)
    groups, break_before = [], []
    for blk in blocks:
        if groups:
            break_before.append(len(groups))
        groups += [blk[i:i + chunk] for i in range(0, len(blk), chunk)]

    channels, activity, peaks = {}, {}, {}
    router = router_info(stats)
    for ch in chans:
        frames = tx = 0
        bitrates, loads, maxload, ids = set(), [], [], set()
        for s in stats:
            c = s["channels"].get(ch)
            if c:
                frames += c["frames"]
                tx += c.get("tx_frames", 0)
                if s["file"]["bitrates"].get(ch):
                    bitrates.add(s["file"]["bitrates"][ch])
                if c.get("busload_mean_pct") is not None:
                    loads.append((c["busload_mean_pct"], s["time"]["duration_s"]))
                    maxload.append(c.get("busload_max_pct", c["busload_mean_pct"]))
            ids |= {i["id"] for i in s["ids"] if i["channel"] == ch}
        e = {"bitrates": sorted(bitrates), "frames": frames, "unique_ids": len(ids)}
        if router.get("labels", {}).get(ch):
            e["label"] = router["labels"][ch]  # CANmod.router channel (demuxed); no bit-rate in the MF4 header
        if tx:
            e["tx_frames"] = tx
        e["fps_active_mean"] = rnd(fr[ch][act].sum() / max(active_s, 1e-9))
        e["fps_max"] = rnd(float(per_min(fr[ch]).max())) if len(cov) else None
        if loads:  # estimates, kept for the agent (the report uses frames/s)
            e["busload_mean_pct"] = rnd(sum(v * w for v, w in loads) / max(sum(w for _, w in loads), 1e-9))
            e["busload_max_pct"] = rnd(max(maxload))
        channels[ch] = e
        activity[ch] = [rnd(fr[ch][g].sum() / max(cov[g].sum(), 1.0)) for g in groups]
        # busiest minute per point (= the average when a point is one minute), so short peaks stay visible
        p_ch = per_min(fr[ch])
        peaks[ch] = [rnd(float(p_ch[g].max())) if len(p_ch[g]) else None for g in groups]

    # ---- IDs
    acc: dict = {}
    for s in stats:
        for i in s["ids"]:
            k = (i["channel"], i["id"])
            a = acc.setdefault(k, {"channel": i["channel"], "id": i["id"], "fd": False, "frames": 0, "est": 0,
                                   "dlc": set(), "up": 0, "capped": False, "first": i["first_utc"],
                                   "last": i["last_utc"], "files": 0, "sessions": set(), "med": [], "p95": [], "w": [],
                                   "chg": 0, "chg_fr": 0})
            a["fd"] |= i["fd"]
            a["frames"] += i["frames"]
            a["est"] += est_storage(i["frames"], i["payload_bytes"])
            a["dlc"] |= set(i["dlc"])
            a["up"] = max(a["up"], i["unique_payloads"])
            a["capped"] |= bool(i.get("unique_payloads_capped"))
            if i.get("payload_changes") is not None:  # older cached stats lack it (re-run raw_stats.py)
                a["chg"] += i["payload_changes"]
                a["chg_fr"] += i["frames"]
            a["first"], a["last"] = min(a["first"], i["first_utc"]), max(a["last"], i["last_utc"])
            a["files"] += 1
            a["sessions"].add(s["file"]["session"])
            a["med"].append(i["period_ms_median"])
            a["p95"].append(i["period_ms_p95"])
            a["w"].append(i["frames"])
    rows = []
    for a in acc.values():
        r = {"channel": a["channel"], "id": display_id(a["id"]), "frames": a["frames"],
             "rate_hz": rnd(a["frames"] / max(active_s, 1e-9)),
             "period_ms_median": rnd(weighted_median(a["med"], a["w"])),
             "period_ms_p95": rnd(weighted_median(a["p95"], a["w"])),
             "dlc": sorted(a["dlc"]), "unique_payloads": a["up"],
             "payload_changes_pct": rnd(100 * a["chg"] / a["chg_fr"]) if a["chg_fr"] else None,
             "est_storage_bytes": a["est"], "est_storage_pct": rnd(100 * a["est"] / max(est_total, 1)),
             "first_utc": a["first"][:19] + "Z", "last_utc": a["last"][:19] + "Z",
             "files": a["files"], "files_pct": rnd(100 * a["files"] / len(stats)),
             "sessions": compact_sessions(a["sessions"])}
        if a["fd"]:
            r["fd"] = True
        if a["capped"]:
            r["unique_payloads_capped"] = True
        rows.append(r)
    rows.sort(key=lambda r: -r["est_storage_bytes"])
    n_top = o.top or len(rows)  # 0 = all IDs
    top, rest = rows[:n_top], rows[n_top:]
    static_all = [r for r in rows if r["payload_changes_pct"] is not None and r["payload_changes_pct"] < STATIC_MAX_CHANGES_PCT]
    static = [{k: r[k] for k in ("channel", "id", "payload_changes_pct", "unique_payloads", "rate_hz", "est_storage_pct")}
              for r in static_all[:20]]

    dev_out = {
        "files": len(stats), "sessions": compact_sessions({s["file"]["session"] for s in stats}),
        "session_count": len({s["file"]["session"] for s in stats}),
        "size_bytes": size_total, "size_gb": rnd(size_total / 1e9),
        "est_storage_bytes": est_total,
        "bytes_per_frame": rnd(size_total / max(sum(s["frames_total"] for s in stats), 1)),
        "est_storage_vs_size_pct": rnd(100 * est_total / max(size_total, 1)),
        "first_utc": iso(t_first), "last_utc": iso(t_last), "span_h": rnd((t_last - t_first) / 3600),
        "logged_h": rnd(recorded_s / 3600), "active_h": rnd(active_s / 3600),
        "frames": sum(s["frames_total"] for s in stats),
        "firmware": value_history(stats, "firmware"), "hardware": value_history(stats, "hardware"),
        "config_crc32": value_history(stats, "config_crc32"), "meta": value_history(stats, "meta"),
        "config_periods": config_periods(stats)[: o.max_spans],
        "header_unsupported_files": sum(s["file"].get("header") == "unsupported" for s in stats),
        "recording": {"spans_total": len(spans), "spans_utc": [[iso(a), iso(b)] for a, b in spans[: o.max_spans]],
                      "no_data_total": len(no_data), "no_data": no_data[: o.max_spans]},
        "states": {**fps, "states": states,
                   "segments_total": len(segments), "trips": trips, "segments": segments[: o.max_spans]},
        "channels": channels,
        "fps_timeline": {"axis": "recorded time (periods without data removed)", "minutes_per_point": chunk,
                         "t_utc": [iso(minutes[g[0]] * 60) for g in groups], "break_before": break_before,
                         "fps": activity, "fps_peak": peaks},
        "ids": {"total": len(rows), "listed": len(top),
                "omitted": {"ids": len(rest), "frames": sum(r["frames"] for r in rest),
                            "est_storage_pct": rnd(sum(r["est_storage_pct"] or 0 for r in rest))},
                "top": top},
        "static_candidates": static,
        "static_total": {"ids": len(static_all), "est_storage_pct": rnd(sum(r["est_storage_pct"] or 0 for r in static_all))},
    }
    if router:
        dev_out["router"] = router
    if len(spans) > o.max_spans:
        dev_out["recording"]["spans_truncated"] = True
    pdev = sorted({s["file"]["path_device"] for s in stats if s["file"].get("path_device")})
    if pdev:
        dev_out["path_device"] = pdev  # folder name(s) that differ from the header serial
    all_ids = {r["id"]: r["frames"] for r in rows}
    return dev_out, all_ids


def compare(dev_ids: dict[str, dict]) -> dict:
    sets = {d: set(v) for d, v in dev_ids.items()}
    common = set.intersection(*sets.values())
    per = {}
    for d, s in sets.items():
        others = set.union(*(x for k, x in sets.items() if k != d)) if len(sets) > 1 else set()
        only = sorted(s - others, key=lambda i: (-dev_ids[d][i], i))  # ties by ID: deterministic
        per[d] = {"ids": len(s), "only_here_count": len(only), "only_here_top_by_frames": only[:30]}
    return {"key": "CAN/LIN ID (channel ignored)", "ids_common_to_all": len(common), "per_device": per}


def aggregate(stats: list[dict], o: argparse.Namespace, inputs: list[str], missing: list[str]) -> dict:
    if o.start or o.end:
        s0 = ts(o.start) if o.start else -1e18
        s1 = ts(o.end) if o.end else 1e18
        stats = [s for s in stats if ts(s["time"]["last_utc"]) >= s0 and ts(s["time"]["first_utc"]) <= s1]
    by_dev: dict = defaultdict(list)
    for s in stats:
        by_dev[s["file"]["device"]].append(s)
    devices, dev_ids = {}, {}
    for d in sorted(by_dev):
        devices[d], dev_ids[d] = aggregate_device(d, by_dev[d], o)
    out = {
        "schema": SCHEMA,
        "generated_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "selection": {"inputs": inputs, "start_utc": o.start, "end_utc": o.end, "files_with_stats": len(stats),
                      "files_missing_stats": len(missing), "missing_examples": missing[:5]},
        "settings": {"no_data_threshold_h": o.gap_h, "merge_tolerance_s": o.merge_s,
                     "segment_merge_min": o.segment_merge_min, "trip_min_min": o.trip_min_min, "top": o.top, "bin_s": 60,
                     "activity_ignore": sorted({c.upper() for c in o.activity_ignore or []})},
        "tools": {"mdf2csv": sorted({s["tool"].get("mdf2csv", "unknown") for s in stats}),
                  **({"mdf2mdf": sorted({s["tool"]["mdf2mdf"] for s in stats if s["tool"].get("mdf2mdf")})}
                     if any(s["tool"].get("mdf2mdf") for s in stats) else {})},
    }
    if stats:
        out["totals"] = {
            "devices": len(devices), "files": len(stats),
            "size_gb": rnd(sum(d["size_bytes"] for d in devices.values()) / 1e9),
            "first_utc": min(d["first_utc"] for d in devices.values()),
            "last_utc": max(d["last_utc"] for d in devices.values()),
            "logged_h": rnd(sum(d["logged_h"] for d in devices.values())),
            "active_h": rnd(sum(d["active_h"] for d in devices.values())),
            "frames": sum(d["frames"] for d in devices.values()),
            "no_data_periods": sum(d["recording"]["no_data_total"] for d in devices.values()),
            "unique_ids": len({i for v in dev_ids.values() for i in v}),
        }
    out["devices"] = devices
    if len(devices) > 1:
        out["comparison"] = compare(dev_ids)
    return out


def add_args(ap: argparse.ArgumentParser) -> None:
    ap.add_argument("paths", nargs="*", type=Path, help="Log files/folders (uses their cached stats)")
    ap.add_argument("--project", type=Path, help="projects/<name>: Use the project's sources + devices")
    ap.add_argument("--device", action="append", help="Device ID (repeatable); alone = all cached stats of it")
    ap.add_argument("--start", help="Only files overlapping this UTC time onwards, e.g. 2026-01-15T00:00:00Z")
    ap.add_argument("--end", help="Only files overlapping up to this UTC time")
    ap.add_argument("--gap-h", type=float, default=1.0,
                    help="List periods without log data longer than this, in hours (default 1)")
    ap.add_argument("--merge-s", type=float, default=120.0, help="Merge recording spans closer than this (default 120 s)")
    ap.add_argument("--segment-merge-min", type=int, default=5,
                    help="Join active periods (trips) separated by up to N minutes (default 5)")
    ap.add_argument("--trip-min-min", type=int, default=3,
                    help="Minimum trip length in minutes; shorter active periods are not trips (default 3)")
    ap.add_argument("--activity-ignore", action="append", metavar="CHANNEL",
                    help="Channel that does not count as bus activity, e.g. CAN9 (repeatable; default: all channels count). "
                         "Its frames still show in FPS and storage")
    ap.add_argument("--top", type=int, default=30, help="IDs listed per device (default 30, 0 = all; totals always included)")
    ap.add_argument("--points", type=int, default=120, help="Max points of the activity timeline per channel")
    ap.add_argument("--max-spans", type=int, default=200, help=argparse.SUPPRESS)
    ap.add_argument("--stats-root", type=Path, default=STATS_ROOT, help=argparse.SUPPRESS)


def run(a: argparse.Namespace) -> dict:
    sel = select(list(a.paths), {d.upper() for d in a.device or []}, a.project, a.stats_root)
    return aggregate(sel["stats"], a, sel["inputs"], sel["missing"])


def public(res: dict) -> dict:
    """stats.json content: Drops the private chart-only keys (prefixed '_')."""
    return {**res, "devices": {d: {k: v for k, v in x.items() if not k.startswith("_")}
                               for d, x in res.get("devices", {}).items()}}


def default_out(project: Path | None) -> Path:
    """<project>/reports/<yyyymmdd>-raw-overview/, without a project TEMP/reports/<yyyymmdd>-raw-overview/."""
    return report_dir("raw-overview", project)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_args(ap)
    ap.add_argument("--out", type=Path, help="Output JSON path (default: <project>/reports/<yyyymmdd>-raw-overview/stats.json, "
                    "without --project TEMP/reports/<yyyymmdd>-raw-overview/stats.json)")
    ap.add_argument("--print", action="store_true", help="Also print the JSON")
    a = ap.parse_args()
    if not (a.paths or a.device or a.project):
        ap.error("give log paths, --device or --project")
    out = a.out or default_out(a.project) / "stats.json"
    res = run(a)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(public(res), indent=1), encoding="utf-8")
    t = res.get("totals")
    if not t:
        print(f"No cached stats found ({res['selection']['files_missing_stats']} log files without stats) - run raw_stats.py first")
        return 1
    print(f"{t['devices']} device(s), {t['files']} files, {t['size_gb']} GB, {t['first_utc']} to {t['last_utc']} UTC, "
          f"recorded {t['logged_h']} h, bus active {t['active_h']} h, {t['unique_ids']} unique IDs")
    if res["selection"]["files_missing_stats"]:
        print(f"WARNING: {res['selection']['files_missing_stats']} log files have no current stats (run raw_stats.py)")
    print(f"stats.json: {out} ({out.stat().st_size / 1024:.0f} KB)")
    if a.print:
        print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
