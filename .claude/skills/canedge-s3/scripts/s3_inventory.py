"""Inventory a CANedge S3 bucket (read-only): List objects, read their metadata where needed, cache it, summarize.

Listing is cheap (1 request per 1,000 keys), but the logging time ('Timestamp' metadata) costs one head_object
request per file. So the scripts only HEAD the files a question needs, and cache everything per device:

  (no --prefix / --all)   Device folders only (1 request per 1,000 devices): The first look at a bucket
  --prefix 12345678/      List the prefix + HEAD the first and last log file per device (logging range)
  --all                   The same for the whole bucket
  --metadata              Also HEAD every other log file in scope: Files without Timestamp, approximate
                          Timestamps, all FW versions. Stops above 50,000 HEADs unless --yes

The cache lives in data/s3-inventory/<bucket>/ (<device>.json per device, _bucket.json for root objects). Logging
time comes ONLY from the 'Timestamp' metadata (set by the CANedge on upload); log files are never read (they may
be compressed/encrypted). Files without it (e.g. manual uploads) can still be ordered by session/split. device.json
files are read for a whitelist (meta, FW, config name + CRC32, SD space + health): A snapshot as of the device's last
heartbeat upload, not the state of older log files (those carry their own in the MF4 header). The config file named
in device.json is read for its CRC32 (config sync). Nothing is ever written to the bucket.

Usage:
  python s3_inventory.py [--bucket B] [--prefix P | --all] [--metadata] [--yes] [--refresh] [--offline] [--json]
  python s3_inventory.py --prefix P | --all --upload-start UTC [--upload-end UTC]   (uploaded in a period, listing only)

Credentials + S3 input bucket come from the data lake profile credentials/<name>.env (canedge-workspace env.py,
written by setup_env.py; --env NAME selects one, default: the only profile).

The other scripts import update_inventory / head_objects / load_inventory / logging_time from this file.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import re
import sys
import zlib
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
from env import GET_USD, aws_client, lake, load_env, require_s3, use_env  # noqa: E402,F401 (re-exported for the other s3 scripts)
from mf4_files import DEVICE_RE, LOG_EXTS  # noqa: E402

INVENTORY_DIR = Path("data/s3-inventory")
SCHEMA = "canedge-s3-inventory/3"
ROOT = "_bucket"  # cache file for objects outside the device folders
WORKERS = 16
HEAD_GUARD = 50_000  # more HEAD requests than this need the user's OK (--yes)
HEADS_PER_S = 200  # rough rate with WORKERS threads
UPLOAD_MARGIN = dt.timedelta(days=1)  # a file can start up to this long after its upload (local-time FW, clock drift)
DEVICE_JSON_FIELDS = ("log_meta", "fw_ver", "cfg_name", "cfg_crc32", "space_used_mb", "sd_used_lifespan")


# ---------------------------------------------------------------- env + client

def s3_client():
    """boto3 S3 client with the profile's keys. Exits with a clear message if the profile is missing/incomplete."""
    require_s3()
    return aws_client("s3", max_pool_connections=WORKERS + 4, retries={"max_attempts": 5, "mode": "standard"})


def default_bucket(bucket: str | None) -> str:
    """--bucket, else the profile's S3 input bucket."""
    b = bucket or lake()["input_bucket"]
    if not b:
        require_s3()
    return b


# ---------------------------------------------------------------- parsing helpers

def parse_key(key: str) -> dict:
    """<device>/<session>/<split>[-<suffix>].<ext> -> kind 'log' with ids, else kind 'other' (configs, DBCs, json;
    device = the device folder, None in the bucket root). The suffix (added on upload) is ignored."""
    parts = key.split("/")
    ext = os.path.splitext(parts[-1])[1].lower()
    dev = parts[0].upper() if len(parts) > 1 and DEVICE_RE.fullmatch(parts[0]) else None
    if ext in LOG_EXTS and len(parts) >= 3 and dev and re.fullmatch(r"\d{8}", parts[-2]):
        m = re.match(r"^(\d{8})(?:-[0-9A-Fa-f]{8})?\.", parts[-1])
        if m:
            return {"kind": "log", "device": dev, "session": int(parts[-2]), "split": int(m.group(1)),
                    "format": ext.lstrip(".").upper()}
    return {"kind": "other", "device": dev}


