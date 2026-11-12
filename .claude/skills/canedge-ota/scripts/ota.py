r"""Over-the-air firmware + configuration updates of CANedge2/3 devices via their S3 bucket (canedge-ota).

  status  [--devices ID ...] [--json]      Per device: Type, firmware, config + whether the device loaded it, last
                                           heartbeat, meta, encrypted credential sections, pending firmware.bin
  fw list --type "CANedge2"                Official firmware packages (from the bundled CANedge Docs)
  fw get  --type "CANedge2" --version V    Download one (data/firmware/, MD5 checked) and show its header
  fw info FILE                             Type, version, MAJOR.MINOR of a firmware zip/bin
  plan    --project P (--devices ID ... | --all | --table T) [change inputs] [--fw FILE ... | --fw-version V]
                                           Plan per device (nothing is written to S3): Gates, config migration,
                                           changes, encryption, validation, masked diff. Writes
                                           P/ota/<utc>-<label>/plan.json + plan.md + the exact files to upload
  apply   --plan DIR [--devices ID ...] [--limit N]
                                           WRITES TO S3 (only after the user OKed the plan; the harness asks):
                                           Per device re-check (device.json, CRC drift, pending firmware), then
                                           <dev>/config-VV.VV.json before <dev>/firmware.bin. Never deletes
  verify  --plan DIR [--target-env NAME]   Did the devices apply it (device.json config CRC / firmware version)?
                                           --target-env: The profile of the new bucket after an S3 server change

Change inputs (combinable, applied in this order): --config FILE [--keep PATH ...] (a prepared config; the keep
paths come from each device: default general.device.meta, general.security, log.encryption, connect),
--partial FILE (deep merge, arrays replaced), --set PATH=VALUE (repeatable), --table CSV (repeatable; first column
the device ID, other columns JSON paths, e.g. a secrets.csv the user fills in), --change-func FILE.py
(canedge_manager signature + optional row=), --encrypt (encrypt all credential sections). Paths: a.b.c, a.list[0].b

Options: --env NAME (data lake profile of the bucket), --bucket NAME (another bucket with the same keys), --json
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import ota_lib as ol  # noqa: E402


def out(obj, as_json: bool, text) -> None:
    if as_json:
        print(json.dumps(obj, indent=2, default=str))
    else:
        text()


def cmd_status(a) -> int:
    b = ol.Bucket(a.env, a.bucket)
    rows = ol.fleet_status(b, a.devices)

    def text():
        print(f"Bucket {b.name}: {len(rows)} device(s)")
        print(f"{'Device':9} {'Type':14} {'FW':9} {'Config':18} {'Synced':6} {'HB days':7} {'Enc':12} Meta / notes")
        for r in rows:
            notes = ([r["meta"]] if r["meta"] else []) + (["firmware.bin pending"] if r["firmware_pending"] else []) \
                + ([f"also {', '.join(r['other_configs'])}"] if r["other_configs"] else []) + r["blocked"]
            sync = {True: "yes", False: "NO", None: "?"}[r["cfg_synced"]]
            print(f"{r['device']:9} {str(r['type'] or '?'):14} {str(r['fw_ver'] or '?'):9} "
                  f"{str(r['cfg_name'] or '?'):18} {sync:6} {str(r['heartbeat_days'] or '?'):7} "
                  f"{','.join(r['encrypted'] or []) or '-':12} {' | '.join(notes)}")
    out(rows, a.json, text)
    return 0


def cmd_fw(a) -> int:
    if a.action == "list":
        rows = ol.fw_catalog(a.type)
        out(rows, a.json, lambda: [print(f"{r['version']}  {r['url']}") for r in rows] or
            print(f"No packages for {a.type!r} (types: {', '.join(ol.DOC_FILES)})"))
        return 0 if rows else 1
    path = ol.fw_get(a.type, a.version) if a.action == "get" else Path(a.file)
    fw = ol.fw_load(path)
    info = {k: fw[k] for k in ("source", "path", "type_name", "version", "rev", "bytes", "sha256")}
    out(info, a.json, lambda: [print(f"{k}: {v}") for k, v in info.items()])
    return 0


def cmd_plan(a) -> int:
    b = ol.Bucket(a.env, a.bucket)
    ch = ol.Changes(a.set, a.partial, a.table, a.config, a.keep, a.change_func, a.encrypt)
    if a.all:
        devices = b.devices()
    elif a.devices:
        devices = [d.upper() for d in a.devices]
    elif ch.table:
        present = set(b.devices())
        devices = sorted(d for d in ch.table if d in present)
        other = len(ch.table) - len(devices)
        if other:
            print(f"{other} table device(s) not in {b.name}: Not planned here", file=sys.stderr)
    else:
        raise SystemExit("ERROR: Select devices: --devices ID ..., --all or a --table")
    if ch.empty() and not (a.fw or a.fw_version):
        raise SystemExit("ERROR: Nothing to do: Give a change input and/or --fw / --fw-version")
    fws = {}
    if a.fw or a.fw_version:
        types = {r["type"] for r in ol.fleet_status(b, devices) if r["type"] in ol.DOC_FILES} if a.fw_version \
            else set()
        fws = ol.resolve_firmware(a.fw, a.fw_version, types)
    project = Path(a.project)
    if not (project / "project.json").is_file():
        raise SystemExit(f"ERROR: {project} is not a project folder (create one with canedge-workspace "
                         "new_project.py)")
    print(f"Planning {len(devices)} device(s) in {b.name} ...", file=sys.stderr)
    pdir = ol.make_plan(b, devices, ch, fws, project / "ota", a.label or "", not a.no_target_check,
                        log=lambda m: print(m, file=sys.stderr))
    plan = ol.load_plan(pdir)

    def text():
        print((pdir / "plan.md").read_text(encoding="utf-8"))
        print(f"PLAN {pdir.as_posix()}")
    out({"plan": pdir.as_posix(), "summary": plan["summary"],
         "devices": [{k: e[k] for k in ("device", "status", "blocks", "warnings", "notes")} for e in plan["devices"]]},
        a.json, text)
    return 0 if plan["summary"]["blocked"] == 0 else 1


def cmd_apply(a) -> int:
    plan = ol.load_plan(Path(a.plan))
    b = ol.Bucket(a.env or plan.get("env"), a.bucket or plan["bucket"])
    res = ol.apply_plan(Path(a.plan), b, [d.upper() for d in a.devices or []], a.limit)
    n = {k: sum(r["result"] == k for r in res) for k in ("applied", "skipped", "failed")}
    left = sum(e["status"] == "write" for e in plan["devices"]) - len(ol.applied_log(Path(a.plan)))
    out({"results": res, "counts": n, "remaining": left}, a.json,
        lambda: print(f"Applied {n['applied']}, skipped {n['skipped']}, failed {n['failed']}; {left} device(s) of "
                      f"the plan not applied yet. Next: verify --plan {a.plan} after the devices' next heartbeat"))
    return 0 if not n["failed"] and not n["skipped"] else 1


def cmd_verify(a) -> int:
    plan = ol.load_plan(Path(a.plan))
    b = ol.Bucket(a.env or plan.get("env"), a.bucket or plan["bucket"])
    target = ol.Bucket(a.target_env) if a.target_env else None
    rows = ol.verify_plan(Path(a.plan), b, target)

    def text():
        for r in rows:
            print(f"{r['device']}  {r['state']:16} {r['detail']}")
        print("Summary: " + ", ".join(f"{s} {sum(r['state'] == s for r in rows)}"
                                      for s in sorted({r['state'] for r in rows})))
    out(rows, a.json, text)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--env", help="data lake profile (credentials/<name>.env) of the bucket")
    ap.add_argument("--bucket", help="another bucket than the profile's input bucket (same keys)")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("status")
    s.add_argument("--devices", nargs="+")
    f = sub.add_parser("fw")
    f.add_argument("action", choices=["list", "get", "info"])
    f.add_argument("file", nargs="?")
    f.add_argument("--type", help='e.g. "CANedge2", "CANedge3 GNSS"')
    f.add_argument("--version")
    p = sub.add_parser("plan")
    p.add_argument("--project", required=True, help="projects/<yyyy-mm-dd>-<slug>")
    p.add_argument("--devices", nargs="+")
    p.add_argument("--all", action="store_true", help="all device folders in the bucket")
    p.add_argument("--fw", nargs="+", help="firmware zip/bin file(s), one per device type")
    p.add_argument("--fw-version", help="official firmware version for every device type in the selection")
    p.add_argument("--config", help="prepared config (canedge-config) for all devices")
    p.add_argument("--keep", nargs="+", help=f"paths taken from each device with --config (default: "
                                             f"{' '.join(ol.DEFAULT_KEEP)})")
    p.add_argument("--partial")
    p.add_argument("--set", action="append", metavar="PATH=VALUE")
    p.add_argument("--table", action="append", metavar="CSV")
    p.add_argument("--change-func")
    p.add_argument("--encrypt", action="store_true", help="encrypt all credential sections (plaintext needed)")
    p.add_argument("--label", help="short slug for the plan folder")
    p.add_argument("--no-target-check", action="store_true", help="skip the test read on a new S3 server")
    x = sub.add_parser("apply")
    x.add_argument("--plan", required=True)
    x.add_argument("--devices", nargs="+")
    x.add_argument("--limit", type=int, help="first N devices not applied yet (canary: 1)")
    v = sub.add_parser("verify")
    v.add_argument("--plan", required=True)
    v.add_argument("--target-env", help="profile of the new bucket (after an S3 server change)")
    a = ap.parse_args()
    if a.cmd == "fw" and ((a.action == "list" and not a.type) or (a.action == "get" and not (a.type and a.version))
                          or (a.action == "info" and not a.file)):
        ap.error("fw list --type T | fw get --type T --version V | fw info FILE")
    try:
        return {"status": cmd_status, "fw": cmd_fw, "plan": cmd_plan, "apply": cmd_apply,
                "verify": cmd_verify}[a.cmd](a)
    except ol.OtaError as e:
        raise SystemExit(f"ERROR: {e}") from None


if __name__ == "__main__":
    sys.exit(main())
