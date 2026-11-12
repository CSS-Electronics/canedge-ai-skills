"""Archive or migrate CANedge log files between S3 buckets (AWS) - server-side, manifest-driven, via AWS CloudShell.

Upload this one file to AWS CloudShell (Python 3 + boto3 preinstalled, uses your console login) and run:

  plan           List the source, HEAD where needed, select files, write a manifest (JSON + CSV) and a summary.
                 Writes nothing to S3
  copy           Server-side copy of the manifest's selected files to the destination (same keys), keeping the
                 CANedge metadata. Skips files already copied
  verify         HEAD each copied file in the destination: size, ETag, metadata, storage class -> 'verified'
  delete-source  Delete ONLY verified log files from the source. Needs --confirm-bucket <source bucket>

Selection (plan):
  --before UTC   Files whose data ends before the cutoff: a file is selected if the NEXT file of the same device (in
                 recording order: session, then split) started at or before the cutoff. So no data newer than the
                 cutoff is selected. A file UPLOADED before the cutoff (LastModified, S3's clock) is selected
                 without a HEAD: Its data was logged before it was uploaded
  --all          Everything (full migration)
  Logging time = the 'Timestamp' metadata the CANedge sets on upload, never LastModified or the key suffix. Files
  without it are placed by recording order; log files on S3 are never read (MFC/MFE cannot be parsed anyway).
  Timestamps before 2019 or in the future (RTC not set) and timestamps that go backwards in recording order are not
  trusted. Timestamps from FW < 01.09.01 (may be local time and/or the file end, Z or not) count as +/-14 h.

Typical run:
  python3 s3_migrate.py plan --src my-input-bucket --dst my-archive-bucket --before 2026-01-01 --storage-class GLACIER_IR
  python3 s3_migrate.py copy --manifest migrate-<utc>.json --limit 5
  python3 s3_migrate.py verify --manifest migrate-<utc>.json
  python3 s3_migrate.py copy --manifest migrate-<utc>.json
  python3 s3_migrate.py verify --manifest migrate-<utc>.json
  python3 s3_migrate.py delete-source --manifest migrate-<utc>.json --confirm-bucket my-input-bucket

SD-card data (no CANedge metadata): Upload it with the canedge-s3-sd-upload skill, which reads the local files.

Testing without AWS: plan --inventory <canedge-s3 inventory folder or JSON>
"""
from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

SCHEMA = "canedge-s3-migrate/1"
DEVICE_RE = re.compile(r"^[0-9A-Fa-f]{8}$")
LOG_EXTS = {".mf4", ".mfc", ".mfe", ".mfm"}
STORAGE_CLASSES = ["STANDARD", "STANDARD_IA", "GLACIER_IR", "GLACIER", "DEEP_ARCHIVE"]
NOT_COPYABLE = {"GLACIER", "DEEP_ARCHIVE"}  # source objects in these classes must be restored first
MIN_PLAUSIBLE = dt.datetime(2019, 1, 1, tzinfo=dt.timezone.utc)
APPROX_MARGIN = dt.timedelta(hours=14)  # approximate Timestamp: local time (any zone) and/or file end
ORDER_TOLERANCE = dt.timedelta(hours=1)  # allowed backward step between files (clock sync)
MAX_COPY = 5 * 1024 ** 3  # single CopyObject limit
# Approx. us-east-1 list prices (other regions ~10-20% higher): storage $/GB-month, PUT/COPY $/1000 requests
PRICES = {"STANDARD": (0.023, 0.005), "STANDARD_IA": (0.0125, 0.01), "GLACIER_IR": (0.004, 0.02),
          "GLACIER": (0.0036, 0.03), "DEEP_ARCHIVE": (0.00099, 0.05)}
HEAD_PRICE, LIST_PRICE = 0.0004, 0.005  # $/1000 requests
CSV_FIELDS = ["key", "size", "device", "session", "split", "logging_start_utc", "time_source", "flags", "decision",
              "reason", "status", "error"]


def utcnow() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def from_iso(s: str | None) -> dt.datetime | None:
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc) if s else None


def parse_cutoff(s: str) -> dt.datetime:
    t = dt.datetime.fromisoformat(s.strip().replace("Z", "+00:00"))
    return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)


