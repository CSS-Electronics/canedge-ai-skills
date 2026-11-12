"""Build a self-contained dashboard HTML from the shared kit (assets/dashboard.html/.css/.js) + an app's own JS.

One file, no build tooling: The website fonts, the report CSS (build_report.CSS), uPlot (vendored, MIT) + the shared
stacked-bar chart, the dashboard kit, the app's CSS/JS and its data are pasted into the template. It works offline
and can be emailed or shared as is. Used by canedge-raw-overview, canedge-s3-overview, canedge-lake-analysis (assets/
dashboard.html: filter bar + tabs), canedge-config and canedge-dbc (own templates with fixed panels, DASH.page(),
Diff tabs via line_diff() + diff_parts()).

  from dashboard import build_html
  html = build_html(title, app_js=Path(".../app.js"), data={"FACTS": facts, "CFG": cfg}, app_css="")
"""
from __future__ import annotations

import base64
import difflib
import json
from pathlib import Path

from build_report import CSS as REPORT_CSS

ASSETS = Path(__file__).resolve().parent.parent / "assets"
FONTS = (("JetBrains Mono", 400, "JetBrainsMono-Regular.woff2"), ("Open Sans", 400, "open-sans-v28-latin-regular.woff2"),
         ("Open Sans", 700, "open-sans-v28-latin-700.woff2"))  # the website fonts (assets/fonts, OFL)


def js_safe(obj) -> str:
    """JSON for a <script> block (no closing tag inside strings)."""
    return json.dumps(obj, separators=(",", ":")).replace("</", "<\\/")


def fonts_css() -> str:
    d = ASSETS / "fonts"
    return "".join(f"@font-face{{font-family:'{n}';font-weight:{w};font-style:normal;font-display:swap;"
                   f"src:url(data:font/woff2;base64,{base64.b64encode((d / f).read_bytes()).decode()}) format('woff2')}}"
                   for n, w, f in FONTS)


def favicon() -> str:
    return "data:image/x-icon;base64," + base64.b64encode((ASSETS / "favicon.ico").read_bytes()).decode()


KIT = {  # optional kit modules: name -> (JS files, CSS files) in assets/, inlined only for the pages that use them
    "series": (["series.js"], []),   # SERIES: zoom group, line charts, state timelines (uPlot)
    "track": (["leaflet/leaflet.js", "track.js"], ["leaflet/leaflet.css", "track.css"]),   # TRACK: GNSS map (Leaflet)
}


def line_diff(a: list[str], b: list[str], names: list[str]) -> dict:
    """Unified line diff of two texts (as lines) for a Diff tab (diff2html renders it as the CANedge editors do):
    {"text": git-style diff, "" if identical; "added" / "removed": changed lines}."""
    lines = list(difflib.unified_diff(a, b, fromfile=names[0], tofile=names[1], lineterm=""))
    body = [x for x in lines[2:] if not x.startswith("@@")]
    return {"text": f"diff --git {names[0]} {names[1]}\n" + "\n".join(lines) if lines else "",
            "added": sum(x.startswith("+") for x in body), "removed": sum(x.startswith("-") for x in body)}


def diff_parts(on: bool = True) -> dict:
    """The template placeholders of a page with a Diff tab: diff2html (vendored, MIT) + diff.js (DIFF.html). Empty
    when the page shows no diff (smaller file)."""
    d = ASSETS / "diff2html"
    if not on:
        return {"__DIFF_CSS__": "", "__DIFF_JS__": ""}
    return {"__DIFF_CSS__": (d / "diff2html.min.css").read_text(encoding="utf-8"),
            "__DIFF_JS__": (d / "diff2html.min.js").read_text(encoding="utf-8") + "\n"
                           + (ASSETS / "diff.js").read_text(encoding="utf-8")}


def build_html(title: str, app_js: Path, data: dict, app_css: str = "", template: Path | None = None,
               extra: dict | None = None, kit: list[str] | tuple = ()) -> str:
    """data: global JS constants for the app (name -> JSON value), e.g. {"FACTS": ..., "CFG": ...}. template: an own
    page template with the same placeholders (default assets/dashboard.html); extra: more {placeholder: text};
    kit: optional kit modules (KIT), e.g. ["series"]."""
    up = ASSETS / "uplot"
    html = (template or ASSETS / "dashboard.html").read_text(encoding="utf-8")
    bad = [k for k in kit if k not in KIT]
    if bad:
        raise ValueError(f"Unknown kit module(s) {bad} (known: {', '.join(KIT)})")
    kit_js = "".join("\n" + (ASSETS / f).read_text(encoding="utf-8") for k in kit for f in KIT[k][0])
    kit_css = "".join("\n" + (ASSETS / f).read_text(encoding="utf-8") for k in kit for f in KIT[k][1])
    parts = {"__TITLE__": title, "__FAVICON__": favicon(), "__FONTS__": fonts_css(), "__REPORT_CSS__": REPORT_CSS,
             "__UPLOT_CSS__": (up / "uPlot.min.css").read_text(encoding="utf-8") + kit_css,
             "__DASH_CSS__": (ASSETS / "dashboard.css").read_text(encoding="utf-8"), "__APP_CSS__": app_css,
             "__UPLOT_JS__": (up / "uPlot.iife.min.js").read_text(encoding="utf-8") + "\n"
                             + (ASSETS / "stacked-bars.js").read_text(encoding="utf-8") + kit_js,
             "__DATA__": " ".join(f"const {k} = {js_safe(v)};" for k, v in data.items()),
             "__DASH_JS__": (ASSETS / "dashboard.js").read_text(encoding="utf-8"),
             "__APP_JS__": app_js.read_text(encoding="utf-8"), **(extra or {})}
    for k, v in parts.items():
        html = html.replace(k, v, 1)
    return html
