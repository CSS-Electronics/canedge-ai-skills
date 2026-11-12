"""Download a device's CANedge log files from S3 (read-only on S3) into data/s3/<bucket>/<key>.

Select files by recording ORDER (works for every file) and/or by LOGGING time (needs the 'Timestamp' metadata
that the CANedge sets on upload):

  --first N / --last N     The first/last N log files of the device in recording order (session, then split;
                           the session counter increases at every power-on and after 256 splits)
  --session S              Only these sessions (repeatable); combine with --first/--last for e.g. the last file
                           of session S
  --key K                  Exact S3 keys (repeatable)
  --start / --end          Logging time window (UTC) by 'Timestamp' metadata. The last file that started before
                           --start is always included: It may contain the start of the window, however long ago it
                           started (as CSS canedge_browser; --no-preceding to skip). Files without Timestamp
                           metadata (e.g. manual uploads) cannot be time-filtered: they are skipped and counted -
                           select them by session/split instead

LastModified (upload time) and the key suffix are never used as logging time. It only bounds which files need a
HEAD request: A file uploaded more than a day before --start was logged before it, so only newer uploads are
HEADed (plus the selected files, to show their logging times). Order selections need no HEADs to select.
Duplicate uploads of the same session/split (lost upload confirmation) are downloaded once. Existing local files
with the same size are skipped. Size guard: Downloads above 1 GB stop until the user agrees to a size, passed as
--max-gb N (or --yes for any size). The dry run shows the estimated AWS egress cost (data transfer out).

Usage:
  python fetch_logs.py --device 12345678 [--first N | --last N] [--session 62] [--start 2026-01-15T01:00]
                       [--end 2026-01-15T03:00] [--key K] [--bucket B] [--dry-run] [--max-gb N | --yes] [--cached] [--json]

Times are UTC (ISO 8601, a date like 2026-01-15 means 00:00 UTC). --end is exclusive.
Prints the local folder + a project.json source snippet to add the data to an analysis project.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import s3_inventory as inv_mod

SIZE_GUARD = 1e9
EGRESS_USD_PER_GB = 0.09  # AWS data transfer out to the internet (first tier, most regions)


def parse_utc(s: str | None) -> dt.datetime | None:
    if not s:
        return None
    t = inv_mod.parse_timestamp(s)
    if not t:
        sys.exit(f"ERROR: Cannot parse time '{s}' - use ISO 8601 UTC, e.g. 2026-01-15T01:00 or 2026-01-15")
    return t


def device_files(inv: dict, device: str) -> list[tuple[str, dict]]:
    """The device's log files in recording order, one per session/split (duplicate uploads: Prefer the copy with
    Timestamp metadata, then the earliest upload)."""
    best: dict[tuple, tuple[str, dict]] = {}
    for k, o in inv["objects"].items():
        if o["kind"] != "log" or o["device"] != device:
            continue
        ok = inv_mod.order_key(o)
        rank = (inv_mod.logging_time(o)[0] is None, o["last_modified"])
        if ok not in best or rank < (inv_mod.logging_time(best[ok][1])[0] is None, best[ok][1]["last_modified"]):
            best[ok] = (k, o)
    return [best[ok] for ok in sorted(best)]


def select(files: list[tuple[str, dict]], a: argparse.Namespace, start, end) -> tuple[list, int, bool, int]:
    """Return (selected [(key, obj)] in recording order, files skipped by the time filter for lack of a Timestamp,
    True if only the file that started before --start matched, files without cached metadata the time filter
    could not place)."""
    sel = files
    if a.key:
        keys = set(a.key)
        sel = [f for f in sel if f[0] in keys]
    if a.session:
        sel = [f for f in sel if f[1]["session"] in set(a.session)]
    no_time, prev_only, unread = 0, False, 0
    if start or end:
        timed = []  # (key, obj, logging time or None = not read, but logged before --start)
        for k, o in sel:
            if "metadata" in o:
                t = inv_mod.logging_time(o)[0]
                no_time += t is None
                if t:
                    timed.append((k, o, t))
            elif start and not inv_mod.uploaded_since(o, start):
                timed.append((k, o, None))
            else:
                unread += 1
        inside = [x for x in timed if x[2] and (not start or x[2] >= start) and (not end or x[2] < end)]
        before = [x for x in timed if not x[2] or x[2] < start] if start and not a.no_preceding else []
        if before:  # the last file before the window may contain its start (no time limit, as canedge_browser)
            prev_only = not inside
            inside.insert(0, before[-1])
        sel = [(k, o) for k, o, _ in inside]
    if a.first:
        sel = sel[: a.first]
    if a.last:
        sel = sel[-a.last:]
    return sel, no_time, prev_only, unread


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--device", required=True, help="Device ID (8 hex chars), as in the S3 key")
    ap.add_argument("--first", type=int, help="First N files in recording order (after the other filters)")
    ap.add_argument("--last", type=int, help="Last N files in recording order (after the other filters)")
    ap.add_argument("--session", type=int, action="append", help="Only these sessions (repeatable)")
    ap.add_argument("--key", action="append", help="Exact S3 keys (repeatable)")
    ap.add_argument("--start", help="Logging time from (UTC, inclusive) - needs Timestamp metadata")
    ap.add_argument("--end", help="Logging time to (UTC, exclusive) - needs Timestamp metadata")
    ap.add_argument("--no-preceding", action="store_true", help="Do not add the file that started before --start")
    ap.add_argument("--bucket")
    ap.add_argument("--env", metavar="NAME", help="Data lake profile credentials/NAME.env (default: the only profile)")
    ap.add_argument("--dry-run", action="store_true", help="Only show what would be downloaded")
    ap.add_argument("--max-gb", type=float, help="Download limit the user agreed to (default 1 GB)")
    ap.add_argument("--yes", action="store_true", help="Confirm a download of any size")
    ap.add_argument("--cached", action="store_true", help="Use the cached inventory as-is (skip the S3 listing)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    if a.first and a.last:
        ap.error("use --first or --last, not both")
    inv_mod.use_env(a.env)
    bucket = inv_mod.default_bucket(a.bucket)
    device = a.device.upper()
    start, end = parse_utc(a.start), parse_utc(a.end)
    log = (lambda *x: print(*x, file=sys.stderr)) if a.json else print

    if a.cached:
        if not (inv_mod.inventory_dir(bucket) / f"{device}.json").exists():
            sys.exit(f"ERROR: No inventory cache for {device} in {bucket} - run without --cached first")
        inv = inv_mod.load_inventory(bucket, [device])
    else:
        try:
            inv = inv_mod.update_inventory(bucket, f"{device}/", log=log)
            if start or end:  # logging times needed: HEAD the files that may start at/after --start
                inv_mod.head_objects(bucket, inv, [k for k, o in inv["objects"].items() if o["kind"] == "log"
                                                   and o["device"] == device
                                                   and (not start or inv_mod.uploaded_since(o, start))], log=log)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"ERROR: S3 request failed: {e.__class__.__name__}: {e}")

    files = device_files(inv, device)
    sel, no_time, prev_only, unread = select(files, a, start, end)
    if sel and not a.cached:
        try:  # the logging times of the selected files (no-op for files already read)
            inv_mod.head_objects(bucket, inv, [k for k, _ in sel], log=log)
        except Exception as e:  # noqa: BLE001
            sys.exit(f"ERROR: S3 request failed: {e.__class__.__name__}: {e}")
    root = Path("data/s3") / bucket
    todo = [(k, o) for k, o in sel if not ((root / k).exists() and (root / k).stat().st_size == o["size"])]
    total = sum(o["size"] for _, o in sel)
    dl_bytes = sum(o["size"] for _, o in todo)
    times = [inv_mod.logging_time(o)[0] for _, o in sel]
    info = {"bucket": bucket, "device": device, "device_files": len(files), "selected": len(sel),
            "selected_gb": round(total / 1e9, 4), "to_download": len(todo), "download_gb": round(dl_bytes / 1e9, 4),
            "egress_usd_estimate": round(dl_bytes / 1e9 * EGRESS_USD_PER_GB, 2),
            "already_local": len(sel) - len(todo), "skipped_without_timestamp": no_time,
            "skipped_not_cached": unread, "only_preceding_file": prev_only,
            "first_key": sel[0][0] if sel else None, "last_key": sel[-1][0] if sel else None,
            "first_logging_utc": inv_mod.iso(min((t for t in times if t), default=None)),
            "last_logging_utc": inv_mod.iso(max((t for t in times if t), default=None)),
            "local_folder": str(root / device), "downloaded": 0, "dry_run": a.dry_run}
    if start or end:
        info["approx_timestamps"] = sum(inv_mod.timestamp_quality(o) == "approx" for _, o in files)
        if info["approx_timestamps"]:
            log(f"NOTE: {info['approx_timestamps']} of the device's files have an approximate Timestamp (FW < 01.09.01: "
                "may be local time and/or the file end). Widen --start/--end by ~14 h plus one split and check the "
                "exact times in the downloaded files")

    if unread:
        log(f"NOTE: {unread} file(s) have no cached metadata and were left out of the time filter - run without "
            "--cached to read it")
    if not sel:
        log(f"No log files of {device} match the selection ({len(files)} log files of this device in the inventory)"
            + (f"; {no_time} file(s) have no Timestamp metadata and cannot be time-filtered - select them by "
               "--session / --first / --last" if no_time else ""))
        if a.json:
            print(json.dumps(info, indent=1))
        return 0
    log(f"{device}: {len(sel)} of {len(files)} files ({total / 1e9:.3f} GB), {sel[0][0]} -> {sel[-1][0]} | "
        f"{len(todo)} to download ({dl_bytes / 1e9:.3f} GB, ~${dl_bytes / 1e9 * EGRESS_USD_PER_GB:.2f} AWS egress), "
        f"{len(sel) - len(todo)} already local")
    for k, o in (todo if len(todo) <= 15 else todo[:10]):
        t = inv_mod.logging_time(o)[0]
        log(f"  {k}  {o['size'] / 1e6:.1f} MB  " + (f"logging {inv_mod.iso(t)}" if t else "no Timestamp metadata"
                                                    if "metadata" in o else "metadata not cached"))
    if len(todo) > 15:
        log(f"  ... {len(todo) - 10} more")
    if prev_only:
        log("NOTE: Only the file that started before --start matches: It may or may not reach into the window - "
            "check its last frame after download")
    if no_time:
        log(f"NOTE: {no_time} file(s) have no Timestamp metadata and were skipped by the time filter - select them "
            "by --session / --first / --last")

    if a.dry_run:
        log("Dry run - nothing downloaded")
    elif dl_bytes > (a.max_gb * 1e9 if a.max_gb else SIZE_GUARD) and not a.yes:
        log(f"SIZE GUARD: {dl_bytes / 1e9:.2f} GB to download (~${dl_bytes / 1e9 * EGRESS_USD_PER_GB:.2f} AWS egress at "
            f"~${EGRESS_USD_PER_GB}/GB) is above the limit of {a.max_gb or SIZE_GUARD / 1e9:g} GB. Ask the user, then re-run "
            "with --max-gb <the GB they agree to> - or narrow the selection")
        info["blocked_by_size_guard"] = True
    elif todo:
        s3 = inv_mod.s3_client()

        def get(r):
            k = r[0]
            p = root / k
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_name(p.name + ".part")
            s3.download_file(bucket, k, str(tmp))
            os.replace(tmp, p)
            return k
        done = 0
        try:
            with ThreadPoolExecutor(8) as ex:
                for _ in ex.map(get, todo):
                    done += 1
                    if done % 25 == 0:
                        log(f"  downloaded {done}/{len(todo)}")
        except Exception as e:  # noqa: BLE001
            log(f"ERROR during download after {done} files: {e.__class__.__name__}: {e}")
            info["downloaded"] = done
            if a.json:
                print(json.dumps(info, indent=1))
            return 1
        info["downloaded"] = done
        log(f"Downloaded {done} files ({dl_bytes / 1e9:.3f} GB)")

    src = {"type": "s3", "bucket": bucket, "prefix": f"{device}/"}
    if a.env:  # data lake profile credentials/<name>.env: Later runs for this project pass --env <name>
        src["env"] = a.env
    if a.start:
        src["start_utc"] = inv_mod.iso(start)
    if a.end:
        src["end_utc"] = inv_mod.iso(end)
    info["project_source"] = src
    log(f"Local folder: {root / device}  (read the files in place, never move or edit them)")
    log(f"project.json source: {json.dumps(src)}")
    if a.json:
        print(json.dumps(info, indent=1))
    return 0


if __name__ == "__main__":
    sys.exit(main())
