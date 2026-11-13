r"""Check, build, upload and remove the data lake control files in the input bucket root (canedge-lake-admin).

  check   FILE [--as NAME] [--offline]   Validate a local control file: The automation's own rules (the vendored
                                         _automation_control_files.py) + cross-checks against the bucket and lake
                                         (message folders, DBC signal names, devices, DBC files, buckets). Exit 1 on
                                         errors
  backlog --device X [--device Y] [--start T] [--end T] [-o FILE]
                                         Build backlog.json from the S3 inventory: Whole devices/sessions as
                                         prefixes, else file keys. --start/--end = logging time (UTC, 'Timestamp'
                                         metadata; files without it are left out and counted). Prints a rough run
                                         time (~33 GB/h) vs the job's limit, with split / --timeout advice
  pull    NAME                           Download the current root file to data/lake-admin/<bucket>/drafts/NAME
                                         (start an edit from the live version; 'not in the bucket' is fine)
  push    FILE [--as NAME] [--offline]   Re-check, back up the current root object to
                                         data/lake-admin/<bucket>/backups/<utc>-NAME, upload FILE, verify (size)
  remove  NAME                           Back up the root object, then delete it (the feature is off again)

push/remove write to the bucket: Only after the user OKed the shown diff (the harness asks before each call). Allowed
targets: The control files (events.json, custom-messages.json, geofences.json, aggregations.json, backlog.json,
dbc-groups.json, routing.json) and can<n>-*.dbc / lin<n>-*.dbc in the bucket root or in one folder (automation
v4.5.0+, e.g. push FILE --as dbc/can1-x.dbc). passwords.json is never read, written or printed.

Usage:
  python control_files.py check data/lake-admin/<bucket>/drafts/events.json
  python control_files.py backlog --device 2F6913DB --end 2026-01-01 -o data/lake-admin/<bucket>/drafts/backlog.json
  python control_files.py push data/lake-admin/<bucket>/drafts/events.json
Options: --env NAME (data lake profile, default: the only one), --json
"""
from __future__ import annotations

import argparse
import datetime as dt
import difflib
import json
import re
import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
for _p in (Path(__file__).resolve().parent, SKILLS / "canedge-workspace" / "scripts", SKILLS / "canedge-s3" / "scripts",
           SKILLS / "canedge-lake-query" / "scripts", SKILLS / "canedge-dbc" / "scripts"):
    sys.path.insert(0, str(_p))
import _automation_control_files as acf  # noqa: E402
from env import aws_client, lake, require_s3, use_env  # noqa: E402

ADMIN_DIR = Path("data/lake-admin")
ZIP_RE = re.compile(r"^mdf-to-parquet-(amazon|google|azure)-function-v(\d+\.\d+\.\d+)\.zip$")
DEVICE_RE = re.compile(r"^[0-9A-F]{8}$")
MAX_DEVICES = 50                     # lake devices whose message folders are listed for the message checks
CLOUDS = {"aws": "Amazon", "google": "Google", "s3": None}
# Backlog run time (rough): The MF4 decoder uses one CPU core and the job decodes one batch at a time. Measured
# 2026-11 on the AWS Glue job (G.1X): 9.6 MB/s decoding + ~5 % S3 download for dense CAN data (MF4) = ~33 GB of log
# files per hour. Google/Azure, compressed (MFC) or sparse data may differ: Treat it as an order of magnitude.
BACKLOG_GB_PER_H = 33
JOB_LIMIT_MIN = {"google": 60}       # Google backlog function timeout; AWS: read from the Glue job (stack default 60
                                     # before 2026-11-11, then 180)
AWS_MAX_TIMEOUT_MIN = 2880           # Glue maximum, settable per run (lake_ops.py start --timeout)


