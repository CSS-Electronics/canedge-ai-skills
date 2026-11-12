"""Validate, inspect or propose a dbc-groups.json (the DBC-to-device mapping).

Commands:
  check   --project projects/<name>            Validate dbc-groups.json: files exist, channel prefixes, device IDs,
                                               devices in multiple groups, duplicates WITHIN a DBC (same CAN ID, or
                                               same PGN with ProtocolType J1939: the decoder refuses the DBC = error)
                                               and overlaps between DBCs on the same channel within a group: Same
                                               CAN ID (or PGN, if a J1939 DBC matches on it) AND same message name =
                                               same Parquet folder (error if the signals/scaling differ: corrupt /
                                               mixed Parquet although decoding "works"; warning if identical).
                                               Different message names are fine (each DBC decodes into its own
                                               folder, e.g. the J1939-73 DM1 one-DBC-per-DTC workaround).
                                               Different groups/channels never overlap.
                                               Without dbc-groups.json: The same checks on all DBCs in dbc/
  check   --dbc <files or folders>             The duplicate + overlap checks for loose DBC files (no project)
  show    --project projects/<name> --device X Print which DBC applies on which channel for device X
  propose --dbc-dir <folder> [--write projects/<name>/dbc-groups.json]
                                               Propose groups from file names like can1-12345678[-label].dbc.
                                               PROPOSAL ONLY: Confirm with the user before using it.

Channel rule (same as the MF4 decoders): canN-/lin1-/lin2- prefix (can1-, can2-, can9-, and can11-, can12-, ... for
CANmod.router channels after demux); no prefix = CAN1 + CAN2.
No dbc-groups.json (or no groups in it) = all DBCs in dbc/ apply to all devices, also like the automation.
Optional top-level "common_dbc_files" (automation v4.4.0+): Added to the dbc_files of every group (load_groups).

Also the shared DBC helpers of the suite (imported by canedge-mdf2parquet, canedge-config, canedge-dbc,
canedge-lake-query): load_dbc, channels_for, by_channel, group_for, dbcs_for_device, pgn_of, fmt_can_id.
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import sys
from pathlib import Path

DEVICE_RE = re.compile(r"^[0-9A-F]{8}$")
PREFIX_RE = re.compile(r"^(can\d{1,2}|lin[12])-", re.I)
REFUSED = "the MF4 decoder refuses this DBC (fix it with canedge-dbc: dbc_lint.py lists the lines)"


def load_dbc(path: Path):
    """cantools database (strict=False: Overlaps etc. are reported by canedge-dbc dbc_lint.py, not here)."""
    import cantools
    logging.getLogger("cantools").setLevel(logging.ERROR)
    return cantools.database.load_file(str(path), strict=False)


def channels_for(fname: str) -> list[str]:
    m = PREFIX_RE.match(fname)
    return [m.group(1).upper()] if m else ["CAN1", "CAN2"]


def by_channel(paths: list[Path]) -> dict[str, list[Path]]:
    """DBC paths per channel (channels_for on each file name)."""
    out: dict[str, list[Path]] = {}
    for p in paths:
        for ch in channels_for(p.name):
            out.setdefault(ch, []).append(p)
    return out


def pgn_of(can_id: int) -> int:
    """J1939 PGN as the CSS decoders (and the DBC editor) match it: (ID >> 8) & 0x3FFFF, destination address zeroed
    for PDU1 (PF < 240)."""
    pgn = (can_id >> 8) & 0x3FFFF
    return pgn & 0x3FF00 if ((pgn >> 8) & 0xFF) < 240 else pgn


def fmt_can_id(can_id: int, extended: bool) -> str:
    """CAN ID as shown to people: Hex, 0x + 3 digits (11-bit) or 8 digits (29-bit). extended comes from the IDE flag
    / the DBC frame format, never from the value. Display only: Keep each tool's own format in files and matching."""
    return f"0x{can_id:08X}" if extended else f"0x{can_id:03X}"


SG_RE = re.compile(r"^\s*SG_\s+(\w+)\s*(?:[mM]\d*\s*)?:\s*(\d+)\|(\d+)@([01])([+-])\s*\(([^,]+),([^)]+)\)")


def protocol_type(text: str) -> str:
    """Database ProtocolType as the CSS decoders use it: The first BA_ "ProtocolType" value, else the BA_DEF_DEF_
    default. "J1939" = PGN matching for 29-bit IDs (+ J1939 TP); anything else = exact ID matching. VFrameFormat
    J1939PG does not count."""
    m = (re.search(r'^\s*BA_\s+"ProtocolType"\s+"([^"]*)"\s*;', text, re.M)
         or re.search(r'^\s*BA_DEF_DEF_\s+"ProtocolType"\s+"([^"]*)"\s*;', text, re.M))
    return m.group(1) if m else ""


