"""Compute per-file raw CAN/LIN statistics for CANedge log files with the CSS mdf2csv converter.

For each log file (MF4/MFC/MFE/MFM): Convert to a temporary CSV with mdf2csv (one file at a time),
compute frames, IDs, periods, payload variety, bus load per 60 s bin etc., write the result to
data/raw-stats/<DEVICE>/<SESSION>/<SPLIT>.json (schema canedge-raw-stats/1, see
references/raw-stats.md) and delete the temporary CSV immediately. Input files are never modified.

Header step: Every input file's MF4 header is read on every run (64 KB, cheap; canedge-workspace mf4_files.py):
Serial number, session, split, firmware, config CRC32, meta name, bit-rates. Cached stats missing header fields are
updated in place (no re-conversion). MFC/MFE/MFM: The header of ONE file per device/session (with files to process)
is read from a finalized copy (mdf2mdf, deleted at once) and applied to all splits of the session (header
"finalized"; device/session/split still come from the path). --no-headers skips this (header "unsupported", no bus
load). Encrypted files need a password file: --passwords, default <project>/passwords.json.
Cache: A file is skipped if its stats JSON exists with the same size_bytes + mtime (and the same demux mapping).
CANmod.router: With --project, files are demuxed first (mdf2mdf) if the DBCs that apply to the device include the
router DBC (MUX-TP mapping, canedge-workspace muxtp.py): The router channels S1-S4 then show as CAN11-14 (CAN15-18
for a second router), the tunnel frames on CAN2 are gone. --muxtp sets the mapping without a project. Files that
look like un-demuxed router data (CAN2 0x010/0x012 CAN FD) are reported (stats 'router_hint').
Size guard: If the not-yet-cached input exceeds --max-gb (default 1), the script prints the file
count, size and a time estimate and exits with code 3 unless --yes is given. Use --sample N to
process only every Nth split per session.

Usage:
  python raw_stats.py <file/folder> [...] [--device ID] [--session N] [--sample N] [--workers N] [--muxtp 010:11]
  python raw_stats.py --project projects/<name> [--yes] [--json]

Exit codes: 0 ok, 1 some files failed, 2 usage error, 3 stopped by the size guard.
"""
from __future__ import annotations

import argparse
import concurrent.futures as cf
import datetime as dt
import json
import re
import shutil
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import pyarrow as pa
import pyarrow.compute as pc

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
# MF4 header + file listing (re-exported for raw_aggregate / canedge-config) and the mdf2csv converter
from mf4_files import describe, fill_headers, find_log_files, project_paths, session_key  # noqa: E402,F401
from dbc_groups import fmt_can_id  # noqa: E402
from tools import (EST_BYTES_CLASSIC, default_workers, frame_weight, mdf2csv_tables, password_file,  # noqa: E402
                   passwords_refusal, resolve, version as tool_version)
import muxtp  # noqa: E402

SCHEMA = "canedge-raw-stats/1"

def est_storage(frames: int, payload_bytes: int) -> int:
    """Estimated storage bytes of an ID: frames x weight(average data length) x 17. Exact for classic CAN (all
    lengths <= 8 B weigh the same); a CAN FD ID with mixed lengths uses its average length."""
    return int(round(frames * frame_weight(payload_bytes / frames) * EST_BYTES_CLASSIC)) if frames else 0

STATS_ROOT = Path("data/raw-stats")
BIN_S = 60
UNIQUE_PAYLOAD_CAP = 1000
# Static ID: Fewer than 5% of its frames carry a new payload (vs the previous frame of the ID). Same basis as
# canedge-config simulate_config.py 'changes %' = what a data prescaler (all bytes) would log
STATIC_MAX_CHANGES_PCT = 5.0
MAX_BINS = 100_000  # ~69 days of 60 s bins per file; larger spans indicate bad timestamps
STUFF_FACTOR = 0.10  # average share of stuff bits in the stuffable part of a frame (estimate)
MB_PER_S_EST = 8.0  # conservative single-worker throughput (MB of MF4 per second, measured 8-14) for the size guard estimate


# ---------------------------------------------------------------------------------------------- helpers
HEADER_KEYS = ("meta", "header", "id_source", "firmware", "hardware", "config_crc32")  # refreshed on cached stats