def parse_timestamp(s: str | None) -> dt.datetime | None:
    """CANedge 'Timestamp' metadata (e.g. 20260616T013000Z) or ISO 8601 -> aware UTC datetime. Values with or without
    Z are read as UTC; before FW 01.09.01 they may be local time (see timestamp_quality)."""
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


def logging_time(obj: dict) -> tuple[dt.datetime | None, str]:
    """Logging time (= file start) of an inventory object: ('Timestamp' metadata, 'metadata') or (None, 'none').
    Never uses LastModified (upload time) or the key suffix."""
    t = parse_timestamp((obj.get("metadata") or {}).get("timestamp"))
    return (t, "metadata") if t else (None, "none")


def timestamp_quality(obj: dict) -> str | None:
    """'exact' = file-created UTC (FW 01.09.01+, or 'Net: sd_manual' = set by an SD upload script from the first
    frame), 'approx' = older/unknown firmware: May be local time (+/-14 h) and before 01.08.05 the file END
    (modified time). None = no Timestamp. Never used to correct a value, only to say how far to trust it."""
    md = obj.get("metadata") or {}
    if not parse_timestamp(md.get("timestamp")):
        return None
    if md.get("net") == "sd_manual":
        return "exact"
    try:
        return "exact" if tuple(int(x) for x in md.get("fw", "").split(".")) >= (1, 9, 1) else "approx"
    except ValueError:
        return "approx"


def firmware(obj: dict) -> str | None:
    return (obj.get("metadata") or {}).get("fw")


def order_key(obj: dict) -> tuple:
    """Recording order of a device's log files: session, then split (session +1 per power-on or after 256 splits)."""
    return obj.get("session") or 0, obj.get("split") or 0


def iso(t: dt.datetime | None) -> str | None:
    return t.astimezone(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if t else None


def uploaded_since(obj: dict, t: dt.datetime) -> bool:
    """False if the file was logged before t for sure: A file is uploaded after it is logged, so an upload before
    t - UPLOAD_MARGIN means a logging start before t. Lets time questions skip the HEADs of older files."""
    return obj["last_modified"] >= iso(t - UPLOAD_MARGIN)


def edge_keys(inv: dict, prefix: str = "") -> list[str]:
    """The first and last log file (recording order) of each device under prefix: Its logging range in 2 HEADs."""
    first: dict[str, tuple] = {}
    last: dict[str, tuple] = {}
    for k, o in inv["objects"].items():
        if o["kind"] == "log" and k.startswith(prefix):
            ok, dev = order_key(o), o["device"]
            if dev not in first or ok < first[dev][0]:
                first[dev] = (ok, k)
            if dev not in last or ok > last[dev][0]:
                last[dev] = (ok, k)
    return [k for _, k in first.values()] + [k for _, k in last.values()]


# ---------------------------------------------------------------- cache (one file per device)

def inventory_dir(bucket: str) -> Path:
    return INVENTORY_DIR / bucket


def load_inventory(bucket: str, devices: list[str] | None = None) -> dict:
    """The cache as one dict ('objects', 'device_json', 'synced' per cache file). devices = only these device files
    (+ the root objects), None = all. Objects without a 'metadata' key have not been HEADed yet."""
    d = inventory_dir(bucket)
    files = sorted(d.glob("*.json")) if devices is None else [d / f"{x}.json" for x in (ROOT, *devices)]
    inv = {"schema": SCHEMA, "bucket": bucket, "endpoint": None, "objects": {}, "device_json": {}, "synced": {}}
    for p in files:
        if not p.exists():
            continue
        s = json.loads(p.read_text(encoding="utf-8"))
        if s.get("schema") != SCHEMA:
            continue
        inv["objects"].update(s["objects"])
        inv["synced"][p.stem] = s.get("synced_utc")
        if s.get("device_json"):
            inv["device_json"][p.stem] = s["device_json"]
        inv["endpoint"] = inv["endpoint"] or s.get("endpoint")
    inv["updated_utc"] = min((v for v in inv["synced"].values() if v), default=None)
    return inv


def save_inventory(inv: dict, shards: set[str]) -> None:
    """Write the given cache files (device IDs and/or ROOT) from the in-memory inventory."""
    d = inventory_dir(inv["bucket"])
    d.mkdir(parents=True, exist_ok=True)
    by: dict[str, dict] = {s: {} for s in shards}
    for k, o in inv["objects"].items():
        s = o.get("device") or ROOT
        if s in by:
            by[s][k] = o
    for s, objs in by.items():
        p = d / f"{s}.json"
        if not objs and s != ROOT:
            p.unlink(missing_ok=True)  # the device folder is gone from the bucket
            continue
        out = {"schema": SCHEMA, "bucket": inv["bucket"], "endpoint": inv.get("endpoint"),
               "synced_utc": inv["synced"].get(s), "objects": objs}
        if s in inv["device_json"]:
            out["device_json"] = inv["device_json"][s]
        tmp = p.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(out, separators=(",", ":")), encoding="utf-8")
        tmp.replace(p)


