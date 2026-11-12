"""Refresh the Grafana JSON schemas used by validate_dashboard.py (assets/grafana-schema/), then re-check the official
CANedge templates against them.

Source: The JSON schemas of the Grafana Foundation SDK (github.com/grafana/grafana-foundation-sdk, jsonschema/),
generated from Grafana's own schemas. One branch per Grafana version (e.g. v11.6.x+cog-v0.0.x) plus main (latest).
The files are pinned to a commit SHA, stripped of descriptions and minified; SOURCE.md records ref, SHA and date.

The schemas describe what Grafana saves (all defaults filled in), not what it accepts on import, so
validate_dashboard.py only reports unknown keys from them (typos that Grafana silently ignores). After the update,
all official templates (assets/templates/ + the URLs in its README) must give 0 schema warnings; otherwise review
the new warnings and extend SCHEMA_IGNORE in validate_dashboard.py (each entry with a reason).

Usage:
  python update_schemas.py [--ref main]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
import tempfile
import urllib.request
from pathlib import Path

import validate_dashboard

REPO = "grafana/grafana-foundation-sdk"
SKILL = Path(__file__).resolve().parent.parent
OUT = SKILL / "assets" / "grafana-schema"
TEMPLATES = SKILL / "assets" / "templates"
# dashboard + the Athena/BigQuery queries + the panel types of the builder and the official templates
FILES = ["dashboard", "athena", "bigquery", "timeseries", "stat", "table", "bargauge", "barchart", "geomap",
         "statetimeline", "text", "histogram", "gauge", "piechart"]


def get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": "canedge-grafana-update-schemas"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read()


def strip_descriptions(x):
    """Drop the description texts (most of the file size); keeps properties that are named 'description'."""
    if isinstance(x, dict):
        return {k: strip_descriptions(v) for k, v in x.items() if not (k == "description" and isinstance(v, str))}
    if isinstance(x, list):
        return [strip_descriptions(v) for v in x]
    return x


def check_templates() -> int:
    """Schema warnings of the bundled templates + the official templates listed in the templates README."""
    urls = re.findall(r"https://\S+?\.json", (TEMPLATES / "README.md").read_text(encoding="utf-8"))
    total = 0
    with tempfile.TemporaryDirectory() as tmp:
        files = sorted(TEMPLATES.glob("*.json"))
        for u in urls:
            p = Path(tmp) / u.rsplit("/", 1)[1]
            if not any(f.name == p.name for f in files):
                try:
                    p.write_bytes(get(u))
                    files.append(p)
                except OSError as e:
                    print(f"  WARNING: Cannot download {u}: {e}")
        for f in files:
            res = validate_dashboard.validate(json.loads(f.read_text(encoding="utf-8")))
            total += len(res["schema"])
            print(f"  {f.name}: {len(res['schema'])} schema warning(s)")
            for w in res["schema"]:
                print(f"      {w}")
    return total


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--ref", default="main", help="Branch or tag of grafana-foundation-sdk (default: main)")
    a = ap.parse_args()
    try:
        c = json.loads(get(f"https://api.github.com/repos/{REPO}/commits/{a.ref}"))
        sha, date = c["sha"], c["commit"]["committer"]["date"]
        OUT.mkdir(parents=True, exist_ok=True)
        for name in FILES:
            s = json.loads(get(f"https://raw.githubusercontent.com/{REPO}/{sha}/jsonschema/{name}.jsonschema.json"))
            (OUT / f"{name}.json").write_text(json.dumps(strip_descriptions(s), separators=(",", ":")), encoding="utf-8")
    except (OSError, ValueError, KeyError) as e:
        print(f"ERROR: {e.__class__.__name__}: {e}", file=sys.stderr)
        return 1
    (OUT / "SOURCE.md").write_text(
        "# Grafana JSON schemas (generated, do not edit)\n\n"
        f"From https://github.com/{REPO}/tree/{sha}/jsonschema (ref `{a.ref}`, commit {date[:10]}), descriptions "
        f"stripped, minified. Downloaded {dt.date.today().isoformat()} by `scripts/update_schemas.py`.\n\n"
        f"Files: {', '.join(FILES)}\n", encoding="utf-8")
    print(f"Wrote {len(FILES)} schemas from {REPO}@{a.ref} ({sha[:12]}, {date[:10]}) to {OUT}")
    print("Official templates:")
    n = check_templates()
    print(f"  -> {n} schema warning(s)" + (" - review them, extend SCHEMA_IGNORE in validate_dashboard.py" if n else ""))
    return 1 if n else 0


if __name__ == "__main__":
    sys.exit(main())