# ---------------------------------------------------------------- keys + timestamps (as canedge-s3 s3_inventory.py)

def parse_key(key: str) -> dict:
    """<device>/<session>/<split>[-<suffix>].<ext> -> log object with ids, else kind 'other'. The suffix is ignored."""
    parts = key.split("/")
    ext = os.path.splitext(parts[-1])[1].lower()
    dev = parts[0].upper() if DEVICE_RE.fullmatch(parts[0]) else None
    if ext in LOG_EXTS and len(parts) >= 3 and dev and re.fullmatch(r"\d{8}", parts[-2]):
        m = re.match(r"^(\d{8})(?:-[0-9A-Fa-f]{8})?\.", parts[-1])
        if m:
            return {"kind": "log", "device": dev, "session": int(parts[-2]), "split": int(m.group(1)),
                    "format": ext.lstrip(".").upper()}
    return {"kind": "other", "device": dev}


def parse_timestamp(s: str | None) -> dt.datetime | None:
    """CANedge 'Timestamp' metadata -> UTC datetime. With or without Z, read as UTC (see timestamp_quality)."""
    if not s:
        return None
    s = s.strip()
    for fmt in ("%Y%m%dT%H%M%SZ", "%Y%m%dT%H%M%S.%fZ", "%Y%m%dT%H%M%S"):
        try:
            return dt.datetime.strptime(s, fmt).replace(tzinfo=dt.timezone.utc)
        except ValueError:
            pass
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        return t if t.tzinfo else t.replace(tzinfo=dt.timezone.utc)
    except ValueError:
        return None


def timestamp_quality(md: dict) -> str:
    """'exact' = file-created UTC (FW 01.09.01+, or Net 'sd_manual' = set from the first frame by an SD upload
    script), else 'approx': May be local time (+/-14 h) and before FW 01.08.05 the file END. A Z does not prove UTC
    (FW 01.08.01-01.08.05 wrote Z with local time)."""
    if md.get("net") == "sd_manual":
        return "exact"
    try:
        return "exact" if tuple(int(x) for x in md.get("fw", "").split(".")) >= (1, 9, 1) else "approx"
    except ValueError:
        return "approx"


# ---------------------------------------------------------------- selection

def decide_device(rows: list, cutoff: dt.datetime) -> None:
    """Set decision/reason on one device's log rows (see the module docstring)."""
    groups = defaultdict(list)
    for r in rows:
        groups[(r["session"], r["split"])].append(r)
    seq = sorted(groups)
    info = []  # per group: lo, hi (bounds of the start time), usable as evidence
    early = [any(r["time_source"] == "upload" for r in groups[g]) for g in seq]  # uploaded before the cutoff
    run_max = None
    for g in seq:
        lo = hi = None
        for r in groups[g]:
            t = from_iso(r["t"])
            if t is None or "implausible" in r["flags"]:
                continue
            margin = APPROX_MARGIN if "approx-timestamp" in r["flags"] else dt.timedelta(0)
            if lo is None or t - margin < lo:
                lo, hi = t - margin, t + margin
        ok = lo is not None
        if ok and run_max is not None and hi < run_max - ORDER_TOLERANCE:
            ok = False
            for r in groups[g]:
                r["flags"].append("out-of-order")
        if ok:
            run_max = lo if run_max is None else max(run_max, lo)
        info.append((lo, hi, ok))
    # prefix: an earlier (or same) file is known to start after the cutoff
    after, prefix_after = [], False
    for lo, hi, ok in info:
        prefix_after = prefix_after or (ok and lo > cutoff)
        after.append(prefix_after)
    later_before = False  # a later file is known to start at/before the cutoff
    for i in range(len(seq) - 1, -1, -1):
        lo, hi, ok = info[i]
        if early[i]:
            decision, reason = "archive", "uploaded before cutoff"
        elif later_before:
            decision, reason = "archive", "ends before cutoff"
        elif i == len(seq) - 1:
            decision, reason = "keep", "newest file"
        elif after[i]:
            decision, reason = "keep", "after cutoff"
        elif lo is not None and lo <= cutoff < hi:
            decision, reason = "keep", "near cutoff (approximate timestamp)"
        elif lo is not None and hi <= cutoff:
            decision, reason = "keep", "contains the cutoff"
        else:
            decision, reason = "keep", "undetermined"
        for r in groups[seq[i]]:
            r["decision"], r["reason"] = decision, reason
        later_before = later_before or early[i] or (ok and hi <= cutoff)


