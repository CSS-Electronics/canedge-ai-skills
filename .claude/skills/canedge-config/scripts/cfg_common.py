"""Shared helpers for the canedge-config scripts (imported by the sibling scripts in this folder).

- load_config(): Read a Configuration File (tolerates a BOM), returns (dict, raw bytes)
- detect_device_type() / extract_version(): Same logic as the CANedge config editor
- Filter semantics (enabled_filters, filter_matches, first_match): A faithful port of the config editor's
  filterEvaluator.js (sequential, first match wins, id_format must match, no match = reject)
- mask_secrets(): Replace passwords/keys with "***" before anything is printed
- signal_bytes(): The data bytes a DBC signal covers (data prescaler masks: bit i = byte i)
- CANmod.router (is_router, router_tunnel, router_channel_map, router_filters, router_match): Its S1-S4 filters
  (phy.can_s1..4.filter[]) work like the CANedge's (CANmod.router Docs, "Filter"): Top to bottom, the first match
  decides, no match = rejected, no enabled filter = all rejected; but mask-only + accept-only (no type/method
  fields), and the frame format (standard / FD / both) must match. Prescalers as on the CANedge (max 100 IDs per
  channel). The router channels show as CAN11-14 (tunnel 0x10), CAN15-18 (0x12) after demux (canedge-workspace muxtp.py)
"""
from __future__ import annotations

import copy
import json
import re
import sys
import zlib
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
from dbc_groups import pgn_of  # noqa: E402

SCRIPT_DIR = Path(__file__).resolve().parent
SCHEMA_ROOT = SCRIPT_DIR.parent / "assets" / "schema-files"

# Channel name in raw stats / mdf2csv BusChannel -> config section
CHANNEL_TO_SECTION = {"CAN1": "can_1", "CAN2": "can_2", "CAN9": "can_internal"}

MAX_PRESCALER_IDS_PER_CHANNEL = 100  # device tracks prescaler state for max 100 unique IDs per physical channel
MAX_PRESCALER_IDS = {"CAN9": 15}  # ... and 15 for CAN-internal (CANedge Docs, "Message prescaling")

FILTER_TYPE_NAMES = {0: "accept", 1: "reject"}
METHOD_NAMES = {0: "range", 1: "mask"}

SECRET_KEY_RE = re.compile(r"(pwd|password|secretkey|accesskey|^pin$|passphrase|token)", re.I)


# --------------------------------------------------------------------------- #
# Loading + device detection
# --------------------------------------------------------------------------- #

def load_config(path: Path) -> tuple[dict, bytes]:
    raw = path.read_bytes()
    cfg = json.loads(raw.decode("utf-8-sig"))
    if not isinstance(cfg, dict):
        raise ValueError("Top-level JSON is not an object")
    return cfg, raw


def crc32_hex(raw: bytes) -> str:
    """Same value the device reports as the config CRC32 (byte sensitive)."""
    return format(zlib.crc32(raw) & 0xFFFFFFFF, "08X")


def g(d, *keys):
    """Safe nested get: d[k1][k2]... or None if any hop is missing."""
    cur = d
    for k in keys:
        if not isinstance(cur, dict) or k not in cur:
            return None
        cur = cur[k]
    return cur


def has(d, *keys) -> bool:
    return g(d, *keys) is not None


def detect_device_type(c: dict) -> str:
    """Port of the config editor's device detection (CANedge part + CANmod.router: phy.can.route)."""
    device = "Other"
    if has(c, "can_2") and not has(c, "connect"):
        device = "CANedge1 GNSS" if has(c, "gnss") else "CANedge1"
    if has(c, "can_2") and has(c, "connect", "wifi"):
        device = "CANedge2 GNSS" if has(c, "gnss") else "CANedge2"
    if has(c, "can_2") and has(c, "connect", "cellular"):
        device = "CANedge3 GNSS" if has(c, "gnss") else "CANedge3"
    if has(c, "sensor") or has(c, "phy", "can"):
        device = "CANmod.router" if is_router(c) else "CANmod (not supported by this skill)"
    return device


def extract_version(filename: str) -> str | None:
    m = re.search(r"config-(\d{2}\.\d{2})", filename)
    return m.group(1) if m else None


