r"""Event analysis for fault diagnosis (references/playbooks/intermittent-faults.md): Group issue indicators into
incidents, then compare the signals before, at and after each onset with reference moments.

  .venv\Scripts\python.exe .claude/skills/canedge-lake-analysis/scripts/events.py incidents
      --episodes "projects/<p>/data/ep/*.parquet" [--merge 30min] [--out inc.parquet] [--json]
  .venv\Scripts\python.exe .claude/skills/canedge-lake-analysis/scripts/events.py align --project projects/<p>
      --events onsets.csv [--reference ref.csv] (--signals a,msg.b | --match <regex> | --all)
      [--pre 10min] [--post 10min] [--gap 5s] [--trace a,msg.b --interval 1s] [--out aligned.json] [--top 40]

incidents: Episode files of lake_query.py episodes --out (device, message, signal, start_t, end_t), one per
indicator. Per device, indicator episodes that start within --merge of the incident's running end form ONE
incident: Counts incidents and days, not events (one stuck unit restarted 10 times is one incident, not 10), and
lists the indicators in the order they started. The first one is closest to the trigger; the later ones are often
consequences. Also prints how often each indicator comes first, and incidents that start within hours after
another (an aftermath, e.g. recovery attempts: check what the earlier one was).

align: Events = CSV/Parquet with device + t (onset, UTC; or start_t of an episodes file) [+ label]; --events are the
"problem" onsets (e.g. a fault code becoming active), --reference the same kind of moment without the problem (e.g.
other start-ups, the same state on units without the fault). Per event 4 windows, per signal (summary of the lake):
  early [t-2*pre, t-pre)  lead [t-pre, t-gap)  onset [t-gap, t+gap)  after [t+gap, t+post)
Per signal and event: "before" = the lead window differs from the early one (a change leading up to the onset),
"vs_ref" = the lead window is outside the range of the reference events' lead windows, "at" = the onset window
leaves the lead window's range, "after" = the after window differs from the lead window. Shares over the problem
events, minus the same share over the reference events (what also happens normally) -> phase: before / at onset /
after / no change. Message rates (rows/s) per window show what goes silent. A ranking, not a verdict: Signals
that change BEFORE the onset and not at the reference events are the leads for a cause; changes AFTER it are
usually consequences. Check each lead in the traces.
--trace: Resampled traces of these signals around every event (problem + reference) on one grid, as ready
"aligned" panels for the analysis page (results-spec.md) in the --out JSON ("panels").

Module use: incidents(df, merge_s), align(db, events, reference, specs | None, match, pre, post, gap, ...).
"""
from __future__ import annotations

import argparse
import glob
import json
import re
import sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[1] / "canedge-lake-query" / "scripts"))
from _backends import LakeError, add_source_args, connect_args, interval_s  # noqa: E402

WINDOWS = ("early", "lead", "onset", "after")


# ---------------------------------------------------------------- incidents
def _utc(s) -> pd.Series:
    return pd.to_datetime(s, utc=True)


def incidents(ep: pd.DataFrame, merge_s: float = 1800) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Episodes (device, indicator, start_t, end_t) -> (incidents, order). Incident = episodes of one device that
    start within merge_s of the incident's running end."""
    ep = ep.copy()
    ep["start_t"], ep["end_t"] = _utc(ep["start_t"]), _utc(ep["end_t"].fillna(ep["start_t"]))
    rows, order = [], []
    for dev, g in ep.sort_values("start_t").groupby("device", sort=True):
        cur = None
        for r in g.itertuples(index=False):
            if cur and (r.start_t - cur["end"]).total_seconds() <= merge_s:
                cur["eps"].append(r)
                cur["end"] = max(cur["end"], r.end_t)
                continue
            if cur:
                rows.append(cur)
            cur = {"device": dev, "start": r.start_t, "end": r.end_t, "eps": [r]}
        if cur:
            rows.append(cur)
    out = []
    for k, c in enumerate(rows):
        first: dict[str, pd.Timestamp] = {}
        for e in c["eps"]:
            first.setdefault(e.indicator, e.start_t)
        seq = sorted(first.items(), key=lambda x: x[1])
        out.append({"device": c["device"], "incident": k + 1, "start": c["start"], "end": c["end"],
                    "duration_s": round((c["end"] - c["start"]).total_seconds(), 1), "episodes": len(c["eps"]),
                    "indicators": len(seq), "first": seq[0][0],
                    "order": ", ".join(f"{i} (+{(t - c['start']).total_seconds():.0f} s)" for i, t in seq)})
        for pos, (i, t) in enumerate(seq):
            order.append({"device": c["device"], "incident": k + 1, "indicator": i, "position": pos + 1,
                          "offset_s": (t - c["start"]).total_seconds()})
    inc = pd.DataFrame(out)
    if not inc.empty:
        inc["incident"] = range(1, len(inc) + 1)
        prev_end = inc.groupby("device")["end"].shift()
        inc["after_prev_h"] = ((inc["start"] - prev_end).dt.total_seconds() / 3600).round(1)
        inc["day"] = inc["start"].dt.strftime("%Y-%m-%d")
    return inc, pd.DataFrame(order)