# ---------------------------------------------------------------- S3 helpers

class S3:
    """boto3 clients per bucket region (CopyObject must go to the destination region)."""

    def __init__(self, workers: int):
        import boto3
        from botocore.config import Config
        self.boto3, self.workers = boto3, workers
        self.config = Config(max_pool_connections=workers + 4, retries={"max_attempts": 10, "mode": "standard"})
        self.default = boto3.client("s3", config=self.config)
        self.clients, self.regions = {}, {}

    def region(self, bucket: str) -> str:
        if bucket not in self.regions:
            loc = self.default.get_bucket_location(Bucket=bucket).get("LocationConstraint")
            self.regions[bucket] = loc or "us-east-1"
        return self.regions[bucket]

    def client(self, bucket: str):
        reg = self.region(bucket)
        if reg not in self.clients:
            self.clients[reg] = self.boto3.client("s3", region_name=reg, config=self.config)
        return self.clients[reg]


def is_missing(e: Exception) -> bool:
    code = getattr(e, "response", {}).get("Error", {}).get("Code")
    return code in ("404", "NoSuchKey", "NotFound")


def run_threads(fn, items: list, workers: int, label: str) -> list:
    out, step = [], max(len(items) // 10, 500)
    with ThreadPoolExecutor(workers) as pool:
        for i, res in enumerate(pool.map(fn, items), start=1):
            out.append(res)
            if i % step == 0 or i == len(items):
                print(f"  {label}: {i}/{len(items)}", flush=True)
    return out


# ---------------------------------------------------------------- manifest

def new_row(key: str, size: int, etag: str, last_modified: str | None, storage_class: str = "STANDARD") -> dict:
    """metadata None = not read (HEAD skipped: decided by upload time, or not a log file)."""
    row = {"key": key, "size": size, "etag": etag, "last_modified": last_modified, "kind": "other", "device": None,
           "session": None, "split": None, "format": None, "metadata": None, "storage_class": storage_class,
           "sse": None, "t": None, "time_source": None, "flags": [], "decision": "keep", "reason": "",
           "delete_allowed": False, "status": "planned", "error": ""}
    row.update(parse_key(key))
    return row


def save_manifest(m: dict, path: Path) -> None:
    tmp = path.with_suffix(".tmp")
    tmp.write_text(json.dumps(m, indent=1))
    tmp.replace(path)
    with path.with_suffix(".csv").open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_FIELDS, extrasaction="ignore")
        w.writeheader()
        for r in m["objects"]:
            w.writerow(dict(r, logging_start_utc=r["t"] or "", flags=" ".join(r["flags"])))


def load_manifest(path: Path) -> dict:
    m = json.loads(path.read_text())
    if m.get("schema") != SCHEMA:
        sys.exit(f"ERROR: {path} is not a {SCHEMA} manifest")
    return m


# ---------------------------------------------------------------- plan

