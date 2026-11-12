"""Check a CANedge or CANmod.router Configuration File: Compact summary (secrets masked) + validation. Run it on
every new file.

Summary: Device type, config version, CRC32, log settings, per channel (CAN INTERNAL, CAN1, CAN2) rx/tx state,
mode, bit-rate, the full filter table (index, state, accept/reject, 11/29-bit, range/mask, f1/f2, prescaler),
transmit lists and control signals; LIN, routing, connectivity and GNSS in one line each.

Validation layers (all automatic, see references/warning-checks.md for every check by ID):
  1. JSON syntax      - reports line/column on failure
  2. JSON Schema      - against assets/schema-files/<DeviceType>/schema-<VV.VV>.json (version from
                        the file name config-VV.VV.json, which must match the device firmware)
  3. Device errors    - config editor checks for rules the device enforces but the schema cannot
                        express (period <= delay, transmit/filter limits, split offset > period):
                        The device rejects the config
  4. Warnings         - the other config editor checks (runConfigurationWarningChecks, ported 1:1)
                        + this skill's filter/control checks: Shadowed filters, empty/invalid ranges,
                        11-bit values above 7FF, control signal issues, file name. (The 100 prescaled
                        IDs per channel limit depends on the traffic: simulate_config.py checks it)

Each error/warning is printed with its check ID in brackets, e.g. "! [file_split_value] ...".

CANmod.router configs (phy.can.route): Schema (assets/schema-files/CANmod.router) + router checks (router_*):
Shadowed / disabled S1-S4 filters, transmit lists, direct mode, tunnel ID collisions (s2p = a p2s ID), tunnel IDs that
need an edited router DBC. Pairing (--pair <the other config>: CANedge + router, either way round; or --project for
the tunnel IDs of the project's router DBC): The CANedge channel must accept the tunnel ID without prescaler, its
bit-rate must match the router's primary bus (1M/4M by default; not auto-detect) and it must not be in Monitoring mode.

Usage:
  .venv\\Scripts\\python.exe .claude/skills/canedge-config/scripts/check_config.py <config.json> [--no-summary] [--json]
                                                                  [--pair <other config.json>] [--project <p>]

Exit code 0 = deployable (warnings allowed: Review them with the user), 1 = invalid JSON, schema errors or
device errors (do not deploy). Read-only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from cfg_common import (  # noqa: E402
    FILTER_TYPE_NAMES, METHOD_NAMES, SCHEMA_ROOT, available_versions, crc32_hex, describe_filter,
    detect_device_type, enabled_filters, extract_version, first_match, g, has, hexint, mask_secrets, prescaler_text,
    CHANNEL_TO_SECTION, FRAME_FORMATS, ROUTER_DEFAULT_CHANNELS, ROUTER_SECTIONS, is_router, router_filters,
    router_tunnel,
)


# --------------------------------------------------------------------------- #
# Editor warning checks (port of config-editor-base actions.js runConfigurationWarningChecks)
# Each returns a list[str]. Messages kept identical to the editor.
# --------------------------------------------------------------------------- #

def chk_transmit_period_delay(c):
    out = []
    for i in (1, 2):
        tx = g(c, f"can_{i}", "transmit")
        if isinstance(tx, list):
            bad = [e for e in tx if isinstance(e, dict)
                   and (e.get("period", 0) or 0) > 0
                   and (e.get("period", 0) or 0) <= (e.get("delay", 0) or 0)]
            if bad:
                out.append(f"Your CAN CH{i} transmit list includes one or more entries with "
                           "period <= delay. This is invalid and will cause the device to reject "
                           "your Configuration File")
    return out


def chk_transmit_monitoring(c):
    out = []
    for i in (1, 2):
        tx = g(c, f"can_{i}", "transmit")
        mode = g(c, f"can_{i}", "phy", "mode")
        if isinstance(tx, list) and mode is not None and len(tx) > 0:
            if mode != 0:
                out.append(f"Your CAN CH{i} has a non-empty transmit list, but the device will "
                           "not transmit any messages unless the mode is set to Normal")
    return out


def chk_transmit_monitoring_routing(c):
    out = []
    routing = c.get("routing")
    if isinstance(routing, list) and len(routing) > 0:
        for route in routing:
            if not isinstance(route, dict):
                continue
            if route.get("state") == 1 and route.get("chn_dst") in (1, 2):
                mode = g(c, f"can_{route.get('chn_dst')}", "phy", "mode")
                if mode is not None and mode != 0:
                    out.append(f"You are routing a message onto CAN CH{route.get('chn_dst')}, but "
                               f"the device will not route any messages unless the CAN "
                               f"CH{route.get('chn_dst')} mode is set to Normal.")
    return out


def chk_transmit_exceed_limits(c):
    MAX_MESSAGES = 224
    MAX_BYTES = 4096
    transmit_list = []
    for i in (1, 2):
        tx = g(c, f"can_{i}", "transmit")
        if isinstance(tx, list):
            transmit_list += tx
    message_count = len(transmit_list)
    byte_count = 0
    for m in transmit_list:
        data = m.get("data") if isinstance(m, dict) else None
        if isinstance(data, str):
            byte_count += len(data) / 2
    if message_count > MAX_MESSAGES or byte_count > MAX_BYTES:
        return [f"Your CAN transmit list(s) exceed the limits: {message_count} messages "
                f"(max 224) and {int(byte_count)} bytes (max 4096). This is invalid and will "
                "cause the device to reject your Configuration File"]
    return []


def chk_filters_disabled(c):
    out = []
    for i in (1, 2):
        ids = g(c, f"can_{i}", "filter", "id")
        if isinstance(ids, list):
            enabled = [e for e in ids if isinstance(e, dict) and e.get("state") == 1]
            if len(enabled) == 0:
                out.append(f"Your CAN CH{i} filter list contains only disabled filters - no data "
                           "will be recorded on this channel")
    return out


def chk_filter_limits(c):
    MAX_11BIT = 128
    MAX_29BIT = 64
    out = []
    for i in (1, 2):
        ids = g(c, f"can_{i}", "filter", "id")
        if isinstance(ids, list):
            count11 = sum(1 for f in ids if isinstance(f, dict) and f.get("id_format") != 1)
            count29 = sum(1 for f in ids if isinstance(f, dict) and f.get("id_format") == 1)
            if count11 > MAX_11BIT:
                out.append(f"Your CAN CH{i} has {count11} 11-bit filters - the max is "
                           f"{MAX_11BIT}. This is invalid and will cause the device to reject "
                           "your Configuration File")
            if count29 > MAX_29BIT:
                out.append(f"Your CAN CH{i} has {count29} 29-bit filters - the max is "
                           f"{MAX_29BIT}. This is invalid and will cause the device to reject "
                           "your Configuration File")
    return out


def chk_config_tls(c):
    endpoint = g(c, "connect", "s3", "server", "endpoint")
    port = g(c, "connect", "s3", "server", "port")
    if isinstance(endpoint, str) and port is not None:
        if "https://" in endpoint and port == 80:
            return ["Your S3 server endpoint uses TLS (https://), but your port is 80. This is "
                    "most likely incorrect and may result in the device being unable to connect. "
                    "Please review the documentation on how to enable TLS"]
    return []


def chk_aws_endpoint(c):
    regex = re.compile(r"^https?://s3\.[a-z]{2}-[a-z]+-\d{1}\.amazonaws\.com$")
    endpoint = g(c, "connect", "s3", "server", "endpoint")
    if isinstance(endpoint, str):
        if "amazonaws" in endpoint and not regex.search(endpoint):
            return ["Your S3 endpoint seems to be for AWS, but with incorrect syntax. Please "
                    "review the documentation on how to configure the device for AWS S3 endpoints"]
    return []


def chk_s3_encrypted_no_kpub(c):
    keyformat = g(c, "connect", "s3", "server", "keyformat")
    secretkey = g(c, "connect", "s3", "server", "secretkey")
    security = g(c, "general", "security")
    if secretkey is not None and keyformat is not None and security is not None:
        kpub = g(c, "general", "security", "kpub")
        if keyformat == 1 and (kpub is None or kpub == ""):
            return ["Your S3 SecretKey format is set to Encrypted, but you have not provided the "
                    "Server public key. Please review the documentation on how to encrypt passwords"]
    return []


def chk_wifi_encrypted_no_kpub(c):
    keyformat = g(c, "connect", "wifi", "keyformat")
    security = g(c, "general", "security")
    if keyformat is not None and security is not None:
        kpub = g(c, "general", "security", "kpub")
        if keyformat == 1 and (kpub is None or kpub == ""):
            return ["Your WiFi Key format is set to Encrypted, but you have not provided the "
                    "Server public key. Please review the documentation on how to encrypt passwords"]
    return []


def chk_file_split_offset_period(c):
    period = g(c, "log", "file", "split_time_period")
    offset = g(c, "log", "file", "split_time_offset")
    if period is not None and offset is not None and offset > period:
        return ["Your log file split time offset is set larger than your file split time period. "
                "This is invalid and will cause the device to reject the Configuration File"]
    return []


def chk_rtc_adjustment(c):
    adj = g(c, "rtc", "adjustment")
    if has(c, "connect") and adj is not None and adj > 400:
        return ["Your RTC Adjustment value exceeds 400 seconds (this field is intended for small "
                "drift corrections, not e.g. time zone adjustments). Large values may cause issues "
                "with S3 connectivity"]
    return []


def chk_config_tls_port(c):
    endpoint = g(c, "connect", "s3", "server", "endpoint")
    port = g(c, "connect", "s3", "server", "port")
    if isinstance(endpoint, str) and port is not None:
        if "http://" in endpoint and port == 443:
            return ["Your S3 server endpoint does not use TLS (http://), but your port is 443. "
                    "This is most likely incorrect and may result in the device being unable to "
                    "connect. Please review the documentation on how to enable TLS"]
    return []


def chk_file_split_value(c):
    period = g(c, "log", "file", "split_time_period")
    if period is not None and 0 < period < 60:
        return [f"Your log files are currently set to split every {period} seconds. This increases "
                "the storage used (due to overhead) and reduces data transfer/processing "
                "performance. Consider increasing your split time"]
    return []


def chk_missing_apn(c):
    endpoint = g(c, "connect", "s3", "server", "endpoint")
    apn = g(c, "connect", "cellular", "apn")
    if isinstance(endpoint, str) and apn is not None:
        if "http" in endpoint and apn == "":
            return ["Your SIM APN is blank. This is most likely incorrect and may result in the "
                    "device being unable to connect. Please add the APN if it exists"]
    return []


def chk_missing_wifi(c):
    endpoint = g(c, "connect", "s3", "server", "endpoint")
    ap = g(c, "connect", "wifi", "accesspoint")
    if isinstance(endpoint, str) and isinstance(ap, list):
        if len(ap) == 0 and "http" in endpoint:
            return ["Your S3 details appear populated, but you have added no WiFi details. Your "
                    "device will therefore be unable to connect to your server."]
    return []


def chk_missing_s3_if_wifi(c):
    ap = g(c, "connect", "wifi", "accesspoint")
    protocol = g(c, "connect", "protocol")
    endpoint = g(c, "connect", "s3", "server", "endpoint")
    if isinstance(ap, list) and g(c, "connect", "s3", "server") is not None:
        no_endpoint = (isinstance(endpoint, str) and "http" not in endpoint) or endpoint is None
        if len(ap) > 0 and protocol == 0 and no_endpoint:
            return ["Your have added WiFi details, but no S3 endpoint is added. Your device will "
                    "therefore be unable to connect to your server."]
    return []


def chk_incorrect_apn_telekom(c):
    endpoint = g(c, "connect", "s3", "server", "endpoint")
    apn = g(c, "connect", "cellular", "apn")
    if isinstance(endpoint, str) and apn is not None:
        if "http" in endpoint and apn == "internet.v6.telekom":
            return ["Your SIM APN is set to 'internet.v6.telekom', which is not compatible with "
                    "the device - please set it to 'internet.telekom' instead"]
    return []


def chk_incorrect_apn_supersim(c):
    region = g(c, "connect", "s3", "server", "region")
    apn = g(c, "connect", "cellular", "apn")
    if isinstance(region, str) and apn is not None:
        if "eu" in region and apn == "super":
            return ["Your Super SIM APN is set to 'super', but your S3 region appears to be in EU "
                    "- please set your APN to 'de1.super' for optimal speed"]
    return []


def chk_incorrect_roaming_supersim(c):
    endpoint = g(c, "connect", "s3", "server", "endpoint")
    roaming = g(c, "connect", "cellular", "roaming")
    apn = g(c, "connect", "cellular", "apn")
    if isinstance(endpoint, str) and roaming is not None and apn is not None:
        if "http" in endpoint and roaming == 0 and apn in ("super", "de1.super", "sg.super"):
            return ["Roaming must be enabled when using a Super SIM"]
    return []


def chk_incorrect_apn_spaces(c):
    endpoint = g(c, "connect", "s3", "server", "endpoint")
    apn = g(c, "connect", "cellular", "apn")
    if isinstance(endpoint, str) and isinstance(apn, str):
        if "http" in endpoint and (apn.startswith(" ") or apn.endswith(" ")):
            return ["Your APN starts/ends with spaces - please review, as this is most likely "
                    "not correct"]
    return []


def chk_gnss_estimate(c):
    method = g(c, "gnss", "alignment", "method")
    if method == 1:
        return ["Your GNSS IMU-mount alignment is set to 'Estimate' - note that the device will "
                "not record any GPS/IMU data in this mode"]
    return []


def chk_control_signal_zero(c):
    out = []
    channels = ["can_1", "can_2", "can_internal"]
    names = ["CAN CH1", "CAN CH2", "CAN INTERNAL"]
    for i, ch in enumerate(channels):
        control = g(c, ch, "control")
        if not isinstance(control, dict):
            continue
        rx = control.get("control_rx_state")
        tx = control.get("control_tx_state")
        if rx is None or tx is None:
            continue
        if rx != 0 or tx != 0:
            for typ in ("start", "stop"):
                sig = g(control, typ, "signal")
                if not isinstance(sig, dict):
                    continue
                if sig.get("factor") is None or sig.get("length") is None:
                    continue
                for field, fname in (("factor", "signal scaling factor"),
                                     ("length", "signal bit length")):
                    if sig.get(field) == 0:
                        out.append(f"You have enabled a Control Signal on {names[i]} ({typ}) with "
                                   f"a {fname} of 0 - this will result in a constant signal value "
                                   "and is most likely not intended.")
    return out


# The editor shows these as warnings, but the device rejects the config: Cross-field rules, byte totals and
# per-format filter counts the schema cannot express. Reported as errors (exit 1)
REJECT_CHECKS = [
    chk_transmit_period_delay, chk_transmit_exceed_limits, chk_filter_limits, chk_file_split_offset_period,
]

EDITOR_WARNING_CHECKS = [
    chk_transmit_monitoring, chk_transmit_monitoring_routing, chk_filters_disabled, chk_config_tls,
    chk_aws_endpoint, chk_s3_encrypted_no_kpub, chk_wifi_encrypted_no_kpub, chk_rtc_adjustment,
    chk_config_tls_port, chk_file_split_value, chk_missing_apn, chk_missing_wifi, chk_missing_s3_if_wifi,
    chk_incorrect_apn_telekom, chk_incorrect_apn_supersim, chk_incorrect_apn_spaces, chk_gnss_estimate,
    chk_incorrect_roaming_supersim, chk_control_signal_zero,
]


# --------------------------------------------------------------------------- #
# Extra filter checks (this skill)
# --------------------------------------------------------------------------- #

def _match_set(f: dict) -> tuple:
    """ID set of a filter: ('range', lo, hi, full) or ('mask', id, mask, full)."""
    full = 0x1FFFFFFF if f.get("id_format") == 1 else 0x7FF
    f1, f2 = hexint(f.get("f1", "0")), hexint(f.get("f2", "0"))
    if f.get("method", 0) == 0:
        return ("range", f1, f2, full)
    return ("mask", f1 & f2 & full, f2 & full, full)


def _set_bounds(s: tuple) -> tuple[int, int]:
    if s[0] == "range":
        return s[1], s[2]
    _, fid, mask, full = s
    return fid, fid | (~mask & full)


def _set_size(s: tuple) -> int:
    if s[0] == "range":
        return max(0, s[2] - s[1] + 1)
    _, _, mask, full = s
    return 1 << bin(~mask & full).count("1")


def _covers(a: tuple, b: tuple) -> bool:
    """True if every ID matched by b is also matched by a."""
    if _set_size(b) == 0:
        return True
    if a[0] == "range":
        lo, hi = _set_bounds(b)
        return a[1] <= lo and hi <= a[2]
    _, aid, amask, full = a
    if b[0] == "mask":
        _, bid, bmask, _ = b
        return (amask & ~bmask & full) == 0 and (bid & amask) == aid
    if _set_size(b) > 4096:  # a is a mask, b a range: only enumerate small ranges
        return False
    return all((i & amask) == aid for i in range(b[1], b[2] + 1))


def chk_filter_details(c: dict, filename: str) -> list[tuple[str, str]]:
    """(check ID, message) per finding."""
    out = []
    if extract_version(filename) is None:
        out.append(("file_name", "The file name has no firmware version. Name it config-VV.VV.json matching the device "
                   "firmware (e.g. config-01.09.json) when deploying, otherwise the device will not load it"))
    elif filename != f"config-{extract_version(filename)}.json":
        out.append(("file_name", f"The file name is {filename}: The device only loads config-{extract_version(filename)}.json. Keep "
                   "variants in folders (e.g. optimized/config-01.09.json) instead of renamed files"))
    for sec, label in (("can_internal", "CAN INTERNAL"), ("can_1", "CAN CH1"), ("can_2", "CAN CH2")):
        flt = enabled_filters(c, sec) or []
        sets = []
        for f in flt:
            s = _match_set(f)
            fmt = f.get("id_format", 0)
            if f.get("method", 0) == 0 and s[1] > s[2]:
                out.append(("filter_empty_range", f"{label} filter {describe_filter(f)}: Range start > end, the filter matches nothing"))
            if fmt == 0 and (hexint(f.get("f1", "0")) > 0x7FF or hexint(f.get("f2", "0")) > 0x7FF):
                out.append(("filter_11bit_value", f"{label} filter {describe_filter(f)}: 11-bit filter with a value above 7FF"))
            for prev, ps in sets:
                if prev.get("id_format", 0) == fmt and _covers(ps, s):
                    out.append(("filter_shadowed", f"{label} filter {describe_filter(f)} is never reached: Earlier filter "
                               f"{describe_filter(prev)} already matches all its IDs (first match wins)"))
                    break
            sets.append((f, s))
        ctrl = g(c, sec, "control")
        if not isinstance(ctrl, dict):
            continue
        if ctrl.get("control_rx_state") == 1 and g(c, sec, "general", "rx_state") == 1:
            out.append(("control_initial_rx_state", f"{label}: The control signal controls reception, but the initial rx_state is Enable, so "
                       "logging is on from power-up until the first stop trigger (check that this is intended)"))
        start, stop = ctrl.get("start") or {}, ctrl.get("stop") or {}
        if ctrl.get("control_rx_state") != 1 and ctrl.get("control_tx_state") != 1:
            continue
        if start.get("message") == stop.get("message") and start.get("signal") == stop.get("signal"):
            lo1, hi1 = start.get("trigger_low", 0), start.get("trigger_high", 0)
            lo2, hi2 = stop.get("trigger_low", 0), stop.get("trigger_high", 0)
            nested = (lo2 <= lo1 and hi1 <= hi2) or (lo1 <= lo2 and hi2 <= hi1)  # Docs: inner range takes precedence
            if max(lo1, lo2) <= min(hi1, hi2) and not nested:
                out.append(("control_range_overlap", f"{label}: Control signal start range [{lo1}, {hi1}] and stop range [{lo2}, {hi2}] partially "
                           "overlap on the same signal: The device behaviour for values in both is not defined"))
        # The control signal only uses frames accepted by the filters of the source channel
        msgs = [start.get("message") or {}] + ([stop.get("message") or {}] if stop.get("message") != start.get("message") else [])
        for m in msgs:
            src = {0: "can_internal", 1: "can_1", 2: "can_2"}.get(m.get("chn", 0))
            flt = enabled_filters(c, src) if src in c else None
            hit = first_match(flt, hexint(m.get("id", "0")), int(m.get("id_format", 0))) if flt else None
            if flt is not None and (hit is None or hit.get("type", 0) == 1):
                out.append(("control_message_rejected", f"{label}: Control message 0x{str(m.get('id', '')).upper()} is not accepted by the filters of "
                           f"{src}, so the control signal never triggers"))
    return out


# --------------------------------------------------------------------------- #
# CANmod.router checks (CANmod.router Docs: filters, transmit, routing; CANedge Intro: CANedge + CANmod.router)
# --------------------------------------------------------------------------- #

def router_filter_text(f: dict) -> str:
    fmt = "29-bit" if f.get("id_format") == 1 else "11-bit"
    s = (f"#{f.get('_index', '?')} {fmt} {FRAME_FORMATS.get(f.get('frame_format', 2), '?')} "
         f"{str(f.get('f1', '')).upper()} mask {str(f.get('f2', '')).upper()}")
    if f.get("prescaler_type", 0):
        s += f" [{prescaler_text(f)}]"
    return s + (f" ({f['name']})" if f.get("name") else "")


def router_checks(c: dict) -> tuple[list, list, list]:
    """(errors, warnings, notes) of a CANmod.router config; errors/warnings as (check ID, message)."""
    errors, warnings, notes = [], [], []
    for sec in ROUTER_SECTIONS:
        s = sec.replace("can_s", "S")
        flt = router_filters(c, sec)
        if flt == []:
            warnings.append(("router_filters_disabled", f"{s}: No enabled filter, so the router forwards nothing from "
                             f"{s} (enable a filter, e.g. accept all: f1 7FF mask 0)"))
        seen = []
        for f in flt or []:
            ff = f.get("frame_format", 2)
            for prev, ps in seen:
                pff = prev.get("frame_format", 2)
                if prev.get("id_format", 0) == f.get("id_format", 0) and pff in (2, ff) and _covers(ps, _match_set(f)):
                    warnings.append(("router_filter_shadowed", f"{s} filter {router_filter_text(f)} is never reached: "
                                     f"Earlier filter {router_filter_text(prev)} already matches all its IDs (first "
                                     "match wins)" + (" - its prescaler never applies" if f.get("prescaler_type") else "")))
                    break
            seen.append((f, _match_set(f)))
        tx = g(c, "phy", sec, "transmit") or []
        bad = [t for t in tx if isinstance(t, dict) and (t.get("period", 0) or 0) > 0
               and (t.get("period", 0) or 0) <= (t.get("delay", 0) or 0)]
        if bad:
            errors.append(("router_transmit_period_delay", f"{s}: Transmit entries with period <= delay "
                           f"({', '.join(str(t.get('name') or t.get('id')) for t in bad)}): Set delay < period"))
        mode = g(c, "phy", sec, "phy", "mode")
        if [t for t in tx if isinstance(t, dict) and t.get("state") == 1] and mode not in (None, 0):
            warnings.append(("router_transmit_mode", f"{s}: Enabled transmit entries, but the mode is not Normal: "
                             "Nothing is transmitted"))
        if mode == 2:
            notes.append(f"{s} is in Monitoring mode: It does not acknowledge frames, so a single node on a bench "
                         "(no other receiver) keeps retransmitting and nothing is received. Fine on a vehicle bus")
    route = g(c, "phy", "can", "route") or {}
    if route.get("mode", 0) == 1:
        warnings.append(("router_direct_mode", "Direct mode: S1-S4 frames are forwarded to the primary bus as plain "
                         "frames (route.direct filters/maps), not as tunnel frames. A CANedge logs them on CAN2 with "
                         "their own IDs: No demux, no can11- DBCs (decode them as CAN2)"))
        return errors, warnings, notes
    t = router_tunnel(c)
    if t is None:
        return errors, warnings, notes
    tid, ext = t
    if g(route, "mux", "s2p", "filter", "rx_msg") == 0:
        warnings.append(("router_tunnel_rx_off", "mux.s2p.filter.rx_msg is off: Frames received on S1-S4 are not "
                         "sent to the primary bus, so a CANedge records nothing from them"))
    p2s = g(route, "mux", "p2s") or g(route, "mux", "input") or []
    for p in p2s if isinstance(p2s, list) else []:
        if isinstance(p, dict) and hexint(p.get("id", "-1") or "-1") == tid and (p.get("id_format", 0) == 1) == ext:
            warnings.append(("router_tunnel_collision", f"The tunnel ID 0x{tid:X} (s2p, router -> CANedge) is also a "
                             "p2s ID (primary -> S1-S4): Use distinct IDs (default s2p 10, p2s 11; a 2nd router e.g. "
                             "s2p 12, never 11)"))
    if ext or tid not in ROUTER_DEFAULT_CHANNELS:
        notes.append(f"Custom tunnel ID 0x{tid:X}: The router DBC must use it (canedge-workspace muxtp.py dbc --id "
                     f"{tid:03X}:11 -o <project>/dbc/can2-canmod-router.dbc), and demux maps it to CAN11-14")
    elif tid != 0x10:
        notes.append(f"Tunnel ID 0x{tid:X} (2nd router convention): Demuxed to CAN{ROUTER_DEFAULT_CHANNELS[tid]}-"
                     f"{ROUTER_DEFAULT_CHANNELS[tid] + 3} (the default router DBC covers 0x10 and 0x12)")
    return errors, warnings, notes


def pair_checks(ce: dict, rc: dict | None, tunnels: list[tuple[str, int, bool]]) -> list[tuple[str, str]]:
    """CANedge config ce + CANmod.router config rc (or only the tunnel IDs of the project's router DBC): Is the router
    data logged? tunnels = [(CANedge channel, tunnel ID, extended)]."""
    out = []
    for ch, tid, ext in tunnels:
        sec = CHANNEL_TO_SECTION.get(ch)
        if sec not in ce:
            continue
        label = f"{ch} tunnel ID 0x{tid:X}"
        if g(ce, sec, "general", "rx_state") == 0 and g(ce, sec, "control", "control_rx_state") != 1:
            out.append(("router_tunnel_rx_off", f"{label}: {sec} rx_state is off, so no CANmod.router data is logged"))
            continue
        flt = enabled_filters(ce, sec)
        if flt is None:
            continue
        m = first_match(flt, tid, int(ext))
        if m is None or m.get("type", 0) == 1:
            out.append(("router_tunnel_rejected", f"{label} is rejected by the CANedge {sec} filters "
                        f"({describe_filter(m) if m else 'no matching filter'}): No CANmod.router data is logged. "
                        f"Accept 0x{tid:X} without prescaler"))
        elif m.get("prescaler_type", 0):
            out.append(("router_tunnel_prescaled", f"{label} is prescaled by the CANedge ({describe_filter(m)}): Tunnel "
                        "segments are lost, so demuxed frames go missing. Filter on the router (S1-S4) instead"))
    if rc is not None:
        sec = CHANNEL_TO_SECTION.get(tunnels[0][0]) if tunnels else "can_2"
        cp, rp = g(ce, sec, "phy") or {}, g(rc, "phy", "can", "phy") or {}
        if cp.get("bit_rate_cfg_mode") == 0:
            out.append(("router_bitrate_autodetect", f"CANedge {sec}: Bit-rate auto-detect does not work with only a "
                        f"CANmod on the channel: Set it to the router's primary bit-rate ({rp.get('bit_rate_std')} / FD "
                        f"{rp.get('bit_rate_fd')})"))
        elif cp.get("bit_rate_cfg_mode") == 1 and rp.get("bit_rate_cfg_mode", 0) == 0 and (
                cp.get("bit_rate_std") != rp.get("bit_rate_std") or cp.get("bit_rate_fd") != rp.get("bit_rate_fd")):
            out.append(("router_bitrate_mismatch", f"CANedge {sec} bit-rate {cp.get('bit_rate_std')} / FD "
                        f"{cp.get('bit_rate_fd')} differs from the router's primary bus {rp.get('bit_rate_std')} / FD "
                        f"{rp.get('bit_rate_fd')}: No data"))
        if cp.get("mode") == 2:
            out.append(("router_canedge_monitoring", f"CANedge {sec} is in Monitoring mode: It does not acknowledge the "
                        "router's frames (the only other node on the bus). Use Normal or Restricted"))
    return out


# --------------------------------------------------------------------------- #
# Summary
# --------------------------------------------------------------------------- #

ONOFF = {0: "off", 1: "on"}
MODE = {0: "Normal", 1: "Restricted", 2: "Monitoring"}
CHN = {0: "CAN9", 1: "CAN1", 2: "CAN2", 3: "LIN1", 4: "LIN2"}


def phy_text(phy, router: bool = False) -> str:
    """router: CANmod.router bit_rate_cfg_mode (0 = bit-rate, 1 = bit-timing; no auto-detect)."""
    if not isinstance(phy, dict):
        return "-"
    brm = phy.get("bit_rate_cfg_mode")
    rates = f"{phy.get('bit_rate_std')} / FD {phy.get('bit_rate_fd')}"
    br = ({0: rates, 1: "bit-timing"} if router else {0: "auto-detect", 1: rates, 2: "bit-timing"}).get(brm, "?")
    return f"mode {MODE.get(phy.get('mode'), phy.get('mode'))}, bit-rate {br}"


def filter_text(i: int, f: dict) -> str:
    fmt = "29-bit" if f.get("id_format") == 1 else "11-bit"
    pre = prescaler_text(f) if f.get("type", 0) == 0 else "-"
    return (f"{i:>3} {ONOFF.get(f.get('state'), '?'):3} {FILTER_TYPE_NAMES.get(f.get('type'), '?'):6} {fmt} "
            f"{METHOD_NAMES.get(f.get('method'), '?'):5} {str(f.get('f1', '')).upper():>8} "
            f"{str(f.get('f2', '')).upper():>8}  {pre:16} {f.get('name', '')}")


def control_text(ctrl) -> list[str]:
    if not isinstance(ctrl, dict) or (ctrl.get("control_rx_state") != 1 and ctrl.get("control_tx_state") != 1):
        return []
    on = [k for k, key in (("rx", "control_rx_state"), ("tx", "control_tx_state")) if ctrl.get(key) == 1]
    out = [f"Control signal ({'+'.join(on)}):"]
    for which in ("start", "stop"):
        s = ctrl.get(which) or {}
        m, sig = s.get("message", {}), s.get("signal", {})
        out.append(f"  {which}: {CHN.get(m.get('chn'), m.get('chn'))} ID {m.get('id')} mask {m.get('id_mask')}, "
                   f"bitpos {sig.get('bitpos')} len {sig.get('length')} "
                   f"{'Intel' if sig.get('byteorder') == 1 else 'Motorola'} x{sig.get('factor')} +{sig.get('offset')}, "
                   f"when {s.get('trigger_low')} <= value <= {s.get('trigger_high')}")
    return out


def router_summary(c: dict) -> list[str]:
    route = g(c, "phy", "can", "route") or {}
    out = [f"[Primary] {phy_text(g(c, 'phy', 'can', 'phy'), True)}"]
    if route.get("mode", 0) == 0:
        t = router_tunnel(c)
        p2s = [str(p.get("id")) for p in g(route, "mux", "p2s") or g(route, "mux", "input") or [] if isinstance(p, dict)]
        out.append(f"Routing: Mux, tunnel (s2p) ID {f'0x{t[0]:X}' if t else '-'}{' 29-bit' if t and t[1] else ''}, "
                   f"p2s IDs {', '.join(p2s) or '-'}, s2p traffic {g(route, 'mux', 's2p', 'filter') or '-'}")
    else:
        out.append("Routing: Direct")
    for sec in ROUTER_SECTIONS:
        ch = g(c, "phy", sec) or {}
        out.append(f"[{sec.replace('can_s', 'S')}] {phy_text(ch.get('phy'), True)}")
        out += [f"  {router_filter_text({**f, '_index': i})}{'' if f.get('state') == 1 else ' (off)'}"
                for i, f in enumerate(ch.get("filter") or []) if isinstance(f, dict)]
        for i, t in enumerate(ch.get("transmit") or []):
            out.append(f"  TX #{i} {ONOFF.get(t.get('state'))} ID {t.get('id')} data {t.get('data')} period "
                       f"{t.get('period')} ms delay {t.get('delay')} ms {t.get('name', '')}")
    return out


def summarize(c: dict) -> list[str]:
    """Summary lines of a config (secrets masked)."""
    if is_router(c):
        return router_summary(c)
    c = mask_secrets(c)
    f, out = g(c, "log", "file") or {}, []
    out.append(f"Log: split {f.get('split_size')} MB / {f.get('split_time_period')} s, cyclic "
               f"{ONOFF.get(f.get('cyclic'))}, compression {g(c, 'log', 'compression', 'level') or 'off'}, "
               f"encryption {ONOFF.get(g(c, 'log', 'encryption', 'state'))}, "
               f"error frames {ONOFF.get(g(c, 'log', 'error_frames', 'state'))}")
    for sec, label in (("can_internal", "CAN9 (internal)"), ("can_1", "CAN1"), ("can_2", "CAN2")):
        ch = c.get(sec)
        if not isinstance(ch, dict):
            continue
        out.append(f"[{label}] rx {ONOFF.get(g(ch, 'general', 'rx_state'))}, tx {ONOFF.get(g(ch, 'general', 'tx_state'))}"
                   + (f", {phy_text(ch['phy'])}" if "phy" in ch else ""))
        out += [f"  {filter_text(i, x)}" for i, x in enumerate(g(ch, "filter", "id") or []) if isinstance(x, dict)]
        for i, t in enumerate(ch.get("transmit") or []):
            out.append(f"  TX #{i} {ONOFF.get(t.get('state'))} ID {t.get('id')} data {t.get('data')} period "
                       f"{t.get('period')} ms delay {t.get('delay')} ms log {t.get('log')} {t.get('name', '')}")
        out += [f"  {x}" for x in control_text(ch.get("control"))]
    for sec in ("lin_1", "lin_2"):
        if sec in c:
            out.append(f"[{sec.upper().replace('_', '')}] mode {g(c, sec, 'phy', 'mode')}, bit-rate "
                       f"{g(c, sec, 'phy', 'bit_rate')}, {len(g(c, sec, 'transmit') or [])} transmit entries")
    routes = [r for r in c.get("routing") or [] if isinstance(r, dict) and r.get("state") == 1]
    if routes:
        out.append(f"Routing: {len(routes)} enabled route(s)")
    if isinstance(c.get("connect"), dict):
        s3 = g(c, "connect", "s3", "server") or {}
        aps = [a.get("ssid") for a in g(c, "connect", "wifi", "accesspoint") or [] if isinstance(a, dict)]
        out.append(f"Connect: S3 {s3.get('endpoint') or '-'}" + (f" bucket {s3['bucket']}" if s3.get("bucket") else "")
                   + (f", WiFi {aps}" if "wifi" in c["connect"] else "")
                   + (f", APN {g(c, 'connect', 'cellular', 'apn')!r}" if "cellular" in c["connect"] else ""))
    if "gnss" in c:
        out.append(f"GNSS: alignment method {g(c, 'gnss', 'alignment', 'method')}, "
                   f"{len(g(c, 'gnss', 'geofence') or [])} geofence(s)")
    return out


# --------------------------------------------------------------------------- #
# Schema validation
# --------------------------------------------------------------------------- #

def validate_schema(config: dict, schema: dict) -> list[str]:
    import jsonschema
    from jsonschema import Draft7Validator
    try:
        Draft7Validator.check_schema(schema)
        validator = Draft7Validator(schema)
    except Exception:
        validator = jsonschema.validators.validator_for(schema)(schema)
    out = []
    for e in sorted(validator.iter_errors(config), key=lambda e: list(e.absolute_path)):
        leaf = e
        while leaf.context:  # descend oneOf/anyOf to the most specific branch error
            leaf = max(leaf.context, key=lambda x: (len(list(x.absolute_path)), -len(x.message)))
        path = ".".join(str(p) for p in leaf.absolute_path) or "(root)"
        msg = leaf.message if len(leaf.message) <= 240 else leaf.message[:240] + " ...(truncated)"
        out.append(f"{path}: {msg}")
    return out


def validate(path: Path, pair: Path | None = None, project: Path | None = None) -> tuple[dict, dict | None]:
    """(validation result, parsed config or None)."""
    res = {"file": str(path), "json_valid": False, "config_crc32": None, "device_type": None,
           "version": None, "schema_file": None, "schema_checked": False, "schema_errors": [],
           "errors": [], "warnings": [], "notes": [], "deployable": False}
    if not path.is_file():
        res["notes"].append(f"File not found: {path}")
        return res, None
    raw = path.read_bytes()
    res["config_crc32"] = crc32_hex(raw)
    try:
        cfg = json.loads(raw.decode("utf-8-sig"))
    except json.JSONDecodeError as e:
        res["notes"].append(f"JSON syntax error at line {e.lineno}, column {e.colno}: {e.msg}")
        return res, None
    if not isinstance(cfg, dict):
        res["notes"].append("Top-level JSON is not an object")
        return res, None
    res["json_valid"] = True

    device = detect_device_type(cfg)
    res["device_type"] = device
    version = extract_version(path.name)
    if version is None:
        avail = available_versions(device)
        if avail:
            version = avail[-1]
            res["notes"].append(f"No version in file name; assumed latest schema for {device}: {version}")
    res["version"] = version

    sp = SCHEMA_ROOT / device / f"schema-{version}.json"
    if not (SCHEMA_ROOT / device).is_dir():
        res["notes"].append(f"No bundled schema for device type '{device}', schema validation skipped")
    elif not sp.is_file():
        res["notes"].append(f"No schema {sp.name} for {device} (available: "
                            f"{', '.join(available_versions(device)) or 'none'}), schema validation skipped")
    else:
        res["schema_file"] = sp.relative_to(SCHEMA_ROOT.parent.parent).as_posix()
        try:
            res["schema_errors"] = validate_schema(cfg, json.loads(sp.read_text(encoding="utf-8-sig")))
            res["schema_checked"] = True
        except ImportError:
            res["notes"].append("Package 'jsonschema' missing, schema validation skipped")

    if has(cfg, "can_2"):
        for checks, key in ((REJECT_CHECKS, "errors"), (EDITOR_WARNING_CHECKS, "warnings")):
            for fn in checks:
                res[key] += [{"check": fn.__name__[4:], "message": m} for m in fn(cfg)]
    if is_router(cfg):
        e, w, n = router_checks(cfg)
        res["errors"] += [{"check": k, "message": m} for k, m in e]
        res["warnings"] += [{"check": k, "message": m} for k, m in w]
        res["notes"] += n
    other = json.loads(pair.read_text(encoding="utf-8-sig")) if pair else None
    ce, rc = (other, cfg) if is_router(cfg) else (cfg, other if is_router(other) else None)
    if pair and other is not None and is_router(other) == is_router(cfg):
        res["notes"].append(f"--pair {pair.name}: Give one CANedge and one CANmod.router config")
    elif ce is not None and has(ce, "can_2") and (rc is not None or project):
        tunnels = []
        if rc is not None and router_tunnel(rc):
            tunnels.append(("CAN2", *router_tunnel(rc)))
        if project:
            import muxtp  # noqa: E402 - canedge-workspace (on sys.path via cfg_common)
            tunnels += [(e["channel"], int(e["id"], 16), bool(e["ext"])) for e in muxtp.for_project(project)
                        if (e["channel"], int(e["id"], 16), bool(e["ext"])) not in tunnels]
        res["warnings"] += [{"check": k, "message": m} for k, m in pair_checks(ce, rc, tunnels)]
        if tunnels:
            res["notes"].append("CANmod.router pairing checked: " + ", ".join(f"{c} 0x{i:X}" for c, i, _ in tunnels))
    res["warnings"] += [{"check": k, "message": m} for k, m in chk_filter_details(cfg, path.name)]
    res["deployable"] = not res["schema_errors"] and not res["errors"]
    if has(cfg, "log", "compression") and not g(cfg, "log", "compression", "level"):
        res["notes"].append("Compression is off: Enabling it (log.compression.level 1024, or 256 for 3+ MB/min) "
                            "typically makes log files 50-70% smaller (MFC, read natively by the CSS MF4 tools)")
    return res, cfg


def print_report(r: dict, summary: list[str]) -> None:
    print(f"Config check: {r['file']}")
    if not r["json_valid"]:
        print("  JSON syntax : FAILED")
        for n in r["notes"]:
            print(f"    - {n}")
        return
    print(f"  Device      : {r['device_type']} (config version {r['version'] or '?'}), CRC32 {r['config_crc32']}")
    for line in summary:
        print(f"  {line}")
    if r["schema_checked"]:
        n = len(r["schema_errors"])
        print(f"  Schema      : {'OK' if n == 0 else f'{n} ERROR(S)'} ({r['schema_file']})")
        for e in r["schema_errors"]:
            print(f"    - {e}")
    else:
        print("  Schema      : SKIPPED")
    n = len(r["errors"])
    print(f"  Errors      : {n}" + (" (the device rejects this config: Fix every one)" if n else ""))
    for e in r["errors"]:
        print(f"    x [{e['check']}] {e['message']}")
    n = len(r["warnings"])
    print(f"  Warnings    : {n}" + (" (tell the user each one with its impact + fix, see references/warning-checks.md)"
                                     if n else ""))
    for w in r["warnings"]:
        print(f"    ! [{w['check']}] {w['message']}")
    for n in r["notes"]:
        print(f"  Note: {n}")
    if not r["deployable"]:
        print("  Result      : DO NOT DEPLOY (fix the schema/device errors above)")
    elif not r["schema_checked"]:
        print("  Result      : NOT VERIFIED (schema validation skipped, see notes)")
    else:
        print("  Result      : Valid" + (" (review the warnings with the user)" if r["warnings"] else ""))


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("config", help="Configuration File (.json)")
    ap.add_argument("--no-summary", action="store_true", help="Validation only")
    ap.add_argument("--json", action="store_true", help="Print a JSON result for the agent")
    ap.add_argument("--pair", type=Path, help="The other config of a CANedge + CANmod.router setup (pairing checks)")
    ap.add_argument("--project", type=Path, help="CANedge config: Check the tunnel IDs of the project's router DBC")
    args = ap.parse_args()
    r, cfg = validate(Path(args.config), args.pair, args.project)
    summary = summarize(cfg) if cfg is not None and not args.no_summary else []
    if args.json:
        print(json.dumps({**r, "summary": summary}, indent=2))
    else:
        print_report(r, summary)
    return 0 if (r["json_valid"] and r["deployable"]) else 1


if __name__ == "__main__":
    sys.exit(main())
