"""Decode CANedge log files (MF4/MFC/MFE/MFM) with DBC files into a LOCAL Parquet data lake.

The CSS decoder (mdf2parquet_decode) decodes and writes the lake itself, in the layout of the CANedge S3 data lake:
  <lake>/<device>/<CANx_Message>/<yyyy>/<mm>/<dd>/<session>_<split>.parquet
This script picks the DBC files per device, skips files already decoded and runs the decoder in batches.

Usage:
  build_lake.py --input <files/folders> --project projects/<name> [--device X] [--dry-run] [--max-gb 1] [--yes]
                [--rebuild]
      DBCs from the project's dbc/ (+ dbc-groups.json), lake = projects/<name>/parquet/, password file =
      projects/<name>/passwords.json if present
  build_lake.py --input <files/folders> --dbc-can1 a.dbc [--dbc-can2 b.dbc ...] --out <lake>
      Quick decode without a project

Rules:
  - Local files only (S3 files: download them first with canedge-s3)
  - Device = the folder <device> of the CANedge layout <device>/<session>/<split>.MF4 (SD card and S3). It selects
    the device's DBC group; the decoder names the output folder from the file header
  - Without a dbc-groups.json all DBCs in dbc/ apply to all devices (like the data lake automation). With one, each
    device gets its group's DBCs (channel from the file name prefix can1-/can2-/can9-/can11-/.../lin1-/lin2-, no
    prefix = CAN1 + CAN2); a device in no group, or files outside the CANedge layout, are REFUSED unless --all-dbcs-confirmed.
    Quick mode refuses input from several devices unless --all-dbcs-confirmed (the same DBCs for all)
  - Incremental: <lake>/_manifest.json lists the decoded input files (size, mtime, DBC fingerprint). Only new or
    changed files are decoded. A device whose DBC files changed is FLAGGED; --rebuild --device X deletes <lake>/<X>
    and decodes its files again
  - DBC check (also with --dry-run): A device whose DBCs the decoder would refuse (same CAN ID twice in one DBC, or the
    same PGN with ProtocolType J1939), or whose DBCs on one channel share an ID + message name with different signals
    (mixed/corrupt Parquet although decoding "works"), is REFUSED (canedge-workspace dbc_groups.py rules)
  - Sample decode (also with --dry-run): A device decoded for the first time with these DBCs (new, changed or
    --rebuild) and more than one file to decode first gets one file (median size) decoded into a temp folder. A failure
    stops the run before anything is written to the lake; the summary shows its message folders. The sample's frame
    lengths are checked too (canedge-dbc dbc_coverage): An ID sent with several lengths and signals beyond the
    shortest (MIXED LENGTHS: skipped frames, unreadable Parquet) stops the run; fix = split the message
    (decoder-technical.md section 4)
  - After each batch, its Parquet files are checked: Unreadable ones are listed (manifest "unreadable_parquet" + the
    summary), never deleted
  - Size guard: Stops if more than --max-gb (default 1) of log files would be decoded, unless --yes
  - Decoder error: The run stops and prints the decoder's error lines. A DBC load error is reported as such (the log
    files are fine and stay undecoded). Otherwise (e.g. a corrupt file) the batch is marked 'error'. Ask the user;
    re-run after a fix, or with --exclude <file>
  - Encrypted MFE/MFM files need a password file (--passwords, or the project's passwords.json). Without one the
    run stops before decoding. Passwords are never printed
  - CANmod.router: The router DBC (can2-canmod-router.dbc, TransportProtocolType MUXTP) among a device's DBCs makes
    the decoder demux the router tunnel frames and decode them with the can11-/can12-/... DBCs. Warns if CAN11+
    DBCs have no router DBC (nothing decoded on them) or a DBC channel is not in the router's MUXTP mapping
  - Input files are never modified; -d is never passed to the decoder
  - Parallel: Several devices are decoded at the same time (--workers, default up to 4), one decoder process per
    device; each device writes only its own <lake>/<device>/ folder. Files outside the CANedge layout (device
    UNKNOWN) switch to one device after another. A decoder error stops new batches on all devices

Exit codes: 0 OK, 1 stopped by a decoder error, 2 stopped by the size guard, 3 device(s) refused or flagged, or encrypted files
without a password file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
from dbc_groups import cross_dbc_collisions, dbcs_for_device, within_dbc_duplicates  # noqa: E402
from mf4_files import DEVICE_RE, ids_from_path, read_header  # noqa: E402
from mf4_files import find_log_files as mf4_find_log_files  # noqa: E402
from tools import (default_workers, finalized_header, password_file as find_password_file,  # noqa: E402
                   passwords_refusal, resolve, version as version_of)
import muxtp  # noqa: E402

COVERAGE = Path(__file__).resolve().parents[2] / "canedge-dbc" / "scripts"
MANIFEST = "_manifest.json"
EST_MB_PER_S = 12.0          # rough end-to-end throughput (measured ~12 MB/s on a laptop for J1939 data)
MAX_CMD_CHARS = 24000        # stay below the Windows command line limit (32767)
# CAN1/2, CAN9 (GNSS/IMU) and the CANmod.router channels after demux (CAN11-14, CAN15-18, ... daisy chains)
CHANNELS = [f"can{i}" for i in range(1, 51)] + ["lin1", "lin2"]


def router_warnings(mapping: dict[str, list[Path]]) -> list[str]:
    """CANmod.router consistency of one device's DBCs: DBCs on CAN10+ need the router DBC (MUXTP) for the decoder to
    demux, and their channels must be in its mapping."""
    mx = muxtp.from_dbcs(mapping)
    demuxed = {f"CAN{c}" for e in mx for c in e["channels"]}
    high = sorted((ch for ch in mapping if ch.startswith("CAN") and int(ch[3:]) >= 10), key=lambda c: int(c[3:]))
    out = []
    if high and not mx:
        out.append(f"DBCs for {', '.join(high)} but no CANmod.router DBC (TransportProtocolType MUXTP): The decoder "
                   f"does not demux, so nothing is decoded on these channels. Add {muxtp.ROUTER_DBC.name} "
                   f"(canedge-mdf2mdf/assets) to the DBCs")
    elif missing := [ch for ch in high if ch not in demuxed]:
        out.append(f"DBCs for {', '.join(missing)}, but the router DBC demuxes only to "
                   f"{', '.join(sorted(demuxed, key=lambda c: int(c[3:])))} ({muxtp.key(mx)}): Nothing is decoded "
                   f"there. Fix the DBC prefix, or the router DBC for a 2nd router (muxtp.py dbc)")
    return out


# ---------------------------------------------------------------- input files + DBCs
def find_log_files(paths: list[Path]) -> list[Path]:
    return sorted({f.resolve() for f in mf4_find_log_files(paths)})


def device_of(path: Path) -> str | None:
    """Device ID from the CANedge folder layout <device>/<session>/<split>.MF4, else None."""
    return ids_from_path(path)["device"]


def dbc_fingerprint(mapping: dict[str, list[Path]]) -> str:
    """sha256 over channel + file name + file content of the DBCs used for a device (first 16 hex chars)."""
    h = hashlib.sha256()
    for ch in sorted(mapping):
        for p in sorted(mapping[ch], key=lambda x: x.name):
            h.update(f"{ch}|{p.name}|".encode())
            h.update(hashlib.sha256(p.read_bytes()).digest())
    return h.hexdigest()[:16]


_DBC_CHECKS: dict[tuple, list[str]] = {}


def dbc_problems(mapping: dict[str, list[Path]]) -> list[str]:
    """Blocking DBC issues for one device's mapping (cached: devices often share DBCs): Duplicates within a DBC (the
    decoder refuses it) + same ID and message name with different signals across DBCs on a channel (mixed Parquet)."""
    key = tuple(sorted((ch, str(p)) for ch, ps in mapping.items() for p in ps))
    if key not in _DBC_CHECKS:
        files = {p.name: p for ps in mapping.values() for p in ps}
        errs = [e for p in files.values() for e in within_dbc_duplicates(p)]
        errs += cross_dbc_collisions(files, {ch: [p.name for p in ps] for ch, ps in mapping.items()})[0]
        _DBC_CHECKS[key] = errs
    return _DBC_CHECKS[key]


def is_dbc_error(output: str) -> bool:
    """The decoder could not load the DBCs (vs a problem with a log file)."""
    return bool(re.search(r"Failed to load DBC|No DBC files found|key collision", output, re.I))


def fmt_size(n: float) -> str:
    if n >= 1e9:
        return f"{n / 1e9:.2f} GB"
    if n >= 1e6:
        return f"{n / 1e6:.1f} MB"
    return f"{n / 1e3:.0f} KB"


# ---------------------------------------------------------------- manifest
def load_manifest(lake: Path) -> dict:
    p = lake / MANIFEST
    man = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {"devices": {}, "files": {}}
    man["schema"] = "canedge-parquet-manifest/2"
    return man


def save_manifest(lake: Path, man: dict) -> None:
    lake.mkdir(parents=True, exist_ok=True)
    tmp = lake / (MANIFEST + ".tmp")
    tmp.write_text(json.dumps(man, indent=1), encoding="utf-8")
    os.replace(tmp, lake / MANIFEST)


# ---------------------------------------------------------------- decoding
def run_decoder(tool: Path, paths: list[str], mapping: dict, lake: Path, password_file: Path | None) -> tuple[int, str]:
    """Decode files (all in one folder) straight into the lake (existing outputs are overwritten). The decoder runs
    in an empty cwd so it never picks up stray DBC/passwords files. Returns (returncode, error lines or output tail)."""
    cmd = [str(tool), "--non-interactive", "--verbosity=2", "-X", "--no-concat", "-O", str(lake.resolve())]
    if password_file:
        cmd.append(f"--password-file={password_file.resolve()}")
    cmd += [f"--dbc-{ch.lower()}={p.resolve()}" for ch, ps in sorted(mapping.items()) for p in ps]
    with tempfile.TemporaryDirectory() as cwd:
        r = subprocess.run(cmd + ["-i"] + paths, cwd=cwd, capture_output=True, text=True)
    lines = [x for x in (r.stdout + r.stderr).strip().splitlines() if x.strip() and not x.startswith("---")]
    # decoder log lines: <date> <time> <pid> <tid> E|F <message>
    errors = [re.sub(r"^.*?\s[EF]\s+", "", x) for x in lines if re.search(r"\s[EF]\s", x)]
    return r.returncode, "\n".join((errors or lines)[-6:])


def mixed_lengths(path: str, mapping: dict) -> list[str]:
    """Messages sent with several frame lengths in this log file whose signals go beyond the shortest one (canedge-dbc
    dbc_coverage on the file's raw stats): The decoder skips the short frames and can write unreadable Parquet."""
    sys.path.insert(0, str(COVERAGE))
    import dbc_coverage as cov
    demux = [f"--muxtp={muxtp.spec(e)}" for e in muxtp.from_dbcs(mapping)]  # CANmod.router: demuxed CAN11+
    r = subprocess.run([sys.executable, str(cov.RAW_STATS), path, "--yes", *demux], capture_output=True, text=True)
    if r.returncode not in (0, 1):
        return []
    out = []
    for chans in cov.bus_ids([cov.describe(Path(path))]).values():
        for ch, ps in sorted(mapping.items()):
            out += [f"{ch} {i['id']} {i['message']} ({i['dbc']}): frames {'/'.join(map(str, i['frame_lengths']))} "
                    f"bytes, beyond the shortest: {', '.join(i['signals_beyond_frame'])}"
                    for i in cov.check_channel(cov.dbc_messages(ps), chans.get(ch, {}), 0)["issues"]
                    if i.get("mixed_lengths")]
    return out


def unreadable_outputs(lake: Path, batch: list[dict]) -> dict[str, list[str]]:
    """Log file path -> the Parquet files the decoder wrote for it (<session>_<split>.parquet) that cannot be read.
    One scan of the device folders per batch."""
    import pyarrow.parquet as pq
    want = {f"{Path(f['path']).parent.name}_{Path(f['path']).stem}.parquet": f["path"] for f in batch}
    outs = [o for dev in {f["device"] or "*" for f in batch} for o in lake.glob(f"{dev}/*/*/*/*/*.parquet")
            if o.name in want]

    def ok(o: Path) -> bool:
        try:
            pq.read_metadata(o)
            return True
        except Exception:
            return False
    bad: dict[str, list[str]] = {}
    with ThreadPoolExecutor(8) as ex:   # footer reads are I/O bound
        for o, good in zip(outs, ex.map(ok, outs)):
            if not good:
                bad.setdefault(want[o.name], []).append(o.as_posix())
    return bad


def batches(files: list[dict]) -> list[list[dict]]:
    """Group files by parent folder (the decoder needs a common root) and chunk by command line length."""
    by_dir: dict[str, list[dict]] = {}
    for f in files:
        by_dir.setdefault(str(Path(f["path"]).parent), []).append(f)
    out = []
    for _, fs in sorted(by_dir.items()):
        cur, n = [], 0
        for f in sorted(fs, key=lambda x: x["path"]):
            if cur and n + len(f["path"]) + 3 > MAX_CMD_CHARS:
                out.append(cur)
                cur, n = [], 0
            cur.append(f)
            n += len(f["path"]) + 3
        if cur:
            out.append(cur)
    return out


def write_devicemeta(lake: Path, man: dict, plan: dict, passwords: Path | None = None) -> None:
    """aggregations/devicemeta/2024/01/01/devicemeta.parquet as the cloud data lake writes it (MetaName = '<meta>
    (<ID>)', DeviceId), so local lakes get the same device names (canedge-lake-query devices(), --device names).
    The meta name comes from the newest decoded MF4 file's header; a device without MF4 files: From a finalized copy
    of its newest decoded MFC/MFE/MFM file (mdf2mdf, one file per device and run)."""
    for dev, p in plan.items():
        done = sorted(f["path"] for f in p["todo"] if man["files"].get(f["path"], {}).get("status") == "ok")
        meta, other = None, None
        for path in reversed(done):
            if Path(path).suffix.lower() != ".mf4":
                other = other or path
                continue
            meta = read_header(Path(path))["meta"]
            if meta:
                break
        if not meta and other:
            try:
                meta = finalized_header(other, passwords)["meta"]
            except (RuntimeError, SystemExit) as e:  # no meta name is fine (devices() falls back to the ID)
                print(f"NOTE: {dev}: No meta name ({e})", file=sys.stderr)
        if meta:
            man["devices"].setdefault(dev, {})["meta"] = meta.strip()
    rows = [(f"{d['meta']} ({dev})", dev) for dev, d in sorted(man["devices"].items()) if d.get("meta") and DEVICE_RE.fullmatch(dev)]
    if not rows:
        return
    import pyarrow as pa
    import pyarrow.parquet as pq
    out = lake / "aggregations" / "devicemeta" / "2024" / "01" / "01"
    out.mkdir(parents=True, exist_ok=True)
    pq.write_table(pa.table({"MetaName": [r[0] for r in rows], "DeviceId": [r[1] for r in rows]}),
                   out / "devicemeta.parquet", compression="snappy")


def decode_device(tool, lake, man, files, mapping, fp, password_file, lock: threading.Lock,
                  stop: threading.Event) -> dict | None:
    """Decode the files of one device into the lake, updating the manifest after each batch. Stops at the first
    failing batch and returns its error (all files of that batch get status 'error', so a re-run decodes them again;
    a DBC load error leaves them out of the manifest: the files are fine). Devices can run in parallel threads: The
    manifest is updated under `lock`, and a failure sets `stop` so no device starts another batch."""
    for batch in batches(files):
        if stop.is_set():
            return None
        rc, tail = run_decoder(tool, [f["path"] for f in batch], mapping, lake, password_file)
        if rc != 0 and is_dbc_error(tail):
            stop.set()
            return {"folder": str(Path(batch[0]["path"]).parent), "files": len(batch), "returncode": rc,
                    "output": tail, "dbc_error": True}
        now = dt.datetime.now(dt.timezone.utc).isoformat(timespec="seconds")
        unreadable = unreadable_outputs(lake, batch) if rc == 0 else {}
        with lock:
            for f in batch:
                entry = {"size": f["size"], "mtime": f["mtime"], "device": f["device"], "dbc_fingerprint": fp,
                         "status": "ok" if rc == 0 else "error", "decoded_utc": now}
                if rc != 0:
                    entry.update(returncode=rc, output=tail)
                elif f["path"] in unreadable:
                    entry["unreadable_parquet"] = unreadable[f["path"]]
                man["files"][f["path"]] = entry
            save_manifest(lake, man)
        if rc != 0:
            stop.set()
            return {"folder": str(Path(batch[0]["path"]).parent), "files": len(batch), "returncode": rc, "output": tail}
    return None


# ---------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--input", type=Path, nargs="+", required=True, help="Log files/folders (local)")
    ap.add_argument("--project", type=Path, help="projects/<name>: DBCs from dbc/ (+ dbc-groups.json), lake = parquet/")
    ap.add_argument("--out", type=Path, help="Quick mode: lake folder (e.g. TEMP/lake)")
    for ch in CHANNELS:
        ap.add_argument(f"--dbc-{ch}", type=Path, action="append", default=[], metavar="DBC",
                        help=argparse.SUPPRESS if ch not in ("can1", "can2", "can9", "lin1") else
                        f"Quick mode: DBC for {ch.upper()} (repeatable; --dbc-canN for any N, e.g. --dbc-can11 for "
                        f"CANmod.router S1 with the router DBC as --dbc-can2, and --dbc-lin2 also work)")
    ap.add_argument("--device", action="append", help="Only these device IDs (repeatable)")
    ap.add_argument("--exclude", type=Path, action="append", default=[],
                    help="Skip this log file, e.g. a corrupt file the user chose to leave out (repeatable)")
    ap.add_argument("--all-dbcs-confirmed", action="store_true",
                    help="User confirmed that all DBCs apply to all devices (also devices in no group)")
    ap.add_argument("--rebuild", action="store_true",
                    help="Delete + re-decode devices whose DBC fingerprint changed (and devices given with --device)")
    ap.add_argument("--max-gb", type=float, default=1.0, help="Size guard (default 1.0 GB of log files to decode)")
    ap.add_argument("--yes", action="store_true", help="Proceed beyond the size guard (user confirmed)")
    ap.add_argument("--workers", type=int, default=0,
                    help="Devices decoded in parallel (default: up to 4, half the CPUs; 1 = one after another). "
                         "Always per device: Files of one device are decoded one batch after another")
    ap.add_argument("--dry-run", action="store_true", help="Only print the plan")
    ap.add_argument("--passwords", "--password-file", dest="password_file", type=Path,
                    help="passwords.json for encrypted MFE/MFM files "
                         "(project default: <project>/passwords.json if present)")
    ap.add_argument("--tool", help="Path to mdf2parquet_decode (default: $CANEDGE_MDF2PARQUET, else canedge-mdf2parquet/bin)")
    ap.add_argument("--json", action="store_true", help="Print the summary as JSON")
    a = ap.parse_args()

    log = (lambda *x: print(*x, file=sys.stderr)) if a.json else print
    plain_dbcs = {ch.upper(): [p.resolve() for p in getattr(a, f"dbc_{ch}")] for ch in CHANNELS if getattr(a, f"dbc_{ch}")}
    if a.project:
        if a.out or plain_dbcs:
            ap.error("use either --project or --dbc-*/--out")
        if not (a.project / "dbc").is_dir():
            sys.exit(f"Not found: {a.project / 'dbc'} (create the project with canedge-workspace new_project.py)")
        lake = a.project / "parquet"
    else:
        if not (a.out and plain_dbcs):
            ap.error("give --project, or at least one --dbc-canX + --out")
        missing = [str(p) for ps in plain_dbcs.values() for p in ps if not p.exists()]
        if missing:
            sys.exit("DBC not found: " + ", ".join(missing))
        lake = a.out
    for p in a.input:
        if not p.exists():
            sys.exit(f"Input not found: {p}")

    files = []
    excluded = {p.resolve() for p in a.exclude}
    for f in find_log_files(a.input):
        if f in excluded:
            continue
        st = f.stat()
        files.append({"path": f.as_posix(), "size": st.st_size, "mtime": round(st.st_mtime, 3), "device": device_of(f)})
    if a.device:
        devs = {d.upper() for d in a.device}
        files = [f for f in files if f["device"] in devs]
    if not files:
        log("No log files found")
        return 0

    by_dev: dict[str, list[dict]] = {}
    for f in files:
        by_dev.setdefault(f["device"] or "UNKNOWN", []).append(f)
    if not a.project and len(by_dev) > 1 and not a.all_dbcs_confirmed:
        log(f"REFUSED: The input has {len(by_dev)} devices ({', '.join(sorted(by_dev))}) and quick mode applies the "
            f"same DBCs to all of them. Use --device to pick one, a project (dbc/ + optional dbc-groups.json), or "
            f"--all-dbcs-confirmed if the user confirmed the DBCs apply to all devices")
        return 3

    man = load_manifest(lake)
    plan, refused, flagged, router_warn = {}, {}, {}, {}
    for dev, fs in sorted(by_dev.items()):
        if a.project:
            mapping, note = dbcs_for_device(a.project, fs[0]["device"], a.all_dbcs_confirmed)
        else:
            mapping, note = plain_dbcs, "command line DBCs"
        if mapping is None:
            refused[dev] = note
            continue
        probs = dbc_problems(mapping)
        if probs:
            refused[dev] = ("DBC errors (the decoder refuses the DBC, or the Parquet would mix schemas):\n    "
                            + "\n    ".join(probs) + "\n  Fix the DBCs with canedge-dbc (dbc_lint.py lists the lines), "
                            "check with canedge-workspace dbc_groups.py check, then re-run")
            continue
        for w in router_warnings(mapping):
            router_warn.setdefault(dev, []).append(w)
        fp = dbc_fingerprint(mapping)
        prev = man["devices"].get(dev, {}).get("dbc_fingerprint")
        rebuild = bool(a.rebuild and a.device and dev in {d.upper() for d in a.device})
        if prev and prev != fp:
            if not a.rebuild:
                flagged[dev] = (f"DBC files changed since the last decode (fingerprint {prev} -> {fp}). Existing "
                                f"Parquet in {lake / dev} was decoded with the old DBCs. Ask the user, then re-run "
                                f"with --rebuild --device {dev} to delete it and decode again")
                continue
            rebuild = True
        todo = fs if rebuild else [
            f for f in fs if (e := man["files"].get(f["path"])) is None or e.get("status") != "ok"
            or e.get("size") != f["size"] or e.get("mtime") != f["mtime"]]
        plan[dev] = {"mapping": mapping, "note": note, "fp": fp, "todo": todo, "skip": len(fs) - len(todo),
                     "rebuild": rebuild, "first": rebuild or prev != fp}

    encrypted = [f["path"] for p in plan.values() for f in p["todo"] if Path(f["path"]).suffix.lower() in (".mfe", ".mfm")]
    try:
        a.password_file = find_password_file(a.password_file, a.project)
    except SystemExit as e:
        log(str(e))
        return 3
    refusal = passwords_refusal(encrypted, a.password_file, a.project)
    if refusal:
        log(refusal)
        return 3
    if encrypted:
        log(f"{len(encrypted)} encrypted file(s): Decrypting with {a.password_file}")

    total = sum(f["size"] for p in plan.values() for f in p["todo"])
    n_todo = sum(len(p["todo"]) for p in plan.values())
    for dev, p in plan.items():
        chans = ", ".join(f"{ch}: {'+'.join(x.name for x in ps)}" for ch, ps in sorted(p["mapping"].items()))
        log(f"{dev}: {len(p['todo'])} to decode ({fmt_size(sum(f['size'] for f in p['todo']))}), "
            f"{p['skip']} up to date{' [REBUILD]' if p['rebuild'] else ''} | {p['note']} | {chans}")
    for dev, ws in router_warn.items():
        for w in ws:
            log(f"WARNING {dev}: {w}")
    for dev, why in refused.items():
        log(f"REFUSED {dev}: {why}")
    for dev, why in flagged.items():
        log(f"FLAGGED {dev}: {why}")

    # sample decode: one file per device that is decoded with these DBCs for the first time, into a temp folder
    tool, samples = None, {}
    for dev, p in plan.items():
        if not (p["first"] and len(p["todo"]) > 1):
            continue
        tool = tool or resolve("mdf2parquet_decode", a.tool)
        f = sorted(p["todo"], key=lambda x: x["size"])[len(p["todo"]) // 2]
        with tempfile.TemporaryDirectory(prefix="canedge-sample-") as tmp:
            rc, tail = run_decoder(tool, [f["path"]], p["mapping"], Path(tmp), a.password_file)
            folders = sum(1 for d in Path(tmp).glob("*/*") if d.is_dir() and d.parent.name != "aggregations")
        rel = "/".join(Path(f["path"]).parts[-3:])
        samples[dev] = {"file": f["path"], "returncode": rc, "message_folders": folders}
        if rc != 0:
            samples[dev].update(output=tail, dbc_error=is_dbc_error(tail))
            log(f"SAMPLE {dev}: {rel} FAILED ({'DBC load error' if is_dbc_error(tail) else f'decoder exit {rc}'}):"
                f"\n  " + "\n  ".join(tail.splitlines()))
        else:
            log(f"SAMPLE {dev}: {rel} OK -> {folders} message folders"
                + (" - WARNING: the DBCs match nothing in this file (wrong DBC/channel?)" if folders == 0 else ""))
            if mixed := mixed_lengths(f["path"], p["mapping"]):
                samples[dev]["mixed_lengths"] = mixed
                log(f"SAMPLE {dev}: MIXED LENGTHS (short frames skipped, unreadable Parquet risk):\n  " + "\n  ".join(mixed))
    if any(s["returncode"] != 0 for s in samples.values()):
        log("STOPPED: Sample decode failed - nothing was written to the lake. A DBC load error = fix the DBCs "
            "(canedge-dbc); otherwise check the file (corrupt/truncated, password) or --exclude it. Then re-run")
        if a.json:
            print(json.dumps({"stopped": "sample", "samples": samples, "refused": refused, "flagged": flagged}, indent=1))
        return 1
    if any(s.get("mixed_lengths") for s in samples.values()):
        log("STOPPED: Nothing was written to the lake. Split each MIXED LENGTHS message: The signals that fit the "
            "shortest frame in one message, the rest in a same-ID message with its own name in a 2nd DBC on that "
            "channel (decoder-technical.md section 4; canedge-dbc). Then re-run")
        if a.json:
            print(json.dumps({"stopped": "mixed_lengths", "samples": samples, "refused": refused, "flagged": flagged},
                             indent=1))
        return 1
        if a.json:
            print(json.dumps({"stopped": "sample", "samples": samples, "refused": refused, "flagged": flagged}, indent=1))
        return 1

    if total > a.max_gb * 1e9 and not a.yes and not a.dry_run:
        log(f"SIZE GUARD: {n_todo} files, {fmt_size(total)} to decode (limit {a.max_gb} GB), estimated "
            f"~{max(1, round(total / 1e6 / EST_MB_PER_S / 60))} min. Ask the user, then re-run with --yes, or limit "
            f"the scope (--device, fewer --input folders)")
        return 2
    if a.dry_run or n_todo == 0:
        if n_todo == 0:
            log("Nothing to decode" + (" (see REFUSED/FLAGGED above)" if refused or flagged else " - the lake is up to date"))
        return 3 if (refused or flagged) else 0

    tool = tool or resolve("mdf2parquet_decode", a.tool)
    version = version_of(tool)
    lake.mkdir(parents=True, exist_ok=True)
    t_start = time.time()
    devs = [dev for dev, p in plan.items() if p["todo"]]
    workers = max(1, a.workers or default_workers(len(devs)))
    if workers > 1 and "UNKNOWN" in devs:
        log("NOTE: Files outside the CANedge folder layout (device UNKNOWN) may belong to another device in the run: "
            "Decoding the devices one after another")
        workers = 1
    for dev in devs:
        p = plan[dev]
        if p["rebuild"]:
            if dev != "UNKNOWN" and (lake / dev).exists():
                log(f"{dev}: deleting {lake / dev} (rebuild)")
                shutil.rmtree(lake / dev)
            man["files"] = {k: v for k, v in man["files"].items() if v.get("device") != p["todo"][0]["device"]}
        man["devices"][dev] = {"dbc_fingerprint": p["fp"], "dbc_note": p["note"],
                               "dbc_files": {ch: [x.name for x in ps] for ch, ps in sorted(p["mapping"].items())},
                               "decoder": version}
        man["decoder"] = version
    lock, stop = threading.Lock(), threading.Event()

    def decode(dev: str) -> dict | None:
        """One device (own output folder <lake>/<dev>): Devices run in parallel, its batches one after another."""
        p = plan[dev]
        if stop.is_set():
            return None
        log(f"{dev}: decoding {len(p['todo'])} files ...")
        err = decode_device(tool, lake, man, p["todo"], p["mapping"], p["fp"], a.password_file, lock, stop)
        if err:
            err["device"] = dev
        else:
            log(f"{dev}: done")
        return err

    if workers > 1:
        log(f"Decoding {len(devs)} devices in parallel ({workers} at a time)")
    with ThreadPoolExecutor(workers) as ex:
        fails = [e for e in ex.map(decode, devs) if e]
    failed = fails[0] if fails else None
    write_devicemeta(lake, man, plan, a.password_file)
    save_manifest(lake, man)
    secs = time.time() - t_start

    summary = {"lake": lake.as_posix(), "decoder": version, "seconds": round(secs, 1), "workers": workers,
               "devices": {}, "stopped": failed, "also_stopped": fails[1:], "refused": refused, "flagged": flagged, "router_warnings": router_warn,
               "samples": samples}
    for dev, p in plan.items():
        ok = sum(1 for f in p["todo"] if man["files"].get(f["path"], {}).get("status") == "ok")
        msgs = sum(1 for d in (lake / dev).iterdir() if d.is_dir()) if (lake / dev).is_dir() else None
        bad = [b for f in p["todo"] for b in man["files"].get(f["path"], {}).get("unreadable_parquet", [])]
        summary["devices"][dev] = {"files_decoded": ok, "files_not_decoded": len(p["todo"]) - ok,
                                   "files_up_to_date": p["skip"], "message_folders": msgs,
                                   "dbc_files": man["devices"].get(dev, {}).get("dbc_files"), "unreadable_parquet": bad}
    if a.json:
        print(json.dumps(summary, indent=1))
    else:
        print(f"{'STOPPED' if failed else 'Done'} after {secs:.1f} s -> {lake.as_posix()}")
        for dev, s in summary["devices"].items():
            print(f"  {dev}: {s['files_decoded']} files decoded, {s['files_not_decoded']} not decoded, "
                  f"{s['files_up_to_date']} up to date, "
                  f"{s['message_folders'] if s['message_folders'] is not None else '?'} message folders")
            if s["unreadable_parquet"]:
                print(f"  WARNING {dev}: {len(s['unreadable_parquet'])} unreadable Parquet file(s), e.g. "
                      f"{s['unreadable_parquet'][0]}. Likely a variable-length ID: Run canedge-dbc dbc_coverage.py on "
                      f"those log files, split the message (decoder-technical.md section 4), then --rebuild")
        for f in fails:
            if f.get("dbc_error"):
                print(f"DBC LOAD ERROR (exit {f['returncode']}): The decoder could not load the DBCs of "
                      f"{f['device']}:\n  " + "\n  ".join(f["output"].splitlines()))
                print("The log files are fine (not marked as errors). Fix the DBCs with canedge-dbc (dbc_lint.py), "
                      "check with dbc_groups.py check, then re-run")
            else:
                print(f"DECODER ERROR (exit {f['returncode']}) in {f['folder']} ({f['files']} files in the "
                      f"batch):\n  " + "\n  ".join(f["output"].splitlines()))
                print("Tell the user which file failed and why (e.g. corrupt/truncated file, wrong password), see "
                      "decoder-technical.md section 4. The run stopped; the files of that batch are marked 'error'. "
                      "Re-run after a fix, or with --exclude <file> if the user wants to leave the file out")
        if not fails:
            print("Next: canedge-lake-query lake_schema.py --project <p> (or --lake <folder>) to see the decoded tables")
    if failed:
        return 1
    return 3 if (refused or flagged) else 0


if __name__ == "__main__":
    sys.exit(main())