def within_dbc_duplicates(path: Path) -> list[str]:
    """Duplicates INSIDE one DBC that make the CSS MF4 decoders refuse the whole file: The same CAN ID twice, or (with
    ProtocolType J1939) two 29-bit IDs with the same PGN. Light text parse, no cantools. [] = none."""
    text = path.read_text(encoding="latin-1")
    j1939_db = protocol_type(text).upper() == "J1939"
    j1939_msg = {int(m.group(1)): m.group(2).upper() == "J1939"
                 for m in re.finditer(r'^\s*BA_\s+"ProtocolType"\s+BO_\s+(\d+)\s+"([^"]*)"', text, re.M)}
    by_id: dict[tuple[bool, int], list[str]] = {}
    by_pgn: dict[int, list[tuple[int, str]]] = {}
    for m in re.finditer(r"^BO_\s+(\d+)\s+(\w+)\s*:", text, re.M):
        raw, name = int(m.group(1)), m.group(2)
        if raw == 0xC0000000:  # VECTOR__INDEPENDENT_SIG_MSG
            continue
        ext, fid = bool(raw & 0x80000000), raw & 0x1FFFFFFF
        by_id.setdefault((ext, fid), []).append(name)
        if ext and j1939_msg.get(raw, j1939_db):
            by_pgn.setdefault(pgn_of(fid), []).append((fid, name))
    out = [f"{path.name}: CAN ID {fmt_can_id(fid, ext)} is defined {len(ns)}x ({', '.join(ns)})"
           for (ext, fid), ns in sorted(by_id.items()) if len(ns) > 1]
    out += [f"{path.name}: J1939 PGN 0x{pgn:X} is used by {', '.join(f'{n} (0x{fid:08X})' for fid, n in hs)} "
            f"(ProtocolType J1939 matches on the PGN; set ProtocolType \"\" for exact 29-bit IDs, or keep one)"
            for pgn, hs in sorted(by_pgn.items()) if len({fid for fid, _ in hs}) > 1]
    return out


def light_layouts(text: str) -> dict[tuple[bool, int], dict]:
    """Same output as message_layouts from BO_/SG_ lines + ProtocolType (database or message level)."""
    j1939_db = protocol_type(text).upper() == "J1939"
    j1939_msg = {int(m.group(1)): m.group(2).upper() == "J1939"
                 for m in re.finditer(r'^\s*BA_\s+"ProtocolType"\s+BO_\s+(\d+)\s+"([^"]*)"', text, re.M)}
    out, cur = {}, None
    for line in text.splitlines():
        mm = re.match(r"^BO_\s+(\d+)\s+(\w+)\s*:\s*(\d+)", line)
        if mm:
            raw = int(mm.group(1))
            ext = bool(raw & 0x80000000)
            cur = {"name": mm.group(2), "dlc": int(mm.group(3)), "sigs": [],
                   "j1939": ext and j1939_msg.get(raw, j1939_db)}
            out[(ext, raw & 0x1FFFFFFF)] = cur
            continue
        sm = SG_RE.match(line)
        if sm and cur is not None:
            name, start, length, order, sign, factor, offset = sm.groups()
            cur["sigs"].append((name, int(start), int(length), "little_endian" if order == "1" else "big_endian",
                                sign == "-", float(factor), float(offset)))
        elif line.strip():   # the next keyword ends the BO_ block (blank lines do not: \r\r\n files have one per line)
            cur = None
    return {k: {"name": d["name"], "layout": (d["dlc"], tuple(sorted(d["sigs"]))), "j1939": d["j1939"],
                "columns": frozenset(s[0] for s in d["sigs"])} for k, d in out.items()}


