r"""Context for the analysis page: Signals + GNSS track per device as resampled tiers (canedge-lake-query resample),
a coarse tier over the whole window plus detail tiers around the findings, within a point budget, and the trips per
device (1 min buckets, split at pauses over 5 min: start, end, first position, average of the first signal). The
page draws lines and routes only from detailed tiers, otherwise daily/weekly bars and one dot per trip.

  .venv\Scripts\python.exe .claude/skills/canedge-lake-analysis/scripts/context.py --athena --results <folder>/results.json
      --signals enginespeed,wheelbasedvehiclespeed [--gnss] [--detail 2026-02-10T10:00..2026-02-10T12:00]
      [--points 300000] [--dbc projects/<p>] [--custom-js <folder>/custom.js]
  Updates "context" in results.json (detail tiers for every finding with from/to + each --detail window) and rebuilds
  report.html. Signals: Exact column names (lake_schema.py --find), or message.signal; each device of results.json
  that has them. --gnss adds the track (latitude/longitude), colored by the first signal.

Module use: build_context(db, devices, signals, start, end, details=[(a, b, devices | None)], gnss=True, points=...).
Resampling (avg per bucket) shows trends, not single frames: For exact timing use a 1 s / 100 ms detail window.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]
sys.path.insert(0, str(SKILLS / "canedge-lake-query" / "scripts"))
sys.path.insert(0, str(HERE))
from _backends import LakeError, add_source_args, connect_args, nice_interval, parse_utc  # noqa: E402
from build_analysis import build, epoch  # noqa: E402

MIN_INTERVAL = 1.0          # detail tiers: never finer than 1 s by default (--min-interval)


def _hits(db, device: str, spec: str) -> dict | None:
    """'signal' or 'message.signal' -> the find() hit of that device (the table with that column; first match)."""
    msg, _, sig = spec.lower().rpartition(".")
    hits = [h for h in db.find(sig, device=device) if h["signal"] == sig and (not msg or h["message"] == msg)]
    return hits[0] if hits else None


def _gnss(db, device: str) -> tuple[str, str, str] | None:
    """(message, latitude column, longitude column) of a device's position message, if any."""
    lat = [h for h in db.find("latitude", device=device) if h["device"]]
    for h in lat:
        lon = [c for c in db.find("longitude", device=device) if c["table"] == h["table"]]
        if lon:
            return h["message"], h["signal"], lon[0]["signal"]
    return None


TRIP_BUCKET, TRIP_GAP_S, TRIP_MIN_S = "1min", 300, 120   # trips: 1 min buckets, split at data gaps > 5 min, >= 2 min long


def _trips(db, device, speed, pos, start, end) -> list[dict]:
    """Trips from the data: Runs of 1 min buckets with data (speed or position), split where the data pauses for
    more than 5 min (logger off / parked). Per trip: start, end, first valid position, average speed (raw unit)."""
    import pandas as pd
    (ms, s), (mp, la, lo) = speed, pos
    try:
        sp = db.resample(device, ms, [s], start, end, agg="avg", interval=TRIP_BUCKET)
        ps = db.resample(device, mp, [la, lo], start, end, agg="avg", interval=TRIP_BUCKET)
    except LakeError as e:   # e.g. too many rows for a long window: no trips rather than a failed page
        print(f"WARNING: No trips for {device}: {e}", file=sys.stderr)
        return []
    df = pd.merge(sp, ps, on="time", how="outer").sort_values("time").reset_index(drop=True)
    if df.empty:
        return []
    t = df["time"].map(lambda x: x.timestamp())
    run = (t.diff() > TRIP_GAP_S).cumsum()
    out = []
    for _, g in df.assign(t=t).groupby(run):
        t0, t1 = g["t"].iloc[0], g["t"].iloc[-1] + 60
        if t1 - t0 < TRIP_MIN_S:
            continue
        fix = g[g[f"avg_{la}"].notna() & g[f"avg_{lo}"].notna() & ~((g[f"avg_{la}"] == 0) & (g[f"avg_{lo}"] == 0))]
        if fix.empty:
            continue
        v = g[f"avg_{s}"].dropna()
        out.append({"t": round(t0), "end": round(t1), "lat": round(float(fix[f"avg_{la}"].iloc[0]), 5),
                    "lon": round(float(fix[f"avg_{lo}"].iloc[0]), 5), "v": round(float(v.mean()), 3) if len(v) else None})
    return out