def load_episodes(patterns: list[str]) -> pd.DataFrame:
    files = sorted({f for p in patterns for f in glob.glob(p)})
    if not files:
        raise LakeError(f"No episode files match {patterns}")
    parts = []
    for f in files:
        d = pd.read_parquet(f) if f.endswith(".parquet") else pd.read_csv(f)
        if d.empty:
            continue
        if "indicator" not in d:
            d["indicator"] = (d["message"].astype(str) + "." + d["signal"].astype(str)) if "message" in d else Path(f).stem
        parts.append(d[["device", "indicator", "start_t", "end_t"]])
    if not parts:
        raise LakeError("The episode files are empty")
    return pd.concat(parts, ignore_index=True)


def run_incidents(a) -> int:
    ep = load_episodes(a.episodes)
    inc, order = incidents(ep, interval_s(a.merge))
    if a.out:
        inc.to_parquet(a.out) if str(a.out).endswith(".parquet") else inc.to_csv(a.out, index=False)
    if a.json:
        print(json.dumps({"incidents": json.loads(inc.to_json(orient="records", date_format="iso")),
                          "first_counts": order[order.position == 1].groupby(["device", "indicator"]).size()
                          .reset_index(name="first").to_dict("records")}, indent=1, default=str))
        return 0
    pd.set_option("display.width", 250)
    pd.set_option("display.max_colwidth", 120)
    pd.set_option("display.max_rows", 500)
    print(f"{len(ep)} episodes of {ep.indicator.nunique()} indicators -> {len(inc)} incidents (merge {a.merge})\n")
    per = inc.groupby("device").agg(incidents=("incident", "size"), days=("day", "nunique"),
                                    episodes=("episodes", "sum"), longest_h=("duration_s", lambda s: round(s.max() / 3600, 2)))
    print("Per device (rank by incidents and days, not episodes):\n" + per.to_string() + "\n")
    show = inc[["device", "incident", "start", "duration_s", "episodes", "after_prev_h", "order"]]
    print("Incidents (indicators in the order they started; after_prev_h = hours since the device's previous incident):")
    print(show.to_string(index=False) + "\n")
    if not order.empty:
        fc = order.groupby(["device", "indicator"]).agg(incidents=("incident", "nunique"),
                                                        first=("position", lambda s: int((s == 1).sum())),
                                                        median_offset_s=("offset_s", "median")).reset_index()
        print("Indicator order per device (first = how often it starts the incident):")
        print(fc.sort_values(["device", "first", "incidents"], ascending=[True, False, False]).to_string(index=False))
    close = inc[inc.after_prev_h.notna() & (inc.after_prev_h < 12)]
    if len(close):
        print(f"\n{len(close)} incidents start < 12 h after the device's previous one: Check whether they are its "
              "aftermath (recovery attempts, restarts, workshop tests) before counting them as separate occurrences")
    return 0


# ---------------------------------------------------------------- align
def load_events(path: Path | None, role: str) -> pd.DataFrame:
    if not path:
        return pd.DataFrame(columns=["device", "t", "label", "role"])
    d = pd.read_parquet(path) if str(path).endswith(".parquet") else pd.read_csv(path)
    if "t" not in d:
        if "start_t" not in d:
            raise LakeError(f"{path}: Needs the columns device + t (or start_t, as an episodes file)")
        d["t"] = d["start_t"]
    d["t"] = _utc(d["t"])
    d["device"] = d["device"].astype(str).str.upper()
    if "label" not in d:
        d["label"] = d["device"] + " " + d["t"].dt.strftime("%Y-%m-%d %H:%M:%S")
    d["role"] = role
    return d[["device", "t", "label", "role"]].reset_index(drop=True)