# ---------------------------------------------------------------- bucket context
class Lake:
    """What the cross-checks need, read lazily: Root objects, device + message folders, DBCs (all listings only)."""

    def __init__(self):
        require_s3()
        self.info = lake()
        self.bucket, self.out = self.info["input_bucket"], self.info["output_bucket"]
        self.s3 = aws_client("s3")
        self._root = self._msgs = self._dbc = None

    def folders(self, bucket: str, prefix: str = "") -> tuple[list[str], list[dict]]:
        dirs, objs = [], []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=bucket, Prefix=prefix, Delimiter="/"):
            dirs += [p["Prefix"][len(prefix):].rstrip("/") for p in page.get("CommonPrefixes", [])]
            objs += page.get("Contents", [])
        return dirs, objs

    @property
    def root(self) -> dict:
        """Input bucket root: Device folders, root object keys -> size, the automation version from the zip name."""
        if self._root is None:
            dirs, objs = self.folders(self.bucket)
            keys = {o["Key"]: o["Size"] for o in objs}
            self._root = {"devices": [d for d in dirs if DEVICE_RE.match(d)], "keys": keys,
                          "version": automation_version(keys),
                          "dbcs": sorted(k for k in keys if acf.DBC_FILE_RE.match(k))}
        return self._root

    def messages(self, devices: list[str] | None = None) -> dict[str, set[str]]:
        """Message folders per device in the output bucket (exact case). devices=None: The lake's devices (capped)."""
        if self._msgs is None:
            self._msgs = {}
        try:
            lake_devs = [d for d in self.folders(self.out)[0] if DEVICE_RE.match(d)]
        except Exception as e:  # noqa: BLE001 - e.g. no output bucket (no data lake) or no access
            raise CheckError(f"Cannot list the output bucket {self.out}: {e.__class__.__name__}") from e
        todo = [d for d in (devices or lake_devs[:MAX_DEVICES]) if d in lake_devs and d not in self._msgs]
        for d in todo:
            self._msgs[d] = set(self.folders(self.out, f"{d}/")[0])
        return {d: m for d, m in self._msgs.items() if devices is None or d in devices}

    def dbc_index(self):
        """canedge-lake-query DbcIndex over the root DBCs + dbc-groups.json (+ the DBC files in a folder that it
        lists, v4.5.0+), downloaded once to data/lake-admin."""
        if self._dbc is None:
            import shutil
            from _dbc import DbcIndex
            base = ADMIN_DIR / self.bucket / "root"
            (base / "dbc").mkdir(parents=True, exist_ok=True)
            for f in base.glob("dbc/*.dbc"):
                if f.name not in self.root["dbcs"]:
                    f.unlink()
            for d in (p for p in (base / "dbc").iterdir() if p.is_dir()):   # folder DBCs: fetched fresh below
                shutil.rmtree(d)
            for name in self.root["dbcs"]:
                local = base / "dbc" / name
                if not (local.exists() and local.stat().st_size == self.root["keys"][name]):
                    self.s3.download_file(self.bucket, name, str(local))
            groups = base / "dbc-groups.json"
            groups.unlink(missing_ok=True)
            folder_dbcs = []
            if "dbc-groups.json" in self.root["keys"]:
                self.s3.download_file(self.bucket, "dbc-groups.json", str(groups))
                try:
                    data = json.loads(groups.read_text(encoding="utf-8-sig"))
                    folder_dbcs = sorted(references("dbc-groups.json", data)["dbcs"])
                except (ValueError, AttributeError, TypeError):
                    pass                                   # an invalid file: check() reports it
            for name in (f for f in folder_dbcs if "/" in f and not acf.dbc_entry_issue(f)):
                local = base / "dbc" / name
                local.parent.mkdir(parents=True, exist_ok=True)
                try:
                    self.s3.download_file(self.bucket, name, str(local))
                except Exception:  # noqa: BLE001 - a missing folder DBC: cross_check reports it
                    pass
            has_folder = any(p.is_dir() for p in (base / "dbc").iterdir())
            self._dbc = DbcIndex(base) if self.root["dbcs"] or has_folder else False
        return self._dbc or None


class CheckError(Exception):
    pass


def _vkey(v: str) -> tuple:
    return tuple(int(x) for x in v.split("."))


def automation_version(keys) -> str | None:
    """The automation version from the function zips in the bucket root. Several versions side by side (older zips
    left after updates): The newest is the relevant one (numeric compare: 4.10.0 > 4.9.0)."""
    versions = {m.group(2) for k in keys if (m := ZIP_RE.match(k))}
    return max(versions, key=_vkey) if versions else None


