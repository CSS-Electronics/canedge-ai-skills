"""Check a CANedge device.json (the status file the device writes about itself) for common root causes. Read-only.

device.json tells what the device actually is and what it actually loaded: Device ID, firmware, the CRC32 of the
loaded Configuration File, SD card, GNSS/cellular module firmware, SIM, TLS certificates. It lives in the SD card
root and (CANedge2/3) in the S3 device folder: canedge-s3 `s3_object.py get <device>/device.json`.

Usage:
  python check_device_json.py <device.json> [--config config-01.09.json] [--expected-id 12345678] [--json]

  --config       The Configuration File the device should run: Its CRC32 vs the loaded cfg_crc32 ("did the device
                 load THIS config?"), and whether its S3 endpoint uses TLS (relevant for missing certificates)
  --expected-id  The ID the device.json should have (S3 folder name, MF4 header serial)

Warnings = likely root causes; notes = worth knowing. SIM ICCID and IMEI are masked in the output.
Exit code: 0 = no warnings, 1 = warnings, 2 = input error.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-config" / "scripts"))
from cfg_common import crc32_hex, load_config  # noqa: E402

VALID_SD_IDS = {"534133324780", "534130384780"}  # sd_info bytes 8-19 of the SD cards CSS Electronics ships
MIN_FW = (1, 8)          # older firmware: Recommend an update
EXACT_TIMESTAMP_FW = (1, 9, 1)  # S3 'Timestamp' metadata is the exact UTC file start from this firmware


def fw_tuple(fw: str) -> tuple[int, int, int]:
    nums = [int(x) for x in re.findall(r"\d+", fw or "")][:3]
    return tuple(nums + [0] * (3 - len(nums)))  # type: ignore[return-value]


def mask(v: str) -> str:
    v = str(v).strip()
    return "..." + v[-4:] if len(v) > 4 else v


def config_uses_tls(cfg: dict) -> tuple[bool | None, str]:
    """Does the config's S3 server use TLS? (None = cannot tell)."""
    server = ((cfg.get("connect") or {}).get("s3") or {}).get("server") or {}
    if not server:
        return None, "no connect.s3.server section (no S3 upload configured)"
    endpoint, port = str(server.get("endpoint", "")), server.get("port")
    if isinstance(server.get("tls"), int):
        return bool(server["tls"]), f"connect.s3.server.tls = {server['tls']}"
    if endpoint.startswith("https://"):
        return True, f"endpoint {endpoint}"
    if endpoint.startswith("http://"):
        return False, f"endpoint {endpoint}"
    if str(port) == "443":
        return True, "port 443"
    if str(port) in ("80", "9000"):
        return False, f"port {port}"
    return None, f"endpoint {endpoint!r}, port {port!r}"