def message_layouts(dbc_path: Path) -> dict[tuple[bool, int], dict]:
    """(extended, frame_id) -> {name, layout, j1939, columns} via cantools, or a light parser if cantools is missing
    or cannot load the file (e.g. cantools 45 fails on a VFrameFormat attribute without default, as in the CSS
    J1939-73 DM1 DBCs). j1939 = ProtocolType J1939 (database or message): The CSS decoders match on the PGN."""
    try:
        db = load_dbc(dbc_path)
    except Exception:  # noqa: BLE001 - ImportError or a cantools load failure: parse the essentials ourselves
        return light_layouts(dbc_path.read_text(encoding="latin-1"))

    def protocol(obj) -> str | None:
        a = obj.dbc.attributes.get("ProtocolType") if obj is not None and obj.dbc is not None else None
        return str(a.value) if a is not None else None

    db_protocol = protocol(db)
    if db_protocol is None:  # no database value: the BA_DEF_DEF_ default, as the decoders use it
        db_protocol = protocol_type(dbc_path.read_text(encoding="latin-1"))
    out = {}
    for m in db.messages:
        sig = tuple(sorted((s.name, s.start, s.length, s.byte_order, s.is_signed, float(s.scale), float(s.offset))
                           for s in m.signals))
        mp = protocol(m)
        j1939 = m.is_extended_frame and (mp if mp is not None else db_protocol).upper() == "J1939"
        out[(m.is_extended_frame, m.frame_id)] = {"name": m.name, "layout": (m.length, sig), "j1939": j1939,
                                                  "columns": frozenset(s.name for s in m.signals)}
    return out


def cross_dbc_collisions(paths: dict[str, Path], per_channel: dict[str, list[str]], tag: str = "") \
        -> tuple[list[str], list[str]]:
    """Overlaps between DBCs on the same channel that mix data in one Parquet folder (CAN<ch>_<message name>[_S<SA>]).
    The same CAN ID (or PGN, as J1939 DBCs with ProtocolType J1939 match on the PGN) in several DBCs is fine when the
    message names differ: Each DBC decodes into its own folder (e.g. the J1939-73 DM1 workaround: one DBC per DTC
    slot, same ID, names DM01_DTC_01, DM01_DTC_02, ...). With the same message name the data shares a folder:
    Different signals = mixed Parquet schemas, same signals with other bits/scaling = inconsistent values."""
    errors, warnings = [], []
    for ch, files in per_channel.items():
        if len(files) < 2:
            continue
        layouts = {f: message_layouts(paths[f]) for f in files}
        hits_by: dict[tuple, list[tuple[str, int, dict]]] = {}
        for f, lay in layouts.items():
            for (ext, fid), d in lay.items():
                hits_by.setdefault(("id", ext, fid), []).append((f, fid, d))
                if ext and d["j1939"]:
                    hits_by.setdefault(("pgn", pgn_of(fid)), [])
        for f, lay in layouts.items():  # every 29-bit message whose PGN a J1939 DBC claims
            for (ext, fid), d in lay.items():
                if ext and ("pgn", pgn_of(fid)) in hits_by:
                    hits_by[("pgn", pgn_of(fid))].append((f, fid, d))
        reported = set()
        for key, hits in sorted(hits_by.items(), key=lambda kv: (kv[0][0], kv[0][-1])):
            by_name: dict[str, list[tuple[str, int, dict]]] = {}
            for h in hits:
                by_name.setdefault(h[2]["name"], []).append(h)
            for mname, same in by_name.items():
                files = {f for f, _, _ in same}
                if len(files) < 2 or (mname, frozenset(files)) in reported:
                    continue  # different message names = separate Parquet folders: fine
                reported.add((mname, frozenset(files)))
                what = f"CAN ID 0x{key[2]:X}" if key[0] == "id" else f"J1939 PGN 0x{key[1]:X}"
                where = ", ".join(f"{f} (0x{fid:X}{' J1939' if d['j1939'] else ''})" for f, fid, d in same)
                if len({d.get("columns") for _, _, d in same}) > 1:
                    errors.append(f"{tag}{ch}: {what} message {mname} is in several DBCs with different signals "
                                  f"({where}). Both decode into the same Parquet folder: Mixed schemas. Rename the "
                                  f"message in one DBC, or keep it in one DBC")
                elif len({str(d["layout"]) for _, _, d in same}) > 1:
                    errors.append(f"{tag}{ch}: {what} message {mname} has the same signals but other bits/scaling in "
                                  f"several DBCs ({where}). Both decode into the same Parquet folder: Inconsistent "
                                  f"values. Rename the message in one DBC, or keep one definition")
                else:
                    warnings.append(f"{tag}{ch}: {what} message {mname} is defined identically in several DBCs "
                                    f"({where}). Redundant: Keep it in one DBC")
    return errors, warnings