# ---------------------------------------------------------------- S3 calls

def list_devices(bucket: str) -> tuple[list[str], list[str]]:
    """Device folders + root object keys via a delimiter listing (1 request per 1,000 entries, nothing per file)."""
    s3 = s3_client()
    devs, root = [], []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Delimiter="/"):
        devs += [p["Prefix"].rstrip("/") for p in page.get("CommonPrefixes", [])
                 if DEVICE_RE.fullmatch(p["Prefix"].rstrip("/"))]
        root += [o["Key"] for o in page.get("Contents", [])]
    return devs, root


def update_inventory(bucket: str, prefix: str = "", refresh: bool = False, log=print) -> dict:
    """Sync the cached listing under prefix with the bucket (ListObjectsV2 + device.json, no HEADs: see head_objects).
    New or re-written keys get a fresh record without metadata. refresh = forget the cached metadata under prefix.
    Returns the inventory of the devices the prefix touches."""
    top = prefix.split("/")[0].upper()
    known = [p.stem for p in inventory_dir(bucket).glob("*.json") if p.stem != ROOT]
    devices = [top] if "/" in prefix else [x for x in known if x.startswith(top)]
    inv = load_inventory(bucket, devices)
    s3 = s3_client()
    objs = inv["objects"]
    listed: dict[str, dict] = {}
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix):
        for o in page.get("Contents", []):
            listed[o["Key"]] = {"size": o["Size"], "etag": o.get("ETag", "").strip('"'),
                                "last_modified": iso(o["LastModified"])}
    removed = [k for k in objs if k.startswith(prefix) and k not in listed]
    for k in removed:
        del objs[k]
    new = 0
    for k, o in listed.items():
        c = objs.get(k)
        if refresh or not c or (c["etag"], c["last_modified"]) != (o["etag"], o["last_modified"]):
            objs[k] = {**o, **parse_key(k)}
            new += 1
    log(f"Listed {len(listed):,} objects under s3://{bucket}/{prefix} - {new:,} new/changed, "
        f"{len(removed):,} removed from cache")
    # device.json: Only whitelisted fields (it also holds keys/ICCID which we do not store). A snapshot: as_of = its
    # LastModified (the device re-uploads it at every heartbeat)
    for k, o in objs.items():
        if o["kind"] == "other" and o.get("device") and k.endswith("/device.json") and k.startswith(prefix):
            cached = inv["device_json"].get(o["device"])
            if cached and cached.get("etag") == o["etag"] and not refresh:
                cached["as_of"] = o["last_modified"]
                continue
            try:
                d = json.loads(s3.get_object(Bucket=bucket, Key=k)["Body"].read())
                inv["device_json"][o["device"]] = {"etag": o["etag"], "as_of": o["last_modified"],
                                                   **{f: d.get(f) for f in DEVICE_JSON_FIELDS}}
            except Exception as e:  # noqa: BLE001
                inv["device_json"][o["device"]] = {"etag": o["etag"], "as_of": o["last_modified"],
                                                   "error": e.__class__.__name__}
    # Config sync (as CANcloud): CRC32 of the device's config file in S3 (<device>/<cfg_name>) vs the cfg_crc32 the
    # device reports. Re-read only when the config object changes
    for dev, dj in inv["device_json"].items():
        if not (f"{dev}/".startswith(prefix) or prefix.startswith(f"{dev}/")):
            continue
        cfg = objs.get(f"{dev}/{dj['cfg_name']}") if dj.get("cfg_name") else None
        if cfg is None:
            dj.pop("cfg_s3", None)
            continue
        if (dj.get("cfg_s3") or {}).get("etag") != cfg["etag"] or refresh:
            try:
                raw = s3.get_object(Bucket=bucket, Key=f"{dev}/{dj['cfg_name']}")["Body"].read()
                dj["cfg_s3"] = {"etag": cfg["etag"], "crc32": format(zlib.crc32(raw) & 0xFFFFFFFF, "08X"),
                                "last_modified": cfg["last_modified"]}
            except Exception as e:  # noqa: BLE001
                dj["cfg_s3"] = {"etag": cfg["etag"], "error": e.__class__.__name__}
    now = iso(dt.datetime.now(dt.timezone.utc))
    shards = set(devices) | {o["device"] for k, o in objs.items() if k.startswith(prefix) and o.get("device")}
    if "/" not in prefix:
        shards.add(ROOT)
    for s in shards:
        inv["synced"][s] = now
    inv["endpoint"] = os.environ.get("CANEDGE_S3_ENDPOINT") or None
    save_inventory(inv, shards)
    inv["updated_utc"] = min((v for v in inv["synced"].values() if v), default=None)
    return inv