def _series(df, cols):
    t = [round(x.timestamp(), 3) for x in df["time"]] if len(df) else []
    return t, [[None if v != v else round(float(v), 6) for v in df[c]] if len(df) else [] for c in cols]


def build_context(db, devices, signals, start, end, details=(), gnss=True, points=300_000, dbc=None,
                  min_interval=MIN_INTERVAL) -> dict:
    t0, t1 = parse_utc(start), parse_utc(end)
    idx = None
    if dbc:
        from _dbc import DbcIndex
        idx = DbcIndex(dbc)
    plan = []   # (device, message, [signals]) per table
    for d in devices:
        by_msg: dict[str, list[str]] = {}
        for s in signals:
            h = _hits(db, d, s)
            if h and h["signal"] not in by_msg.setdefault(h["message"], []):
                by_msg[h["message"]].append(h["signal"])
        plan += [(d, m, ss) for m, ss in by_msg.items() if ss]
    tracks = {d: g for d in devices if gnss and (g := _gnss(db, d))}
    n_series = sum(len(ss) for _, _, ss in plan) + 2 * len(tracks)
    if not n_series:
        raise LakeError(f"None of the devices has the signals {', '.join(signals)} (lake_schema.py --find)")
    wins = [(t0, t1, None)] + [(parse_utc(a), parse_utc(b), ds) for a, b, ds in details]
    per = points / n_series / len(wins)   # points per series and tier (rough)
    out_sigs, out_tracks = {}, {}

    coarse_iv = None
    for k, (a, b, ds) in enumerate(wins):
        span = (b - a).total_seconds()
        iv = max(nice_interval(span / max(per, 10)), min_interval if k else 0)
        if k and iv >= coarse_iv:   # not finer than the whole-window tier: Would only duplicate it
            continue
        coarse_iv = coarse_iv or iv
        for d, m, ss in plan:
            if ds and d not in ds:
                continue
            df = db.resample(d, m, ss, a, b, agg="avg", interval=f"{iv:g}s" if iv >= 1 else f"{round(iv * 1000)}ms")
            t, vals = _series(df, [f"avg_{s}" for s in ss])
            info = idx.match(d, m) if idx else None
            for s, v in zip(ss, vals):
                key = f"{d}|{m}|{s}"
                sd = ((info or {}).get("signals", {}).get(s)) or {}
                e = out_sigs.setdefault(key, {"key": key, "device": d, "message": m, "signal": s,
                                              "label": sd.get("name") or s, "unit": sd.get("unit") or None,
                                              "values": {str(x): y for x, y in (sd.get("value_table") or {}).items()} or None,
                                              "tiers": []})
                e["tiers"].append({"interval": iv, "from": epoch(a), "to": epoch(b), "t": t, "v": v})
        for d, (m, la, lo) in tracks.items():
            if ds and d not in ds:
                continue
            df = db.resample(d, m, [la, lo], a, b, agg="avg", interval=f"{iv:g}s" if iv >= 1 else f"{round(iv * 1000)}ms")
            t, (lat, lon) = _series(df, [f"avg_{la}", f"avg_{lo}"])
            bad = [i for i, (x, y) in enumerate(zip(lat, lon)) if x is None or y is None or (x == 0 and y == 0)]
            for i in bad:   # no fix: 0/0 or NULL
                lat[i] = lon[i] = None
            first = next((key for key, e in out_sigs.items() if e["device"] == d), None)
            e = out_tracks.setdefault(d, {"device": d, "message": m, "color_by": first, "tiers": []})
            e["tiers"].append({"interval": iv, "from": epoch(a), "to": epoch(b), "t": t, "lat": lat, "lon": lon})
    # Trips (one dot per trip start on the map; a route line only from detailed tiers): Per device with a position
    # message, the average of its first context signal (e.g. speed) per trip
    out_trips = []
    for d, (mp, la, lo) in tracks.items():
        first = next(((m, ss[0]) for dd, m, ss in plan if dd == d), None)
        if first:
            key = f"{d}|{first[0]}|{first[1]}"
            e = out_sigs.get(key, {})
            out_trips.append({"device": d, "color_by": key, "label": e.get("label"), "unit": e.get("unit"),
                              "trips": _trips(db, d, first, (mp, la, lo), t0, t1)})
    total = sum(len(t["t"]) for e in out_sigs.values() for t in e["tiers"]) + \
        sum(2 * len(t["t"]) for e in out_tracks.values() for t in e["tiers"]) + sum(len(x["trips"]) for x in out_trips)
    return {"signals": list(out_sigs.values()), "tracks": list(out_tracks.values()), "trips": out_trips, "points": total}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_source_args(ap)
    ap.add_argument("--results", type=Path, required=True, help="results.json to update (window, devices, findings)")
    ap.add_argument("--signals", required=True, help="Comma separated signal columns (or message.signal)")
    ap.add_argument("--gnss", action="store_true", help="Add the GNSS track (latitude/longitude) per device")
    ap.add_argument("--detail", action="append", default=[], help="Extra detail window a..b (UTC ISO), repeatable")
    ap.add_argument("--points", type=int, default=300_000, help="Point budget for the page (default 300000)")
    ap.add_argument("--min-interval", type=float, default=MIN_INTERVAL, help="Finest detail bucket in s (default 1)")
    ap.add_argument("--dbc", type=Path, help="DBCs for units + value labels (default with --project: its DBCs)")
    ap.add_argument("--custom-js", type=Path, help="Passed on to the rebuild")
    a = ap.parse_args()
    try:
        db = connect_args(a)
        R = json.loads(a.results.read_text(encoding="utf-8"))
        details = [(f["from"], f["to"], f.get("devices")) for f in R.get("findings", []) if f.get("from") and f.get("to")]
        details += [(*w.split(".."), None) for w in a.detail]
        dbc = a.dbc or (a.project if a.project and any((a.project / "dbc").glob("*.dbc")) else None)
        ctx = build_context(db, list(R["devices"]), [s.strip() for s in a.signals.split(",") if s.strip()],
                            R["window"][0], R["window"][1], details, a.gnss, a.points, dbc, a.min_interval)
    except LakeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    pts = ctx.pop("points")
    n_tiers = max([len(x["tiers"]) for x in ctx["signals"] + ctx["tracks"]] + [0])   # detail tiers not finer: skipped
    R["context"] = ctx
    prov = R.setdefault("provenance", {})
    prov["context"] = (f"{len(ctx['signals'])} signal series + {len(ctx['tracks'])} GNSS track(s), {pts:,} points, "
                       f"{n_tiers} tier(s) (resampled, avg per bucket)")
    if hasattr(db, "budget"):
        prov["context_queries"] = db.budget()
    a.results.write_text(json.dumps(R, separators=(",", ":")), encoding="utf-8")
    out = build(a.results, a.custom_js)
    print(f"Context: {len(ctx['signals'])} signal series, {len(ctx['tracks'])} track(s), {pts:,} points, "
          f"{n_tiers} tier(s)" + (f" | {db.budget()}" if hasattr(db, "budget") else ""))
    print(f"Saved: {a.results} + {out} ({out.stat().st_size / 1e6:.1f} MB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