# ---------------------------------------------------------------- what a file refers to
def references(name: str, data) -> dict:
    """Messages (exact names / 'contains' strings), signals per message, devices and DBC files a control file uses."""
    ref = {"messages": {}, "contains": {}, "devices": set(), "dbcs": set(), "fatal_signals": name == "events.json"}

    def add(msg, signals, where):
        ref["messages"].setdefault(msg, {"signals": set(), "where": where})["signals"].update(signals)

    if name == "events.json":
        gps = data.get("general", {})
        for m in gps.get("messages_gps", []) if gps.get("include_gps_data", True) else []:
            add(m, set(), "general.messages_gps")
        for i, ev in enumerate(data.get("events", [])):
            where = f"events[{i}] ({ev.get('event_name')})"
            if ev.get("messages_match_type") == "equals":
                for m in {m for lst in ev["messages_filtered_list"] for m in lst}:
                    add(m, set(ev.get("trigger_signals", [])), where)
            elif ev.get("messages_match_type") == "contains":
                ref["contains"][ev["messages_filtered_list"]] = where
    elif name == "custom-messages.json":
        for i, cm in enumerate(data):
            where = f"[{i}] ({cm.get('custom_message_name')})"
            if cm.get("messages_match_type") == "equals":
                for m in {m for lst in cm["messages_filtered_list"] for m in lst}:
                    add(m, set(), where)
            elif cm.get("messages_match_type") == "contains":
                ref["contains"][cm["messages_filtered_list"]] = where
    elif name == "aggregations.json":
        for c in data.get("device_clusters", []):
            ref["devices"].update(c.get("devices", []))
        for i, cd in enumerate(data.get("cluster_details", [])):
            det = cd.get("details", {})
            add(det.get("trip_identifier", {}).get("message"), set(), f"cluster_details[{i}].trip_identifier")
            for a in det.get("aggregations", []):
                add(a["message"], set(a.get("signal", [])), f"cluster_details[{i}].aggregations ({a['message']})")
    elif name == "dbc-groups.json":
        for g in data.get("dbc_groups", []):
            ref["devices"].update(g.get("devices", []))
            ref["dbcs"].update(g.get("dbc_files", []))
        ref["dbcs"].update(data.get("common_dbc_files", []))
    elif name == "routing.json":
        ref["devices"].update(data.get("devices", {}))
    ref["messages"].pop(None, None)
    return ref


# ---------------------------------------------------------------- check
def check(path: Path, name: str, offline: bool) -> dict:
    """Validate a local file. Returns {name, issues: [{level, path, message}], context}."""
    raw = path.read_bytes()
    lk = None if offline else Lake()
    cloud = CLOUDS.get(lk.info["cloud"]) if lk else None
    data, issues = acf.parse(name, raw, cloud=cloud, input_bucket=lk.bucket if lk else None)
    out = {"file": path.as_posix(), "name": name, "issues": [i._asdict() for i in issues], "context": {}}
    if data is None or lk is None or any(i.level == "error" for i in issues):
        if lk is None:
            out["context"]["note"] = "offline: the automation's rules only (no bucket/lake cross-checks)"
        return out

    def issue(level, where, msg):
        out["issues"].append({"level": level, "path": where, "message": msg})

    try:
        root = lk.root
        out["context"].update(bucket=lk.bucket, automation_version=root["version"], root_dbcs=root["dbcs"])
        if root["version"] and _vkey(root["version"]) < _vkey(acf.CONTROL_FILES_VERSION):
            issue("warning", "", f"the deployed automation is v{root['version']} - these checks follow "
                                 f"v{acf.CONTROL_FILES_VERSION} (some rules differ: see references/control-files.md)")
        if (name == "events.json" and data.get("general", {}).get("include_gps_data") is False and
                (not root["version"] or _vkey(root["version"]) <= (4, 3, 0))):
            issue("error", "general.include_gps_data", "false makes v4.3.0 and older fail for every log file with an "
                                                       "event (KeyError 'Latitude') - use true (GPS columns stay "
                                                       "empty without GPS data)")
        if (name == "dbc-groups.json" and (not root["version"] or _vkey(root["version"]) < (4, 3, 2)) and
                (two := sorted({f for g in data.get("dbc_groups", []) for f in g.get("dbc_files", [])
                                if re.match(r"^(can|lin)[0-9]{2}-", f)}))):
            issue("error", "dbc_files", f"{', '.join(two)}: Two-digit channel prefixes (CANmod.router channels) need "
                                        f"automation v4.3.2+ (v{root['version'] or '?'} stops every log file with a "
                                        "CONTROL FILE ERROR). Update the lake first, or leave dbc-groups.json out")
        if (name == "dbc-groups.json" and data.get("common_dbc_files") and
                (not root["version"] or _vkey(root["version"]) < (4, 4, 0))):
            issue("error", "common_dbc_files", f"needs automation v4.4.0+ (v{root['version'] or '?'} ignores the key, "
                                               "so the devices in groups would be decoded without these DBC files). "
                                               "Update the lake first, or list the files in each group's dbc_files")
        if (name == "dbc-groups.json" and (not root["version"] or _vkey(root["version"]) < (4, 5, 0)) and
                (folder := sorted({f for f in references(name, data)["dbcs"] if "/" in f}))):
            issue("error", "dbc_files", f"{', '.join(folder)}: DBC files in a folder need automation v4.5.0+ "
                                        f"(v{root['version'] or '?'} stops every log file with a CONTROL FILE ERROR). "
                                        "Update the lake first, or keep the DBC files in the bucket root")
        ref = references(name, data)
        cross_check(lk, name, data, ref, issue)
    except CheckError as e:
        issue("warning", "", f"cross-checks skipped: {e}")
    return out