def available_versions(device_type: str) -> list[str]:
    d = SCHEMA_ROOT / device_type
    if not d.is_dir():
        return []
    out = []
    for f in d.glob("schema-*.json"):
        m = re.match(r"schema-(\d{2}\.\d{2})\.json$", f.name)
        if m:
            out.append(m.group(1))
    return sorted(out)


# --------------------------------------------------------------------------- #
# Secrets
# --------------------------------------------------------------------------- #

def mask_secrets(obj):
    """Deep copy with secret values replaced. Public key (kpub) is shortened, not hidden."""
    obj = copy.deepcopy(obj)

    def walk(o):
        if isinstance(o, dict):
            for k, v in o.items():
                if isinstance(v, (dict, list)):
                    walk(v)
                elif k == "kpub" and isinstance(v, str) and v:
                    o[k] = v[:8] + "..." if len(v) > 8 else v
                elif SECRET_KEY_RE.search(k) and k != "keyformat" and v not in ("", None):
                    o[k] = "***"
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(obj)
    return obj


# --------------------------------------------------------------------------- #
# Filter semantics (port of config-editor-base filterEvaluator.js)
# --------------------------------------------------------------------------- #

def hexint(s) -> int:
    if isinstance(s, int):
        return s
    s = str(s).strip()
    if s.lower().startswith("0x"):
        s = s[2:]
    return int(s, 16) if s else 0


def enabled_filters(cfg: dict, section: str) -> list[dict] | None:
    """Enabled filters of a section, each tagged with its 0-based index in the full list.
    Returns None if the section has no filter list (device then logs everything by default)."""
    ids = g(cfg, section, "filter", "id")
    if not isinstance(ids, list):
        return None
    out = []
    for i, f in enumerate(ids):
        if isinstance(f, dict) and f.get("state") == 1:
            ff = dict(f)
            ff["_index"] = i
            out.append(ff)
    return out


def filter_matches(f: dict, can_id: int, ide: int) -> bool:
    if (f.get("id_format", 0) == 1) != (ide == 1):
        return False
    f1, f2 = hexint(f.get("f1", "0")), hexint(f.get("f2", "0"))
    if f.get("method", 0) == 0:
        return f1 <= can_id <= f2
    return (f1 & f2) == (can_id & f2)


def first_match(filters: list[dict], can_id: int, ide: int) -> dict | None:
    for f in filters:
        if filter_matches(f, can_id, ide):
            return f
    return None


def describe_filter(f: dict) -> str:
    name = f.get("name") or ""
    t = FILTER_TYPE_NAMES.get(f.get("type"), "?")
    fmt = "29-bit" if f.get("id_format") == 1 else "11-bit"
    meth = METHOD_NAMES.get(f.get("method"), "?")
    sep = "-" if f.get("method", 0) == 0 else " mask "
    s = f"#{f.get('_index', '?')} {t} {fmt} {meth} {str(f.get('f1', '')).upper()}{sep}{str(f.get('f2', '')).upper()}"
    if f.get("type", 0) == 0 and f.get("prescaler_type", 0):
        s += f" [{prescaler_text(f)}]"
    if name:
        s += f" ({name})"
    return s


