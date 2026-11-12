"""Shared output paths of the canedge-* skills (conventions.md section 3).

- report_dir(slug, project): <project>/reports/<yyyymmdd>-<slug>/, or TEMP/reports/<yyyymmdd>-<slug>/ without a project
  (the script fallback; tasks that produce something the user keeps run in a project)
- open_line(path, label): "OPEN: file:///..." - the clickable link the agent copies verbatim into its reply as
  **[Open report](<url>)** (canedge-workspace "Replies to the user"). Never build the URL by hand: Windows
  backslashes, drive letters and spaces must be encoded

- open_in_browser(path) / CLI `python paths.py open <file>`: Opens the page in the user's default browser. Needed
  because some agent clients (e.g. the VS Code chat) open file:/// links in an editor tab instead of the browser

Paths are relative to the working directory (the repo root).
"""
from __future__ import annotations

import datetime as dt
import os
import sys
import webbrowser
from pathlib import Path

TEMP = Path("TEMP")


def report_dir(slug: str, project: Path | str | None = None, date: dt.date | None = None) -> Path:
    """The folder for a page (report, dashboard, analysis, config/DBC diff). Created if missing."""
    root = Path(project) if project else TEMP
    out = root / "reports" / f"{(date or dt.date.today()):%Y%m%d}-{slug}"
    out.mkdir(parents=True, exist_ok=True)
    return out


def file_url(path: Path | str) -> str:
    return Path(path).resolve().as_uri()


def open_line(path: Path | str, label: str = "report") -> str:
    """One stdout line with the page's file:/// URL, e.g. 'OPEN report: file:///C:/.../report.html'."""
    return f"OPEN {label}: {file_url(path)}"


def open_in_browser(path: Path | str) -> bool:
    """Open a page in the default browser (Windows: the .html file handler, elsewhere: webbrowser)."""
    p = Path(path).resolve()
    if not p.is_file():
        raise FileNotFoundError(p)
    if sys.platform == "win32":
        os.startfile(p)  # noqa: S606 (the user's own .html handler, normally the default browser)
        return True
    return webbrowser.open(p.as_uri())


if __name__ == "__main__":
    if len(sys.argv) != 3 or sys.argv[1] != "open":
        sys.exit("Usage: python paths.py open <file.html>")
    try:
        ok = open_in_browser(sys.argv[2])
    except (FileNotFoundError, OSError) as e:
        sys.exit(f"Could not open {sys.argv[2]}: {e}")
    print(f"Opened in the browser: {file_url(sys.argv[2])}" if ok else "No browser found: Give the user the link only")