def cmd_plan(a) -> int:
    if not a.before and not a.all:
        sys.exit("ERROR: Pass --before <UTC date/time> or --all")
    cutoff = parse_cutoff(a.before) if a.before else None
    devices = {d.upper() for d in a.devices or []}
    excluded = {d.upper() for d in a.exclude_devices or []}
    s3, warnings = None, []

    # 1. objects
    if a.inventory:  # a canedge-s3 inventory folder (one JSON per device) or a single JSON with "objects"
        p = Path(a.inventory)
        inv = {"objects": {}}
        for f in sorted(p.glob("*.json")) if p.is_dir() else [p]:
            part = json.loads(f.read_text())
            inv["objects"].update(part["objects"])
            inv.setdefault("bucket", part.get("bucket"))
        src = a.src or inv["bucket"]
        rows = []
        for key, o in inv["objects"].items():
            row = new_row(key, o["size"], o.get("etag", ""), o.get("last_modified"))
            row["metadata"] = {k.lower(): v for k, v in (o.get("metadata") or {}).items()}
            rows.append(row)
    else:
        if not a.src or not a.dst:
            sys.exit("ERROR: Pass --src and --dst")
        src, s3 = a.src, S3(a.workers)
        rows = []
        prefixes = [f"{d}/" for d in sorted(devices)] if devices and not a.include_other else [""]
        for prefix in prefixes:
            pages = s3.client(src).get_paginator("list_objects_v2").paginate(Bucket=src, Prefix=prefix)
            for page in pages:
                for o in page.get("Contents", []):
                    rows.append(new_row(o["Key"], o["Size"], o.get("ETag", "").strip('"'), iso(o["LastModified"]),
                                        o.get("StorageClass", "STANDARD")))
        print(f"Listed {len(rows)} objects in s3://{src}/")
    dst = a.dst or "(not set)"
    rows = [r for r in rows if not (r["device"] and (r["device"] in excluded or (devices and r["device"] not in devices)))]
    logs = [r for r in rows if r["kind"] == "log"]
    others = [r for r in rows if r["kind"] == "other"]

    # 2. uploaded before the cutoff = logged before it (S3's clock, not the device's): No HEAD needed
    if cutoff:
        for r in logs:
            if r["last_modified"] and r["last_modified"] < iso(cutoff):
                r["time_source"], r["metadata"] = "upload", None
    to_head = [r for r in logs if r["time_source"] != "upload"]

    # 3. metadata (HEAD) of the other log files
    if s3 and to_head:
        cli = s3.client(src)
        print(f"HEAD {len(to_head)} of {len(logs)} log files (the others were uploaded before the cutoff)")

        def head(r):
            try:
                h = cli.head_object(Bucket=src, Key=r["key"])
                r["metadata"] = {k.lower(): v for k, v in h.get("Metadata", {}).items()}
                r["storage_class"] = h.get("StorageClass", "STANDARD")
                r["sse"] = h.get("ServerSideEncryption")
            except Exception as e:
                r["error"] = f"head: {e}"
        run_threads(head, to_head, a.workers, "HEAD")
    if s3 and len(to_head) < len(logs):  # files not HEADed: The bucket's default encryption applies
        try:
            rules = s3.client(src).get_bucket_encryption(Bucket=src)["ServerSideEncryptionConfiguration"]["Rules"]
            sse = rules[0]["ApplyServerSideEncryptionByDefault"]["SSEAlgorithm"]
        except Exception:
            sse = None
        for r in logs:
            if r["time_source"] == "upload":
                r["sse"] = sse

    # 4. logging time (Timestamp metadata only, log files are never read)
    for r in to_head:
        t = parse_timestamp((r["metadata"] or {}).get("timestamp"))
        if t:
            r["t"], r["time_source"] = iso(t), "metadata"
            if timestamp_quality(r["metadata"]) == "approx":
                r["flags"].append("approx-timestamp")
            if t < MIN_PLAUSIBLE or t > utcnow() + dt.timedelta(days=1):
                r["flags"].append("implausible")
        else:
            r["flags"].append("no-timestamp")

    # 5. decisions
    by_device = defaultdict(list)
    for r in logs:
        by_device[r["device"]].append(r)
    for dev, rs in by_device.items():
        if cutoff:
            decide_device(rs, cutoff)
        else:
            for r in rs:
                r["decision"], r["reason"] = "archive", "all"
    for r in logs:
        r["delete_allowed"] = True
        if r["error"].startswith("head"):
            r["decision"], r["reason"] = "keep", "metadata not readable"
        elif r["decision"] == "archive" and r["storage_class"] in NOT_COPYABLE:
            r["decision"], r["reason"] = "keep", f"source in {r['storage_class']} (restore first)"
        elif r["decision"] == "archive" and r["size"] > MAX_COPY:
            r["decision"], r["reason"] = "keep", "larger than 5 GB (copy manually)"
    for r in others:
        r["decision"], r["reason"] = ("archive", "other object (copy only)") if a.include_other else ("keep", "not a log file")

    # 6. warnings
    sel = [r for r in rows if r["decision"] == "archive"]
    sel_gb = sum(r["size"] for r in sel) / 1e9
    if s3:
        try:
            if s3.client(src).get_bucket_versioning(Bucket=src).get("Status") == "Enabled":
                warnings.append(f"Versioning is ON in {src}: Deleted files stay billed as noncurrent versions "
                                "(add a lifecycle rule that expires noncurrent versions)")
        except Exception:
            pass
        try:
            if s3.region(src) != s3.region(a.dst):
                warnings.append(f"Cross-region copy ({s3.region(src)} -> {s3.region(a.dst)}): ~$0.02/GB transfer "
                                f"= ~${sel_gb * 0.02:.2f}")
        except Exception as e:
            sys.exit(f"ERROR: Destination bucket {a.dst} not accessible ({e}). Create it first")
    kms = sum(1 for r in sel if r["sse"] == "aws:kms")
    if kms:
        warnings.append(f"{kms} files use SSE-KMS: Your login needs kms:Decrypt on the source key")
    small = sum(1 for r in sel if r["size"] < 131072)
    if small and a.storage_class in ("STANDARD_IA", "GLACIER_IR"):
        warnings.append(f"{small} selected files are < 128 KB: Billed as 128 KB each in {a.storage_class}")
    if a.storage_class != "STANDARD":
        days = {"STANDARD_IA": 30, "GLACIER_IR": 90, "GLACIER": 90, "DEEP_ARCHIVE": 180}[a.storage_class]
        warnings.append(f"{a.storage_class}: Minimum storage duration {days} days (earlier deletes are billed "
                        "for the full period); reading files later costs retrieval fees")

    # 7. manifest + summary
    m = {"schema": SCHEMA, "created_utc": iso(utcnow()), "src": src, "dst": a.dst, "cutoff_utc": iso(cutoff),
         "all": bool(a.all), "storage_class": a.storage_class,
         "objects": sorted(rows, key=lambda r: r["key"])}
    path = Path(a.out or f"migrate-{utcnow():%Y%m%dT%H%M%SZ}.json")
    save_manifest(m, path)
    print_plan_summary(m, by_device, others, warnings)
    storage, put = PRICES[a.storage_class]
    req = (len(rows) / 1000 * LIST_PRICE + len(to_head) / 1000 * HEAD_PRICE +
           len(sel) / 1000 * (put + 2 * HEAD_PRICE))
    print(f"\nEstimated one-off request cost (plan + copy + verify): ~${req:.2f}. Archive storage: ~${sel_gb * storage:.2f}"
          f"/month in {a.storage_class} (vs ~${sel_gb * PRICES['STANDARD'][0]:.2f}/month in STANDARD). "
          "Approx. us-east-1 list prices")
    print(f"Manifest: {path} (+ {path.with_suffix('.csv').name}). Destination: s3://{dst}/ (same keys). Next: copy --limit 5")
    return 0