def load_groups(project: Path) -> list[dict]:
    """The project's DBC groups. No dbc-groups.json (or no groups in it) = one group: all DBCs in dbc/ for all
    devices, like the data lake automation. common_dbc_files (optional) are added to every group's dbc_files."""
    p = project / "dbc-groups.json"
    data = json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}
    groups = data.get("dbc_groups", [])
    if groups:
        common = data.get("common_dbc_files", [])
        return [{**g, "dbc_files": g.get("dbc_files", []) + [f for f in common if f not in g.get("dbc_files", [])]}
                for g in groups] if common else groups
    return [{"devices": ["*"], "dbc_files": sorted(f.name for f in (project / "dbc").glob("*.dbc"))}]


def group_for(groups: list[dict], *devices: str | None) -> tuple[int, dict] | None:
    """(index, group) of the first DBC group that applies to any of the devices (the automation uses the first
    match); a group with devices ["*"] (no dbc-groups.json) applies to all. None = no group."""
    devs = {d.upper() for d in devices if d}
    return next(((gi, g) for gi, g in enumerate(groups)
                 if g.get("devices") == ["*"] or devs & {str(d).upper() for d in g.get("devices", [])}), None)


def dbcs_for_device(project: Path, device: str | None, all_confirmed: bool = False, alt_device: str | None = None)         -> tuple[dict[str, list[Path]] | None, str]:
    """The DBCs per channel for a device: (mapping, note), or (None, reason) if none apply. alt_device = another ID of
    the same files (a folder name that differs from the header serial). all_confirmed = the user confirmed that all
    DBCs in dbc/ apply (used when the device is in no group)."""
    dbc_dir = project / "dbc"
    hit = group_for(load_groups(project), device, alt_device)
    if hit:
        gi, g = hit
        names = g.get("dbc_files", [])
        note = "all project DBCs (no dbc-groups.json)" if g["devices"] == ["*"] else f"dbc-groups.json group {gi}"
    elif all_confirmed:
        names, note = sorted(f.name for f in dbc_dir.glob("*.dbc")), "all project DBCs (--all-dbcs-confirmed)"
    else:
        what = (f"device {device} is not in any group of {project / 'dbc-groups.json'}" if device else
                "files outside the CANedge folder layout <device>/<session>/<split> (device unknown)")
        return None, (f"{what}. Applying the wrong DBC can give plausible but wrong values. Fix: Confirm with the user "
                      f"which DBCs apply, add a group and run canedge-workspace dbc_groups.py check (or pass "
                      f"--all-dbcs-confirmed if the user confirmed that all DBCs in dbc/ apply)")
    missing = [n for n in names if not (dbc_dir / n).exists()]
    if missing:
        return None, f"{note}: DBC files missing in {dbc_dir}: {', '.join(missing)}"
    if not names:
        return None, f"{note}: no DBC files"
    return by_channel([dbc_dir / n for n in names]), note


def project_devices(project: Path) -> list[str]:
    p = project / "project.json"
    return json.loads(p.read_text(encoding="utf-8")).get("devices", []) if p.exists() else []


def cmd_check(project: Path) -> int:
    dbc_dir = project / "dbc"
    errors, warnings = [], []
    groups = load_groups(project)
    all_dbcs = groups[0]["devices"] == ["*"]
    if all_dbcs:
        print("No dbc-groups.json: All DBCs in dbc/ apply to all devices")
        groups = [{"devices": project_devices(project), "dbc_files": groups[0]["dbc_files"]}]
        if not groups[0]["dbc_files"]:
            warnings.append(f"no DBC files in {dbc_dir}")
    mine = {d.upper() for d in project_devices(project)}   # project.json devices: Only their groups' files checked
    if not all_dbcs and mine:
        print(f"Checking the DBC files of the groups with the project's devices ({', '.join(sorted(mine))})")
    seen_dev: dict[str, int] = {}
    checked: set[str] = set()
    for gi, g in enumerate(groups):
        for d in g.get("devices", []):
            if not DEVICE_RE.match(str(d)):
                errors.append(f"group {gi}: '{d}' is not an 8-character hex device ID (use upper case)")
            if d in seen_dev:
                errors.append(f"device {d} is in group {seen_dev[d]} and group {gi} - the automation uses the first match only")
            seen_dev.setdefault(d, gi)
        if not all_dbcs and mine and not (set(g.get("devices", [])) & mine or "*" in g.get("devices", [])):
            continue   # e.g. the bucket's dbc-groups.json as-is: Other devices' DBCs need not be in dbc/
        per_channel: dict[str, list[str]] = {}
        for f in g.get("dbc_files", []):
            if not (dbc_dir / f).exists():
                errors.append(f"group {gi}: {f} not found in {dbc_dir}")
                continue
            if not PREFIX_RE.match(f):
                warnings.append(f"group {gi}: {f} has no canN- prefix (can1-, can2-, can11-, ...), so it applies to CAN1 AND CAN2")
            for ch in channels_for(f):
                per_channel.setdefault(ch, []).append(f)
        for f in g.get("dbc_files", []):
            if (dbc_dir / f).exists() and f not in checked:
                checked.add(f)
                errors += [f"{d} - {REFUSED}" for d in within_dbc_duplicates(dbc_dir / f)]
        errors_c, warnings_c = cross_dbc_collisions({f: dbc_dir / f for f in g.get("dbc_files", [])
                                                     if (dbc_dir / f).exists()}, per_channel, f"group {gi} ")
        errors += errors_c
        warnings += warnings_c
    if not all_dbcs:
        for d in project_devices(project):
            if d not in seen_dev:
                warnings.append(f"project device {d} is not in any DBC group - its data would not be decoded as intended")
        print(f"{len(groups)} group(s), {len(seen_dev)} device(s)")
    for e in errors:
        print("ERROR:   " + e)
    for w in warnings:
        print("WARNING: " + w)
    if not errors and not warnings:
        print("OK")
    return 1 if errors else 0