def stats_path(meta: dict, root: Path = STATS_ROOT) -> Path:
    if meta["session"] is None or meta["split"] is None:
        stem = re.sub(r"[^A-Za-z0-9_.-]", "_", Path(meta["path"]).stem)
        return root / meta["device"] / "00000000" / f"{stem}.json"
    return root / meta["device"] / f"{meta['session']:08d}" / f"{meta['split']:08d}.json"


def is_cached(meta: dict, root: Path = STATS_ROOT) -> bool:
    p = stats_path(meta, root)
    if not p.exists():
        return False
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    f, ids = d.get("file", {}), d.get("ids") or []
    if ids and "payload_changes" not in ids[0]:
        return False  # stats from before payload_changes (static ID basis): recompute
    if (d.get("demux") or {}).get("muxtp", "") != muxtp.key(meta.get("muxtp") or []):
        return False  # demuxed vs not demuxed (or another mapping): recompute, never mix
    return f.get("size_bytes") == meta["size_bytes"] and abs(float(f.get("mtime", -1)) - meta["mtime"]) < 1e-2


def refresh_header(meta: dict, root: Path = STATS_ROOT) -> bool:
    """Update the header fields of a cached stats file in place (no re-conversion). True if it changed."""
    p = stats_path(meta, root)
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return False
    f = d.get("file", {})
    if meta["header"] == "unsupported" and f.get("header") not in (None, "unsupported"):
        return False  # never replace a known header (e.g. from a finalized copy) with an unknown one
    new = {k: meta[k] for k in HEADER_KEYS + ("bitrates",)}
    if meta["header"] == "finalized" and f.get("id_source") == "header":
        new["id_source"] = "header"  # loose MFC/MFE/MFM file: IDs from its own finalized header (process_one)
    if all(k in f and f[k] == v for k, v in new.items()):
        return False
    f.update(new)
    p.write_text(json.dumps(d, separators=(",", ":")), encoding="utf-8")
    return True