class HeadGuard(Exception):
    """More HEAD requests needed than allowed without the user's OK."""


def head_estimate(n: int) -> str:
    usd = n * GET_USD
    return (f"{n:,} HEAD requests ({f'~${usd:.2f}' if usd >= 0.01 else '< $0.01'} on AWS S3, "
            f"~{max(1, round(n / HEADS_PER_S / 60))} min)")


def head_objects(bucket: str, inv: dict, keys, max_heads: int | None = None, log=print) -> int:
    """Read the metadata (head_object, threaded) of the given log files that have none cached yet and save the
    touched cache files. Raises HeadGuard if that takes more than max_heads requests. Returns the number of HEADs."""
    objs = inv["objects"]
    todo = [k for k in dict.fromkeys(keys) if objs[k]["kind"] == "log" and "metadata" not in objs[k]]
    if max_heads is not None and len(todo) > max_heads:
        raise HeadGuard(head_estimate(len(todo)))
    if not todo:
        return 0
    if len(todo) > 1000:
        log(f"Reading metadata: {head_estimate(len(todo))}")
    s3 = s3_client()

    def head(key: str) -> tuple[str, dict | None, str | None]:
        try:
            r = s3.head_object(Bucket=bucket, Key=key)
            return key, {k.lower(): v for k, v in (r.get("Metadata") or {}).items()}, None
        except Exception as e:  # noqa: BLE001 - keep going, the object stays without metadata
            return key, None, e.__class__.__name__

    errors = 0
    with ThreadPoolExecutor(WORKERS) as ex:
        for i, (k, md, err) in enumerate(ex.map(head, todo), 1):
            if err:
                objs[k]["error"] = f"head_object: {err}"
                errors += 1
            else:
                objs[k]["metadata"] = md
                objs[k].pop("error", None)
            if i % 2000 == 0:
                log(f"  head_object {i:,}/{len(todo):,}")
    if errors:
        log(f"WARNING: head_object failed for {errors} file(s) (e.g. AccessDenied: The policy lacks s3:GetObject)")
    save_inventory(inv, {objs[k].get("device") or ROOT for k in todo})
    return len(todo)


