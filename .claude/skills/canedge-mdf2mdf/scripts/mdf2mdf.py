"""Write demuxed / finalized MF4 copies of CANedge log files with the CSS mdf2mdf converter.

Use it when a demuxed or plain MF4 FILE is needed (asammdf, Vector/PEAK tools, sharing): The canedge-* scripts demux
on the fly and need no copy. Per input file (MF4/MFC/MFE/MFM): mdf2mdf [--muxtp-canN=ID#a:b:c:d] into a temp folder,
then the result is moved to <out>/<DEVICE>/<SESSION>/<SPLIT>.MF4 (the CANedge layout, so other scripts still read
device/session/split; the MF4 header is kept). Input files are never modified (no -d).

Demux mapping (CANmod.router, canedge-workspace muxtp.py):
  --project              from the router DBC of each device's DBCs (can2-canmod-router.dbc in <project>/dbc/)
  --muxtp 010:11         explicit ([canN:]ID:S1[,S2,S3,S4]; 'default' = can2 0x010 -> CAN11-14; repeatable)
  --no-demux             only decompress / decrypt / finalize (MFC/MFE/MFM -> MF4, e.g. for asammdf)

Usage:
  python mdf2mdf.py --project projects/<name> [--device X] [--session N]      out: <project>/temp/mf4-demuxed/
  python mdf2mdf.py <files/folders> --muxtp default [--out <folder>]          out: TEMP/mf4-demuxed/
  python mdf2mdf.py <files/folders> --no-demux [--passwords passwords.json]   out: TEMP/mf4/ (<project>/temp/mf4/)

Existing outputs are skipped unless --force (they are not checked for the mapping: Use another --out or --force
after changing it). --summary converts each output with mdf2csv and prints the frames per
channel (default for up to 10 files).
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import json
import os
import shutil
import sys
import tempfile
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
import muxtp  # noqa: E402
from mf4_files import describe, find_log_files, project_paths  # noqa: E402
from tools import default_workers, demux, mdf2csv_tables, password_file, passwords_refusal, resolve, version  # noqa: E402


def out_path(meta: dict, root: Path) -> Path:
    if meta["session"] is None or meta["split"] is None:
        return root / meta["device"] / "00000000" / f"{Path(meta['path']).stem}.MF4"
    return root / meta["device"] / f"{meta['session']:08d}" / f"{meta['split']:08d}.MF4"


def convert(meta: dict, tool: Path, root: Path, passwords: str | None, force: bool) -> dict:
    dst = out_path(meta, root)
    res = {"input": meta["path"], "output": str(dst), "demux": muxtp.key(meta["muxtp"])}
    if dst.exists() and not force and dst.stat().st_mtime >= meta["mtime"]:
        res["status"] = "skipped (exists)"
        return res
    tmp = Path(tempfile.mkdtemp(prefix="mdf2mdf-", dir=root))
    try:
        mf4 = demux(meta["path"], tool, meta["muxtp"], tmp, passwords)
        dst.parent.mkdir(parents=True, exist_ok=True)
        os.replace(mf4, dst)
        res["status"] = "written"
        res["size_bytes"] = dst.stat().st_size
    except Exception as e:  # noqa: BLE001 - report and continue
        res["status"] = "failed"
        res["error"] = f"{type(e).__name__}: {e}"
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
    return res


def channel_summary(path: str) -> dict:
    tables = mdf2csv_tables(path, resolve("mdf2csv"), columns=["BusChannel"])
    c: Counter = Counter()
    for bus, t in tables:
        c.update(f"{bus}{x}" for x in t["BusChannel"].to_pylist())
    return dict(sorted(c.items()))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path, help="Log files and/or folders (searched recursively)")
    ap.add_argument("--project", type=Path, help="Use the project's sources, devices filter and router DBC")
    ap.add_argument("--device", action="append", help="Only these device IDs (repeatable)")
    ap.add_argument("--session", type=int, action="append", help="Only these sessions (repeatable)")
    ap.add_argument("--muxtp", action="append", help="Demux mapping [canN:]ID:S1[,S2,S3,S4] or 'default' (repeatable)")
    ap.add_argument("--no-demux", action="store_true", help="Only decompress / decrypt / finalize")
    ap.add_argument("--out", type=Path, help="Output root (default: <project>/temp/mf4-demuxed, else TEMP/mf4-demuxed; "
                                             "--no-demux: .../mf4)")
    ap.add_argument("--passwords", help="passwords.json for encrypted files (MFE/MFM; default <project>/passwords.json)")
    ap.add_argument("--workers", type=int, default=0, help="Parallel files (default: up to 4)")
    ap.add_argument("--force", action="store_true", help="Overwrite existing outputs")
    ap.add_argument("--summary", action=argparse.BooleanOptionalAction, default=None,
                    help="Frames per channel of each output (default: on for up to 10 files)")
    ap.add_argument("--tool", help="Path to mdf2mdf (default: env CANEDGE_MDF2MDF, else canedge-mdf2mdf/bin)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()

    paths, devices = list(a.paths), {d.upper() for d in a.device or []}
    if a.project:
        pp, pdev = project_paths(a.project)
        paths += pp
        devices |= set(pdev)
    if not paths:
        ap.error("give log files/folders or --project")
    metas = [describe(f) for f in find_log_files([p for p in paths if p.exists()])]
    metas = [m for m in metas if (not devices or m["device"] in devices)
             and (not a.session or m["session"] in set(a.session))]
    if not metas:
        print("No log files found.")
        return 0
    try:
        cli = [muxtp.parse_spec(x) for x in a.muxtp or []]
    except ValueError as e:
        ap.error(str(e))
    for m in metas:
        m["muxtp"] = ([] if a.no_demux else cli or
                      (muxtp.for_device(a.project, m["device"], alt_device=m.get("path_device")) if a.project else []))
    no_map = [m for m in metas if not m["muxtp"]]
    if no_map and not a.no_demux:
        ap.error(f"{len(no_map)} files have no demux mapping: Add the router DBC to the project's dbc/ "
                 f"({muxtp.ROUTER_DBC}), pass --muxtp default, or --no-demux to only decompress/decrypt")
    sub = "mf4" if a.no_demux else "mf4-demuxed"
    root = a.out or ((a.project / "temp" / sub) if a.project else Path("TEMP") / sub)
    in_dirs = {Path(m["path"]).parent.resolve() for m in metas}
    if any(root.resolve() == d or d in root.resolve().parents for d in in_dirs):
        ap.error(f"--out {root} is inside an input log folder: Choose a folder outside the user's log files")
    root.mkdir(parents=True, exist_ok=True)
    pw = password_file(a.passwords, a.project)
    refusal = passwords_refusal([m["path"] for m in metas], pw, a.project)
    if refusal:
        print(refusal)
        return 2
    tool = resolve("mdf2mdf", a.tool)
    keys = sorted({muxtp.key(m["muxtp"]) for m in metas})
    print(f"{len(metas)} log files -> {root} (mdf2mdf {version(tool)}; " +
          ("no demux" if keys == [""] else "demux " + "; ".join(k for k in keys if k)) + ")")

    workers = max(1, a.workers or default_workers(len(metas)))
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        results = list(ex.map(lambda m: convert(m, tool, root, pw, a.force), metas))
    summary = a.summary if a.summary is not None else len(results) <= 10
    for r in results:
        if summary and r["status"] != "failed":
            try:
                r["channels"] = channel_summary(r["output"])
            except Exception as e:  # noqa: BLE001
                r["channels_error"] = str(e)
    if a.json:
        print(json.dumps(results, indent=1))
    else:
        for r in results:
            line = f"{r['status']:>16}  {r['output']}"
            if r.get("error"):
                line += f"  ({r['error']})"
            if r.get("channels"):
                line += "  " + ", ".join(f"{k} {v:,}" for k, v in r["channels"].items())
            print(line)
    failed = [r for r in results if r["status"] == "failed"]
    print(f"Done: {sum(r['status'] == 'written' for r in results)} written, "
          f"{sum(r['status'].startswith('skipped') for r in results)} skipped, {len(failed)} failed")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