def _hits(db, dev: str, specs: list[str] | None, match: str | None) -> list[dict]:
    if specs:
        hits = []
        for s in specs:
            msg, _, sig = s.lower().rpartition(".")
            hits += [h for h in db.find(f"^{re.escape(sig)}$", regex=True, device=dev)
                     if h["device"] and (not msg or h["message"] == msg)]
        return hits
    return [h for h in db.find(match or ".*", regex=True, device=dev) if h["device"]]


def _windows(t: pd.Timestamp, pre: float, post: float, gap: float) -> dict:
    s = pd.Timedelta(seconds=1)
    return {"early": (t - 2 * pre * s, t - pre * s), "lead": (t - pre * s, t - gap * s),
            "onset": (t - gap * s, t + gap * s), "after": (t + gap * s, t + post * s)}


def window_stats(db, ev: pd.DataFrame, specs, match, pre, post, gap) -> pd.DataFrame:
    """Per event x window x signal: n_rows, n, min, max, avg (lake summary by total)."""
    rows, hits_of = [], {}
    for k, e in ev.iterrows():
        hits = hits_of.setdefault(e.device, _hits(db, e.device, specs, match))
        if not hits:
            continue
        for w, (a, b) in _windows(e.t, pre, post, gap).items():
            df = db.summary(hits, a.isoformat(), b.isoformat(), by="total", keep_empty=True)
            if df.empty:
                continue
            df = df.assign(event=k, window=w, dur=(b - a).total_seconds())
            rows.append(df[["event", "window", "dur", "device", "message", "signal", "n_rows", "n", "min", "max", "avg"]])
        print(f"  {e.role} {e.label}: {len(hits)} signals", file=sys.stderr)
    if not rows:
        raise LakeError("No data in any window (check the devices, times (UTC) and signals)")
    return pd.concat(rows, ignore_index=True)