def check(dev: dict, config: dict | None = None, config_raw: bytes | None = None, expected_id: str | None = None) -> dict:
    info, warnings, notes = {}, [], []

    dev_id = str(dev.get("id", "")).upper()
    info["Device ID"] = dev_id or "(missing)"
    if expected_id and dev_id != expected_id.upper():
        warnings.append(f"Device ID {dev_id} differs from the expected {expected_id.upper()}: The device.json and the "
                        f"files you compare it with come from different devices")
    for key, label in (("type", "Type"), ("hw_ver", "Hardware")):
        if dev.get(key):
            info[label] = dev[key]

    fw = dev.get("fw_ver", "")
    info["Firmware"] = fw or "(missing)"
    if fw and fw_tuple(fw)[:2] < MIN_FW:
        warnings.append(f"Firmware {fw} is older than 01.08: Update the firmware (many issues are fixed since)")
    elif fw and fw_tuple(fw) < EXACT_TIMESTAMP_FW:
        notes.append(f"Firmware {fw}: The S3 'Timestamp' metadata is approximate before 01.09.01 (time filters on S3)")

    crc = str(dev.get("cfg_crc32", "") or "").upper()
    for key, label in (("cfg_name", "Config file"), ("cfg_ver", "Config version")):
        if dev.get(key):
            info[label] = dev[key]
    info["Config CRC32 (loaded)"] = crc or "(missing)"
    if config_raw is not None:
        file_crc = crc32_hex(config_raw)
        info["Config CRC32 (--config)"] = file_crc
        if crc and file_crc != crc:
            warnings.append(f"The device runs another config ({crc}) than --config ({file_crc}): Not deployed yet, edited "
                            f"since, or rejected as invalid (the device then falls back to its default config). Check it "
                            f"with canedge-config check_config.py. The CRC32 covers the exact file bytes: Re-saving changes it")
        elif crc:
            info["Config match"] = "the device loaded this config"

    sd = str(dev.get("sd_info", "") or "")
    if not sd:
        notes.append("sd_info is blank: No SD card reported")
    elif sd[8:20] in VALID_SD_IDS:
        info["SD card"] = "CSS Electronics card"
    else:
        warnings.append(f"SD card not recognised as a CSS Electronics card (sd_info bytes 8-19 '{sd[8:20]}'): Other "
                        f"cards can cause logging, storage, upload and firmware update problems. Use a CSS SD card")
    if dev.get("sd_used_lifespan") not in (None, ""):
        info["SD wear"] = f"{dev['sd_used_lifespan']} %"
    used, _, total = str(dev.get("space_used_mb", "")).partition("/")
    if total.strip().isdigit() and used.strip().isdigit() and int(total):
        info["Storage"] = f"{used.strip()} / {total.strip()} MB ({100 * int(used) / int(total):.1f} %)"

    for key, label, which in (("gnss_fw_ver", "GNSS module FW", "GNSS"), ("cellular_fw_ver", "Cellular module FW", "cellular (LTE)")):
        if key in dev:  # key absent = the variant has no such module
            if str(dev[key] or "").strip():
                info[label] = dev[key]
            else:
                warnings.append(f"{key} is blank: The {which} module probably failed to update during the last firmware "
                                f"update. Re-run the firmware update")

    if "cellular_iccid" in dev:
        if str(dev["cellular_iccid"] or "").strip():
            info["SIM ICCID"] = mask(dev["cellular_iccid"])
        else:
            warnings.append("cellular_iccid is blank: The device cannot read the SIM card (or none is inserted). A likely "
                            "root cause of CANedge3 upload problems")
    if dev.get("cellular_imei"):
        info["IMEI"] = mask(dev["cellular_imei"])

    certs = dev.get("certs_server_md5")
    if certs is not None:
        n = len(certs) if isinstance(certs, list) else 0
        info["TLS server certificates"] = n
        tls, detail = config_uses_tls(config) if config is not None else (None, "")
        if n == 0 and tls:
            warnings.append(f"No TLS server certificate loaded, but the config uses TLS ({detail}): A likely root cause "
                            f"of failed uploads. Add the server certificate (CANedge Intro: Cloud server TLS)")
        elif n == 0 and tls is None:
            notes.append("No TLS server certificate loaded: Fine without TLS. With an https:// S3 endpoint it blocks "
                         "uploads (pass --config to check)")

    if dev.get("reset_cause"):
        notes.append(f"Last reset cause: {dev['reset_cause']}")
    if dev.get("log_meta"):
        info["Meta name"] = dev["log_meta"]
    return {"info": info, "warnings": warnings, "notes": notes}


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("device_json", type=Path)
    ap.add_argument("--config", type=Path, help="The Configuration File the device should run")
    ap.add_argument("--expected-id", help="Expected device ID (S3 folder, MF4 header serial)")
    ap.add_argument("--json", action="store_true", help="Print JSON")
    a = ap.parse_args()
    try:
        dev = json.loads(a.device_json.read_text(encoding="utf-8-sig"))
        config, raw = load_config(a.config) if a.config else (None, None)
    except (OSError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 2
    r = check(dev, config, raw, a.expected_id)
    if a.json:
        print(json.dumps(r, indent=1))
    else:
        print(f"device.json {a.device_json}")
        for k, v in r["info"].items():
            print(f"  {k}: {v}")
        for w in r["warnings"]:
            print(f"! {w}")
        for n in r["notes"]:
            print(f"- {n}")
        print("Result: " + (f"{len(r['warnings'])} warning(s)" if r["warnings"] else "no warnings"))
    return 1 if r["warnings"] else 0


if __name__ == "__main__":
    sys.exit(main())