def iso(ts: float) -> str:
    return dt.datetime.fromtimestamp(ts, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def r1(x: float) -> float:
    return round(float(x), 1)


# ---------------------------------------------------------------------------------------------- bus load
def frame_bus_time(ide: np.ndarray, edl: np.ndarray, brs: np.ndarray, n: np.ndarray,
                   nominal: float, data_rate: float) -> np.ndarray:
    """Estimated time on the bus [s] per frame (incl. ~10% dynamic stuff bits and 3 bit intermission).

    Classical CAN: 11-bit = 47 + 8n bits (34 + 8n stuffable), 29-bit = 67 + 8n bits (54 + 8n stuffable).
    CAN FD: arbitration (17 / 36 bits, stuffed) + 13 tail bits at the nominal rate; ESI, DLC, data (5 + 8n,
    stuffed), stuff count (4), CRC (17 / 21) and fixed stuff bits at the data rate if BRS is set.
    """
    n = n.astype(np.float64)
    ext = ide.astype(bool)
    classical = np.where(ext, 67 + 8 * n + STUFF_FACTOR * (54 + 8 * n), 47 + 8 * n + STUFF_FACTOR * (34 + 8 * n))
    t = classical / nominal
    fd = edl.astype(bool)
    if fd.any():
        arb = np.where(ext[fd], 36.0, 17.0) * (1 + STUFF_FACTOR) + 13
        nn = n[fd]
        crc = np.where(nn > 16, 21.0, 17.0)
        data_bits = (5 + 8 * nn) * (1 + STUFF_FACTOR) + 4 + crc + np.ceil((4 + crc) / 4)
        rate = np.where(brs[fd].astype(bool), data_rate, nominal)
        t[fd] = arb / nominal + data_bits / rate
    return t


# ---------------------------------------------------------------------------------------------- stats
def table_to_df(t: pa.Table, lin: bool) -> pd.DataFrame:
    """Columns: ts, ch (str), ide, id (int), dlc, n, edl, brs, payload (int code)."""
    idd = pc.dictionary_encode(t["ID"]).combine_chunks()
    id_vals = np.array([int(x, 16) if x else -1 for x in idd.dictionary.to_pylist()], dtype=np.int64)
    pay = pc.dictionary_encode(t["DataBytes"]).combine_chunks()
    zeros = np.zeros(t.num_rows, dtype=np.int8)
    col = lambda name: t[name].to_numpy() if name in t.column_names else zeros  # noqa: E731
    prefix = "LIN" if lin else "CAN"
    return pd.DataFrame({
        "ts": t["TimestampEpoch"].to_numpy(),
        "ch": np.char.add(prefix, col("BusChannel").astype(str)),
        "ide": col("IDE").astype(np.int8),
        "id": id_vals[idd.indices.to_numpy(zero_copy_only=False)],
        "dlc": col("DataLength" if lin else "DLC").astype(np.int16),
        "n": col("DataLength").astype(np.int16),
        "edl": col("EDL").astype(np.int8),
        "brs": col("BRS").astype(np.int8),
        "payload": pay.indices.to_numpy(zero_copy_only=False).astype(np.int64),
    })


def display_id(s: str) -> str:
    """A cached ID string as shown to people (canedge-workspace fmt_can_id): 11-bit 0x0123 -> 0x123, 29-bit + LIN as is.
    The cache keeps its own format (id_str)."""
    return fmt_can_id(int(s, 16), False) if len(s) == 6 else s


def id_str(ch: str, ide: int, ident: int) -> str:
    if ch.startswith("LIN"):
        return f"0x{ident:02X}"
    return f"0x{ident:08X}" if ide else f"0x{ident:04X}"


def compute_stats(df: pd.DataFrame, meta: dict, data_rate: float) -> dict:
    t0, t1 = float(df["ts"].min()), float(df["ts"].max())
    origin = np.floor(t0 / BIN_S) * BIN_S
    nbins = int((t1 - origin) // BIN_S) + 1
    bins_ok = nbins <= MAX_BINS
    # covered seconds per bin (overlap of the bin with the file's first..last frame)
    if bins_ok:
        starts = origin + BIN_S * np.arange(nbins)
        covered = np.clip(np.minimum(starts + BIN_S, t1) - np.maximum(starts, t0), 0, BIN_S)
        if t1 == t0:
            covered[:] = 1.0
    channels: dict = {}
    for ch, g in df.groupby("ch", sort=True):
        c = {"frames": int(len(g)), "unique_ids": int(g[["ide", "id"]].drop_duplicates().shape[0]),
             "error_frames": None}
        tx = int(g["dir"].sum()) if "dir" in g else 0
        if tx:
            c["tx_frames"] = tx
        rate = meta["bitrates"].get(ch) if ch.startswith("CAN") else None
        busy = None
        if rate:
            busy = frame_bus_time(g["ide"].to_numpy(), g["edl"].to_numpy(), g["brs"].to_numpy(), g["n"].to_numpy(),
                                  rate, data_rate)
            dur = max(t1 - t0, 1e-3)
            c["busload_mean_pct"] = r1(100 * busy.sum() / dur)
        if bins_ok:
            idx = ((g["ts"].to_numpy() - origin) // BIN_S).astype(np.int64)
            frames = np.bincount(idx, minlength=nbins)
            b = {"start_utc": iso(origin), "frames": frames.tolist()}
            if busy is not None:
                load = 100 * np.bincount(idx, weights=busy, minlength=nbins) / np.maximum(covered, 1.0)
                b["busload_pct"] = [r1(x) for x in load]
                c["busload_max_pct"] = r1(load.max())  # same bins as the mean (coverage floored at 1 s)
            c["bins"] = b
        elif busy is not None:
            c["busload_max_pct"] = c["busload_mean_pct"]
        if busy is not None:
            # keep schema key order: frames, unique_ids, error_frames, busload_mean_pct, busload_max_pct, bins
            c = {k: c[k] for k in ("frames", "unique_ids", "error_frames", "tx_frames", "busload_mean_pct",
                                   "busload_max_pct", "bins") if k in c}
        channels[ch] = c

    # ---- per ID
    df = df.sort_values(["ch", "ide", "id", "ts"], kind="mergesort").reset_index(drop=True)
    key = df["ch"].astype(str) + "|" + df["ide"].astype(str) + "|" + df["id"].astype(str)
    gid, uniq = pd.factorize(key, sort=False)
    ts = df["ts"].to_numpy()
    d = np.diff(ts, prepend=np.nan)
    same = np.r_[False, gid[1:] == gid[:-1]]
    d[~same] = np.nan
    per = pd.DataFrame({"g": gid, "d": d}).groupby("g")["d"]
    med, p95 = per.median().to_numpy(), per.quantile(0.95).to_numpy()
    grp = df.assign(g=gid).groupby("g", sort=True)
    agg = grp.agg(ch=("ch", "first"), ide=("ide", "first"), id=("id", "first"), frames=("ts", "size"),
                  first=("ts", "first"), last=("ts", "last"), edl=("edl", "max"), nmax=("n", "max"),
                  payload_bytes=("n", "sum"))
    up = df.assign(g=gid).drop_duplicates(["g", "payload"]).groupby("g").size().to_numpy()
    pay = df["payload"].to_numpy()
    chg = np.bincount(gid, weights=(~same | np.r_[True, pay[1:] != pay[:-1]]).astype(float), minlength=len(uniq))
    dlcs = df.assign(g=gid).drop_duplicates(["g", "dlc"]).groupby("g")["dlc"].apply(lambda s: sorted(int(x) for x in s))
    ids = []
    for i, row in enumerate(agg.itertuples()):
        e = {"channel": row.ch, "id": id_str(row.ch, int(row.ide), int(row.id)), "ide": int(row.ide),
             "fd": bool(row.edl), "frames": int(row.frames), "first_utc": iso(row.first), "last_utc": iso(row.last),
             "dlc": dlcs.iloc[i], "data_length_max": int(row.nmax),
             "period_ms_median": None if np.isnan(med[i]) else round(med[i] * 1000, 3),
             "period_ms_p95": None if np.isnan(p95[i]) else round(p95[i] * 1000, 3),
             "unique_payloads": int(min(up[i], UNIQUE_PAYLOAD_CAP)), "payload_changes": int(chg[i]),
             "payload_bytes": int(row.payload_bytes),
             "est_storage_bytes": est_storage(int(row.frames), int(row.payload_bytes))}
        if up[i] >= UNIQUE_PAYLOAD_CAP:
            e["unique_payloads_capped"] = True
        ids.append(e)
    ids.sort(key=lambda e: -e["est_storage_bytes"])
    return {"time": {"first_utc": iso(t0), "last_utc": iso(t1), "duration_s": round(t1 - t0, 3)},
            "frames_total": int(len(df)), "bin_s": BIN_S, "channels": channels, "ids": ids,
            **({} if bins_ok else {"warnings": [f"Timestamp span {t1 - t0:.0f} s too large for 60 s bins - bins omitted"]})}


def process_one(meta: dict, tool: str, tmp_root: str, version: str, data_rate: float,
                passwords: str | None, root: str, mdf2mdf: str | None = None, mdf2mdf_version: str | None = None) -> dict:
    """Convert one file, compute + write its stats, always delete the temporary CSV. Returns a summary dict."""
    t_start = time.time()
    res = {"path": meta["path"], "device": meta["device"], "session": meta["session"], "split": meta["split"],
           "size_bytes": meta["size_bytes"]}
    try:
        try:
            mx = meta.get("muxtp") or []
            tables = mdf2csv_tables(meta["path"], Path(tool), passwords, tmp_root,  # temp CSVs deleted at once
                                    muxtp=mx, mdf2mdf=Path(mdf2mdf) if mx and mdf2mdf else None)
        except RuntimeError as e:
            res["error"] = str(e)
            return res
        frames = []
        for bus, t in tables:
            lin = bus == "LIN"
            if t.num_rows:
                df = table_to_df(t, lin)
                df["dir"] = t["Dir"].to_numpy().astype(np.int8)
                frames.append(df)
        if not frames:
            res["error"] = "no frames in file"
            return res
        df = pd.concat(frames, ignore_index=True)
        st = compute_stats(df, meta, data_rate)
        fmeta = {k: meta[k] for k in ("path", "size_bytes", "mtime", "device", "session", "split", "format",
                                       *HEADER_KEYS, "bitrates")}
        for k in ("path_device", "header_serial"):
            if meta.get(k):
                fmeta[k] = meta[k]
        if meta.get("header_ids"):  # loose MFC/MFE/MFM file: Device/session/split from its finalized header
            fmeta.update(meta["header_ids"], id_source="header")
            fmeta.pop("header_serial", None)
        out = {"schema": SCHEMA, "file": fmeta, "tool": {"mdf2csv": version}, **st}
        if mx:
            out["tool"]["mdf2mdf"] = mdf2mdf_version
            out["demux"] = {"muxtp": muxtp.key(mx), "mapping": mx, "labels": muxtp.labels(mx)}
        hint = muxtp.detect(st["ids"], mx)
        if hint:
            out["router_hint"] = [{"channel": e["channel"], "id": e["id"], "frames": e["frames"]} for e in hint]
            res["router_hint"] = out["router_hint"]
        op = stats_path(meta, Path(root))
        op.parent.mkdir(parents=True, exist_ok=True)
        op.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
        loads = [f"{ch} {c['busload_mean_pct']}%" for ch, c in st["channels"].items() if "busload_mean_pct" in c]
        res.update(frames=st["frames_total"], ids=len(st["ids"]), loads=loads, duration_s=st["time"]["duration_s"],
                   stats=str(op), warnings=st.get("warnings", []))
        return res
    except Exception as e:  # noqa: BLE001 - report and continue with the next file
        res["error"] = f"{type(e).__name__}: {e}"
        return res
    finally:
        res["seconds"] = round(time.time() - t_start, 2)


def clean_stale_tmp(tmp_root: Path, max_age_h: float = 6) -> None:
    if not tmp_root.exists():
        return
    now = time.time()
    for d in tmp_root.iterdir():
        if d.is_dir() and now - d.stat().st_mtime > max_age_h * 3600:
            shutil.rmtree(d, ignore_errors=True)


def select_sample(metas: list[dict], n: int) -> list[dict]:
    """Every Nth split per device/session (always incl. the first split)."""
    by: dict = {}
    for m in metas:
        by.setdefault((m["device"], m["session"]), []).append(m)
    out = []
    for _, ms in sorted(by.items(), key=lambda kv: (kv[0][0], kv[0][1] or 0)):
        ms.sort(key=lambda m: (m["split"] or 0, m["path"]))
        out += ms[::n]
    return out


# ---------------------------------------------------------------------------------------------- main
def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="*", type=Path, help="Log files and/or folders (searched recursively)")
    ap.add_argument("--project", type=Path, help="projects/<name>: Use the project's sources (+ devices filter)")
    ap.add_argument("--device", action="append", help="Only these device IDs (repeatable)")
    ap.add_argument("--session", type=int, action="append", help="Only these sessions (repeatable)")
    ap.add_argument("--sample", type=int, default=1, help="Process every Nth split per session (default 1 = all)")
    ap.add_argument("--max-gb", type=float, default=1.0, help="Size guard threshold for uncached input (default 1)")
    ap.add_argument("--yes", action="store_true", help="Proceed even if the uncached input exceeds --max-gb")
    ap.add_argument("--workers", type=int, default=0,
                    help="Parallel files (default: up to 4, half the CPUs; 1 = one after another; each uses its own temp dir)")
    ap.add_argument("--force", action="store_true", help="Recompute even if cached")
    ap.add_argument("--tool", help="Path to mdf2csv (default: env CANEDGE_MDF2CSV, else canedge-mdf2csv/bin)")
    ap.add_argument("--passwords", help="passwords.json for encrypted files (MFE/MFM; default <project>/passwords.json)")
    ap.add_argument("--no-headers", action="store_true",
                    help="MFC/MFE/MFM: Do not read the header from a finalized copy (no meta/firmware/CRC/bus load)")
    ap.add_argument("--muxtp", action="append",
                    help="CANmod.router demux for all files: [canN:]ID:S1[,S2,S3,S4] (e.g. 010:11, repeatable; "
                         "'default' = 010:11). Default with --project: from the router DBC of each device")
    ap.add_argument("--no-demux", action="store_true", help="Never demux (stats of the raw tunnel frames on CAN2)")
    ap.add_argument("--fd-data-bitrate", type=float, default=2_000_000,
                    help="Assumed CAN FD data-phase bit-rate for BRS frames (default 2000000, not in the MF4 header)")
    ap.add_argument("--stats-root", type=Path, default=STATS_ROOT, help=argparse.SUPPRESS)
    ap.add_argument("--json", action="store_true", help="Print a JSON run summary at the end")
    a = ap.parse_args()

    paths = list(a.paths)
    devices = {d.upper() for d in a.device or []}
    if a.project:
        pp, pdev = project_paths(a.project)
        paths += pp
        devices |= set(pdev)
    if not paths:
        ap.error("give one or more paths, or --project")
    if a.sample < 1:
        ap.error("--sample must be >= 1")
    missing = [str(p) for p in paths if not p.exists()]
    if missing:
        print("WARNING: Not found: " + ", ".join(missing), file=sys.stderr)
    metas = [describe(f) for f in find_log_files([p for p in paths if p.exists()])]
    if devices:
        metas = [m for m in metas if m["device"] in devices]
    if a.session:
        metas = [m for m in metas if m["session"] in set(a.session)]
    if a.sample > 1:
        metas = select_sample(metas, a.sample)
    if not metas:
        print("No log files found.")
        return 0
    try:
        cli_mx = [muxtp.parse_spec(x) for x in a.muxtp or []]
    except ValueError as e:
        ap.error(str(e))
    dev_mx: dict = {}
    for m in metas:
        if a.no_demux:
            m["muxtp"] = []
        elif cli_mx:
            m["muxtp"] = cli_mx
        elif a.project:
            k = (m["device"], m.get("path_device"))
            if k not in dev_mx:
                dev_mx[k] = muxtp.for_device(a.project, m["device"], alt_device=m.get("path_device"))
            m["muxtp"] = dev_mx[k]
        else:
            m["muxtp"] = []
    demux_on = sorted({muxtp.key(m["muxtp"]) for m in metas if m["muxtp"]})
    if demux_on:
        print("CANmod.router demux (mdf2mdf): " + "; ".join(demux_on))

    todo = metas if a.force else [m for m in metas if not is_cached(m, a.stats_root)]
    cached = len(metas) - len(todo)
    todo_paths = {m["path"] for m in todo}
    # The same device/session/split in two folders (e.g. SD + S3 copy) shares one stats file: Process only the last
    # copy, so parallel workers never write the same file
    n_copies = len(todo)
    todo = list({stats_path(m, a.stats_root): m for m in todo}.values())
    workers = max(1, a.workers or default_workers(len(todo)))
    gb = sum(m["size_bytes"] for m in todo) / 1e9
    est_min = gb * 1000 / (MB_PER_S_EST * max(1, min(workers, 4) * 0.8)) / 60
    print(f"{len(metas)} log files selected, {cached} cached, {len(todo)} to process ({gb:.2f} GB)"
          + (f", {n_copies - len(todo)} duplicate copies skipped (same device/session/split)" if n_copies > len(todo) else ""))
    pw = password_file(a.passwords, a.project)
    refusal = passwords_refusal([m["path"] for m in todo], pw, a.project)
    if refusal:
        print(refusal)
        return 2
    if gb > a.max_gb and not a.yes:
        s5 = select_sample(todo, 5)
        print(f"SIZE GUARD: {len(todo)} files / {gb:.2f} GB not yet processed - estimated ~{max(est_min, 1):.0f} min "
              f"(rough, local disk). Re-run with --yes to proceed, or reduce the input: --sample 5 (every 5th split "
              f"per session) = {len(s5)} files / {sum(m['size_bytes'] for m in s5) / 1e9:.2f} GB, or filter with "
              f"--device / --session.")
        return 3

    tmp_root = a.stats_root / "_tmp"
    tmp_root.mkdir(parents=True, exist_ok=True)
    clean_stale_tmp(tmp_root)
    # MFC/MFE/MFM header: Known from cached stats of the same session, else ONE finalized file per session that has
    # files to process (mdf2mdf). Before processing, so the bit-rates are known for the bus load
    stale_cached = 0
    if not a.no_headers and any(m["header"] == "unsupported" for m in metas):
        known = {}
        for m in metas:
            if m["header"] == "unsupported" and m["path"] not in todo_paths:
                try:
                    f = json.loads(stats_path(m, a.stats_root).read_text(encoding="utf-8"))["file"]
                except (OSError, ValueError, KeyError):
                    continue
                if f.get("header") not in (None, "unsupported"):
                    known.setdefault(session_key(m), f)
                else:
                    stale_cached += 1
        only = {session_key(m) for m in todo if m["header"] == "unsupported"}
        t_h = time.time()
        hres = fill_headers(metas, pw, known, only, a.workers, tmp_root)
        if hres["finalized"]:
            print(f"MFC/MFE/MFM header: {hres['finalized']} session(s) read from a finalized copy of one file (mdf2mdf, "
                  f"{time.time() - t_h:.0f} s), {hres['reused']} known from cached stats or MF4 files of the session")
        for e in hres["failed"]:
            print(f"WARNING: Header not readable: {e}")
    refreshed = sum(refresh_header(m, a.stats_root) for m in metas if m["path"] not in todo_paths)
    if refreshed:
        print(f"{refreshed} cached stats updated with header fields (meta name etc.)")
    unsup = sum(m["header"] == "unsupported" for m in todo)
    if unsup:
        print(f"NOTE: {unsup} MFC/MFE/MFM files: Header fields unknown "
              f"({'--no-headers' if a.no_headers else 'mdf2mdf failed, see the warnings'}: no meta name, firmware, "
              f"config CRC32 or bus load)")
    if stale_cached:
        print(f"NOTE: {stale_cached} cached MFC/MFE/MFM stats were computed without header (no bus load): Re-run with "
              f"--force to recompute them")
    if not todo:
        print(f"Nothing to do - stats are cached in {a.stats_root}")
        return 0

    tool = resolve("mdf2csv", a.tool)
    version = tool_version(tool)
    mdf2mdf = resolve("mdf2mdf") if any(m["muxtp"] for m in todo) else None
    mdf2mdf_version = tool_version(mdf2mdf) if mdf2mdf else None
    t0 = time.time()
    results = []
    args = (str(tool), str(tmp_root), version, a.fd_data_bitrate, str(pw) if pw else None, str(a.stats_root),
            str(mdf2mdf) if mdf2mdf else None, mdf2mdf_version)

    def report(i: int, r: dict) -> None:
        tag = f"{r['device']}/{(r['session'] or 0):08d}/{(r['split'] or 0):08d}"
        if "error" in r:
            print(f"[{i}/{len(todo)}] {tag} FAILED: {r['error']}")
        else:
            print(f"[{i}/{len(todo)}] {tag} {r['size_bytes'] / 1e6:.1f} MB -> {r['frames']:,} frames, {r['ids']} IDs, "
                  f"{r['duration_s'] / 60:.1f} min" + (f", load {', '.join(r['loads'])}" if r["loads"] else "")
                  + f" ({r['seconds']} s)")
            for w in r.get("warnings", []):
                print(f"    WARNING: {w}")
        sys.stdout.flush()

    if workers > 1:
        with cf.ProcessPoolExecutor(max_workers=workers) as ex:
            futs = [ex.submit(process_one, m, *args) for m in todo]
            for i, f in enumerate(cf.as_completed(futs), 1):
                results.append(f.result())
                report(i, results[-1])
    else:
        for i, m in enumerate(todo, 1):
            results.append(process_one(m, *args))
            report(i, results[-1])
    try:
        tmp_root.rmdir()  # only removes it if empty (another run may be using it)
    except OSError:
        pass

    ok = [r for r in results if "error" not in r]
    failed = [r for r in results if "error" in r]
    el = time.time() - t0
    mb = sum(r["size_bytes"] for r in ok) / 1e6
    print(f"Done: {len(ok)} processed ({mb:.0f} MB, {sum(r['frames'] for r in ok):,} frames) in {el:.0f} s "
          f"({mb / max(el, 1e-3):.1f} MB/s), {cached} cached, {len(failed)} failed. Stats: {a.stats_root}")
    hinted = [r for r in ok if r.get("router_hint")]
    if hinted:
        ids = sorted({f"{h['channel']} {h['id']}" for r in hinted for h in r["router_hint"]})
        print(f"NOTE: {len(hinted)} files look like CANmod.router data that is not demuxed ({', '.join(ids)} CAN FD "
              f"tunnel frames). Confirm with the user, then add the router DBC to the project (copy "
              f"{muxtp.ROUTER_DBC} to <project>/dbc/) and re-run, or pass --muxtp default")
    mism = sorted({(m["path_device"], m["device"]) for m in metas if m.get("path_device")})
    for pdv, dv in mism:
        print(f"NOTE: Folder {pdv} contains files with header serial {dv} - stats are stored under {dv}")
    if a.json:
        print(json.dumps({"selected": len(metas), "cached": cached, "processed": len(ok), "failed":
                          [{"path": r["path"], "error": r["error"]} for r in failed], "seconds": round(el, 1),
                          "devices": sorted({m["device"] for m in metas}), "stats_root": str(a.stats_root),
                          "mdf2csv": version}, indent=1))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
