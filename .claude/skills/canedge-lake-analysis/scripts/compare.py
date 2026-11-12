r"""Rank what deviates: A target device vs other devices, or a window vs a baseline window, from a saved summary
(canedge-lake-query `summary --by day --out f.parquet`). pandas only, no lake access, no cost.

  .venv\Scripts\python.exe .claude/skills/canedge-lake-analysis/scripts/compare.py --summary f.parquet --target 12345678
      [--baseline AABBCCDD,11223344]          device vs devices (default baseline: all other devices in the file)
  ... --target 12345678 --window 2026-09-01..2026-10-01 --baseline-window 2026-06-01..2026-09-01
                                              the device in a window vs its own earlier days
  options: --metrics avg,max,min,coverage  --key message (default) | signal  --top 25  --min-days 3  --json  --out f.json

Per message + signal and metric: The target's median daily value vs the baseline (the median of the baseline
devices' medians, or the baseline days' median), the difference in % and a robust z (difference / 1.4826 x MAD of
the baseline; with fewer than 3 baseline values or a constant baseline only the % is meaningful). Counters (only
rising: odometer, energy, hours) are listed, not compared by level. Several summary files (e.g. one per month) or a
glob are merged. coverage = rows per day (a message that
vanishes or appears = an ECU, a bus or a filter changed). Plus signals that only the target or only the baseline has
(other DBC, other vehicle type). --key signal: Compare by signal name across messages (e.g. J1939 source
addresses that differ per vehicle). A deviation is a lead, not a diagnosis: Check it in the data (episodes, context).
The JSON includes a ready fixed "table" panel for results.json.
"""
from __future__ import annotations

import argparse
import glob
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-lake-query" / "scripts"))
from _backends import LakeError, match_device  # noqa: E402 - the device ID / meta name rules of canedge-lake-query

METRICS = {"avg": "daily average", "max": "daily max", "min": "daily min", "coverage": "rows per day"}


def load(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path) if path.suffix == ".parquet" else pd.read_csv(path)
    need = {"device", "message", "signal", "period", "n_rows", "n", "sum", "min", "max"}
    if need - set(df.columns):
        raise SystemExit(f"{path}: not a summary file (missing {', '.join(sorted(need - set(df.columns)))})")
    df["device"] = df["device"].astype(str).str.upper()
    df["avg"] = df["sum"] / df["n"].where(df["n"] > 0)
    df["coverage"] = df["n_rows"].astype(float)
    df["day"] = pd.to_datetime(df["period"].astype(str).str.replace("/", "-"), errors="coerce", utc=True)
    return df


def resolve(df: pd.DataFrame, x: str) -> str:
    """Device ID, or a meta name that exactly one device in the file has (names are not unique)."""
    names = df.dropna(subset=["device_name"]).groupby("device")["device_name"].first().astype(str).to_dict() \
        if "device_name" in df.columns else {}
    try:
        return match_device(x, sorted(set(df["device"])), names, "in the summary")
    except LakeError as e:
        raise SystemExit(f"ERROR: {e}") from None


def window(df: pd.DataFrame, spec: str | None) -> pd.DataFrame:
    if not spec:
        return df
    a, _, b = spec.partition("..")
    t0, t1 = pd.Timestamp(a, tz="UTC"), pd.Timestamp(b, tz="UTC")
    return df[(df["day"] >= t0) & (df["day"] < t1)]


def robust(target: float, base: np.ndarray) -> tuple[float, float | None, float | None]:
    """(baseline median, % difference, robust z or None with < 3 baseline values or no baseline spread)."""
    med = float(np.median(base))
    pct = None if med == 0 else 100 * (target - med) / abs(med)
    if len(base) < 3:
        return med, pct, None
    mad = 1.4826 * float(np.median(np.abs(base - med)))
    if mad == 0:   # no baseline spread (constant or coarsely quantized signal): z is meaningless, rank by %
        return med, pct, None
    floor = max(abs(med) * 0.02, 1e-9)   # near-constant baselines: at least a 2% spread, so z stays meaningful
    return med, pct, (target - med) / max(mad, floor)


