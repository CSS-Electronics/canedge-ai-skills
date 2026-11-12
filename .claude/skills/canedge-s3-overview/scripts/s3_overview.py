"""S3 bucket overview dashboard: Devices, log files and GB by logging day and by upload day, devices that stopped
logging or uploading, firmware per device and the non-log objects. ONE self-contained report.html (+ report.json
with the same numbers for the agent), built on the canedge-reports dashboard kit.

Updates the inventory cache first (canedge-s3 s3_inventory.py, read-only) unless --offline: Lists all objects, then
reads the metadata (HEAD) of the first + last file per device and of every file uploaded since the window start
(minus 1 day). Default window: The whole history if the bucket/prefix has <= 50,000 log files, else the last 90 days
(older uploads are counted, but have no logging day). Above 50,000 HEADs it stops unless --yes. Logging time = the
S3 metadata 'Timestamp'; LastModified (upload time) is only used for the upload figures.

Usage:
  python s3_overview.py [--bucket B] [--prefix P] [--days 30] [--window DAYS] [--upload-delays]
                        [--out folder | --project projects/<name>] [--offline] [--yes] [--as-of 2026-10-19] [--tz ZONE]
                        [--json]

Output: <out>/report.html + report.json (default out: projects/<name>/reports/<yyyymmdd>-s3-overview with
--project, else TEMP/reports/<yyyymmdd>-s3-overview-<bucket>; yyyymmdd = run date). --json prints the key numbers for
the agent. The last line (or "open_url" with --json) is the file:/// link for the reply.
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import statistics
import sys
from collections import defaultdict
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(SKILLS / "canedge-s3" / "scripts"))
sys.path.insert(0, str(SKILLS / "canedge-reports" / "scripts"))
sys.path.insert(0, str(SKILLS / "canedge-workspace" / "scripts"))
import s3_inventory as inv_mod  # noqa: E402
from dashboard import build_html  # noqa: E402
from display_time import DisplayTime  # noqa: E402
from paths import file_url, open_line, report_dir  # noqa: E402

DAY = 86400
AUTO_WINDOW = 90  # days, when the bucket/prefix has more log files than the HEAD guard
MAX_OTHERS = 1000


def epoch(t: dt.datetime | None) -> int | None:
    return int(t.timestamp()) if t else None


def device_status(dj: dict | None) -> dict | None:
    """The device.json snapshot as in CANcloud: Heartbeat (its LastModified), FW, config + sync, SD space + health."""
    if not dj or dj.get("error"):
        return None
    used, _, total = (dj.get("space_used_mb") or "").partition("/")
    cfg = dj.get("cfg_s3") or {}
    sync = None if not cfg.get("crc32") or not dj.get("cfg_crc32") else cfg["crc32"].upper() == dj["cfg_crc32"].upper()
    return {"heartbeat": epoch(inv_mod.parse_timestamp(dj.get("as_of"))), "fw": dj.get("fw_ver"),
            "cfg_name": dj.get("cfg_name"), "cfg_crc32": dj.get("cfg_crc32"), "cfg_s3_crc32": cfg.get("crc32"),
            "sync": sync, "sd_used_mb": int(used) if used.isdigit() else None,
            "sd_total_mb": int(total) if total.isdigit() else None,
            "sd_life_pct": int(dj["sd_used_lifespan"]) if str(dj.get("sd_used_lifespan") or "").isdigit() else None}


def build_facts(inv: dict, prefix: str, as_of: dt.datetime, days: int, delays: bool) -> dict:
    devs: dict[str, dict] = {}
    by_day: dict[tuple, list] = defaultdict(lambda: [0, 0])  # (device, UTC day) -> files, bytes (logging day)
    by_up: dict[tuple, list] = defaultdict(lambda: [0, 0])   # (device, UTC day) -> files, bytes (upload day)
    others = []
    for k, o in inv["objects"].items():
        if not k.startswith(prefix):
            continue
        up = inv_mod.parse_timestamp(o["last_modified"])
        if o.get("device") and o["device"] not in devs:  # every device folder, also without log files (heartbeat only)
            devs[o["device"]] = {"files": 0, "bytes": 0, "sessions": set(), "splits": defaultdict(int), "first": None,
                                 "last": None, "last_up": None, "unread": 0, "no_ts": 0, "approx": 0, "fw": {}, "delays": []}
        if o["kind"] != "log":
            others.append([k, o["size"], epoch(up)])
            continue
        d = devs[o["device"]]
        d["files"] += 1
        d["bytes"] += o["size"]
        d["sessions"].add(o["session"])
        d["splits"][(o["session"], o["split"])] += 1
        d["last_up"] = max(d["last_up"] or up, up)
        u = by_up[(o["device"], epoch(up) // DAY * DAY)]
        u[0] += 1
        u[1] += o["size"]
        if "metadata" not in o:
            d["unread"] += 1
            continue
        t, _ = inv_mod.logging_time(o)
        fw = inv_mod.firmware(o)
        if fw:
            f = d["fw"].setdefault(fw, [0, None, None])
            f[0] += 1
            if t:
                f[1], f[2] = min(f[1] or t, t), max(f[2] or t, t)
        if t is None:
            d["no_ts"] += 1
            continue
        d["approx"] += inv_mod.timestamp_quality(o) == "approx"
        d["first"], d["last"] = min(d["first"] or t, t), max(d["last"] or t, t)
        g = by_day[(o["device"], epoch(t) // DAY * DAY)]
        g[0] += 1
        g[1] += o["size"]
        if delays:
            d["delays"].append((up - t).total_seconds() / 60)
    names = sorted(devs)
    idx = {n: i for i, n in enumerate(names)}
    dj = inv.get("device_json", {})
    dev_rows = []
    for n in names:
        d = devs[n]
        fws = sorted(([fw, c, epoch(a), epoch(b)] for fw, (c, a, b) in d["fw"].items()),
                     key=lambda r: (r[3] or 0, r[0]))
        dev_rows.append({"id": n, "meta": (dj.get(n) or {}).get("log_meta"), "status": device_status(dj.get(n)),
                         "files": d["files"], "bytes": d["bytes"], "sessions": len(d["sessions"]),
                         "first_log": epoch(d["first"]), "last_log": epoch(d["last"]), "last_up": epoch(d["last_up"]),
                         "unread": d["unread"], "no_ts": d["no_ts"], "approx": d["approx"],
                         "dup": sum(c - 1 for c in d["splits"].values()), "fw": fws,
                         **({"delay_min": round(statistics.median(d["delays"]), 1)} if d["delays"] else {})})

    def cols(m: dict) -> dict:
        ks = sorted(m)
        return {"dev": [idx[k[0]] for k in ks], "t": [k[1] for k in ks], "files": [m[k][0] for k in ks],
                "bytes": [m[k][1] for k in ks]}
    others.sort()
    return {"bucket": inv["bucket"], "prefix": prefix, "as_of": epoch(as_of), "days": days, "devices": names,
            "dev": dev_rows, "day": cols(by_day), "up": cols(by_up), "others": others[:MAX_OTHERS],
            "others_total": len(others), "synced": inv.get("updated_utc"), "delays": delays}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--bucket")
    ap.add_argument("--env", metavar="NAME", help="Data lake profile credentials/NAME.env (default: the only profile)")
    ap.add_argument("--prefix", default="")
    ap.add_argument("--days", type=int, default=30, help="Not logging / not uploading = nothing in the last N days")
    ap.add_argument("--window", type=int, help="Days of logging history to read (default: all if <= 50,000 log files, "
                                               "else 90)")
    ap.add_argument("--upload-delays", action="store_true", help="Add the median upload delay per device")
    ap.add_argument("--out", type=Path)
    ap.add_argument("--project", type=Path)
    ap.add_argument("--offline", action="store_true", help="Use the cached inventory only (no S3 calls)")
    ap.add_argument("--yes", action="store_true", help=f"Allow more than {inv_mod.HEAD_GUARD:,} HEAD requests")
    ap.add_argument("--as-of", help="Reference time (UTC) for 'not logging/uploading' instead of now")
    ap.add_argument("--tz", help="Display time zone (IANA name). Default: project display_tz, else the viewer's PC zone")
    ap.add_argument("--json", action="store_true", help="Print the key numbers as JSON")
    a = ap.parse_args()
    inv_mod.use_env(a.env)
    bucket = inv_mod.default_bucket(a.bucket)
    log = (lambda *x: print(*x, file=sys.stderr)) if a.json else print
    as_of = (dt.datetime.fromisoformat(a.as_of).replace(tzinfo=dt.timezone.utc) if a.as_of
             else dt.datetime.now(dt.timezone.utc))

    if a.offline:
        if not any(inv_mod.inventory_dir(bucket).glob("*.json")):
            sys.exit(f"ERROR: No inventory cache for {bucket} - run without --offline first")
        inv = inv_mod.load_inventory(bucket)
    else:
        try:
            inv = inv_mod.update_inventory(bucket, a.prefix, log=log)
            inv_mod.head_objects(bucket, inv, inv_mod.edge_keys(inv, a.prefix), log=log)
            logs = [k for k, o in inv["objects"].items() if k.startswith(a.prefix) and o["kind"] == "log"]
            window = a.window or (None if len(logs) <= inv_mod.HEAD_GUARD else AUTO_WINDOW)
            since = None
            if window:
                last = max((t for k in logs for t in [inv_mod.logging_time(inv["objects"][k])[0]] if t), default=None)
                end = min(as_of, last) if last and last < as_of - dt.timedelta(days=window) else as_of
                since = end - dt.timedelta(days=window)  # all data older than the window: The last active period
            inv_mod.head_objects(bucket, inv, [k for k in logs if not since or inv_mod.uploaded_since(inv["objects"][k], since)],
                                 None if a.yes else inv_mod.HEAD_GUARD, log=log)
        except inv_mod.HeadGuard as e:
            sys.exit(f"HEAD GUARD: The overview needs {e}. Ask the user, then re-run with --yes - or narrow it "
                     "(--prefix <device>/, a shorter --window)")
        except Exception as e:  # noqa: BLE001
            sys.exit(f"ERROR: S3 request failed: {e.__class__.__name__}: {e}")

    facts = build_facts(inv, a.prefix, as_of, a.days, a.upload_delays)
    if not facts["devices"]:
        msg = (f"s3://{bucket}/{a.prefix}: No CANedge log files found ({facts['others_total']} other objects). "
               "Nothing to report - check the bucket name/prefix, or the devices have not uploaded yet.")
        print(json.dumps({"bucket": bucket, "log_files": 0, "report": None}) if a.json else msg)
        return 0
    project_cfg = json.loads((a.project / "project.json").read_text(encoding="utf-8")) if a.project else {}
    cfg = {"title": "S3 bucket overview", "tz": DisplayTime.from_project(project_cfg, a.tz).page_tz(),
           "generated": dt.datetime.now(dt.timezone.utc).strftime("%y/%m/%d %H:%M UTC"),
           "endpoint": inv.get("endpoint")}
    out = a.out or report_dir("s3-overview" if a.project else f"s3-overview-{bucket}", a.project)
    out.mkdir(parents=True, exist_ok=True)
    html = build_html(cfg["title"], SKILLS / "canedge-s3-overview" / "assets" / "app.js", {"FACTS": facts, "CFG": cfg})
    (out / "report.html").write_text(html, encoding="utf-8")

    cutoff = epoch(as_of) - a.days * DAY
    rows = facts["dev"]
    key = {"bucket": bucket, "prefix": a.prefix, "as_of_utc": inv_mod.iso(as_of), "devices": len(rows),
           "log_files": sum(r["files"] for r in rows), "gb": round(sum(r["bytes"] for r in rows) / 1e9, 3),
           "metadata_read": sum(r["files"] - r["unread"] for r in rows),
           f"not_logging_{a.days}d": [r["id"] for r in rows if not r["last_log"] or r["last_log"] < cutoff],
           f"not_uploading_{a.days}d": [r["id"] for r in rows if not r["last_up"] or r["last_up"] < cutoff],
           "config_not_synced": [r["id"] for r in rows if (r["status"] or {}).get("sync") is False],
           "without_timestamp": sum(r["no_ts"] for r in rows), "approx_timestamp": sum(r["approx"] for r in rows),
           "firmware": sorted({f[0] for r in rows for f in r["fw"]}), "other_objects": facts["others_total"],
           "report": str(out / "report.html"), "open_url": file_url(out / "report.html")}
    (out / "report.json").write_text(json.dumps({"summary": key, "devices": rows}, indent=1), encoding="utf-8")
    if a.json:
        print(json.dumps(key, indent=1))
    else:
        print(f"{key['devices']} device(s), {key['log_files']:,} log files, {key['gb']:.2f} GB "
              f"(metadata read for {key['metadata_read']:,})")
        print(f"Not logging in {a.days} d: {', '.join(key[f'not_logging_{a.days}d']) or 'none'}. "
              f"Not uploading in {a.days} d: {', '.join(key[f'not_uploading_{a.days}d']) or 'none'}")
        print(f"HTML: {out / 'report.html'} ({len(html.encode('utf-8')) / 1024:.0f} KB, single file)")
        print(f"JSON: {out / 'report.json'}")
        print(open_line(out / "report.html", "dashboard"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