def print_plan_summary(m: dict, by_device: dict, others: list, warnings: list) -> None:
    mode = f"files ending before {m['cutoff_utc']}" if m["cutoff_utc"] else "all files"
    print(f"\nPlan: {mode}, s3://{m['src']} -> s3://{m['dst']} ({m['storage_class']})")
    head = f"{'Device':<10}{'Files':>7}{'Archive':>9}{'GB':>9}{'Keep':>7}{'Undet.':>8}{'NoTS':>6}{'Clock':>7}  Archived logging range"
    lines = []
    for dev in sorted(by_device):
        rs = by_device[dev]
        sel = [r for r in rs if r["decision"] == "archive"]
        ts = sorted(r["t"] for r in sel if r["t"])
        clock = sum(1 for r in rs if {"implausible", "out-of-order"} & set(r["flags"]))
        undet = sum(1 for r in rs if r["reason"] == "undetermined")
        nots = sum(1 for r in rs if "no-timestamp" in r["flags"])
        early = sum(1 for r in sel if r["time_source"] == "upload")
        rng = (f"{ts[0][:16]} -> {ts[-1][:16]}" if ts else "-") + (f" (+{early} uploaded before the cutoff)" if early else "")
        lines.append((bool(undet or clock), f"{dev:<10}{len(rs):>7}{len(sel):>9}{sum(r['size'] for r in sel) / 1e9:>9.2f}"
                      f"{len(rs) - len(sel):>7}{undet:>8}{nots:>6}{clock:>7}  {rng}"))
    print(head)
    shown = lines if len(lines) <= 30 else [l for l in lines if l[0]][:30]
    for _, text in shown:
        print(text)
    if len(lines) > 30:
        print(f"  ({len(lines)} devices: showing those with undetermined/clock issues only; all rows in the CSV)")
    all_logs = [r for rs in by_device.values() for r in rs]
    sel = [r for r in all_logs if r["decision"] == "archive"]
    sel_other = [r for r in others if r["decision"] == "archive"]
    print(f"Total: {len(sel)} of {len(all_logs)} log files selected, {sum(r['size'] for r in sel) / 1e9:.2f} GB"
          + (f"; {len(sel_other)} other objects (copy only)" if sel_other else ""))
    reasons = defaultdict(int)
    for r in all_logs:
        if r["decision"] == "keep":
            reasons[r["reason"]] += 1
    if reasons:
        print("Kept: " + ", ".join(f"{n} {k}" for k, n in sorted(reasons.items(), key=lambda x: -x[1])))
    flags = defaultdict(int)
    for r in all_logs:
        for f in r["flags"]:
            flags[f] += 1
    if flags:
        print("Flags: " + ", ".join(f"{n} {k}" for k, n in sorted(flags.items())))
    for w in warnings:
        print(f"WARNING: {w}")


