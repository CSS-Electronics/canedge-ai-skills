r"""Build the analysis page (report.html) from results.json on the shared dashboard kit (canedge-reports), and the
helpers that turn canedge-lake-query results into the page's facts tables.

  .venv\Scripts\python.exe .claude/skills/canedge-lake-analysis/scripts/build_analysis.py <folder>/results.json
      [--custom-js <folder>/custom.js] [--out <folder>/report.html]

Module use (the analysis script, e.g. projects/<p>/reports/<yyyymmdd>-<slug>/analysis.py):
  sys.path.insert(0, ".claude/skills/canedge-lake-analysis/scripts")
  from build_analysis import new_results, facts_summary, facts_hist, facts_episodes, save
  R = new_results(db, "Duty cycle - 4 vehicles", start, end, question="...")
  R["facts"]["daily"] = facts_summary(summary_df)          # db.summary(..., by="day")
  R["facts"]["bands"] = facts_hist(hist_df)                # db.hist(..., by="day")
  R["tabs"] = [{"title": "Duty cycle", "panels": [{"type": "bars", "facts": "bands", "scale": 1 / 3600, "unit": "h"}]}]
  save(R, folder)                                          # results.json + report.html, prints the OPEN link
Format: references/results-spec.md. The page holds aggregated results only and works offline.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import math
import re
import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "canedge-reports" / "scripts"))
sys.path.insert(0, str(SKILLS / "canedge-workspace" / "scripts"))
from build_report import source_note  # noqa: E402
from dashboard import build_html  # noqa: E402
from paths import open_line  # noqa: E402

ASSETS = Path(__file__).resolve().parent.parent / "assets"
SCHEMA = "canedge-lake-analysis/1"
MAX_MB = 15


def epoch(x) -> int | None:
    """'yyyy/MM/dd' (date_created), ISO string, datetime or pandas Timestamp -> epoch seconds (UTC)."""
    if x is None or (isinstance(x, float) and math.isnan(x)):
        return None
    import pandas as pd
    if isinstance(x, str) and len(x) == 10 and x[4] == "/":
        x = x.replace("/", "-")
    ts = pd.Timestamp(x)
    if ts is pd.NaT:
        return None
    ts = ts.tz_localize("UTC") if ts.tzinfo is None else ts.tz_convert("UTC")
    return int(ts.timestamp()) if ts.timestamp() == int(ts.timestamp()) else round(ts.timestamp(), 3)


def _clean(v):
    if v is None:
        return None
    if isinstance(v, float):
        return None if math.isnan(v) or math.isinf(v) else (round(v, 6) if abs(v) < 1e15 else v)
    if hasattr(v, "item"):   # numpy scalars
        return _clean(v.item())
    return v


def _deep(o):
    """JSON-safe copy: NaN/inf -> null, numpy scalars -> Python, timestamps -> ISO."""
    if isinstance(o, dict):
        return {str(k): _deep(v) for k, v in o.items()}
    if isinstance(o, (list, tuple)):
        return [_deep(v) for v in o]
    if hasattr(o, "isoformat"):
        return o.isoformat()
    return _clean(o)


def _facts(df, cols: list[str], time_col: str | None, period: int | None, extra: dict | None = None) -> dict:
    cols = [c for c in cols if c in df.columns]
    rows = []
    for r in df[cols].itertuples(index=False):
        row = [_clean(x) for x in r]
        if time_col:
            i = cols.index(time_col)
            row[i] = epoch(r[i])
        rows.append(row)
    out_cols = ["t" if c == time_col else c for c in cols]
    return {"columns": out_cols, "rows": rows, **({"period": period} if period else {}), **(extra or {})}


def facts_summary(df) -> dict:
    """db.summary() / summary --out -> facts: device, t (period start), message, signal, n_rows, n, sum, min, max.
    by='total' has no time column (not filtered by period)."""
    by = df.attrs.get("by") or ("total" if set(df["period"].astype(str)) == {"total"} else "day")
    cols = ["device", "period", "message", "signal", "n_rows", "n", "sum", "min", "max"]
    if by == "total":
        return _facts(df, [c for c in cols if c != "period"], None, None)
    return _facts(df, cols, "period", 86400 if by == "day" else 3600)


def facts_hist(df) -> dict:
    """db.hist() -> facts: device, t, message, signal, bin, label, seconds, n (by='total': no time column)."""
    cols = ["device", "period", "message", "signal", "bin", "label", "seconds", "n"]
    if str(df.attrs.get("by")) == "total" or (len(df) and set(df["period"].astype(str)) == {"total"}):
        return _facts(df, [c for c in cols if c != "period"], None, None)
    return _facts(df, cols, "period", 86400 if df.attrs.get("by", "day") == "day" else 3600)


def facts_episodes(df) -> dict:
    """db.episodes() -> facts: device, t (start), end, duration_s, message, signal, max, min."""
    d = df.copy()
    d["end"] = [epoch(x) for x in d["end_t"]] if len(d) else []
    return _facts(d, ["device", "start_t", "end", "duration_s", "message", "signal", "max", "min"], "start_t", None)


def new_results(db, title: str, start, end, question: str = "", devices: list[str] | None = None,
                tz: str | None = None) -> dict:
    """The frame of results.json: Devices (ID -> meta name) of the lake or the given IDs, window, provenance basics."""
    names = db.devices()
    ids = devices or sorted(names)
    gen = dt.datetime.now(dt.timezone.utc)
    return {"schema": SCHEMA, "title": title, "question": question, "summary": [], "confidence": "", "tz": tz,
            "devices": {d: names.get(d) for d in ids}, "window": [_iso(start), _iso(end)], "step": 86400,
            "activity": None, "facts": {}, "findings": [], "tabs": [], "context": {"signals": [], "tracks": []},
            "generated": gen.strftime("%y/%m/%d %H:%M UTC"),
            "provenance": {"data_source": db.describe_source(), "devices": ids,
                           "time_range_utc": [_iso(start), _iso(end)], "generated_utc": gen.isoformat(timespec="seconds")}}


def _iso(x) -> str:
    return dt.datetime.fromtimestamp(epoch(x), dt.timezone.utc).isoformat().replace("+00:00", "Z")


def check(R: dict) -> list[str]:
    """Spec problems that would break the page (missing facts / columns, unknown panel types)."""
    errs = []
    types = {"kpis", "table", "series", "bars", "episodes", "context", "custom", "note", "aligned"}
    facts = R.get("facts", {})
    for t in R.get("tabs", []):
        for p in t.get("panels", []):
            if p.get("type") not in types:
                errs.append(f"Tab '{t.get('title')}': unknown panel type {p.get('type')!r}")
            refs = [p.get("facts")] + [it.get("facts") for g in p.get("groups", []) for it in g.get("items", [])]
            for f in filter(None, refs):
                if f not in facts:
                    errs.append(f"Tab '{t.get('title')}': facts '{f}' not in results.json")
            if p.get("mode") == "fixed" and p.get("type") == "table" and not ("columns" in p and "rows" in p):
                errs.append(f"Tab '{t.get('title')}': fixed table needs columns + rows")
    if R.get("activity") and R["activity"] not in facts:
        errs.append(f"activity: facts '{R['activity']}' not in results.json")
    for name, f in facts.items():
        if "device" not in f.get("columns", []):
            errs.append(f"facts '{name}': no device column (not filterable)")
    return errs


SUMMARY_MAX_CHARS = 100   # one line at laptop width (15 px text in the ~990 px page column)
HEADLINE_MAX_CHARS = 68   # one line as the page title (23 px monospace)
CARD_TITLE_MAX, CARD_NOTE_MAX = 22, 34   # one line each in a card at 4 cards per row (the title is upper case)


def summary_warnings(R: dict) -> list[str]:
    """The executive summary rules: 3-5 bullets, each one line, bold on the key phrase only (at most once)."""
    out, bullets = [], R.get("summary") or []
    for i, c in enumerate(([R["answer"]] if R.get("answer") else []) + (R.get("findings") or [])):
        for key, lim in (("title", CARD_TITLE_MAX), ("note", CARD_NOTE_MAX)):
            if len(str(c.get(key) or "")) > lim:
                out.append(f"card {i + 1} {key} has {len(str(c[key]))} characters (> {lim}: cut off with ...): {c[key]}")
    head = ((R.get("answer") or {}).get("headline") or "")
    if len(head) > HEADLINE_MAX_CHARS:
        out.append(f"answer.headline has {len(head)} characters (> {HEADLINE_MAX_CHARS}: wraps as the page title); the numbers belong in the summary")
    if bullets and not 3 <= len(bullets) <= 5:
        out.append(f"summary has {len(bullets)} bullets (use 3-5; the page shows at most 5)")
    for i, x in enumerate(bullets, 1):
        bolds = re.findall(r"\*\*(.+?)\*\*", x)
        plain = x.replace("**", "")
        if len(plain) > SUMMARY_MAX_CHARS:
            out.append(f"summary bullet {i} has {len(plain)} characters (> {SUMMARY_MAX_CHARS}: wraps): {plain[:60]}...")
        if len(bolds) > 1:
            out.append(f"summary bullet {i}: {len(bolds)} bold parts (bold only the key phrase, once)")
        if bolds and sum(map(len, bolds)) > 0.6 * len(plain):
            out.append(f"summary bullet {i}: most of it is bold (bold only the key phrase)")
    return out


def build(results: Path, custom_js: Path | None = None, out: Path | None = None) -> Path:
    R = json.loads(results.read_text(encoding="utf-8"))
    errs = check(R)
    if errs:
        raise SystemExit("results.json problems:\n  " + "\n  ".join(errs))
    for w in summary_warnings(R):
        print(f"WARNING: {w}")
    app = ASSETS / "app.js"
    js = app.read_text(encoding="utf-8") + ("\n" + custom_js.read_text(encoding="utf-8") if custom_js else "")
    tmp = results.parent / ".app-bundle.js"   # build_html reads the app JS from a file
    tmp.write_text(js, encoding="utf-8")
    try:
        cfg = {"tz": R.get("tz"), "source": source_note(R.get("provenance") or {})}   # source: footer sentence
        html = build_html(R.get("title", "Analysis"), tmp, {"R": R, "CFG": cfg},
                          app_css=(ASSETS / "app.css").read_text(encoding="utf-8"), kit=["series", "track"])
    finally:
        tmp.unlink(missing_ok=True)
    out = out or results.parent / "report.html"
    out.write_text(html, encoding="utf-8")
    mb = len(html.encode("utf-8")) / 1e6
    if mb > MAX_MB:
        print(f"WARNING: {out} is {mb:.1f} MB (> {MAX_MB} MB): Fewer context points (context.py --points) or coarser facts")
    print(f"Saved: {out} ({out.stat().st_size / 1e6:.1f} MB)")
    print(open_line(out, "report"))
    return out


def save(R: dict, folder: str | Path, custom_js: str | Path | None = None) -> Path:
    """Write results.json (NaN -> null) into folder and build report.html next to it. Without context signals in R,
    the context of an existing results.json (context.py) is kept, so re-running analysis.py does not drop it."""
    folder = Path(folder)
    folder.mkdir(parents=True, exist_ok=True)
    p = folder / "results.json"
    ctx = R.get("context") or {}
    if p.exists() and not (ctx.get("signals") or ctx.get("tracks")):
        old = json.loads(p.read_text(encoding="utf-8")).get("context") or {}
        if old.get("signals") or old.get("tracks"):
            R["context"] = old
            print("Kept the context of the existing results.json (context.py: re-run it for new windows/signals)")
    p.write_text(json.dumps(_deep(R), allow_nan=False, separators=(",", ":")), encoding="utf-8")
    return build(p, Path(custom_js) if custom_js else None)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("results", type=Path, help="results.json")
    ap.add_argument("--custom-js", type=Path, help="JS with ANALYSIS.panel('name', (el, view, R, panel) => ...)")
    ap.add_argument("--out", type=Path, help="Default: report.html next to results.json")
    a = ap.parse_args()
    build(a.results, a.custom_js, a.out)
    return 0


if __name__ == "__main__":
    sys.exit(main())