def rising(df: pd.DataFrame, keys: list[str]) -> set:
    """Keys of counters (odometer, energy, operating hours): Each day starts at or above the previous day's max and the
    value grows. Their level only says how old the vehicle is, so avg/min/max are not compared (coverage still is)."""
    out = set()
    for k, g in df.dropna(subset=["min", "max"]).sort_values("day").groupby(keys):
        lo, hi = g["min"].to_numpy(), g["max"].to_numpy()
        if len(g) >= 3 and hi[-1] > lo[0] and (lo[1:] >= hi[:-1] - 1e-9 * np.abs(hi[:-1])).all():
            out.add(k)
    return out


def compare(df, target, baseline_devs=None, win=None, base_win=None, metrics=("avg", "max", "coverage"),
            key="message", min_days=3) -> dict:
    keys = ["message", "signal"] if key == "message" else ["signal"]
    if key == "signal":   # merge messages (e.g. per source address): sums/counts add up, max of max
        df = df.groupby(["device", "signal", "day"], as_index=False).agg(
            n_rows=("n_rows", "sum"), n=("n", "sum"), sum=("sum", "sum"), min=("min", "min"), max=("max", "max"))
        df["avg"], df["coverage"] = df["sum"] / df["n"].where(df["n"] > 0), df["n_rows"].astype(float)
    if base_win:   # the device in a window vs its own baseline window
        mode = "window"
        t = window(df[df["device"] == target], win)
        b = window(df[df["device"] == target], base_win)
        base_label = f"{target} {base_win}"
    else:
        mode = "devices"
        t = window(df[df["device"] == target], win)
        others = [d for d in sorted(set(df["device"])) if d != target]
        bd = baseline_devs or others
        b = window(df[df["device"].isin(bd)], win)
        base_label = ", ".join(bd)
    counters = rising(df[df["device"] == target], keys)
    rows = []
    tk = {k: g for k, g in t.groupby(keys)}
    bk = {k: g for k, g in b.groupby(keys)}
    for k in sorted(set(tk) & set(bk)):
        gt, gb = tk[k], bk[k]
        for m in metrics:
            tv = gt[m].dropna()
            if len(tv) < min(min_days, max(len(gt), 1)) or (m != "coverage" and k in counters):
                continue
            if mode == "devices":   # one value per baseline device: its median day
                bv = gb.groupby("device")[m].median().dropna().to_numpy()
            else:
                bv = gb[m].dropna().to_numpy()
            if not len(bv) or not len(tv):
                continue
            tmed = float(tv.median())
            med, pct, z = robust(tmed, bv)
            kk = k if isinstance(k, tuple) else (k,)
            rows.append({**dict(zip(keys, kk)), "metric": m, "target": tmed, "baseline": med, "diff_pct": pct, "z": z,
                         "target_days": int(len(tv)), "baseline_n": int(len(bv))})
    ranked = sorted(rows, key=lambda r: -(abs(r["z"]) if r["z"] is not None else abs(r["diff_pct"] or 0) / 100))
    only_t = [dict(zip(keys, k if isinstance(k, tuple) else (k,))) for k in sorted(set(tk) - set(bk))]
    only_b = [dict(zip(keys, k if isinstance(k, tuple) else (k,))) for k in sorted(set(bk) - set(tk))]
    return {"mode": mode, "target": target, "baseline": base_label, "window": win, "baseline_window": base_win,
            "key": key, "metrics": list(metrics), "compared": len({tuple(r[c] for c in keys) for r in rows}),
            "ranked": ranked, "only_target": only_t, "only_baseline": only_b,
            "counters": [dict(zip(keys, k if isinstance(k, tuple) else (k,))) for k in sorted(counters)]}