def cross_check(lk: Lake, name: str, data, ref: dict, issue) -> None:
    root = lk.root
    for d in sorted(ref["devices"]):
        if DEVICE_RE.match(d) and d not in root["devices"]:
            issue("warning", "devices", f"{d} has no folder in the input bucket {lk.bucket}")
    for f in sorted(ref["dbcs"]):
        if "/" in f:
            if not _object_exists(lk, f):
                issue("error", "dbc_files", f"{f} is not in the input bucket {lk.bucket}")
        elif f not in root["keys"]:
            issue("error", "dbc_files", f"{f} is not in the input bucket root (DBC files there: "
                                        f"{', '.join(root['dbcs']) or 'none'})")
    if name == "routing.json" and lk.info["cloud"] == "aws":
        for dev, rule in data.get("devices", {}).items():
            b = rule.get("output_bucket") if isinstance(rule, dict) else None
            if b and not _bucket_exists(lk.s3, b):
                issue("warning", f"devices.{dev}.output_bucket", f"bucket {b} not found - the device's data goes to "
                                                                  "the default output bucket until it is deployed")
    if name == "backlog.json":
        backlog_checks(lk, data, issue)
    if not (ref["messages"] or ref["contains"]):
        return

    devices = sorted(ref["devices"]) or None
    msgs = lk.messages(devices)
    found = set().union(*msgs.values()) if msgs else set()
    lower = {m.lower(): m for m in found}
    idx = lk.dbc_index()
    custom = set()
    if "custom-messages.json" in root["keys"] and name != "custom-messages.json":
        try:
            custom = {c["custom_message_name"] for c in json.loads(lk.s3.get_object(
                Bucket=lk.bucket, Key="custom-messages.json")["Body"].read())}
        except Exception:  # noqa: BLE001 - an unreadable live file is reported by its own check
            pass
    scope = f"{len(msgs)} lake device(s)" + (" (first 50)" if devices is None and len(msgs) >= MAX_DEVICES else "")
    for text, where in ref["contains"].items():
        if not any(text in m for m in found):
            issue("warning", where, f'no message folder contains "{text}" in {scope}')
    for msg, info in ref["messages"].items():
        where = info["where"]
        if msg not in found:
            if msg.lower() in lower:
                issue("error", where, f'message "{msg}" never matches: the lake folder is "{lower[msg.lower()]}" '
                                      "(names are case-sensitive)")
                continue
            issue("warning", where, f'message "{msg}" is not in the lake ({scope}) - no data decoded for it yet, '
                                    "or a typo (folder names = CAN<n>_<DBC message name>)")
        signal_check(lk, idx, msg, info["signals"], where, msgs, custom, ref["fatal_signals"], issue)


