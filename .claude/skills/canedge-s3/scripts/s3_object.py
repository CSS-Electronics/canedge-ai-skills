"""Basic read-only S3 object access for questions the other scripts do not cover (read-only, never writes to S3).

  ls   [PREFIX] [--folders] [--max N]   Keys under a prefix with size + LastModified. --folders: one level only
                                        (e.g. ls --folders = the bucket root: device folders + root files like DBCs)
  head KEY                              Size, LastModified, ETag, storage class, content type + user metadata
                                        (for a log file: Timestamp, Fw, Hw, Net, Put-Index)
  get  KEY [KEY ...] [--yes]            Download objects to data/s3/<bucket>/<key> (e.g. device.json, a config, a DBC,
                                        a log file). Existing files with the same size are kept. Stops above 1 GB
                                        unless --yes

Usage:
  python s3_object.py ls 12345678/ --folders
  python s3_object.py head 12345678/00000086/00000003-6501A2B3.MF4
  python s3_object.py get 12345678/config-01.09.json 12345678/device.json

Credentials/bucket as s3_inventory.py (data lake profile credentials/<name>.env, --env NAME). For selections of log files (first/last N, sessions,
logging time) use fetch_logs.py; for bucket summaries s3_inventory.py.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

import s3_inventory as inv_mod

SIZE_GUARD = 1e9


def cmd_ls(s3, bucket: str, a) -> dict:
    folders, objs, n = [], [], 0
    kw = {"Bucket": bucket, "Prefix": a.prefix, **({"Delimiter": "/"} if a.folders else {})}
    for page in s3.get_paginator("list_objects_v2").paginate(**kw):
        folders += [p["Prefix"] for p in page.get("CommonPrefixes", [])]
        for o in page.get("Contents", []):
            n += 1
            if len(objs) < a.max:
                objs.append({"key": o["Key"], "size": o["Size"], "last_modified": inv_mod.iso(o["LastModified"])})
    return {"bucket": bucket, "prefix": a.prefix, "folders": folders, "objects": objs, "objects_total": n}


def cmd_head(s3, bucket: str, a) -> dict:
    h = s3.head_object(Bucket=bucket, Key=a.key)
    md = {k.lower(): v for k, v in (h.get("Metadata") or {}).items()}
    out = {"bucket": bucket, "key": a.key, "size": h["ContentLength"], "last_modified": inv_mod.iso(h["LastModified"]),
           "etag": h.get("ETag", "").strip('"'), "storage_class": h.get("StorageClass", "STANDARD"),
           "content_type": h.get("ContentType"), "metadata": md}
    if md.get("timestamp"):
        t = inv_mod.parse_timestamp(md["timestamp"])
        out["logging_start_utc"] = inv_mod.iso(t)
        out["timestamp_quality"] = inv_mod.timestamp_quality({"metadata": md})
    return out


def cmd_get(s3, bucket: str, a) -> dict:
    root = Path("data/s3") / bucket
    sizes = {k: s3.head_object(Bucket=bucket, Key=k)["ContentLength"] for k in a.keys}
    todo = [k for k in a.keys if not ((root / k).exists() and (root / k).stat().st_size == sizes[k])]
    total = sum(sizes[k] for k in todo)
    out = {"bucket": bucket, "files": [str(root / k) for k in a.keys], "to_download": len(todo),
           "download_mb": round(total / 1e6, 3)}
    if total > SIZE_GUARD and not a.yes:
        out["blocked_by_size_guard"] = True
        return out
    for k in todo:
        p = root / k
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_name(p.name + ".part")
        s3.download_file(bucket, k, str(tmp))
        os.replace(tmp, p)
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket", help="Default: The profile's S3 input bucket")
    ap.add_argument("--env", metavar="NAME", help="Data lake profile credentials/NAME.env (default: the only profile)")
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("ls")
    p.add_argument("prefix", nargs="?", default="")
    p.add_argument("--folders", action="store_true", help="One level only (folders + files directly under the prefix)")
    p.add_argument("--max", type=int, default=200, help="Objects to show (the total is always counted)")
    p = sub.add_parser("head")
    p.add_argument("key")
    p = sub.add_parser("get")
    p.add_argument("keys", nargs="+")
    p.add_argument("--yes", action="store_true", help="Confirm downloads above 1 GB")
    a = ap.parse_args()
    inv_mod.use_env(a.env)
    bucket = inv_mod.default_bucket(a.bucket)
    try:
        res = {"ls": cmd_ls, "head": cmd_head, "get": cmd_get}[a.cmd](inv_mod.s3_client(), bucket, a)
    except Exception as e:  # noqa: BLE001 - botocore errors: Show a clean message, no secrets
        sys.exit(f"ERROR: S3 request failed: {e.__class__.__name__}: {e}")
    if res.get("blocked_by_size_guard"):
        print(f"SIZE GUARD: {res['download_mb'] / 1000:.2f} GB to download (> 1 GB). Ask the user, then re-run with --yes")
    print(json.dumps(res, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
