"""List CANedge log files (MF4/MFC/MFE/MFM) and read their header metadata - without decoding them.

Reads only the first 64 KB of each file: Device ID, session, split, firmware/hardware, config CRC32, meta name,
CAN/LIN bit-rates and the session start time (UTC). Works for raw SD-card folders (LOG/<device>/<session>/<split>.MF4),
S3 downloads (data/s3/<bucket>/<device>/<session>/<split>.MF4) and loose files.

MF4 only: Header fields are read from uncompressed, unencrypted .MF4 files. For MFC/MFE/MFM they are unknown
(header "unsupported"; device/session/split then come from the path) until fill_headers() reads them from a finalized
copy (mdf2mdf) of one file per device/session (header "finalized"; raw stats, decoding and config simulation do
this). This is an interim reader until the CSS MF4 tools export the header for all formats.

Usage:
  python mf4_files.py <path> [<path> ...] [--device 12345678] [--session 62] [--json] [--out files.json]
  python mf4_files.py --project projects/<name> [--json]

Never modifies the input files. The other skills import read_header / describe / find_log_files / project_paths from
this file (the one MF4 header reader of the suite).
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import re
import struct
import sys
from pathlib import Path

LOG_EXTS = {".mf4", ".mfc", ".mfe", ".mfm"}
DEVICE_RE = re.compile(r"^[0-9A-Fa-f]{8}$")


def read_header(path: Path) -> dict:
    """Parse the MF4 header (first 64 KB) of an .MF4 file. Fields that cannot be read are None.

    header = "mf4" (read) or "unsupported" (MFC/MFE/MFM, or no serial number found). meta = the device meta name
    (header "comment" = config general.device.meta; None if not set).
    Note: header_start_utc is the HD block start time. On CANedge logs this can be the SESSION start, not the
    split start (a late split can begin many hours later). Use the first frame timestamp (e.g. via mdf2csv)
    when you need the exact time span of a split.
    """
    info = {"header_start_utc": None, "firmware": None, "hardware": None, "serial": None, "config_crc32": None,
            "meta": None, "session": None, "split": None, "bitrates": {}, "header": "unsupported"}
    if path.suffix.lower() != ".mf4":
        return info
    try:
        with path.open("rb") as f:
            b = f.read(65536)
    except OSError:
        return info
    # HD block start time: '##HD' at offset 64, then id/reserved/length/link_count (24 B), links, then start_time_ns
    if b[64:68] == b"##HD":
        try:
            link_count = struct.unpack_from("<Q", b, 80)[0]
            ns = struct.unpack_from("<Q", b, 64 + 24 + link_count * 8)[0]
            if ns:
                info["header_start_utc"] = dt.datetime.fromtimestamp(ns / 1e9, dt.timezone.utc).isoformat()
        except (struct.error, ValueError, OverflowError, OSError):
            pass
    text = b.decode("utf-8", "replace")

    def field(name: str) -> str | None:
        m = re.search(r'<e name="' + re.escape(name) + r'"[^>]*>([^<]*)</e>', text)
        return m.group(1).strip() if m else None

    info["firmware"] = field("firmware version")
    info["hardware"] = field("hardware version")
    info["serial"] = field("serial number")
    info["config_crc32"] = field("config crc32 checksum")
    info["meta"] = field("comment") or None
    if info["serial"]:
        info["header"] = "mf4"
    for k in ("session", "split"):
        v = field(k)
        info[k] = int(v) if v and v.isdigit() else None
    for m in re.finditer(r'<e name="((?:CAN|LIN)\d) Bit-rate"[^>]*>\s*(\d+)\s*</e>', text):
        if int(m.group(2)) > 0:
            info["bitrates"][m.group(1)] = int(m.group(2))
    return info


def ids_from_path(path: Path) -> dict:
    """Device/session/split from <device>/<session>/<split>[-<suffix>].MF4 (fallback when the header lacks them)."""
    out = {"device": None, "session": None, "split": None}
    m = re.match(r"^(\d{8})", path.stem)
    if m:
        out["split"] = int(m.group(1))
    parts = path.parts
    if len(parts) >= 3 and re.fullmatch(r"\d{8}", parts[-2]):
        out["session"] = int(parts[-2])
        if DEVICE_RE.fullmatch(parts[-3]):
            out["device"] = parts[-3].upper()
    return out


def find_log_files(paths: list[Path]) -> list[Path]:
    files: list[Path] = []
    for p in paths:
        if p.is_file() and p.suffix.lower() in LOG_EXTS:
            files.append(p)
        elif p.is_dir():
            files += [f for f in p.rglob("*") if f.is_file() and f.suffix.lower() in LOG_EXTS]
    return sorted(set(files))


def describe(path: Path) -> dict:
    """One file: Path, size, mtime, device (header serial, else the folder name, else UNKNOWN), session, split,
    format + the header fields. path_device = a folder name that differs from the header serial."""
    hdr = read_header(path)
    ids = ids_from_path(path)
    st = path.stat()
    dev = (hdr["serial"] or ids["device"] or "UNKNOWN").upper()
    d = {
        "path": str(path.resolve()), "size_bytes": st.st_size, "mtime": round(st.st_mtime, 3), "device": dev,
        "session": hdr["session"] if hdr["session"] is not None else ids["session"],
        "split": hdr["split"] if hdr["split"] is not None else ids["split"],
        "format": path.suffix.upper().lstrip("."),
        "firmware": hdr["firmware"], "hardware": hdr["hardware"], "config_crc32": hdr["config_crc32"],
        "meta": hdr["meta"], "header": hdr["header"], "id_source": "header" if hdr["serial"] else "path",
        "bitrates": hdr["bitrates"], "header_start_utc": hdr["header_start_utc"],
    }
    if ids["device"] and ids["device"] != dev:
        d["path_device"] = ids["device"]  # folder name differs from the header serial number
    return d


SESSION_FIELDS = ("firmware", "hardware", "config_crc32", "meta", "bitrates", "header_start_utc")


def session_key(m: dict) -> tuple:
    return (m["device"], m["session"]) if m.get("session") is not None else ("file", m["path"])


def apply_header(m: dict, hdr: dict, source: str = "finalized") -> None:
    """Copy the session-wide header fields into a describe() dict. Device/session/split stay as they are (from the
    path for MFC/MFE/MFM), so cache paths never change; a differing header serial is kept as header_serial."""
    for k in SESSION_FIELDS:
        m[k] = hdr.get(k) if k != "bitrates" else dict(hdr.get("bitrates") or {})
    m["header"] = source
    serial = (hdr.get("serial") or "").upper()
    if serial and serial != m["device"]:
        m["header_serial"] = serial
    if m.get("session") is None and serial and hdr.get("session") is not None:
        # loose file (no <device>/<session>/ folders): Its own header, so device/session/split are exact. Kept apart
        # (header_ids): Cache paths use the path-based IDs
        m["header_ids"] = {"device": serial, "session": hdr["session"], "split": hdr.get("split")}


def fill_headers(metas: list[dict], passwords: str | Path | None = None, known: dict | None = None,
                 only: set | None = None, workers: int = 0, tmp_root: str | Path | None = None) -> dict:
    """Header fields for MFC/MFE/MFM files (header "unsupported"), in place: The CANedge writes the same header for
    all splits of a session (firmware, config, meta and bit-rates only change with a restart = new session), so ONE
    file per device/session is finalized with mdf2mdf (the smallest, ~0.6 s per MB) and its header applied to all
    splits of that session. A plain MF4 of the same session, or known[session_key] (e.g. from cached raw stats), is
    used instead when available. only = session keys to finalize (None = all); other sessions are filled from known
    or MF4 siblings only. Encrypted files without a password file are skipped. Returns counts + failures."""
    import concurrent.futures as cf
    from tools import default_workers, finalized_header, resolve
    known = dict(known or {})
    for m in metas:
        if m["header"] != "unsupported":
            known.setdefault(session_key(m), m)
    groups: dict = {}
    for m in metas:
        if m["header"] == "unsupported":
            groups.setdefault(session_key(m), []).append(m)
    res = {"sessions": len(groups), "reused": 0, "finalized": 0, "skipped_encrypted": 0, "failed": []}
    todo = {}
    for k, ms in groups.items():
        if k in known:
            for m in ms:
                apply_header(m, known[k])
            res["reused"] += 1
        elif only is None or k in only:
            cand = [m for m in ms if passwords or Path(m["path"]).suffix.lower() not in (".mfe", ".mfm")]
            if cand:
                todo[k] = min(cand, key=lambda m: m["size_bytes"])
            else:
                res["skipped_encrypted"] += 1
    if not todo:
        return res
    tool = resolve("mdf2mdf")

    def one(item):
        k, m = item
        try:
            return k, finalized_header(m["path"], passwords, tool, tmp_root), None
        except Exception as e:  # noqa: BLE001 - report, the files keep "unsupported"
            return k, None, f"{m['path']}: {e}"

    with cf.ThreadPoolExecutor(max_workers=max(1, workers or default_workers(len(todo)))) as ex:
        for k, hdr, err in ex.map(one, todo.items()):
            if err or not hdr or not hdr.get("serial"):
                res["failed"].append(err or f"{todo[k]['path']}: no header in the finalized copy")
                continue
            for m in groups[k]:
                apply_header(m, hdr)
            res["finalized"] += 1
    return res


def project_paths(project: Path) -> tuple[list[Path], list[str]]:
    """Resolve a project's sources (project.json) to local paths + its device filter. S3 sources map to the
    data/s3 download cache (fetched via the canedge-s3 skill)."""
    cfg = json.loads((project / "project.json").read_text(encoding="utf-8"))
    paths = []
    for src in cfg.get("sources", []):
        if src.get("type") == "local":
            paths.append(Path(src["path"]))
        elif src.get("type") == "s3":
            paths.append(Path("data/s3") / src["bucket"] / src.get("prefix", "").strip("/"))
    return paths, [d.upper() for d in cfg.get("devices") or []]


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path)
    ap.add_argument("--project", type=Path, help="projects/<name> - use the project's data sources")
    ap.add_argument("--device", action="append", help="Only these device IDs (repeatable)")
    ap.add_argument("--session", type=int, action="append", help="Only these sessions (repeatable)")
    ap.add_argument("--json", action="store_true", help="Print JSON instead of a table")
    ap.add_argument("--out", type=Path, help="Write the JSON list to this file")
    a = ap.parse_args()

    paths = list(a.paths) + (project_paths(a.project)[0] if a.project else [])
    if not paths:
        ap.error("give one or more paths, or --project")
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print("WARNING: Not found: " + ", ".join(missing), file=sys.stderr)
    rows = [describe(f) for f in find_log_files([p for p in paths if p.exists()])]
    if a.device:
        devs = {d.upper() for d in a.device}
        rows = [r for r in rows if r["device"] in devs]
    if a.session:
        rows = [r for r in rows if r["session"] in set(a.session)]

    if a.out:
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(rows, indent=1), encoding="utf-8")
    if a.json:
        print(json.dumps(rows, indent=1))
        return 0

    total = sum(r["size_bytes"] for r in rows)
    by_dev: dict[str, list[dict]] = {}
    for r in rows:
        by_dev.setdefault(r["device"], []).append(r)
    print(f"{len(rows)} log files, {total / 1e9:.2f} GB, {len(by_dev)} device(s)")
    for dev, rs in sorted(by_dev.items()):
        starts = sorted(r["header_start_utc"] for r in rs if r["header_start_utc"])
        sessions = sorted({r["session"] for r in rs if r["session"] is not None})
        fw = sorted({r["firmware"] for r in rs if r["firmware"]})
        rates = rs[0]["bitrates"]
        print(f"  {dev}: {len(rs)} files, {sum(r['size_bytes'] for r in rs) / 1e9:.2f} GB, "
              f"{len(sessions)} sessions, FW {', '.join(fw) or '?'}, bit-rates {rates or '?'}, "
              f"session starts {starts[0][:19] if starts else '?'} -> {starts[-1][:19] if starts else '?'} UTC")
    return 0


if __name__ == "__main__":
    sys.exit(main())