def signal_check(lk, idx, msg, signals, where, msgs, custom, fatal, issue) -> None:
    if not signals:
        return
    if msg in custom or idx is None:
        why = "a calculated table" if msg in custom else "no DBC files in the bucket root"
        issue("warning", where, f'signals of "{msg}" not checked ({why})')
        return
    device = next((d for d, ms in msgs.items() if msg in ms), None) or next(iter(lk.root["devices"]), "")
    hit = idx.match(device, msg)
    if hit is None:
        issue("warning", where, f'"{msg}" matches no DBC message (for device {device or "-"}) - signals not checked')
        return
    names = {s["name"]: s for s in hit["signals"].values()}   # the decoder's names (SG_ names, also for long names)
    long_names = {s["long_name"]: s["name"] for s in hit["signals"].values() if s.get("long_name")}
    for s in sorted(signals):
        if s in names:
            continue
        exact = next((n for n in names if n.lower() == s.lower()), None)
        level = "error" if fatal else "warning"
        short = long_names.get(s) or next((v for k, v in long_names.items() if k.lower() == s.lower()), None)
        if short:
            issue(level, where, f'signal "{s}" is the long name (SystemSignalLongSymbol) of "{short}" in {hit["dbc"]}: '
                                f'The decoder and the data lake use the SG_ name, use "{short}"')
        elif exact:
            issue(level, where, f'signal "{s}" is "{exact}" in {hit["dbc"]} (names are case-sensitive)')
        else:
            issue(level, where, f'signal "{s}" is not in DBC message {hit["message"]} ({hit["dbc"]}); its signals: '
                                f"{', '.join(sorted(names)[:15])}" + (" ..." if len(names) > 15 else ""))


def _bucket_exists(s3, bucket: str) -> bool:
    from botocore.exceptions import ClientError
    try:
        s3.head_bucket(Bucket=bucket)
        return True
    except ClientError as e:
        return e.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket")


def _object_exists(lk: Lake, key: str) -> bool:
    """A key in the input bucket (e.g. a DBC file in a folder) exists. Other errors than 'not found' count as found."""
    return get_root(lk, key) is not None


def backlog_checks(lk: Lake, data: dict, issue) -> None:
    """Entries that select nothing, total files/size, devices that already have Parquet files (duplicates)."""
    import s3_inventory as inv_mod
    entries = [acf.classify_backlog_entry(e) for e in data.get("files", [])]
    devices = sorted({e.split("/")[0] for kind, e in entries if kind})
    files = size = 0
    for d in devices:
        if d not in lk.root["devices"]:
            issue("warning", "files", f"device {d} has no folder in the input bucket")
            continue
        inv = inv_mod.update_inventory(lk.bucket, f"{d}/", log=lambda *_: None)
        logs = {k: o for k, o in inv["objects"].items() if o["kind"] == "log" and o["device"] == d}
        for kind, e in entries:
            if not kind or not e.startswith(f"{d}/"):
                continue
            sel = [o for k, o in logs.items() if (k == e if kind == "file" else k.startswith(e))]
            if not sel:
                issue("warning", "files", f'"{e}" matches no log file in the bucket')
            files += len(sel)
            size += sum(o.get("size", 0) for o in sel)
    lake_devs = set(lk.folders(lk.out)[0])
    dup = [d for d in devices if d in lake_devs]
    if dup:
        issue("warning", "files", f"{len(dup)} device(s) already have Parquet files ({', '.join(dup[:10])}) - files "
                                  "processed before get a second Parquet file (duplicate rows): Delete their Parquet "
                                  "files for the backlog period first (AWS/Google console) if they were processed")
    amount = f"{size / 1e9:.2f} GB" if size >= 1e8 else f"{size / 1e6:.1f} MB"
    est = backlog_estimate(lk, size)
    issue("info", "files", f"selects {files:,} log files ({amount}) of {len(devices)} device(s): {est['text']}")
    if est["warning"]:
        issue("warning", "files", est["warning"])