# ---------------------------------------------------------------- copy / verify / delete

def cmd_copy(a) -> int:
    path = Path(a.manifest)
    m = load_manifest(path)
    todo = [r for r in m["objects"] if r["decision"] == "archive" and r["status"] in ("planned", "failed")]
    if a.limit:
        todo = todo[:a.limit]
    s3 = S3(a.workers)
    cli, src, dst = s3.client(m["dst"]), m["src"], m["dst"]

    def copy(r):
        try:
            try:
                h = cli.head_object(Bucket=dst, Key=r["key"])
                if h["ContentLength"] == r["size"]:
                    r["status"], r["error"] = "copied", "already in destination"
                else:
                    r["status"], r["error"] = "failed", "destination exists with a different size"
                return
            except Exception as e:
                if not is_missing(e):
                    raise
            args = {"Bucket": dst, "Key": r["key"], "CopySource": {"Bucket": src, "Key": r["key"]},
                    "StorageClass": m["storage_class"]}
            cli.copy_object(**args)
            r["status"], r["error"] = "copied", ""
        except Exception as e:
            r["status"], r["error"] = "failed", f"copy: {e}"
    print(f"Copying {len(todo)} objects s3://{src} -> s3://{dst} ({m['storage_class']})")
    try:
        run_threads(copy, todo, a.workers, "copy")
    finally:
        save_manifest(m, path)
    return report(m, path)


def cmd_verify(a) -> int:
    path = Path(a.manifest)
    m = load_manifest(path)
    todo = [r for r in m["objects"] if r["status"] == "copied"]
    s3 = S3(a.workers)
    cli, dst = s3.client(m["dst"]), m["dst"]

    def verify(r):
        try:
            h = cli.head_object(Bucket=dst, Key=r["key"])
            want_meta = r["metadata"]  # None = not read in the plan (CopyObject keeps the metadata as is)
            got_meta = {k.lower(): v for k, v in h.get("Metadata", {}).items()}
            etag = h.get("ETag", "").strip('"')
            problems = []
            if h["ContentLength"] != r["size"]:
                problems.append(f"size {h['ContentLength']} != {r['size']}")
            if r["kind"] == "log" and want_meta is not None and got_meta != want_meta:
                problems.append(f"metadata {got_meta} != {want_meta}")
            if h.get("StorageClass", "STANDARD") != m["storage_class"] and r["error"] != "already in destination":
                problems.append(f"storage class {h.get('StorageClass', 'STANDARD')}")
            if r["etag"] and "-" not in r["etag"] and "-" not in etag and r["sse"] != "aws:kms" and etag != r["etag"]:
                problems.append("ETag differs")
            r["status"], r["error"] = ("failed", "verify: " + "; ".join(problems)) if problems else ("verified", "")
        except Exception as e:
            r["status"], r["error"] = "failed", f"verify: {e}"
    print(f"Verifying {len(todo)} objects in s3://{dst}")
    try:
        run_threads(verify, todo, a.workers, "verify")
    finally:
        save_manifest(m, path)
    return report(m, path)