def panel(res: dict, top: int, names: dict) -> dict:
    """A fixed table panel for results.json (compare scores do not merge, so they do not follow the filters)."""
    cols = (["Message"] if res["key"] == "message" else []) + ["Signal", "Metric", "Target", "Baseline", "Diff (%)", "Robust z"]
    rows = [([r["message"]] if res["key"] == "message" else []) + [r["signal"], METRICS[r["metric"]], _r(r["target"]),
            _r(r["baseline"]), _r(r["diff_pct"]), _r(r["z"])] for r in res["ranked"][:top]]
    tname = names.get(res["target"]) or res["target"]
    return {"type": "table", "mode": "fixed", "title": f"Largest deviations: {tname} vs {'its baseline window' if res['mode'] == 'window' else 'the other devices'}",
            "tip": "Median daily value of the target vs the baseline. Robust z = difference / spread of the baseline "
                   "(1.4826 x MAD); empty with fewer than 3 baseline values or no baseline spread. Counters (only rising) are "
                   "left out. A lead, not a diagnosis",
            "columns": cols, "rows": rows, "bars": [], "sort": None}   # rows in the ranked order (|z|, else |diff %|)


def _r(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) else float(f"{x:.4g}")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--summary", nargs="+", required=True,
                    help="summary --by day --out file(s) (.parquet / .csv; several or a glob, e.g. one per month)")
    ap.add_argument("--target", required=True, help="Device ID (or a unique meta name)")
    ap.add_argument("--baseline", help="Comma separated device IDs (default: all other devices)")
    ap.add_argument("--window", help="Target days: yyyy-mm-dd..yyyy-mm-dd (UTC, end exclusive)")
    ap.add_argument("--baseline-window", help="Compare the target with its own days in this window instead")
    ap.add_argument("--metrics", default="avg,max,coverage", help=f"Comma separated: {', '.join(METRICS)}")
    ap.add_argument("--key", default="message", choices=["message", "signal"])
    ap.add_argument("--min-days", type=int, default=3, help="Minimum target days per signal (default 3)")
    ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--out", type=Path, help="Save the full result (+ the table panel) as JSON")
    a = ap.parse_args()
    files = sorted({Path(f) for x in a.summary for f in (glob.glob(x) or [x])})
    df = pd.concat([load(f) for f in files], ignore_index=True) if len(files) > 1 else load(files[0])
    target = resolve(df, a.target)
    base = [resolve(df, x) for x in a.baseline.split(",")] if a.baseline else None
    ms = [m.strip() for m in a.metrics.split(",") if m.strip()]
    bad = [m for m in ms if m not in METRICS]
    if bad:
        raise SystemExit(f"Unknown metric(s) {bad} (use {', '.join(METRICS)})")
    res = compare(df, target, base, a.window, a.baseline_window, ms, a.key, a.min_days)
    names = df.dropna(subset=["device_name"]).groupby("device")["device_name"].first().to_dict() \
        if "device_name" in df.columns else {}
    res["panel"] = panel(res, a.top, names)
    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(res, indent=1, default=str), encoding="utf-8")
    if a.json:
        print(json.dumps({**res, "ranked": res["ranked"][:a.top]}, indent=1, default=str))
        return 0
    print(f"{target} vs {res['baseline']} ({res['mode']}{', ' + a.window if a.window else ''}): {res['compared']} "
          f"signal(s) compared, by {a.key}")
    nm = lambda r: f"{r.get('message', '')}.{r['signal']}" if a.key == "message" else r["signal"]   # noqa: E731
    w = min(72, max([len(nm(r)) for r in res["ranked"][:a.top]] + [6]))
    print(f"  {'signal':{w}} {'metric':13} {'target':>11} {'baseline':>11} {'diff %':>8} {'z':>7}")
    for r in res["ranked"][:a.top]:
        name = nm(r)
        f = lambda x: "-" if x is None else f"{x:.4g}"   # noqa: E731
        print(f"  {name[:w]:{w}} {r['metric']:13} {f(r['target']):>11} {f(r['baseline']):>11} {f(r['diff_pct']):>8} {f(r['z']):>7}")
    for lab, xs in (("Only the target has", res["only_target"]), ("Only the baseline has", res["only_baseline"]),
                    ("Counters, level not compared", res["counters"])):
        if xs:
            print(f"  {lab} ({len(xs)}): " + ", ".join(".".join(str(v) for v in x.values()) for x in xs[:15])
                  + (" ..." if len(xs) > 15 else ""))
    if a.out:
        print(f"Saved: {a.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