# ---------------------------------------------------------------- summary

def summarize(inv: dict, prefix: str = "") -> dict:
    devs: dict[str, dict] = {}
    others = []
    for k, o in sorted(inv["objects"].items()):
        if not k.startswith(prefix):
            continue
        if o["kind"] != "log":
            others.append({"key": k, "size": o["size"], "last_modified": o["last_modified"]})
            continue
        d = devs.setdefault(o["device"], {"files": 0, "bytes": 0, "sessions": set(), "first": None, "last": None,
                                          "unread": 0, "no_timestamp": 0, "approx": 0, "fw": set(),
                                          "last_upload": None, "keys": {}})
        d["files"] += 1
        d["bytes"] += o["size"]
        d["sessions"].add(o["session"])
        d["keys"].setdefault(order_key(o), []).append(k)
        d["last_upload"] = max(d["last_upload"] or "", o["last_modified"])
        if "metadata" not in o:
            d["unread"] += 1
            continue
        t, _ = logging_time(o)
        if t is None:
            d["no_timestamp"] += 1
        else:
            d["first"] = min(d["first"], t) if d["first"] else t
            d["last"] = max(d["last"], t) if d["last"] else t
            d["approx"] += timestamp_quality(o) == "approx"
        if firmware(o):
            d["fw"].add(firmware(o))
    out = {}
    for dev, d in sorted(devs.items()):
        order = sorted(d["keys"])
        dj = inv.get("device_json", {}).get(dev) or {}
        out[dev] = {"files": d["files"], "gb": round(d["bytes"] / 1e9, 4), "sessions": len(d["sessions"]),
                    "first_file": d["keys"][order[0]][0], "last_file": d["keys"][order[-1]][0],
                    "first_logging_utc": iso(d["first"]), "last_logging_utc": iso(d["last"]),
                    "last_upload_utc": d["last_upload"], "metadata_read": d["files"] - d["unread"],
                    "firmware": sorted(d["fw"]), "without_timestamp": d["no_timestamp"],
                    "approx_timestamps": d["approx"], "duplicate_uploads": sum(len(v) - 1 for v in d["keys"].values()),
                    "log_meta": dj.get("log_meta"), "log_meta_as_of": dj.get("as_of"),
                    "cache_synced_utc": inv["synced"].get(dev)}
    return {"bucket": inv["bucket"], "prefix": prefix, "updated_utc": inv.get("updated_utc"),
            "log_files": sum(d["files"] for d in out.values()),
            "gb": round(sum(d["gb"] for d in out.values()), 4), "devices": out, "other_objects": others}


def print_summary(s: dict) -> None:
    print(f"\ns3://{s['bucket']}/{s['prefix']} - {s['log_files']:,} log files, {s['gb']:.2f} GB, "
          f"{len(s['devices'])} device(s) (cache synced {s['updated_utc']})")
    if not s["devices"] and not s["other_objects"]:
        print("  The bucket/prefix is empty - no CANedge has uploaded data here yet")
    for dev, d in s["devices"].items():
        meta = f" '{d['log_meta']}' (device.json {(d['log_meta_as_of'] or '?')[:10]})" if d["log_meta"] else ""
        rng = (f"logging {(d['first_logging_utc'] or '?')[:16]} -> {(d['last_logging_utc'] or '?')[:16]} UTC"
               if d["first_logging_utc"] else "logging time unknown")
        warn = []
        if d["metadata_read"] < d["files"]:
            warn.append(f"metadata read for {d['metadata_read']:,} of {d['files']:,} files (the counts below and FW "
                        "cover those only; --metadata reads all)")
        if d["without_timestamp"]:
            warn.append(f"{d['without_timestamp']} without Timestamp metadata (no logging time: Select them by "
                        "session/split, e.g. fetch_logs.py --first/--last)")
        if d["approx_timestamps"]:
            warn.append(f"{d['approx_timestamps']} with approximate Timestamp (FW < 01.09.01: may be local time "
                        "and/or the file end - logging range approximate)")
        if d["duplicate_uploads"]:
            warn.append(f"{d['duplicate_uploads']} duplicate upload(s) of the same session/split")
        print(f"  {dev}{meta}: {d['files']:,} files, {d['gb']:.2f} GB, {d['sessions']} sessions "
              f"(first {d['first_file'].split('/', 1)[1]}, last {d['last_file'].split('/', 1)[1]}), {rng}, "
              f"FW {', '.join(d['firmware']) or '?'}, last upload {(d['last_upload_utc'] or '?')[:16]} UTC"
              + (f" | {'; '.join(warn)}" if warn else ""))
    if s["other_objects"]:
        print(f"Non-log objects ({len(s['other_objects'])}):")
        for o in s["other_objects"][:25]:
            print(f"  {o['key']} ({o['size']:,} B)")
        if len(s["other_objects"]) > 25:
            print(f"  ... {len(s['other_objects']) - 25} more (see --json)")