def cmd_delete(a) -> int:
    path = Path(a.manifest)
    m = load_manifest(path)
    if a.confirm_bucket != m["src"]:
        sys.exit(f"ERROR: --confirm-bucket must be the source bucket name ({m['src']})")
    todo = [r for r in m["objects"] if r["status"] == "verified" and r["delete_allowed"]]
    if a.limit:
        todo = todo[:a.limit]
    s3 = S3(a.workers)
    cli, src = s3.client(m["src"]), m["src"]

    def unchanged(r):  # never delete a source object that changed after the plan
        try:
            h = cli.head_object(Bucket=src, Key=r["key"])
            same = h["ContentLength"] == r["size"] and (not r["etag"] or h.get("ETag", "").strip('"') == r["etag"])
            if not same:
                r["error"] = "source changed since plan (not deleted)"
            return same
        except Exception as e:
            r["error"] = f"source check: {e}"
            return False
    print(f"Deleting up to {len(todo)} verified log files from s3://{src}")
    ok = [r for r, same in zip(todo, run_threads(unchanged, todo, a.workers, "check")) if same]
    try:
        for i in range(0, len(ok), 1000):
            batch = ok[i:i + 1000]
            res = cli.delete_objects(Bucket=src, Delete={"Objects": [{"Key": r["key"]} for r in batch], "Quiet": False})
            done = {d["Key"] for d in res.get("Deleted", [])}
            errs = {e["Key"]: e.get("Message", e.get("Code")) for e in res.get("Errors", [])}
            for r in batch:
                if r["key"] in done:
                    r["status"], r["error"] = "deleted", ""
                else:
                    r["error"] = f"delete: {errs.get(r['key'], 'not confirmed')}"
            print(f"  delete: {min(i + 1000, len(ok))}/{len(ok)}", flush=True)
    finally:
        save_manifest(m, path)
    try:
        if cli.get_bucket_versioning(Bucket=src).get("Status") == "Enabled":
            print(f"WARNING: Versioning is ON in {src}: Deleted files remain as noncurrent versions (still billed)")
    except Exception:
        pass
    return report(m, path)


def report(m: dict, path: Path) -> int:
    sel = [r for r in m["objects"] if r["decision"] == "archive"]
    status = defaultdict(int)
    for r in sel:
        status[r["status"]] += 1
    errors = [r for r in sel if r["error"] and r["error"] != "already in destination"]
    for r in errors[:20]:
        print(f"  {r['key']}: {r['error']}")
    if len(errors) > 20:
        print(f"  ... {len(errors) - 20} more (see the CSV)")
    print(f"Selected objects by status: {dict(status)}. Manifest updated: {path}")
    return 1 if errors else 0


# ---------------------------------------------------------------- CLI

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="Select files + write the manifest (no S3 writes)")
    p.add_argument("--src", help="Source bucket")
    p.add_argument("--dst", help="Destination (archive) bucket")
    p.add_argument("--before", help="Cutoff, UTC (e.g. 2026-01-01 or 2026-01-01T12:00)")
    p.add_argument("--all", action="store_true", help="Select all files (full migration)")
    p.add_argument("--devices", nargs="+", help="Only these device IDs")
    p.add_argument("--exclude-devices", nargs="+", help="Skip these device IDs")
    p.add_argument("--include-other", action="store_true",
                   help="Also copy non-log objects (device.json, configs, DBCs, ...); these are never deleted")
    p.add_argument("--storage-class", default="STANDARD", choices=STORAGE_CLASSES)
    p.add_argument("--inventory", help="A canedge-s3 inventory folder (data/s3-inventory/<bucket>) or JSON instead of "
                   "listing S3 (testing)")
    p.add_argument("--out", help="Manifest path (default migrate-<utc>.json)")
    for name, fn, hlp in (("copy", cmd_copy, "Copy the selected files"), ("verify", cmd_verify, "Verify the copies"),
                          ("delete-source", cmd_delete, "Delete verified log files from the source")):
        q = sub.add_parser(name, help=hlp)
        q.add_argument("--manifest", required=True)
        if name != "verify":
            q.add_argument("--limit", type=int, help="At most N objects (test runs)")
        if name == "delete-source":
            q.add_argument("--confirm-bucket", required=True, help="Type the source bucket name to confirm")
        q.add_argument("--workers", type=int, default=32)
        q.set_defaults(fn=fn)
    p.add_argument("--workers", type=int, default=32)
    p.set_defaults(fn=cmd_plan)
    a = ap.parse_args()
    return a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