def signal_bytes(bitpos: int, length: int, bo: int) -> list[int]:
    """Data bytes a DBC signal covers (bo 1 = Intel: LSB at bitpos counting up; 0 = Motorola, DBC sawtooth
    numbering: MSB at bitpos, counting down within a byte, then the next byte)."""
    if bo == 1:
        return list(range(bitpos // 8, (bitpos + max(length, 1) - 1) // 8 + 1))
    bit, out = bitpos, {bitpos // 8}
    for _ in range(length - 1):
        bit = bit + 15 if bit % 8 == 0 else bit - 1
        out.add(bit // 8)
    return sorted(out)


def prescaler_text(f: dict) -> str:
    pt = f.get("prescaler_type", 0) or 0
    pv = f.get("prescaler_value")
    if pt == 1:
        return f"count 1/{pv}"
    if pt == 2:
        return f"time {pv} ms"
    if pt == 3:
        return f"data mask {pv or 'all bytes'}"
    return "none"


# --------------------------------------------------------------------------- #
# CANmod.router
# --------------------------------------------------------------------------- #

ROUTER_SECTIONS = ("can_s1", "can_s2", "can_s3", "can_s4")
ROUTER_MAX_FILTERS = 32     # per secondary channel (schema maxItems)
ROUTER_MAX_TRANSMIT = 16
ROUTER_FPS_GUIDE = 6000     # practical total frames/s of all router channels after filters (CANmod.router product page)
ROUTER_DEFAULT_CHANNELS = {0x10: 11, 0x12: 15}   # tunnel ID -> first demux channel (CANedge Intro convention)
FRAME_FORMATS = {0: "standard", 1: "FD", 2: "both"}


def is_router(c: dict | None) -> bool:
    return bool(c) and has(c, "phy", "can", "route")


def router_tunnel(c: dict) -> tuple[int, bool] | None:
    """(tunnel CAN ID, extended) the router sends S1-S4 on (mux mode): phy.can.route.mux.s2p.message (FW 01.02),
    mux.output.message (01.01). None = direct mode / not set."""
    route = g(c, "phy", "can", "route") or {}
    if route.get("mode", 0) != 0:
        return None
    m = g(route, "mux", "s2p", "message") or g(route, "mux", "output", "message")
    if not isinstance(m, dict) or m.get("id") in (None, ""):
        return None
    return hexint(m["id"]), m.get("id_format", 0) == 1


def router_channel_map(c: dict, mapping: list[dict] | None = None) -> dict[str, str] | None:
    """Demuxed channel -> router section, e.g. {"CAN11": "can_s1", ...}: The demux mapping entry (muxtp.py) for the
    config's tunnel ID, else the default channels (0x10 -> CAN11-14, 0x12 -> CAN15-18). None = unknown (pass one)."""
    t = router_tunnel(c)
    if t is None:
        return None
    tid, ext = t
    for e in mapping or []:
        if int(e["id"], 16) == tid and bool(e.get("ext")) == ext:
            return {f"CAN{ch}": sec for ch, sec in zip(e["channels"], ROUTER_SECTIONS)}
    base = None if ext else ROUTER_DEFAULT_CHANNELS.get(tid)
    return {f"CAN{base + i}": sec for i, sec in enumerate(ROUTER_SECTIONS)} if base else None


def router_filters(c: dict, section: str) -> list[dict] | None:
    """Enabled filters of a router section (phy.<can_sN>.filter[]), normalized to the CANedge fields (type 0 accept,
    method 1 mask) and tagged with their 0-based index. None = no filter list."""
    lst = g(c, "phy", section, "filter")
    if not isinstance(lst, list):
        return None
    return [{**f, "type": 0, "method": 1, "_index": i} for i, f in enumerate(lst)
            if isinstance(f, dict) and f.get("state") == 1]


def router_match(filters: list[dict], can_id: int, ide: int, fd: bool) -> dict | None:
    """First router filter that matches (ID format, frame format, ID & mask). A filter without frame_format (FW
    01.01) matches both."""
    for f in filters:
        ff = f.get("frame_format", 2)
        if ff != 2 and (ff == 1) != bool(fd):
            continue
        if filter_matches(f, can_id, ide):
            return f
    return None


# --------------------------------------------------------------------------- #
# J1939 helpers (same as the config editor filter builder)
# --------------------------------------------------------------------------- #

j1939_pgn = pgn_of  # PGN of a 29-bit J1939 ID (PDU1: destination byte zeroed), canedge-workspace dbc_groups.py


# J1939 transport protocol (multi-frame) PGNs: The decoder reassembles their messages, so every segment must be
# logged - never reject or prescale them unless the user asks (also used on NMEA 2000 / ISOBUS networks)
J1939_TP_PGNS = {0xEC00: "TP.CM", 0xEB00: "TP.DT", 0xC800: "ETP.CM", 0xC700: "ETP.DT"}


def j1939_tp(ide: int, can_id: int) -> str | None:
    """'J1939 TP.DT' etc. for a 29-bit ID with a J1939 transport protocol PGN, else None."""
    return f"J1939 {J1939_TP_PGNS[j1939_pgn(can_id)]}" if ide and j1939_pgn(can_id) in J1939_TP_PGNS else None