def print_devices(bucket: str, devs: list[str], root: list[str], as_json: bool) -> None:
    cached = sorted(p.stem for p in inventory_dir(bucket).glob("*.json") if p.stem != ROOT)
    if as_json:
        print(json.dumps({"bucket": bucket, "devices": devs, "cached": [d for d in devs if d in cached],
                          "root_objects": root}, indent=1))
        return
    print(f"s3://{bucket}/ - {len(devs)} device folder(s), {len(root)} root object(s) (no per-file requests made)")
    for i in range(0, len(devs), 8):
        print("  " + "  ".join(d + ("*" if d in cached else " ") for d in devs[i:i + 8]))
    if cached:
        print("  * = in the local cache (--offline --prefix <device>/ summarizes it without S3 calls)")
    print("Next: --prefix <device>/ (one device) or --all (whole bucket) lists the files and reads the first/last file "
          "per device; add --metadata to read every file's metadata")


def upload_summary(inv: dict, prefix: str, start: dt.datetime | None, end: dt.datetime | None) -> dict:
    """Log files UPLOADED in [start, end) (S3 LastModified), per device and per UTC day: An S3 troubleshooting view
    ("what arrived when"), not logging time. Listing data only: No HEAD requests."""
    s, e = iso(start) or "", iso(end) or "9999"
    devs: dict[str, dict] = {}
    days: dict[str, list] = defaultdict(lambda: [0, 0])
    for k, o in sorted(inv["objects"].items()):
        if o["kind"] != "log" or not k.startswith(prefix) or not s <= o["last_modified"] < e:
            continue
        d = devs.setdefault(o["device"], {"files": 0, "bytes": 0, "sessions": set(), "first_upload": None,
                                          "last_upload": None, "keys": []})
        d["files"] += 1
        d["bytes"] += o["size"]
        d["sessions"].add(o["session"])
        d["first_upload"] = min(d["first_upload"] or o["last_modified"], o["last_modified"])
        d["last_upload"] = max(d["last_upload"] or o["last_modified"], o["last_modified"])
        d["keys"].append((order_key(o), k))
        day = days[o["last_modified"][:10]]
        day[0] += 1
        day[1] += o["size"]
    out = {dev: {"files": d["files"], "gb": round(d["bytes"] / 1e9, 4), "sessions": len(d["sessions"]),
                 "first_upload_utc": d["first_upload"], "last_upload_utc": d["last_upload"],
                 "first_file": min(d["keys"])[1], "last_file": max(d["keys"])[1]} for dev, d in sorted(devs.items())}
    return {"bucket": inv["bucket"], "prefix": prefix, "uploaded_from_utc": iso(start), "uploaded_to_utc": iso(end),
            "updated_utc": inv.get("updated_utc"), "log_files": sum(d["files"] for d in out.values()),
            "gb": round(sum(d["gb"] for d in out.values()), 4), "devices": out,
            "per_day_utc": {k: {"files": v[0], "gb": round(v[1] / 1e9, 4)} for k, v in sorted(days.items())}}