def backlog_job_limit(lk: Lake) -> tuple[int | None, str]:
    """The backlog job's run-time limit in minutes and where it comes from (AWS: the Glue job's Timeout)."""
    cloud = lk.info["cloud"]
    if cloud == "aws":
        try:
            job = aws_client("glue").get_job(JobName=f"{lk.info['lake_id']}-process-backlog")["Job"]
            return job.get("Timeout") or AWS_MAX_TIMEOUT_MIN, "the Glue job's timeout"
        except Exception as e:  # noqa: BLE001 - no data lake / no glue:GetJob: assume the old stack default
            return 60, f"assumed (Glue job not readable: {e.__class__.__name__})"
    return JOB_LIMIT_MIN.get(cloud), "the backlog function's timeout"


def backlog_estimate(lk: Lake, size_bytes: int) -> dict:
    """Rough run time of a backlog of size_bytes vs the job's limit, with the advice when it will not fit."""
    gb = size_bytes / 1e9
    minutes = gb / BACKLOG_GB_PER_H * 60
    limit, source = backlog_job_limit(lk)
    dur = f"~{minutes / 60:.1f} h" if minutes >= 90 else f"~{max(1, round(minutes))} min"
    out = {"gb": round(gb, 2), "minutes": round(minutes), "limit_min": limit, "limit_source": source,
           "text": f"{dur} at ~{BACKLOG_GB_PER_H} GB/h (rough)" + (f", job limit {limit} min ({source})" if limit else ""),
           "warning": None}
    if limit and minutes > 0.8 * limit:
        per_run = 0.8 * limit / 60 * BACKLOG_GB_PER_H
        runs = -(-gb // per_run)
        advice = (f"Split it into ~{runs:.0f} runs of up to ~{per_run:.0f} GB (backlog --device/--start/--end per run, "
                  "push + start one at a time)")
        if lk.info["cloud"] == "aws":
            need = min(AWS_MAX_TIMEOUT_MIN, -(-minutes * 1.3 // 30) * 30)
            advice += (f", or start one run with a higher limit: lake_ops.py start --job backlog --timeout {need:.0f} "
                       "(or raise the job's timeout in the stack)")
        out["warning"] = f"{dur} probably exceeds the {limit} min backlog job limit: {advice}"
    return out


# ---------------------------------------------------------------- backlog builder
def build_backlog(a) -> dict:
    import s3_inventory as inv_mod
    from fetch_logs import parse_utc
    lk = Lake()
    start, end = parse_utc(a.start), parse_utc(a.end)
    entries, stats = [], {}
    for d in [x.upper() for x in a.device]:
        inv = inv_mod.update_inventory(lk.bucket, f"{d}/", log=lambda *_: None)
        logs = {k: o for k, o in inv["objects"].items() if o["kind"] == "log" and o["device"] == d}
        if not logs:
            raise SystemExit(f"ERROR: no log files for device {d} in {lk.bucket}")
        if start or end:
            inv_mod.head_objects(lk.bucket, inv, logs, max_heads=None if a.yes else inv_mod.HEAD_GUARD)
        sel, untimed = {}, 0
        for k, o in logs.items():
            t, _ = inv_mod.logging_time(o)
            if (start or end) and t is None:
                untimed += 1
                continue
            if (start and t < start) or (end and t >= end):
                continue
            sel[k] = o
        sessions = {}
        for k, o in logs.items():
            sessions.setdefault(o["session"], []).append(k)
        if len(sel) == len(logs):
            entries.append(f"{d}/")
        else:
            for s, keys in sorted(sessions.items()):
                chosen = sorted(k for k in keys if k in sel)
                if len(chosen) == len(keys):
                    entries.append(f"{d}/{s:08d}/")
                else:
                    entries += chosen
        stats[d] = {"files": len(sel), "of": len(logs), "gb": round(sum(o.get("size", 0) for o in sel.values()) / 1e9, 3),
                    "no_timestamp_skipped": untimed}
    data = {"config": {"batch_size": {"min": a.min, "max": a.max}}, "files": entries}
    out = Path(a.output or ADMIN_DIR / lk.bucket / "drafts" / "backlog.json")
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")
    est = backlog_estimate(lk, sum(s["gb"] for s in stats.values()) * 1e9)
    return {"file": out.as_posix(), "entries": len(entries), "devices": stats, "run time": est["text"],
            **({"warning": est["warning"]} if est["warning"] else {})}


# ---------------------------------------------------------------- pull / push / remove
def target_name(name: str) -> str:
    if name == "passwords.json":
        raise SystemExit("ERROR: passwords.json is never handled by this skill (it holds secrets): The user edits it")
    if "/" in name and not acf.dbc_entry_issue(name):
        return name                                   # a DBC file in one folder, e.g. dbc/can1-x.dbc (v4.5.0+)
    if "/" in name or "\\" in name or not (name in acf.CONTROL_FILES or acf.DBC_FILE_RE.match(name)):
        raise SystemExit(f"ERROR: {name} is not a control file or DBC file ({', '.join(acf.CONTROL_FILES)}, "
                         "can<n>-*.dbc, lin<n>-*.dbc in the root or one folder, e.g. dbc/can1-<name>.dbc)")
    return name


def folder_write_denied(e: Exception, name: str) -> None:
    """A DBC file in a folder that the programmatic credentials may not overwrite/delete: Stacks before the folder
    DBC update (AWS: Delete denied in folders; Google: overwrite = delete) - say so instead of a bare AccessDenied."""
    code = getattr(e, "response", {}).get("Error", {}).get("Code")
    if "/" in name and code in ("AccessDenied", "403"):
        raise SystemExit(f"ERROR: Access denied for {name}: The data lake's programmatic access permissions predate "
                         "DBC files in folders (update the stack/Terraform), or replace/delete the file in the "
                         "cloud console") from e


def get_root(lk: Lake, name: str) -> bytes | None:
    from botocore.exceptions import ClientError
    try:
        return lk.s3.get_object(Bucket=lk.bucket, Key=name)["Body"].read()
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") in ("404", "NoSuchKey"):
            return None
        raise


def backup(lk: Lake, name: str, old: bytes | None) -> str | None:
    if old is None:
        return None
    stamp = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    p = ADMIN_DIR / lk.bucket / "backups" / f"{stamp}-{name.replace('/', '_')}"
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(old)
    return p.as_posix()


def diff(old: bytes | None, new: bytes | None, name: str, max_lines: int = 80) -> list[str]:
    if name.endswith(".dbc"):
        return [f"(DBC file: {len(old or b''):,} -> {len(new or b''):,} bytes)"]
    a = (old or b"").decode("utf-8-sig", errors="replace").splitlines()
    b = (new or b"").decode("utf-8-sig", errors="replace").splitlines()
    lines = list(difflib.unified_diff(a, b, f"bucket/{name}", f"new/{name}", lineterm="", n=2))
    return lines[:max_lines] + ([f"... ({len(lines) - max_lines} more diff lines)"] if len(lines) > max_lines else [])


def push(a) -> dict:
    path = Path(a.file)
    name = target_name(a.as_name or path.name)
    if name.endswith(".dbc"):
        from dbc_info import load
        try:
            load(path)
        except Exception as e:  # noqa: BLE001
            raise SystemExit(f"ERROR: {path} does not load as a DBC ({e.__class__.__name__}) - run canedge-dbc "
                             "dbc_lint.py first")
    else:
        res = check(path, name, a.offline)
        errors = [i for i in res["issues"] if i["level"] == "error"]
        if errors:
            print_check(res)
            raise SystemExit(f"ERROR: {name} has {len(errors)} error(s) - not uploaded")
    lk = Lake()
    new, old = path.read_bytes(), get_root(lk, name)
    saved = backup(lk, name, old)
    try:
        lk.s3.put_object(Bucket=lk.bucket, Key=name, Body=new,
                         **({} if lk.info["cloud"] == "google" else {"ContentType": "application/json" if
                                                                      name.endswith(".json") else "text/plain"}))
    except Exception as e:  # noqa: BLE001 - explained for folder DBCs, re-raised otherwise
        folder_write_denied(e, name)
        raise
    size = lk.s3.head_object(Bucket=lk.bucket, Key=name)["ContentLength"]
    return {"uploaded": f"{lk.bucket}/{name}", "bytes": len(new), "verified": size == len(new),
            "backup": saved, "replaced": old is not None, "diff": diff(old, new, name)}


def remove(a) -> dict:
    name = target_name(a.name)
    lk = Lake()
    old = get_root(lk, name)
    if old is None:
        return {"removed": None, "note": f"{name} is not in {lk.bucket}"}
    saved = backup(lk, name, old)
    try:
        lk.s3.delete_object(Bucket=lk.bucket, Key=name)
    except Exception as e:  # noqa: BLE001 - explained for folder DBCs, re-raised otherwise
        folder_write_denied(e, name)
        raise
    return {"removed": f"{lk.bucket}/{name}", "backup": saved, "still_there": get_root(lk, name) is not None}


def pull(a) -> dict:
    name = target_name(a.name)
    lk = Lake()
    data = get_root(lk, name)
    p = ADMIN_DIR / lk.bucket / "drafts" / name
    if data is None:
        return {"file": None, "note": f"{name} is not in {lk.bucket} (the feature is off) - start from the Intro "
                                      "example"}
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_bytes(data)
    return {"file": p.as_posix(), "bytes": len(data)}


# ---------------------------------------------------------------- output
def print_check(res: dict) -> None:
    ctx = res.get("context", {})
    if ctx.get("bucket"):
        print(f"{res['name']}: checked against {ctx['bucket']} (automation v{ctx.get('automation_version') or '?'}, "
              f"root DBCs: {', '.join(ctx.get('root_dbcs') or []) or 'none'})")
    elif ctx.get("note"):
        print(f"{res['name']}: {ctx['note']}")
    order = {"error": 0, "warning": 1, "info": 2}
    for i in sorted(res["issues"], key=lambda i: order[i["level"]]):
        where = f"{i['path']}: " if i["path"] else ""
        print(f"  {i['level'].upper():8} {where}{i['message']}")
    n = sum(i["level"] == "error" for i in res["issues"])
    print(f"Result: {'INVALID' if n else 'OK'} ({n} error(s), "
          f"{sum(i['level'] == 'warning' for i in res['issues'])} warning(s))")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    common = argparse.ArgumentParser(add_help=False)   # --env/--json before or after the command
    common.add_argument("--env", default=argparse.SUPPRESS, help="Data lake profile (credentials/<name>.env)")
    common.add_argument("--json", action="store_true", default=argparse.SUPPRESS)
    ap.add_argument("--env", help="Data lake profile (credentials/<name>.env)")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check", parents=[common])
    c.add_argument("file")
    c.add_argument("--as", dest="as_name", help="Control file name if the file is named differently")
    c.add_argument("--offline", action="store_true")
    b = sub.add_parser("backlog", parents=[common])
    b.add_argument("--device", action="append", required=True)
    b.add_argument("--start")
    b.add_argument("--end")
    b.add_argument("--min", type=int, default=10)
    b.add_argument("--max", type=int, default=256)
    b.add_argument("-o", "--output")
    b.add_argument("--yes", action="store_true", help="Allow more than 50,000 HEAD requests (logging times)")
    for n in ("pull", "remove"):
        sub.add_parser(n, parents=[common]).add_argument("name")
    p = sub.add_parser("push", parents=[common])
    p.add_argument("file")
    p.add_argument("--as", dest="as_name")
    p.add_argument("--offline", action="store_true", help="Skip the bucket/lake cross-checks before uploading")
    a = ap.parse_args()
    use_env(a.env)

    if a.cmd == "check":
        res = check(Path(a.file), target_name(a.as_name or Path(a.file).name), a.offline)
        print(json.dumps(res, indent=1)) if a.json else print_check(res)
        return 1 if any(i["level"] == "error" for i in res["issues"]) else 0
    res = {"backlog": build_backlog, "pull": pull, "push": push, "remove": remove}[a.cmd](a)
    if a.json:
        print(json.dumps(res, indent=1, default=str))
        return 0
    for k, v in res.items():
        if k == "diff":
            print("Diff (bucket -> new):" if v else "Diff: (no change)")
            print("\n".join(f"  {line}" for line in v))
        elif k == "devices":
            for d, s in v.items():
                print(f"  {d}: {s['files']:,} of {s['of']:,} log files, {s['gb']} GB"
                      + (f", {s['no_timestamp_skipped']} without Timestamp skipped" if s["no_timestamp_skipped"] else ""))
        else:
            print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