def cmd_show(project: Path, device: str) -> int:
    hit = group_for(load_groups(project), device)
    if not hit:
        print(f"{device} is not in any group")
        return 1
    for f in hit[1].get("dbc_files", []):
        print(f"{'/'.join(channels_for(f)):10s} {f}")
    return 0


def cmd_propose(dbc_dir: Path, write: Path | None) -> int:
    by_dev: dict[str, list[str]] = {}
    unassigned = []
    for f in sorted(p.name for p in dbc_dir.glob("*.dbc")):
        m = re.match(r"^(?:can\d{1,2}|lin[12])-([0-9A-Fa-f]{8})(?:[-_.].*)?\.dbc$", f, re.I)
        if m:
            by_dev.setdefault(m.group(1).upper(), []).append(f)
        else:
            unassigned.append(f)
    groups = [{"devices": [d], "dbc_files": fs} for d, fs in sorted(by_dev.items())]
    proposal = {"dbc_groups": groups}
    print(json.dumps(proposal, indent=2))
    if unassigned:
        print("Not device-specific by name (ask the user which devices they apply to): " + ", ".join(unassigned))
    print("PROPOSAL ONLY - confirm with the user before using it.")
    if write:
        if write.exists() and json.loads(write.read_text(encoding="utf-8")).get("dbc_groups"):
            print(f"{write} already has groups - not overwritten")
            return 1
        write.write_text(json.dumps(proposal, indent=2), encoding="utf-8")
        print(f"Written to {write}")
    return 0


def cmd_check_files(paths: list[Path]) -> int:
    """Cross-DBC overlaps for loose DBC files (no project): All files count as one group (same device)."""
    per_channel: dict[str, list[str]] = {}
    for p in paths:
        for ch in channels_for(p.name):
            per_channel.setdefault(ch, []).append(p.name)
    errors = [f"{d} - {REFUSED}" for p in paths for d in within_dbc_duplicates(p)]
    errors_c, warnings = cross_dbc_collisions({p.name: p for p in paths}, per_channel)
    errors += errors_c
    print(f"{len(paths)} DBC file(s): " + ", ".join(f"{ch} {len(fs)}" for ch, fs in sorted(per_channel.items())))
    for e in errors:
        print("ERROR:   " + e)
    for w in warnings:
        print("WARNING: " + w)
    if not errors and not warnings:
        print("OK - no duplicates within a DBC, no overlaps between DBCs on the same channel")
    return 1 if errors else 0


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    cg = c.add_mutually_exclusive_group(required=True)
    cg.add_argument("--project", type=Path)
    cg.add_argument("--dbc", type=Path, nargs="+", help="Loose DBC files or folders (no project), treated as one device")
    s = sub.add_parser("show"); s.add_argument("--project", type=Path, required=True); s.add_argument("--device", required=True)
    p = sub.add_parser("propose"); p.add_argument("--dbc-dir", type=Path, required=True); p.add_argument("--write", type=Path)
    a = ap.parse_args()
    if a.cmd == "check" and a.dbc:
        return cmd_check_files(sorted({f for d in a.dbc for f in (d.glob("*.dbc") if d.is_dir() else [d])}))
    if a.cmd == "check":
        return cmd_check(a.project)
    if a.cmd == "show":
        return cmd_show(a.project, a.device)
    return cmd_propose(a.dbc_dir, a.write)


if __name__ == "__main__":
    sys.exit(main())
