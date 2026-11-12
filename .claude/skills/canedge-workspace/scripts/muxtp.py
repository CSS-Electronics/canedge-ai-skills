"""CANmod.router demux (MUX-TP) mapping for all canedge-* scripts: Which tunnel frames to demux into which channels.

A CANmod.router on the CANedge CAN2 sends its 4 secondary channels (S1-S4) as CAN FD "tunnel" frames on one CAN ID
(default 0x10; a second, daisy-chained router e.g. 0x12). Demuxing (mdf2mdf --muxtp-can2=010#11:12:13:14) turns them
back into the original frames on virtual channels CAN11-14 (CAN15-18 for 0x12), the tunnel frames are removed.

Single source of truth: The router DBC in the project (can2-canmod-router.dbc, bundled in canedge-mdf2mdf/assets).
Its attribute BA_ "TransportProtocolType" BO_ 16 "MUXTP#11:12:13:14" tells the MF4 decoders to demux, and these
scripts read the same attribute, so raw stats, config simulation and decoding always use the same channels.

Mapping = list of {"channel": "CAN2", "id": "010", "ext": False, "channels": [11, 12, 13, 14]} (id = hex as mdf2mdf
wants it: 3 digits 11-bit, 8 digits 29-bit).

Library:
  for_device(project, device)  mapping from the DBCs that apply to the device ([] = no router DBC = no demux)
  from_dbcs(paths)             mapping from DBC files (channel from the file prefix, can2- -> CAN2)
  parse_spec("010:11")         CLI spec -> mapping entry ([canN:]ID:S1[,S2,S3,S4]; one channel = 4 consecutive)
  flags(mapping)               mdf2mdf arguments ["--muxtp-can2=010#11:12:13:14"]
  spec(entry)                  entry -> CLI spec 'can2:010:11,12,13,14' (pass to another script's --muxtp)
  labels(mapping)              {"CAN11": "router 0x010 S1", ...} and the tunnel channel
  detect(ids)                  raw-stats IDs that look like router tunnel frames (CAN2 0x010/0x012 CAN FD)
  router_dbc(mapping)          DBC text for custom tunnel IDs / daisy chains

CLI:
  python muxtp.py show --project projects/<name> [--device X]   mapping + mdf2mdf flags + channel labels
  python muxtp.py dbc --id 010:11 --id 012:15 [-o can2-canmod-router.dbc]   router DBC for 1+ routers
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
ROUTER_DBC = SKILLS / "canedge-mdf2mdf" / "assets" / "can2-canmod-router.dbc"
DEFAULT = [{"channel": "CAN2", "id": "010", "ext": False, "channels": [11, 12, 13, 14]}]
# Tunnel IDs of the documented setups: 0x10 = router 1 (default), 0x12 = router 2 (CANedge Intro). Used to detect
# un-demuxed router data in raw stats.
KNOWN_TUNNEL_IDS = (0x10, 0x12)
MUXTP_RE = re.compile(r'^\s*BA_\s+"TransportProtocolType"\s+BO_\s+(\d+)\s+"MUXTP#([\d:]+)"\s*;', re.M)


def _entry(channel: str, can_id: int, ext: bool, chans: list[int]) -> dict:
    return {"channel": channel.upper(), "id": f"{can_id:08X}" if ext else f"{can_id:03X}", "ext": ext,
            "channels": [int(c) for c in chans]}


def from_dbcs(paths) -> list[dict]:
    """Mapping from DBC files: Every message with TransportProtocolType "MUXTP#a:b:c:d", channel from the file
    prefix (dbc_groups.channels_for; a router DBC without prefix would apply to CAN1 + CAN2). paths = list of Paths
    or {channel: [Paths]} (dbc_groups.by_channel / dbcs_for_device)."""
    from dbc_groups import channels_for
    items = ([(ch, p) for ch, ps in paths.items() for p in ps] if isinstance(paths, dict)
             else [(ch, Path(p)) for p in paths for ch in channels_for(Path(p).name)])
    out, seen = [], set()
    for ch, p in items:
        if not ch.upper().startswith("CAN"):
            continue
        for m in MUXTP_RE.finditer(Path(p).read_text(encoding="latin-1")):
            raw = int(m.group(1))
            e = _entry(ch, raw & 0x1FFFFFFF, bool(raw & 0x80000000), [int(x) for x in m.group(2).split(":") if x])
            key = (e["channel"], e["id"])
            if key not in seen:
                seen.add(key)
                out.append(e)
    return sorted(out, key=lambda e: (e["channel"], e["id"]))


def for_device(project: Path | str, device: str | None, alt_device: str | None = None,
               all_confirmed: bool = False) -> list[dict]:
    """The demux mapping for a device of a project: The MUXTP messages of the DBCs that apply to it ([] = none)."""
    from dbc_groups import dbcs_for_device
    project = Path(project)
    if not (project / "dbc").is_dir():
        return []
    dbcs, _ = dbcs_for_device(project, device, all_confirmed=all_confirmed, alt_device=alt_device)
    if dbcs is None:  # device not in a dbc-groups.json group: a router DBC anywhere in dbc/ still tells the setup
        dbcs = {}
    return from_dbcs(dbcs)


def for_project(project: Path | str) -> list[dict]:
    """The demux mapping of all DBCs in the project's dbc/ (when the device is unknown)."""
    project = Path(project)
    return from_dbcs(sorted((project / "dbc").glob("*.dbc"))) if (project / "dbc").is_dir() else []


