"""Upload CANedge log files from an SD card (or a local copy of it) to S3 with the CANedge S3 metadata.

The CANedge2/3 adds metadata when it uploads a file ('Timestamp' = logging start, 'Fw', 'Hw', ...). Files copied
from an SD card have none, so S3 tools cannot place them in time (canedge-s3 time filters, the S3 overview,
data lake backlog processing). This script reads each local MF4 file (header + first frame) and uploads it with
the same metadata keys as the CSS api-examples upload_sd_to_s3.py:

  Hw = hardware version + '/00.00', Fw, Net = 'sd_manual', Timestamp = first frame (UTC),
  Put-Index = rank of the file in the device's recording order (session, then split)

  plan     Read the local files, write a manifest (JSON + CSV) and a summary. No S3 calls
  upload   Upload the planned files to <device>/<session>/<split>.MF4. Skips files already in the bucket (also
           when the device uploaded them itself, with an upload suffix): Lists the device folders once
  verify   HEAD each uploaded file: Size + metadata -> 'verified'

Only uncompressed, unencrypted MF4 files can be read: MFC/MFE/MFM files are listed as unsupported and never
uploaded. The local files are only read, never moved or changed.

Credentials/bucket: The profile credentials/<name>.env (--env NAME, as canedge-s3); the keys need s3:PutObject
(the data lake stack's programmatic access keys, or the device keys). Run from the repo root:

  python sd_upload.py plan --src E:/LOG [--devices 12345678 ...] [--dst my-input-bucket]
  python sd_upload.py upload --manifest data/sd-upload/sd-upload-<utc>.json [--limit 5]
  python sd_upload.py verify --manifest data/sd-upload/sd-upload-<utc>.json
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import struct
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SKILLS / "canedge-s3" / "scripts"), str(SKILLS / "canedge-workspace" / "scripts")]
import s3_inventory as inv_mod  # noqa: E402
from mf4_files import find_log_files, ids_from_path, read_header  # noqa: E402

SCHEMA = "canedge-s3-sd-upload/1"
CSV_FIELDS = ["key", "path", "size", "device", "session", "split", "logging_start_utc", "status", "error"]
WORKERS = 8


# ---------------------------------------------------------------- MF4: first frame (header fields: mf4_files)

CHUNK = 262144  # header ~20 KB + start of the data block + first records
MAX_FETCH = 4 * 1024 * 1024


class Reader:
    """Byte access to the start of a file; fetches more via fetch(start, end_inclusive) when needed."""

    def __init__(self, fetch, size: int):
        self.fetch, self.size = fetch, size
        self.buf = fetch(0, min(CHUNK, size) - 1)

    def get(self, off: int, n: int) -> bytes:
        while off + n > len(self.buf) and len(self.buf) < min(self.size, MAX_FETCH):
            end = min(len(self.buf) * 2, self.size, MAX_FETCH)
            self.buf += self.fetch(len(self.buf), end - 1)
        if off + n > len(self.buf):
            raise ValueError(f"MF4 block at {off} beyond fetched range")
        return self.buf[off:off + n]


def _block(r: Reader, off: int, expect: bytes) -> tuple[list, int]:
    bid, _, _, n_links = struct.unpack("<4s4sQQ", r.get(off, 24))
    if bid != expect:
        raise ValueError(f"expected {expect!r} at {off}, got {bid!r}")
    return list(struct.unpack(f"<{n_links}Q", r.get(off + 24, 8 * n_links))), off + 24 + 8 * n_links


def _first_frame_utc(r: Reader) -> dt.datetime:
    """HD start time (= SESSION start on CANedge files) + the smallest record time in the fetched data."""
    hd_links, hd_data = _block(r, 64, b"##HD")
    start_ns, _, _, time_flags = struct.unpack("<QhhB", r.get(hd_data, 13))
    if time_flags & 1:
        raise ValueError("MF4 start time is local time")
    dg_links, dg_data = _block(r, hd_links[0], b"##DG")
    recid_size = r.get(dg_data, 1)[0]
    groups = {}  # rec_id -> (record size or None for VLSD, master time channel)
    cg = dg_links[1]
    while cg:
        cg_links, cg_data = _block(r, cg, b"##CG")
        rec_id, _, flags, _, _, data_bytes, inval_bytes = struct.unpack("<QQHH4sII", r.get(cg_data, 32))
        master, cn = None, cg_links[1]
        while cn and not (flags & 1):
            cn_links, cn_data = _block(r, cn, b"##CN")
            cn_type, sync, data_type, _, byte_off, bit_count = struct.unpack("<BBBBII", r.get(cn_data, 12))
            if cn_type in (2, 3) and sync == 1:  # master time channel: FW 01.09 uint48 us, FW 01.06 float64 ns
                if data_type == 0:
                    fmt = None
                elif data_type == 4 and bit_count in (32, 64):
                    fmt = "<f" if bit_count == 32 else "<d"
                else:
                    raise ValueError(f"unsupported time channel data type {data_type}")
                factor, offset = 1.0, 0.0
                if cn_links[4]:
                    _, cc_data = _block(r, cn_links[4], b"##CC")
                    cc_type = r.get(cc_data, 1)[0]
                    if cc_type == 1:
                        offset, factor = struct.unpack("<dd", r.get(cc_data + 24, 16))
                    elif cc_type != 0:
                        raise ValueError(f"unsupported time conversion type {cc_type}")
                master = (byte_off, bit_count // 8, fmt, factor, offset)
                break
            cn = cn_links[0]
        groups[rec_id] = (None if flags & 1 else data_bytes + inval_bytes, master)
        cg = cg_links[0]
    data_off = dg_links[2]
    bid, _, dt_len, _ = struct.unpack("<4s4sQQ", r.get(data_off, 24))
    if bid != b"##DT":
        raise ValueError(f"unsupported data block {bid!r}")
    pos = data_off + 24
    end = len(r.buf) if dt_len <= 24 else min(len(r.buf), data_off + dt_len)  # unfinalized files: length 24
    best = None
    while pos + recid_size <= end:
        # record id size 0 = sorted file with one channel group (e.g. re-saved by an MF4 tool)
        rec_id = int.from_bytes(r.buf[pos:pos + recid_size], "little") if recid_size else next(iter(groups))
        if rec_id not in groups:
            raise ValueError(f"unknown record id {rec_id}")
        size, master = groups[rec_id]
        body = pos + recid_size
        if size is None:  # VLSD record: uint32 length + bytes
            if body + 4 > end:
                break
            pos = body + 4 + struct.unpack_from("<I", r.buf, body)[0]
            continue
        if body + size > end:
            break
        if master:
            byte_off, n, fmt, factor, offset = master
            b = r.buf[body + byte_off:body + byte_off + n]
            t = (struct.unpack(fmt, b)[0] if fmt else int.from_bytes(b, "little")) * factor + offset
            best = t if best is None else min(best, t)
        pos = body + size
    if best is None:
        raise ValueError("no timestamped records found")
    if not 0 <= best <= 60 * 86400:
        raise ValueError(f"implausible first frame offset {best:.0f} s")
    return dt.datetime.fromtimestamp(start_ns / 1e9 + best, dt.timezone.utc)


def read_mf4(path: Path, row: dict) -> dict:
    """MF4 header fields + first frame UTC of a local file. Fails if the header does not match the key."""
    hdr = read_header(path)
    serial, session, split = hdr["serial"], hdr["session"], hdr["split"]
    if (serial or "").upper() != row["device"] or session != row["session"] or split != row["split"]:
        raise ValueError(f"MF4 header ({serial}/{session}/{split}) does not match the file path")
    with path.open("rb") as f:
        def fetch(x: int, y: int) -> bytes:
            f.seek(x)
            return f.read(y - x + 1)
        return {"fw": hdr["firmware"], "hw": hdr["hardware"], "first": _first_frame_utc(Reader(fetch, row["size"]))}


# ---------------------------------------------------------------- manifest

def save_manifest(m: dict, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1), encoding="utf-8")
    tmp.replace(path)
    with path.with_suffix(".csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in m["objects"]:
            w.writerow(dict(r, logging_start_utc=(r["metadata"] or {}).get("Timestamp", "")))


def load_manifest(path: Path) -> dict:
    m = json.loads(path.read_text(encoding="utf-8"))
    if m.get("schema") != SCHEMA:
        sys.exit(f"ERROR: {path} is not a {SCHEMA} manifest")
    return m


def report(m: dict, path: Path) -> int:
    status = defaultdict(int)
    for r in m["objects"]:
        status[r["status"]] += 1
    errors = [r for r in m["objects"] if r["status"] == "failed"]
    for r in errors[:20]:
        print(f"  {r['key']}: {r['error']}")
    if len(errors) > 20:
        print(f"  ... {len(errors) - 20} more (see the CSV)")
    print(f"Files by status: {dict(status)}. Manifest updated: {path}")
    return 1 if errors else 0


# ---------------------------------------------------------------- steps

def cmd_plan(a) -> int:
    src = Path(a.src)
    devices = {d.upper() for d in a.devices or []}
    rows = []
    for p in find_log_files([src]):
        info = {**ids_from_path(p), "format": p.suffix.lstrip(".").upper()}
        if None in (info["device"], info["session"], info["split"]) or (devices and info["device"] not in devices):
            continue
        key = f"{info['device']}/{info['session']:08d}/{info['split']:08d}.{info['format']}"
        rows.append({"key": key, "path": str(p.resolve()), "size": p.stat().st_size, **info, "metadata": None,
                     "status": "planned", "error": ""})
    if not rows:
        sys.exit(f"ERROR: No CANedge log files (<device>/<session>/<split>.MF4) under {src}")

    def read(r: dict) -> None:
        if r["format"] != "MF4":
            r["status"], r["error"] = "unsupported", f"{r['format']} (compressed/encrypted) cannot be read"
            return
        try:
            info = read_mf4(Path(r["path"]), r)
            if not (info["fw"] and info["hw"]):
                raise ValueError("firmware/hardware version missing in the MF4 header")
            r["metadata"] = {"Hw": info["hw"] + "/00.00", "Fw": info["fw"], "Net": "sd_manual",
                             "Timestamp": info["first"].strftime("%Y%m%dT%H%M%SZ")}
        except Exception as e:  # noqa: BLE001 - one bad file must not stop the plan
            r["status"], r["error"] = "unreadable", f"mf4: {e}"
    with ThreadPoolExecutor(WORKERS) as ex:
        list(ex.map(read, rows))
    by_dev = defaultdict(list)
    for r in rows:
        by_dev[r["device"]].append(r)
    for rs in by_dev.values():  # Put-Index: rank in recording order (as a device counts its uploads)
        for i, r in enumerate(sorted(rs, key=lambda x: (x["session"], x["split"])), start=1):
            if r["metadata"]:
                r["metadata"]["Put-Index"] = str(i)

    m = {"schema": SCHEMA, "created_utc": inv_mod.iso(dt.datetime.now(dt.timezone.utc)), "src": str(src.resolve()),
         "dst": a.dst, "objects": sorted(rows, key=lambda r: r["key"])}
    path = Path(a.out or f"data/sd-upload/sd-upload-{dt.datetime.now(dt.timezone.utc):%Y%m%dT%H%M%SZ}.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    save_manifest(m, path)
    print(f"Plan: {len(rows)} log files under {src}")
    print(f"{'Device':<10}{'Files':>7}{'GB':>8}{'Upload':>8}{'Skip':>6}  Logging range (first frame, UTC)")
    for dev, rs in sorted(by_dev.items()):
        ok = [r for r in rs if r["metadata"]]
        ts = sorted(r["metadata"]["Timestamp"] for r in ok)
        rng = f"{ts[0][:13]} -> {ts[-1][:13]}" if ts else "-"
        print(f"{dev:<10}{len(rs):>7}{sum(r['size'] for r in rs) / 1e9:>8.2f}{len(ok):>8}{len(rs) - len(ok):>6}  {rng}")
    skipped = defaultdict(int)
    for r in rows:
        if r["status"] != "planned":
            skipped[r["error"].split(":")[0]] += 1
    if skipped:
        print("Not uploaded: " + ", ".join(f"{n} {k}" for k, n in skipped.items()) + " (details in the CSV)")
    print(f"Manifest: {path} (+ {path.with_suffix('.csv').name}). Next: upload --manifest {path} --limit 5")
    return 0


def cmd_upload(a) -> int:
    path = Path(a.manifest)
    m = load_manifest(path)
    inv_mod.use_env(a.env)
    bucket = inv_mod.default_bucket(a.bucket or m.get("dst"))
    m["dst"] = bucket
    todo = [r for r in m["objects"] if r["status"] in ("planned", "failed") and r["metadata"]]
    s3 = inv_mod.s3_client()
    existing = set()  # (device, session, split) already in the bucket, with or without an upload suffix
    for dev in sorted({r["device"] for r in todo}):
        for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=f"{dev}/"):
            for o in page.get("Contents", []):
                k = inv_mod.parse_key(o["Key"])
                if k["kind"] == "log":
                    existing.add((k["device"], k["session"], k["split"]))
    for r in todo:
        if (r["device"], r["session"], r["split"]) in existing:
            r["status"], r["error"] = "in bucket", "already in the bucket (not uploaded)"
    todo = [r for r in todo if r["status"] != "in bucket"]
    if a.limit:
        todo = todo[:a.limit]

    def upload(r: dict) -> None:
        try:
            s3.upload_file(r["path"], bucket, r["key"], ExtraArgs={"Metadata": r["metadata"]})
            r["status"], r["error"] = "uploaded", ""
        except Exception as e:  # noqa: BLE001
            r["status"], r["error"] = "failed", f"upload: {e.__class__.__name__}: {e}"
    print(f"Uploading {len(todo)} files ({sum(r['size'] for r in todo) / 1e9:.2f} GB) to s3://{bucket}/")
    try:
        with ThreadPoolExecutor(WORKERS) as ex:
            for i, _ in enumerate(ex.map(upload, todo), 1):
                if i % 25 == 0:
                    print(f"  uploaded {i}/{len(todo)}", flush=True)
    finally:
        save_manifest(m, path)
    return report(m, path)


def cmd_verify(a) -> int:
    path = Path(a.manifest)
    m = load_manifest(path)
    todo = [r for r in m["objects"] if r["status"] == "uploaded"]
    inv_mod.use_env(a.env)
    s3 = inv_mod.s3_client()

    def verify(r: dict) -> None:
        try:
            h = s3.head_object(Bucket=m["dst"], Key=r["key"])
            got = {k.lower(): v for k, v in (h.get("Metadata") or {}).items()}
            want = {k.lower(): v for k, v in r["metadata"].items()}
            problems = ([f"size {h['ContentLength']} != {r['size']}"] if h["ContentLength"] != r["size"] else []) + \
                       ([f"metadata {got} != {want}"] if got != want else [])
            r["status"], r["error"] = ("failed", "verify: " + "; ".join(problems)) if problems else ("verified", "")
        except Exception as e:  # noqa: BLE001
            r["status"], r["error"] = "failed", f"verify: {e.__class__.__name__}: {e}"
    print(f"Verifying {len(todo)} files in s3://{m['dst']}/")
    try:
        with ThreadPoolExecutor(WORKERS * 2) as ex:
            list(ex.map(verify, todo))
    finally:
        save_manifest(m, path)
    return report(m, path)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="Read the local log files + write the manifest (no S3 calls)")
    p.add_argument("--src", required=True, help="SD card (or copy) folder holding <device>/<session>/<split>.MF4")
    p.add_argument("--devices", nargs="+", help="Only these device IDs")
    p.add_argument("--dst", help="Destination bucket (default at upload: The profile's S3 input bucket)")
    p.add_argument("--out", help="Manifest path (default data/sd-upload/sd-upload-<utc>.json)")
    p.set_defaults(fn=cmd_plan)
    q = sub.add_parser("upload", help="Upload the planned files")
    q.add_argument("--manifest", required=True)
    q.add_argument("--bucket", help="Override the manifest's destination bucket")
    q.add_argument("--limit", type=int, help="At most N files (test runs)")
    q.add_argument("--env", metavar="NAME", help="Data lake profile credentials/NAME.env (default: the only profile)")
    q.set_defaults(fn=cmd_upload)
    v = sub.add_parser("verify", help="Check size + metadata of the uploaded files")
    v.add_argument("--manifest", required=True)
    v.add_argument("--env", metavar="NAME", help="Data lake profile credentials/NAME.env (default: the only profile)")
    v.set_defaults(fn=cmd_verify)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
