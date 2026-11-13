"""Download the CSS Electronics MF4 tools (mdf2csv, mdf2mdf, mdf2parquet_decode) for this OS into their skill's bin/
folder. A build elsewhere: Set CANEDGE_MDF2CSV / CANEDGE_MDF2MDF / CANEDGE_MDF2PARQUET instead.

Usage (from the repo root; install.bat / install.sh run it):
  .venv\\Scripts\\python.exe .claude/skills/canedge-workspace/scripts/install_tools.py [--force] [--json]

Builds: Windows x64, Linux x64, Linux ARM64 (no macOS build: Use a Linux VM/container). The pinned versions, URLs and
MD5s are the TOOLS manifest in tools.py. A tool that already runs at the pinned version is skipped (--force
re-downloads). Each zip is checked against its MD5 before the binary is extracted. Stdlib only.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shutil
import sys
import tempfile
import urllib.request
import zipfile
from pathlib import Path

from tools import TOOLS, installed_path, platform_key, version


def install(name: str, plat: str, force: bool) -> dict:
    """Install one tool; returns {tool, status, version, path, error}. status: installed | skipped | failed."""
    t = TOOLS[name]
    dst = installed_path(name)
    res = {"tool": name, "pinned": t["version"], "path": str(dst), "status": "failed", "version": None, "error": None}
    if not force and dst.exists() and version(dst).startswith(t["version"]):
        res.update(status="skipped", version=version(dst))
        return res
    url, md5 = t["builds"][plat]
    tmp = Path(tempfile.mkdtemp(prefix=f"{name}-"))
    try:
        zpath = tmp / "tool.zip"
        req = urllib.request.Request(url, headers={"User-Agent": "canedge-ai-skills install_tools.py"})
        with urllib.request.urlopen(req, timeout=120) as r, open(zpath, "wb") as f:
            shutil.copyfileobj(r, f)
        got = hashlib.md5(zpath.read_bytes()).hexdigest()
        if got != md5:
            raise RuntimeError(f"MD5 mismatch for {url} (got {got}, expected {md5})")
        member = name + (".exe" if plat.startswith("win") else "")
        with zipfile.ZipFile(zpath) as z:
            if member not in z.namelist():
                raise RuntimeError(f"{member} not in {url} (contains: {', '.join(z.namelist())})")
            dst.parent.mkdir(parents=True, exist_ok=True)
            part = dst.with_name(dst.name + ".part")
            with z.open(member) as src, open(part, "wb") as out:
                shutil.copyfileobj(src, out)
        if os.name != "nt":
            part.chmod(0o755)
        os.replace(part, dst)
        v = version(dst)
        if not v.startswith(t["version"]):
            raise RuntimeError(f"installed, but '{dst} --version' reports {v}")
        res.update(status="installed", version=v)
    except Exception as e:  # noqa: BLE001 - report any download/extract problem plainly
        res["error"] = (f"{e.__class__.__name__}: {e}. Manual install: Download the {plat} build of {t['version']} "
                        f"from {t['docs']}, unzip it to {dst} (or set {t['env']} to its path)")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--force", action="store_true", help="Re-download even if the pinned version is installed")
    ap.add_argument("--json", action="store_true", help="Print the result as JSON")
    a = ap.parse_args()
    plat = platform_key()
    if plat is None:
        msg = ("No CSS Electronics MF4 tool build for this platform (builds: Windows x64, Linux x64, Linux ARM64). "
               "Use a Linux VM/container, or point the skills to a build via --tool / " +
               " / ".join(t["env"] for t in TOOLS.values()))
        print(json.dumps({"platform": None, "error": msg}) if a.json else f"ERROR: {msg}")
        return 1
    results = [install(n, plat, a.force) for n in TOOLS]
    if a.json:
        print(json.dumps({"platform": plat, "tools": results}, indent=2))
    else:
        for r in results:
            line = {"installed": f"installed {r['version']}", "skipped": f"already installed ({r['version']})"}
            print(f"{r['tool']} ({plat}): " + (line.get(r["status"]) or f"FAILED - {r['error']}"))
    return 0 if all(r["status"] != "failed" for r in results) else 1


if __name__ == "__main__":
    sys.exit(main())