def parse_spec(spec: str) -> dict:
    """'[canN:]ID:S1[,S2,S3,S4]' -> mapping entry. ID hex (3 digits = 11-bit, 8 = 29-bit; 0x prefix allowed);
    one channel = 4 consecutive (010:11 = 11,12,13,14). 'default' = can2:010:11."""
    if spec.strip().lower() == "default":
        return dict(DEFAULT[0])
    parts = spec.strip().split(":")
    ch = "CAN2"
    if parts and parts[0].lower().startswith("can"):
        ch = parts.pop(0).upper()
    if len(parts) != 2:
        raise ValueError(f"bad MUX-TP spec {spec!r}: use [canN:]ID:S1[,S2,S3,S4], e.g. 010:11 or can2:012:15,16,17,18")
    sid = parts[0].lower().removeprefix("0x")
    can_id = int(sid, 16)
    ext = len(sid) > 3 or can_id > 0x7FF
    chans = [int(x) for x in re.split(r"[,;]", parts[1]) if x]
    if len(chans) == 1:
        chans = [chans[0] + i for i in range(4)]
    if not 1 <= len(chans) <= 4:
        raise ValueError(f"bad MUX-TP spec {spec!r}: 1 or 4 channels")
    src = int(ch[3:])
    if src in chans:
        raise ValueError(f"bad MUX-TP spec {spec!r}: the demux channels cannot include the input channel {ch}")
    return _entry(ch, can_id, ext, chans)


def flags(mapping: list[dict]) -> list[str]:
    """mdf2mdf arguments, one --muxtp-canN=ID#a:b:c:d per tunnel ID."""
    return [f"--muxtp-{e['channel'].lower()}={e['id']}#{':'.join(str(c) for c in e['channels'])}" for e in mapping]


def spec(entry: dict) -> str:
    """Mapping entry -> CLI spec for --muxtp (parse_spec round trip), e.g. 'can2:010:11,12,13,14'."""
    return f"{entry['channel'].lower()}:{entry['id']}:{','.join(str(c) for c in entry['channels'])}"


def key(mapping: list[dict]) -> str:
    """Stable text form of a mapping (cache keys, provenance): the mdf2mdf flags joined, '' = no demux."""
    return " ".join(flags(mapping))


def labels(mapping: list[dict]) -> dict[str, str]:
    """Channel labels for reports: {"CAN11": "router 0x010 S1", ..., "CAN2": "router link (0x010)"}."""
    out: dict[str, str] = {}
    multi = len(mapping) > 1
    for i, e in enumerate(mapping, 1):
        tag = f"router {i} (0x{e['id']})" if multi else f"router 0x{e['id']}"
        for s, c in enumerate(e["channels"], 1):
            out[f"CAN{c}"] = f"{tag} S{s}"
    for ch in sorted({e["channel"] for e in mapping}):
        ids = ", ".join(f"0x{e['id']}" for e in mapping if e["channel"] == ch)
        out[ch] = f"router link ({ids})"
    return out