def classify(st: pd.DataFrame, ev: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Window stats -> (signal ranking, message rates)."""
    st = st.merge(ev[["role"]], left_on="event", right_index=True)
    key = ["message", "signal"]
    piv = st.pivot_table(index=["event", "role", *key], columns="window", values=["avg", "min", "max", "n"],
                         aggfunc="first")
    piv.columns = [f"{a}_{b}" for a, b in piv.columns]
    piv = piv.reset_index()
    for c in [f"{a}_{w}" for a in ("avg", "min", "max", "n") for w in WINDOWS]:
        if c not in piv:
            piv[c] = np.nan
    # typical within-window spread per signal (early + lead) = the scale of a "change"; constant signals: any change
    spread = pd.concat([piv["max_early"] - piv["min_early"], piv["max_lead"] - piv["min_lead"]])
    spread.index = pd.concat([piv[key], piv[key]]).apply(tuple, axis=1)
    scale = spread.groupby(level=0).median().fillna(0)
    piv["s"] = piv[key].apply(tuple, axis=1).map(scale).fillna(0)
    tiny = 1e-9 + 1e-3 * piv[["avg_early", "avg_lead"]].abs().max(axis=1).fillna(0)
    s = np.maximum(piv["s"], tiny)

    def moved(a, b, lo_b, hi_b):   # window a vs window b: mean shift > half the typical spread, or outside b's range
        out = ((piv[a] - piv[b]).abs() > 0.5 * s)
        out |= (piv[a.replace("avg", "max")] > piv[hi_b] + 0.25 * s) | (piv[a.replace("avg", "min")] < piv[lo_b] - 0.25 * s)
        return out & piv[a].notna() & piv[b].notna()

    piv["before"] = moved("avg_lead", "avg_early", "min_early", "max_early")
    piv["at"] = ((piv["max_onset"] > piv["max_lead"] + 0.25 * s) | (piv["min_onset"] < piv["min_lead"] - 0.25 * s)) \
        & piv["avg_onset"].notna() & piv["avg_lead"].notna()
    piv["after"] = moved("avg_after", "avg_lead", "min_lead", "max_lead")
    # problem lead windows vs the range of the reference lead windows (same signal)
    ref = piv[piv.role == "reference"].groupby(key).agg(rlo=("avg_lead", "min"), rhi=("avg_lead", "max"),
                                                       nref=("avg_lead", "count"))
    piv = piv.merge(ref, left_on=key, right_index=True, how="left")
    piv["vs_ref"] = ((piv["avg_lead"] > piv["rhi"] + 0.25 * s) | (piv["avg_lead"] < piv["rlo"] - 0.25 * s)) \
        & piv["avg_lead"].notna() & (piv["nref"] >= 2)
    agg = piv.groupby([*key, "role"])[["before", "at", "after", "vs_ref"]].mean().unstack("role")
    agg.columns = [f"{a}_{b}" for a, b in agg.columns]
    agg = agg.reset_index()
    for c in ("before", "at", "after", "vs_ref"):
        for r in ("problem", "reference"):
            if f"{c}_{r}" not in agg:
                agg[f"{c}_{r}"] = np.nan
    n = piv[piv.role == "problem"].groupby(key)["event"].nunique().rename("n_problem")
    nr = piv[piv.role == "reference"].groupby(key)["event"].nunique().rename("n_reference")
    agg = agg.merge(n, left_on=key, right_index=True, how="left").merge(nr, left_on=key, right_index=True, how="left")
    agg = agg[agg.n_problem.fillna(0) > 0]
    ref0 = lambda c: agg[f"{c}_reference"].fillna(0)   # noqa: E731
    agg["before_spec"] = np.maximum(agg["before_problem"] - ref0("before"), agg["vs_ref_problem"].fillna(0))
    agg["at_spec"] = agg["at_problem"] - ref0("at")
    agg["after_spec"] = agg["after_problem"] - ref0("after")

    def phase(r):
        if r.before_spec >= 0.5:
            return "before"
        if r.at_spec >= 0.5:
            return "at onset"
        if r.after_spec >= 0.5:
            return "after"
        if max(r.before_problem, r.at_problem, r.after_problem) >= 0.5:
            return "also normal"
        return "no change"
    agg["phase"] = agg.apply(phase, axis=1)
    rank = {"before": 0, "at onset": 1, "after": 2, "also normal": 3, "no change": 4}
    agg["score"] = agg[["before_spec", "at_spec", "after_spec"]].max(axis=1)
    agg = agg.sort_values(by=["phase", "score"], key=lambda c: c.map(rank) if c.name == "phase" else -c)
    # message rates (rows/s) per window: what goes silent or bursts
    mr = st.groupby(["event", "role", "message", "window"]).agg(rows=("n_rows", "max"), dur=("dur", "first")).reset_index()
    mr["rate"] = mr["rows"] / mr["dur"]
    rates = mr.pivot_table(index=["message", "role"], columns="window", values="rate", aggfunc="median").reset_index()
    for w in WINDOWS:
        if w not in rates:
            rates[w] = np.nan
    rates["silent_after"] = rates["after"] < 0.1 * rates["lead"]
    rates["silent_before"] = rates["lead"] < 0.1 * rates["early"]
    return agg.reset_index(drop=True), rates


def traces(db, ev: pd.DataFrame, specs: list[str], pre, post, interval: str) -> list[dict]:
    """One 'aligned' panel per signal: Every event resampled on a common grid of seconds from the onset."""
    step = interval_s(interval)
    grid = np.round(np.arange(-pre, post + step / 2, step), 6)
    panels = []
    for spec in specs:
        msg, _, sig = spec.lower().rpartition(".")
        evs = []
        for _, e in ev.iterrows():
            hits = [h for h in db.find(f"^{re.escape(sig)}$", regex=True, device=e.device)
                    if h["device"] and (not msg or h["message"] == msg)]
            if not hits:
                continue
            a, b = e.t - pd.Timedelta(seconds=pre), e.t + pd.Timedelta(seconds=post)
            df = db.resample(e.device, hits[0]["message"], [sig], a.isoformat(), b.isoformat(), agg="avg",
                             interval=interval)
            vals = [None] * len(grid)
            if not df.empty:
                rel = (_utc(df["time"]) - e.t).dt.total_seconds().to_numpy()
                ix = np.round((rel + pre) / step).astype(int)
                for i, v in zip(ix, df[f"avg_{sig}"].to_numpy()):
                    if 0 <= i < len(grid) and v is not None and not pd.isna(v):
                        vals[i] = float(v)
            evs.append({"label": e.label, "role": e.role, "values": vals})
        if evs:
            panels.append({"type": "aligned", "title": spec, "mode": "fixed", "unit": "",
                           "data": {"t": grid.tolist(), "events": evs},
                           "computed": {"devices": sorted(ev.device.unique().tolist())}})
    return panels


def run_align(a) -> int:
    db = connect_args(a)
    ev = pd.concat([load_events(a.events, "problem"), load_events(a.reference, "reference")], ignore_index=True)
    if ev.empty:
        raise LakeError("No events")
    specs = [s.strip() for s in a.signals.split(",")] if a.signals else None
    if not (specs or a.match or a.all):
        raise LakeError("Give --signals, --match or --all")
    pre, post, gap = interval_s(a.pre), interval_s(a.post), interval_s(a.gap)
    print(f"{(ev.role == 'problem').sum()} problem + {(ev.role == 'reference').sum()} reference events, "
          f"windows: lead {a.pre}, onset +-{a.gap}, after {a.post}", file=sys.stderr)
    st = window_stats(db, ev, specs, None if a.all else a.match, pre, post, gap)
    rank, rates = classify(st, ev)
    panels = traces(db, ev, [s.strip() for s in a.trace.split(",")], pre, post, a.interval) if a.trace else []
    if a.out:
        out = {"events": json.loads(ev.to_json(orient="records", date_format="iso")),
               "windows": {"pre_s": pre, "post_s": post, "gap_s": gap},
               "ranking": json.loads(rank.to_json(orient="records")),
               "rates": json.loads(rates.to_json(orient="records")), "panels": panels}
        Path(a.out).write_text(json.dumps(out, indent=1), encoding="utf-8")
        print(f"Saved {a.out}", file=sys.stderr)
    pd.set_option("display.width", 250)
    pd.set_option("display.max_rows", 500)
    cols = ["message", "signal", "phase", "before_problem", "vs_ref_problem", "at_problem", "after_problem",
            "before_reference", "at_reference", "after_reference", "n_problem", "n_reference"]
    show = rank[rank.phase.isin(["before", "at onset", "after"])][cols].head(a.top)
    print("Signals by phase (shares of the problem / reference events; 'before' = changes in the lead-up and not at the "
          "reference events = leads for a cause; 'after' = likely consequences):")
    print(show.round(2).to_string(index=False) if len(show) else "  (no signal changes more around the problem onsets "
          "than around the reference events)")
    sil = rates[(rates.role == "problem") & (rates.silent_after | rates.silent_before)]
    if len(sil):
        print("\nMessages that go silent (median rows/s per window, problem events):")
        print(sil[["message", "early", "lead", "onset", "after", "silent_before", "silent_after"]].round(2).to_string(index=False))
    print(f"\n{(rank.phase == 'also normal').sum()} signals change as much around the reference events (normal), "
          f"{(rank.phase == 'no change').sum()} do not change")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("incidents", help="Group indicator episodes into incidents, in onset order")
    p.add_argument("--episodes", nargs="+", required=True, help="Episode files (lake_query.py episodes --out), globs ok")
    p.add_argument("--merge", default="30min", help="Episodes starting within this of the incident's end join it")
    p.add_argument("--out", help="Save the incidents (.parquet / .csv)")
    p.add_argument("--json", action="store_true")
    p = sub.add_parser("align", help="Signals before / at / after the onsets vs reference moments")
    add_source_args(p, sub=True)
    p.add_argument("--events", type=Path, required=True, help="Problem onsets: device + t (or an episodes file)")
    p.add_argument("--reference", type=Path, help="Reference moments without the problem: device + t")
    p.add_argument("--signals", help="Comma-separated signal or message.signal")
    p.add_argument("--match", help="Regex on signal names (all tables of the event's device)")
    p.add_argument("--all", action="store_true", help="Every signal of the event's device")
    p.add_argument("--pre", default="10min")
    p.add_argument("--post", default="10min")
    p.add_argument("--gap", default="5s", help="Half-width of the onset window")
    p.add_argument("--trace", help="Signals for aligned traces (page panels in --out)")
    p.add_argument("--interval", default="1s", help="Trace resolution")
    p.add_argument("--out", help="JSON with events, ranking, rates and the aligned panels")
    p.add_argument("--top", type=int, default=40)
    ap.set_defaults(project=None, lake=None, athena=False, bigquery=False, synapse=False, env=None)
    a = ap.parse_args()
    try:
        return run_incidents(a) if a.cmd == "incidents" else run_align(a)
    except LakeError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
