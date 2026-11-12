"""Create a new analysis project folder: projects/<yyyy-mm-dd>-<slug>/ with project.json, project.md and subfolders.

Usage:
  python new_project.py <slug> [--description "..."] [--local D:/LOG] [--s3 my-bucket:12345678/]
                         [--device 12345678] [--tz Europe/Berlin] [--date 2026-10-29]
  python new_project.py --list      (existing projects as JSON: name, description, devices, sources, reports;
                                     pick one to reuse before creating a new one, conventions.md section 3)

Never overwrites an existing project.json / project.md.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import sys
from pathlib import Path

PROJECT_MD = """# {name}

## Goal
{description}

## Data
- Sources: See project.json
- DBC mapping: Not confirmed yet (all DBCs in dbc/ apply to all devices, or dbc-groups.json if they differ)
- Time: Processing in UTC, displayed in {tz}

## Context + caveats
- (What the agent must know about this data: which bus to trust, known DBC issues, logging gaps, ...)

## Findings log
- {today}: Project created
"""


def list_projects(root: Path) -> list[dict]:
    """Compact facts per project, so the agent can match a task to an existing project without opening each file."""
    rows = []
    for pj in sorted(root.glob("*/project.json")) if root.is_dir() else []:
        try:
            cfg = json.loads(pj.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            rows.append({"name": pj.parent.name, "error": f"project.json unreadable: {e}"})
            continue
        src = [s.get("path") if s.get("type") == "local" else f"s3://{s.get('bucket')}/{s.get('prefix') or ''}"
               for s in cfg.get("sources", [])]
        reports = sorted(p.name for p in (pj.parent / "reports").iterdir() if p.is_dir())             if (pj.parent / "reports").is_dir() else []
        rows.append({"name": pj.parent.name, "description": cfg.get("description"), "devices": cfg.get("devices", []),
                     "sources": src, "reports": reports})
    return rows


def main() -> int:
    if "--list" in sys.argv[1:]:
        ap = argparse.ArgumentParser()
        ap.add_argument("--list", action="store_true")
        ap.add_argument("--root", type=Path, default=Path("projects"))
        a = ap.parse_args()
        print(json.dumps(list_projects(a.root), indent=1))
        return 0
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("slug", help="Short kebab-case name, e.g. charging-fault")
    ap.add_argument("--description", default="(describe the question this project answers)")
    ap.add_argument("--local", action="append", default=[], help="Local folder/file with raw log files (repeatable)")
    ap.add_argument("--s3", action="append", default=[], help="S3 source as bucket:prefix (repeatable)")
    ap.add_argument("--device", action="append", default=[], help="Device ID filter (repeatable)")
    ap.add_argument("--tz", help="Display time zone (IANA name, e.g. Europe/Berlin). Default: the PC's time zone")
    ap.add_argument("--date", default=dt.date.today().isoformat(), help="Prefix = start date (default: today, yyyy-mm-dd)")
    ap.add_argument("--root", type=Path, default=Path("projects"))
    a = ap.parse_args()

    slug = re.sub(r"[^a-z0-9-]+", "-", a.slug.lower()).strip("-")
    name = f"{a.date}-{slug}"
    proj = a.root / name
    if (proj / "project.json").exists():
        print(f"Project already exists: {proj} (not modified)")
        return 0
    for sub in ("dbc", "parquet", "temp", "reports"):
        (proj / sub).mkdir(parents=True, exist_ok=True)

    sources = [{"type": "local", "path": p.replace("\\", "/")} for p in a.local]
    for s in a.s3:
        bucket, _, prefix = s.partition(":")
        sources.append({"type": "s3", "bucket": bucket, "prefix": prefix, "start_utc": None, "end_utc": None})
    cfg = {
        "name": name,
        "created": a.date,
        "description": a.description,
        "sources": sources,
        "devices": [d.upper() for d in a.device],
        **({"display_tz": a.tz} if a.tz else {}),
        "notes": "",
    }
    (proj / "project.json").write_text(json.dumps(cfg, indent=2), encoding="utf-8")
    if not (proj / "project.md").exists():
        (proj / "project.md").write_text(PROJECT_MD.format(name=name, description=a.description,
                                         tz=a.tz or "the PC's local time zone", today=a.date), encoding="utf-8")
    print(f"Created {proj}")
    print("Next: Copy ALL the user's DBCs into dbc/ (not a subset picked by file name), then ask the user BEFORE using "
          "them if they apply to all devices (if not: dbc-groups.json; reuse a confirmed one from an earlier project)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