def print_upload_summary(s: dict) -> None:
    rng = f"{(s['uploaded_from_utc'] or 'start')[:16]} -> {(s['uploaded_to_utc'] or 'now')[:16]} UTC"
    print(f"\ns3://{s['bucket']}/{s['prefix']} - UPLOADED {rng} (S3 LastModified, not logging time): "
          f"{s['log_files']:,} log files, {s['gb']:.3f} GB, {len(s['devices'])} device(s)")
    for dev, d in s["devices"].items():
        print(f"  {dev}: {d['files']:,} files, {d['gb']:.3f} GB, {d['sessions']} sessions, uploads "
              f"{d['first_upload_utc'][:16]} -> {d['last_upload_utc'][:16]} UTC "
              f"(files {d['first_file'].split('/', 1)[1]} ... {d['last_file'].split('/', 1)[1]})")
    if len(s["per_day_utc"]) > 1:
        print(f"Per day: {len(s['per_day_utc'])} upload days (see --json for the list)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", help="Default: The profile's S3 input bucket")
    ap.add_argument("--env", metavar="NAME", help="Data lake profile credentials/NAME.env (default: the only profile)")
    ap.add_argument("--prefix", help="Only this prefix, e.g. 12345678/")
    ap.add_argument("--all", action="store_true", help="The whole bucket")
    ap.add_argument("--metadata", action="store_true", help="HEAD every log file in scope, not just the first/last")
    ap.add_argument("--yes", action="store_true", help=f"Allow more than {HEAD_GUARD:,} HEAD requests")
    ap.add_argument("--refresh", action="store_true", help="Forget the cached metadata under the prefix, re-read it")
    ap.add_argument("--offline", action="store_true", help="Summarize the cache only (no S3 calls)")
    ap.add_argument("--upload-start", help="Only log files UPLOADED from this UTC time (S3 LastModified), no HEADs")
    ap.add_argument("--upload-end", help="... until this UTC time (exclusive)")
    ap.add_argument("--json", action="store_true", help="Print the summary as JSON")
    a = ap.parse_args()
    use_env(a.env)
    bucket = default_bucket(a.bucket)
    prefix = a.prefix or ""
    up = (a.upload_start or a.upload_end) is not None
    up_start, up_end = parse_timestamp(a.upload_start), parse_timestamp(a.upload_end)
    if (a.upload_start and not up_start) or (a.upload_end and not up_end):
        sys.exit("ERROR: Cannot parse --upload-start/--upload-end - use ISO 8601 UTC, e.g. 2026-01-15 or 2026-01-15T08:00")
    log = (lambda *x: print(*x, file=sys.stderr)) if a.json else print
    try:
        if a.offline:
            if not any(inventory_dir(bucket).glob("*.json")):
                sys.exit(f"ERROR: No cache in {inventory_dir(bucket)} - run without --offline first")
            inv = load_inventory(bucket)
        elif a.prefix is None and not a.all:
            print_devices(bucket, *list_devices(bucket), a.json)
            return 0
        else:
            inv = update_inventory(bucket, prefix, a.refresh, log=log)
            if not up:  # upload questions need the listing only
                keys = ([k for k, o in inv["objects"].items() if o["kind"] == "log" and k.startswith(prefix)]
                        if a.metadata else edge_keys(inv, prefix))
                head_objects(bucket, inv, keys, None if a.yes else HEAD_GUARD, log=log)
    except HeadGuard as e:
        sys.exit(f"HEAD GUARD: --metadata needs {e}. Ask the user, then re-run with --yes - or scope it with "
                 "--prefix <device>/")
    except Exception as e:  # noqa: BLE001 - botocore errors: Show a clean message, no secrets
        sys.exit(f"ERROR: S3 request failed: {e.__class__.__name__}: {e}")
    s = upload_summary(inv, prefix, up_start, up_end) if up else summarize(inv, prefix)
    if a.json:
        print(json.dumps(s, indent=1))
    else:
        (print_upload_summary if up else print_summary)(s)
        print(f"\nCache: {inventory_dir(bucket)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
