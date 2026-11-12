"""Build the raw CAN data overview dashboard: ONE self-contained report.html (+ stats.json for the agent).

The dashboard filters by device (multi-select) and time range (brush / date inputs) in the browser: It sums the
embedded facts (facts.py), so no log file is re-processed. Run raw_stats.py first (cached per-file stats).

Single file, no build tooling: The shared dashboard kit (canedge-reports dashboard.py: Template, CSS, uPlot, stacked-bar
chart, dashboard.js), app.js and the facts JSON are pasted into one report.html. It works offline and can be emailed or shared as is.
Print / Save as PDF in the browser prints the current filtered view.

Times: Data in UTC; shown in --tz, else project.json display_tz, else the viewer's PC time zone.

Usage:
  python build_dashboard.py --project projects/<name>        (out: <project>/reports/<yyyymmdd>-raw-overview/)
  python build_dashboard.py <file/folder> [...] [--out <folder>] [--device ID] [--start ..] [--end ..]
                            [--tz Europe/Berlin]       (out without a project: TEMP/reports/<yyyymmdd>-raw-overview/)
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILL = HERE.parent
REPORTS = SKILL.parent / "canedge-reports"
sys.path.insert(0, str(HERE))
sys.path.insert(0, str(REPORTS / "scripts"))
from raw_aggregate import add_args, default_out, public, run, select  # noqa: E402
from facts import build_facts, facts_summary  # noqa: E402
from dashboard import build_html as build_dashboard_html  # noqa: E402
from display_time import DisplayTime  # noqa: E402
from paths import open_line  # noqa: E402  (canedge-workspace, on sys.path via raw_stats)


def build_html(facts: dict, cfg: dict) -> str:
    return build_dashboard_html(cfg["title"], SKILL / "assets" / "app.js", {"FACTS": facts, "CFG": cfg})


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    add_args(ap)
    ap.add_argument("--out", type=Path, help="Output folder (default: <project>/reports/<yyyymmdd>-raw-overview/, "
                    "without --project TEMP/reports/<yyyymmdd>-raw-overview/)")
    ap.add_argument("--tz", help="Display time zone (IANA name, e.g. Europe/Berlin). Default: project display_tz, else the viewer's PC zone")
    a = ap.parse_args()
    if not (a.paths or a.device or a.project):
        ap.error("give log paths, --device or --project")
    out = a.out or default_out(a.project)
    out.mkdir(parents=True, exist_ok=True)
    project_cfg = json.loads((a.project / "project.json").read_text(encoding="utf-8")) if a.project else {}

    stats = run(a)  # stats.json: the agent layer (same per-minute model as the facts)
    if not stats.get("totals"):
        print(f"No cached stats found ({stats['selection']['files_missing_stats']} log files without stats) - run raw_stats.py first")
        return 1
    (out / "stats.json").write_text(json.dumps(public(stats), indent=1), encoding="utf-8")

    sel = select(list(a.paths), {d.upper() for d in a.device or []}, a.project, a.stats_root)
    files = sel["stats"]
    if a.start or a.end:  # same per-file time filter as raw_aggregate
        from raw_aggregate import ts
        s0, s1 = ts(a.start) if a.start else -1e18, ts(a.end) if a.end else 1e18
        files = [s for s in files if ts(s["time"]["last_utc"]) >= s0 and ts(s["time"]["first_utc"]) <= s1]
    facts = build_facts(files, a)
    cfg = {"title": "Raw CAN data overview",
           "tz": DisplayTime.from_project(project_cfg, a.tz).page_tz(),
           "generated": dt.datetime.now(dt.timezone.utc).strftime("%y/%m/%d %H:%M UTC"),
           "sources": stats["selection"]["inputs"][:10],
           "tool": ", ".join(stats["tools"]["mdf2csv"]) + "".join(f" + mdf2mdf {v} (CANmod.router demux)"
                                                                  for v in stats["tools"].get("mdf2mdf", [])),
           "activity_ignore": sorted({c.upper() for c in a.activity_ignore or []})}
    html = build_html(facts, cfg)
    (out / "report.html").write_text(html, encoding="utf-8")
    print(facts_summary(facts))
    print(f"HTML: {out / 'report.html'} ({len(html.encode('utf-8')) / 1024:.0f} KB, single file)")
    print(f"stats.json (agent layer): {out / 'stats.json'} ({(out / 'stats.json').stat().st_size / 1024:.0f} KB)")
    for d, v in stats["devices"].items():
        h = v.get("router", {}).get("router_hint")
        if h:
            print(f"NOTE: {d}: {h['files']} files look like CANmod.router data that is not demuxed ({', '.join(h['ids'])} "
                  f"CAN FD tunnel frames). Confirm with the user, then add the router DBC to the project dbc/ folder "
                  f"and re-run raw_stats.py + this script (canedge-workspace conventions, CANmod.router)")
    if stats["selection"]["files_missing_stats"]:
        print(f"WARNING: {stats['selection']['files_missing_stats']} input files have no stats and are not in the report")
    print(open_line(out / "report.html", "dashboard"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
