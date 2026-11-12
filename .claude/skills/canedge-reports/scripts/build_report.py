"""Build a shareable report (self-contained HTML + JSON) from a report spec.

Usage:
  python build_report.py --spec <dir>/report_spec.json [--out <dir>]

The spec is JSON (see references/report-spec.md). Figures are SVG/PNG files referenced relative to the
spec file and embedded into the HTML, so report.html is a single file you can email or upload anywhere.
The HTML is interactive (collapsible details, hover tooltips on any element with a data-tip attribute,
incl. elements inside SVG figures). report.json = the spec + its "data" block, for agents/follow-up questions.
The last line ("OPEN report: file:///...") is the link for the reply (canedge-workspace "Replies to the user").
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import html
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
from paths import open_line  # noqa: E402

CSS = """
:root{--blue:#3d85c6;--red:#ff6666;--orange:#ff9900;--green:#10cba9;--text:#2b2f36;--muted:#6b717b;--line:#e3e7ec;--bg:#f6f8fa;--bar-h:12px}
*{box-sizing:border-box}body{margin:0;background:#fff;color:var(--text);font:14px/1.5 -apple-system,Segoe UI,Roboto,Helvetica,Arial,sans-serif}
.wrap{max-width:1040px;margin:0 auto;padding:28px 24px 48px}
header{border-bottom:3px solid var(--blue);padding-bottom:12px;margin-bottom:18px}
h1{font-size:24px;margin:0 0 4px}h2{font-size:17px;margin:52px 0 10px;padding-bottom:4px;border-bottom:1px solid var(--line)}
.sub{color:var(--muted)}
.summary{border:1px solid var(--line);border-radius:6px;padding:12px 16px;margin:14px 0}.summary ul{margin:6px 0 0;padding-left:20px}
.conf{color:var(--muted);font-size:13px;margin-top:6px}
.kpis{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));gap:10px;margin:16px 0}
.kpi{border:1px solid var(--line);border-radius:6px;padding:10px 12px}.kpi .v{font-size:22px;font-weight:700;color:var(--blue);white-space:nowrap}
.kpi .l{font-size:12px;color:var(--muted);text-transform:uppercase;letter-spacing:.03em;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.kpi .n{font-size:12px;color:var(--muted)}.kpi.warn .v{color:var(--red)}
.kgroups{display:grid;grid-template-columns:repeat(auto-fit,minmax(220px,1fr));gap:12px;margin:18px 0}
.kgroup{border:1px solid var(--line);border-radius:8px;padding:10px 14px 4px}
.kgroup .gt{font-size:11px;font-weight:700;color:var(--blue);text-transform:uppercase;letter-spacing:.06em;margin-bottom:4px}
.krow{display:flex;justify-content:space-between;align-items:baseline;gap:8px;padding:6px 0;border-top:1px solid var(--line)}
.krow:first-of-type{border-top:0}.krow .l{font-size:13px;color:var(--muted);white-space:nowrap}
.krow .v{font-size:19px;font-weight:700;color:var(--text);white-space:nowrap}.krow.warn .v{color:var(--red)}
.krow .n{font-size:11.5px;color:var(--muted);text-align:right;line-height:1.3}
.insights{margin:8px 0 12px;padding:0;list-style:none}
.insights li{position:relative;padding:4px 0 4px 18px;font-size:14.5px}
.insights li:before{content:"";position:absolute;left:2px;top:12px;width:7px;height:7px;border-radius:50%;background:var(--blue)}
.info{display:inline-flex;align-items:center;justify-content:center;width:16px;height:16px;border-radius:50%;border:1px solid #b9c0c9;
color:#7d848e;font:600 10.5px/1 Georgia,serif;font-style:italic;cursor:help;margin-left:6px;vertical-align:2px}
.info:hover{border-color:var(--blue);color:var(--blue)}
table{border-collapse:collapse;width:100%;margin:8px 0;font-size:13px}th,td{border-bottom:1px solid var(--line);padding:5px 8px;text-align:left;vertical-align:middle}
th{background:var(--bg);font-weight:600}tr.sub td{background:#f8f9fb}table.lim tr.xtra{display:none}table.lim.open tr.xtra{display:table-row}
.more{background:none;border:0;padding:6px 8px;color:var(--blue);font:inherit;font-size:13px;cursor:pointer}.more:hover{text-decoration:underline}.ttitle{font-weight:600;font-size:14.5px;margin:22px 0 4px}tr.grp td{border-top:1px solid #6b717b}td.num,th.num{text-align:right;font-variant-numeric:tabular-nums}
td.bar{min-width:140px}.bc{display:flex;align-items:center;gap:8px}.bc .t{flex:0 0 58px;text-align:right;font-variant-numeric:tabular-nums}
.bc .k{flex:1;height:var(--bar-h);background:#eef1f4;border-radius:2px;overflow:hidden}.bc .f{height:100%;background:var(--blue);opacity:.75}
.stack{display:flex;height:var(--bar-h);min-width:160px;border-radius:2px;overflow:hidden;background:#eef1f4}.stack span{height:100%}
figure{margin:12px 0 4px}table+figure,div.cap+figure,details+figure{margin-top:36px}table+.ttitle,.more+.ttitle,figure+.ttitle{margin-top:34px}figure figcaption:only-child,figure svg+figcaption{text-align:right;margin-top:-4px}figure svg,figure img{width:100%;height:auto}figcaption{font-size:12px;color:var(--muted)}
figcaption .src{display:block;font-family:Consolas,monospace;font-size:11px}
details{margin:12px 0 -1px}details+details{margin-top:0}
details>summary{display:block;cursor:pointer;user-select:none;list-style:none;background:#fff;color:#434343;padding:8px 10px;
font-size:15px;border:solid #e6e6e6;border-width:1px 0}
details>summary::-webkit-details-marker{display:none}details>summary:hover{background:#ededed}
details>summary:after{content:"\\002B";float:right;margin-right:10px;font-weight:bold;font-size:110%}
details[open]>summary{background:var(--blue);color:#fff}details[open]>summary:after{content:"\\2212"}
details>:not(summary){margin-left:10px;margin-right:18px}details>table{width:calc(100% - 28px)}details[open]{padding-bottom:6px}
pre{background:var(--bg);border:1px solid var(--line);padding:10px;overflow-x:auto;font-size:12px;white-space:pre-wrap}
.cap{font-size:12px;color:var(--muted)}footer{margin-top:36px;color:var(--muted);font-size:12px;border-top:1px solid var(--line);padding-top:8px}
#tip{position:fixed;z-index:10;display:none;max-width:460px;background:#2b2f36;color:#fff;font-size:12px;line-height:1.45;padding:7px 10px;
border-radius:5px;white-space:pre-line;pointer-events:none;box-shadow:0 2px 8px rgba(0,0,0,.2)}
[data-tip]{cursor:help}svg [data-tip]:hover{opacity:.75}svg rect.hc:hover{opacity:1;fill-opacity:.07}
.tabs{display:flex;flex-wrap:wrap;border-bottom:1px solid #cbcbcb;margin:30px 0 0}
.tab{background:#fff;border:0;border-bottom:5px solid #fff;padding:8px 16px;font:inherit;font-size:15px;
color:#434343;text-transform:uppercase;cursor:pointer;transition:background-color .2s,color .2s,border-bottom-color .2s}
.tab:hover{background:#eee}.tab.on{color:var(--blue);background:#fff;border-bottom-color:var(--blue)}
.tab .info{text-transform:none}
.panel{display:none;padding-top:6px}.panel.on{display:block}.panel>h2:first-child{display:none}
.panel+details.block{margin-top:40px}
.u-legend .u-value{min-width:3.6em;text-align:left;font-variant-numeric:tabular-nums}.u-legend .u-series:first-child .u-value{min-width:11em}
@media print{table.lim tr.xtra{display:table-row!important}.more{display:none}.tabs{display:none}.panel{display:block!important}.panel>h2:first-child{display:block!important}}
@media print{.wrap{max-width:none;padding:0}h2{break-after:avoid}figure,table,.kpis,.kgroups{break-inside:avoid}.info,#tip{display:none!important}
details>summary:after{content:""}details[open]>summary{background:#fff;color:#434343;font-weight:600}}
@page{size:A4;margin:14mm}
"""

DEFAULT_DECIMALS = 1  # decimals for table columns with non-whole numbers (override per table: "decimals")
VISIBLE_ROWS = 15  # longer tables show this many rows + a "+ X more" button (all rows when printed)

# Tooltip for every element with a data-tip attribute (HTML or SVG); text only (no HTML injection)
JS = """(function(){var t=document.getElementById('tip');
document.addEventListener('mouseover',function(e){var el=e.target.closest&&e.target.closest('[data-tip]');
if(!el){t.style.display='none';return;}t.textContent=el.getAttribute('data-tip');t.style.display='block';});
document.addEventListener('mousemove',function(e){if(t.style.display!=='block')return;
var x=e.clientX+14,y=e.clientY+16;if(x+t.offsetWidth>innerWidth-8)x=e.clientX-t.offsetWidth-14;
if(y+t.offsetHeight>innerHeight-8)y=e.clientY-t.offsetHeight-12;t.style.left=x+'px';t.style.top=y+'px';});
document.querySelectorAll('.tab').forEach(function(b){b.addEventListener('click',function(){
document.querySelectorAll('.tab,.panel').forEach(function(x){x.classList.remove('on');});
b.classList.add('on');document.querySelector(".panel[data-panel='"+b.dataset.tab+"']").classList.add('on');});});
document.querySelectorAll('.more').forEach(function(b){b.addEventListener('click',function(){
var t=b.previousElementSibling,o=t.classList.toggle('open');b.textContent=o?'Show less':b.dataset.more;});});})();"""


def esc(s) -> str:
    """Escape text; allow **bold** and `code` as the only markup."""
    t = html.escape(str(s))
    t = re.sub(r"\*\*(.+?)\*\*", r"<strong>\1</strong>", t)
    return re.sub(r"`(.+?)`", r"<code>\1</code>", t)


def info(tip: str | None) -> str:
    """Small (i) icon that shows `tip` on hover (hidden when printed)."""
    return f"<span class='info' data-tip='{html.escape(str(tip), quote=True)}'>i</span>" if tip else ""


def embed_figure(fig: dict, base: Path, compact: bool) -> str:
    p = (base / fig["file"]).resolve()
    if not p.exists():
        body = f"<p class='cap'>[missing figure: {html.escape(fig['file'])}]</p>"
    elif p.suffix.lower() == ".svg":
        svg = p.read_text(encoding="utf-8")
        body = svg[svg.find("<svg"):]  # strip XML prolog/doctype
    else:
        mime = "image/png" if p.suffix.lower() == ".png" else "image/jpeg"
        body = f"<img alt='' src='data:{mime};base64,{base64.b64encode(p.read_bytes()).decode()}'>"
    if compact:  # caption + source only on hover: in the title's (i), else in a small (i) below the figure
        tip = "\n".join(x for x in (fig.get("caption"), f"Source: {fig['source']}" if fig.get("source") else None) if x)
        if fig.get("title"):
            return f"<div class='ttitle'>{esc(fig['title'])}{info(tip)}</div><figure>{body}</figure>"
        cap = f"<figcaption>{info(tip)}</figcaption>" if tip else ""
        return f"<figure>{body}{cap}</figure>"
    cap = esc(fig.get("caption", ""))
    src = f"<span class='src'>Source: {html.escape(fig['source'])}</span>" if fig.get("source") else ""
    return f"<figure>{body}<figcaption>{cap}{src}</figcaption></figure>"


def cell_html(v, num: bool, bar_max: float | None, dec: int | None = None) -> str:
    """One table cell. v may be a plain value, or {"stack": [{"pct", "color", "tip"}], "tip"} for a stacked bar.
    dec = fixed decimals for numbers in this column (None: whole numbers stay whole, floats keep 3 significant digits)."""
    if isinstance(v, dict) and "stack" in v:
        parts = "".join(f"<span style='width:{max(float(s['pct']), 0):.2f}%;background:{html.escape(s['color'])}'></span>"
                        for s in v["stack"])
        return f"<td><div class='stack' data-tip='{html.escape(v.get('tip', ''), quote=True)}'>{parts}</div></td>"
    raw = v
    if isinstance(v, (int, float)) and not isinstance(v, bool) and dec is not None:
        v = f"{v:,.{dec}f}"
    elif isinstance(v, float):
        v = f"{v:,.3g}" if abs(v) < 1000 else f"{v:,.0f}"
    elif isinstance(v, int):
        v = f"{v:,}"
    if bar_max and isinstance(raw, (int, float)):
        w = 100 * max(float(raw), 0) / bar_max
        return (f"<td class='bar'><div class='bc'><span class='t'>{esc(v)}</span><span class='k'>"
                f"<span class='f' style='display:block;width:{w:.1f}%'></span></span></div></td>")
    return f"<td class='{'num' if num else ''}'>{esc(v)}</td>"


def table_html(t: dict, compact: bool) -> str:
    """Table; "bars": [column indexes] draws in-cell bars scaled to the column maximum; "title": short bold title above
    the table ("accordion" = old name); tables longer than VISIBLE_ROWS ("visible_rows") show a "+ X more" button. A row
    is a list of cells, or
    {"cells": [...], "class": "sub"} for a lightly shaded sub-row (e.g. the channels below a device total)."""
    cols = t.get("columns", [])
    rows_in = t.get("rows", [])
    rows = [r["cells"] if isinstance(r, dict) else r for r in rows_in]
    row_cls = [r.get("class", "") if isinstance(r, dict) else "" for r in rows_in]
    if t.get("group_first_col"):  # separator line where the first-column value (e.g. the device) changes;
        last = None              # repeated values are shown only on the group's first row
        rows = [list(r) for r in rows]
        for k, row in enumerate(rows):
            v = row[0] if row else ""
            if v in ("", None):
                continue
            if v == last:
                row[0] = ""
            else:
                if last is not None:
                    row_cls[k] = (row_cls[k] + " grp").strip()
                last = v
    num = [all(isinstance(r[i], (int, float)) for r in rows if i < len(r)) for i in range(len(cols))]
    # one number format per column: if any value has decimals, all numbers in the column get the same count
    dec_cfg = {int(k): v for k, v in (t.get("decimals") or {}).items()}
    col_dec = {i: dec_cfg.get(i, DEFAULT_DECIMALS) for i in range(len(cols))
               if i in dec_cfg or any(isinstance(r[i], float) and r[i] != int(r[i]) for r in rows if i < len(r))}
    bars = set(t.get("bars", []))
    bar_max = {i: max([float(r[i]) for r in rows if i < len(r) and isinstance(r[i], (int, float))] + [0]) or None
               for i in bars}
    tips = t.get("column_tips", {})
    head = "".join(f"<th class='{'num' if num[i] and i not in bars else ''}'>{esc(c)}{info(tips.get(c))}</th>"
                   for i, c in enumerate(cols))
    n_vis = t.get("visible_rows", VISIBLE_ROWS)
    row_cls = [(rc + " xtra").strip() if k >= n_vis else rc for k, rc in enumerate(row_cls)]
    body = "".join((f"<tr class='{rc}'>" if rc else "<tr>")
                   + "".join(cell_html(v, i < len(num) and num[i], bar_max.get(i), col_dec.get(i)) for i, v in enumerate(r))
                   + "</tr>" for r, rc in zip(rows, row_cls))
    more = len(rows) - n_vis
    cap = ""
    if t.get("caption"):
        cap = (f"<div class='cap'>{info(t['caption'])}</div>" if compact else f"<div class='cap'>{esc(t['caption'])}</div>")
    out = f"<table class='lim'><thead><tr>{head}</tr></thead><tbody>{body}</tbody></table>"
    if more > 0:
        out += f"<button type='button' class='more' data-more='+ {more} more'>+ {more} more</button>"
    out += cap
    title = t.get("title") or t.get("accordion")
    return (f"<div class='ttitle'>{esc(title)}{info(t.get('title_tip'))}</div>" if title else "") + out


def provenance_rows(p: dict) -> list[tuple[str, str]]:
    """The "Data used" facts as (label, plain text) rows; multi-value entries are separated by newlines."""
    rows = []
    def add(k, v):
        if v not in (None, "", [], {}):
            rows.append((k, str(v)))
    add("Devices", ", ".join(p.get("devices", [])))
    tr = p.get("time_range_utc")
    if tr and p.get("time_range_local"):  # preferred: Local time first, UTC in brackets (see display_time.py)
        lt = p["time_range_local"]
        add("Time range", f"{lt[0]} to {lt[1]} ({p.get('display_tz', 'local time')})\nUTC: {tr[0]} to {tr[1]}")
    elif tr:
        off = p.get("display_utc_offset")
        local = ""
        if off and off not in ("+00:00", "Z"):
            try:
                sign = 1 if off[0] == "+" else -1
                hh, mm = off[1:].split(":")
                delta = dt.timedelta(hours=int(hh), minutes=int(mm)) * sign
                conv = [(dt.datetime.fromisoformat(x.replace("Z", "+00:00")) + delta).strftime("%y/%m/%d %H:%M:%S") for x in tr]
                local = f"\nLocal UTC{off}: {conv[0]} to {conv[1]}"
            except (ValueError, IndexError):
                pass
        add("Time range (UTC)", f"{tr[0]} to {tr[1]}{local}")
    add("Data sources", "\n".join(p.get("sources", [])))
    if p.get("files") is not None:
        add("Log files", p["files"])
    add("DBC files", "\n".join(f"{d}: {', '.join(fs)}" for d, fs in (p.get("dbc_files") or {}).items()))
    add("Tools", ", ".join(f"{k} {v}" for k, v in (p.get("tools") or {}).items()))
    for k, v in (p.get("extra") or {}).items():
        add(k, v)
    add("Generated", p.get("generated_at", ""))
    return rows


def source_note(p: dict) -> str:
    """The footer sentence on where the data came from: provenance "processing" if given, else derived from
    "data_source" / "sources" / "tools" (Athena, BigQuery, the MF4 tools on log files, a local Parquet lake)."""
    if p.get("processing"):
        return str(p["processing"])
    text = " ".join([str(p.get("data_source") or ""), *map(str, p.get("sources") or []),
                     json.dumps(p.get("tools") or {})]).lower()
    cloud = [n for k, n in (("athena", "the S3 data lake via Amazon Athena"),
                            ("bigquery", "the data lake via Google BigQuery"),
                            ("synapse", "the data lake via Azure Synapse")) if k in text]
    if cloud:
        return f"Data queried from {' and '.join(cloud)}, processed with open source Python."
    if any(k in text for k in ("mdf2", ".mf4", ".mfc", ".mfe", ".mfm", "mf4 tools")):
        return "Data processed locally with the CSS Electronics MF4 tools and open source Python."
    if any(k in text for k in ("local lake", "parquet", "duckdb")):
        return "Data queried from a local Parquet data lake with DuckDB and open source Python."
    return "Data processed with open source Python."


def provenance_tip(p: dict) -> str:
    return "DATA USED\n" + "\n".join(f"{k}: {v}" for k, v in provenance_rows(p))


def kpis_html(spec: dict) -> str:
    if spec.get("kpi_groups"):  # grouped tiles: [{"title", "kpis": [{"label", "value", "note", "warn", "tip"}]}]
        groups = []
        for g in spec["kpi_groups"]:
            rows = "".join(
                f"<div class='krow{' warn' if k.get('warn') else ''}'><div><div class='l'>{esc(k['label'])}{info(k.get('tip'))}</div>"
                f"</div><div style='text-align:right'><div class='v'>{esc(k['value'])}</div>"
                f"<div class='n'>{esc(k.get('note', ''))}</div></div></div>" for k in g["kpis"])
            groups.append(f"<div class='kgroup'><div class='gt'>{esc(g['title'])}{info(g.get('tip'))}</div>{rows}</div>")
        return f"<div class='kgroups'>{''.join(groups)}</div>"
    if spec.get("kpis"):
        tiles = "".join(
            f"<div class='kpi{' warn' if k.get('warn') else ''}'><div class='l'>{esc(k['label'])}</div>"
            f"<div class='v'>{esc(k['value'])}</div><div class='n'>{esc(k.get('note', ''))}</div></div>"
            for k in spec["kpis"])
        return f"<div class='kpis'>{tiles}</div>"
    return ""


def build_html(spec: dict, base: Path) -> str:
    compact = bool(spec.get("compact"))
    parts = [f"<header><h1>{esc(spec.get('title', 'Report'))}</h1>"]
    # "Data used" = (i) hover at the end of the subtitle line
    parts.append(f"<div class='sub'>{esc(spec.get('subtitle', ''))}{info(provenance_tip(spec.get('provenance', {})))}</div>")
    parts.append("</header>")
    # spec["question"] is kept in report.json for the agent, not shown: The title says what the report covers
    if spec.get("summary"):
        items = "".join(f"<li>{esc(s)}</li>" for s in spec["summary"])
        conf = f"<div class='conf'>Confidence: {esc(spec['confidence'])}</div>" if spec.get("confidence") else ""
        parts.append(f"<div class='summary'><strong>Summary</strong><ul>{items}</ul>{conf}</div>")
    parts.append(kpis_html(spec))
    tabs = bool(spec.get("tabs"))
    panels = []
    for s in spec.get("sections", []):
        p = [f"<h2>{esc(s.get('heading', ''))}{info(s.get('info'))}</h2>"]
        if s.get("insights"):
            p.append("<ul class='insights'>" + "".join(f"<li>{esc(b)}</li>" for b in s["insights"]) + "</ul>")
        for t in s.get("text", []):
            p.append(f"<p>{esc(t)}</p>")
        if s.get("bullets"):
            p.append("<ul>" + "".join(f"<li>{esc(b)}</li>" for b in s["bullets"]) + "</ul>")
        figs = [embed_figure(f, base, compact) for f in s.get("figures", [])]
        tbls = [table_html(t, compact)
                for t in ([s["table"]] if s.get("table") else []) + s.get("tables", [])]
        p += (tbls + figs) if s.get("tables_first") else (figs + tbls)
        panels.append("".join(p))
    if tabs:  # one tab per section; the section's (i) moves into its tab
        secs = spec.get("sections", [])
        bar = "".join(f"<button type='button' class='tab{' on' if i == 0 else ''}' data-tab='{i}'>"
                      f"{esc(s.get('heading', ''))}{info(s.get('info'))}</button>" for i, s in enumerate(secs))
        parts.append(f"<div class='tabs'>{bar}</div>" + "".join(
            f"<div class='panel{' on' if i == 0 else ''}' data-panel='{i}'>{h}</div>" for i, h in enumerate(panels)))
    else:
        parts += panels
    for key, title in (("method", "Method"), ("caveats", "Caveats + limitations")):
        if spec.get(key):
            parts.append(f"<details class='block'><summary>{title}</summary><ul>"
                         + "".join(f"<li>{esc(x)}</li>" for x in spec[key]) + "</ul></details>")
    rep = spec.get("reproduce") or {}
    if rep:
        body = "".join(f"<div class='cap'>{k.upper()}</div><pre>{html.escape(rep[k])}</pre>"
                       for k in ("script", "command", "sql") if rep.get(k))
        parts.append(f"<details class='block'><summary>Reproduce</summary>{body}</details>")
    parts.append(f"<footer>Generated {esc(spec['provenance'].get('generated_at', ''))} with the canedge-reports skill. "
                 f"{esc(source_note(spec['provenance']))}</footer>")
    title = html.escape(spec.get("title", "Report"))
    return (f"<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' "
            f"content='width=device-width,initial-scale=1'><title>{title}</title><style>{CSS}</style></head>"
            f"<body><div class='wrap'>{''.join(parts)}</div><div id='tip'></div><script>{JS}</script></body></html>")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=Path, required=True)
    ap.add_argument("--out", type=Path, help="Output folder (default: The spec's folder)")
    ap.add_argument("--no-pdf", action="store_true", help=argparse.SUPPRESS)  # no-op: Kept for old reproduce commands
    a = ap.parse_args()

    spec = json.loads(a.spec.read_text(encoding="utf-8"))
    base = a.spec.parent
    out = a.out or base
    out.mkdir(parents=True, exist_ok=True)
    spec.setdefault("provenance", {})
    spec["provenance"].setdefault("generated_at", dt.datetime.now(dt.timezone.utc).strftime("%y/%m/%d %H:%M UTC"))

    html_path = out / "report.html"
    html_path.write_text(build_html(spec, base), encoding="utf-8")
    (out / "report.json").write_text(json.dumps(spec, indent=1, default=str), encoding="utf-8")
    print(f"HTML: {html_path}")
    print(f"JSON: {out / 'report.json'}")
    print(open_line(html_path, "report"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