def detect(ids: list[dict], mapping: list[dict] | None = None) -> list[dict]:
    """Raw-stats ID entries (raw_stats.py 'ids': channel, id '0x0010', ide, fd, frames) that look like CANmod.router
    tunnel frames not covered by the demux mapping: CAN2 11-bit CAN FD on 0x010 / 0x012. [] = nothing suspicious."""
    covered = {(e["channel"], int(e["id"], 16)) for e in mapping or []}
    hits = []
    for e in ids:
        if e.get("channel") != "CAN2" or e.get("ide") or not e.get("fd"):
            continue
        cid = int(str(e["id"]), 16)
        if cid in KNOWN_TUNNEL_IDS and ("CAN2", cid) not in covered:
            hits.append(e)
    return hits


def router_dbc(mapping: list[dict]) -> str:
    """Router DBC text (the bundled can2-canmod-router.dbc) with one tunnel message per mapping entry, e.g. for a
    changed tunnel ID (phy.can.route.mux.s2p.message.id) or daisy-chained routers. All entries must be on the same
    input channel (the DBC file name prefix sets it)."""
    text = ROUTER_DBC.read_text(encoding="latin-1")
    text = re.sub(r"^BO_\s+\d+\s+Router\w*\s*:.*$\n?", "", text, flags=re.M)
    text = MUXTP_RE.sub("", text)
    bos, attrs = [], []
    for i, e in enumerate(mapping, 1):
        raw = int(e["id"], 16) | (0x80000000 if e["ext"] else 0)
        name = "Router" if i == 1 else f"Router{i}"
        bos.append(f"BO_ {raw} {name}: 0 Vector__XXX\n")
        attrs.append(f'BA_ "TransportProtocolType" BO_ {raw} "MUXTP#{":".join(str(c) for c in e["channels"])}";\n')
    text = text.replace("BU_:\n", "BU_:\n\n\n" + "\n".join(bos), 1)
    return text.rstrip("\n") + "\n" + "".join(attrs)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("show", help="Demux mapping of a project (from its router DBC)")
    s.add_argument("--project", type=Path, required=True)
    s.add_argument("--device", help="Device ID (DBC groups); default: all DBCs in dbc/")
    s.add_argument("--json", action="store_true")
    d = sub.add_parser("dbc", help="Write a router DBC for custom tunnel IDs / daisy chains")
    d.add_argument("--id", action="append", required=True, help="[canN:]ID:S1[,S2,S3,S4], e.g. 010:11 012:15")
    d.add_argument("-o", "--out", type=Path, help="Output file (default: print). Name it can2-<...>.dbc")
    a = ap.parse_args()
    sys.path.insert(0, str(Path(__file__).resolve().parent))
    if a.cmd == "show":
        m = for_device(a.project, a.device) if a.device else for_project(a.project)
        if a.json:
            print(json.dumps({"mapping": m, "mdf2mdf_flags": flags(m), "labels": labels(m)}, indent=1))
        elif not m:
            print("No CANmod.router DBC (TransportProtocolType MUXTP) in the project: No demux. Router on CAN2? Copy "
                  f"{ROUTER_DBC} into the project's dbc/ folder")
        else:
            for e in m:
                print(f"{e['channel']} ID 0x{e['id']} -> " + ", ".join(f"CAN{c} (S{i})" for i, c in
                                                                    enumerate(e["channels"], 1)))
            print("mdf2mdf flags: " + " ".join(flags(m)))
        return 0
    m = [parse_spec(x) for x in a.id]
    if len({e["channel"] for e in m}) > 1:
        ap.error("all --id must use the same input channel (one DBC file per channel)")
    chans = [c for e in m for c in e["channels"]]
    if len(chans) != len(set(chans)):
        ap.error("demux channels overlap between the --id entries")
    text = router_dbc(m)
    if a.out:
        a.out.write_text(text, encoding="latin-1")
        print(f"Wrote {a.out} ({', '.join(flags(m))})")
    else:
        print(text)
    return 0


if __name__ == "__main__":
    sys.exit(main())
