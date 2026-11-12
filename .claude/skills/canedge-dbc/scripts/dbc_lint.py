"""Lint a DBC file: syntax + structure checks with errors vs warnings, message/signal names and line numbers.

Layer 1 - cantools: Load with strict=True (report the exact error) and fall back to strict=False (report whether
the file is usable at all, plus cantools' own load warnings such as overwritten duplicate frame IDs/names).

Layer 2 - checks ported from the CSS Electronics online DBC editor (src/validation/dbcErrors.ts +
columnValidators.ts) plus a few extras. They run on a light raw-text parser, so every issue carries a line number
and the checks still run when cantools cannot load the file. See references/lint-checks.md for the full list.

Usage:
  python dbc_lint.py <file.dbc> [--json] [--limit 10]
  python dbc_lint.py <file.dbc> --fix --out <fixed.dbc>     # SAFE mechanical fixes only, never overwrites input

--fix applies only: SG_ " : " separator spacing, and "+" -> "-" on float/double signals (SIG_VALTYPE_ 1/2). The
result is re-linted. Everything else is reported for the user/agent to decide.

Exit code: 0 = no errors (warnings allowed), 1 = errors found, 2 = file not readable.
"""
from __future__ import annotations

import argparse
import bisect
import json
import logging
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
from dbc_groups import fmt_can_id as hex_id  # noqa: E402
from dbc_groups import pgn_of as extract_pgn  # noqa: E402 (same as the DBC editor's extractPgn, canId.ts)

VECTOR_INDEPENDENT_ID = 0xC0000000  # VECTOR__INDEPENDENT_SIG_MSG pseudo message
CAN_FD_LENGTHS = {0, 1, 2, 3, 4, 5, 6, 7, 8, 12, 16, 20, 24, 32, 48, 64}
DEFAULT_VFRAME_ENUM = ["StandardCAN", "ExtendedCAN", "reserved", "J1939PG"]

IDENT_RE = re.compile(r"^\w*$", re.ASCII)
BO_RE = re.compile(r"^\s*BO_\s+(?P<id>\d+)\s+(?P<name>[^\s:]+)\s*:\s*(?P<dlc>\d+)\s*(?P<tx>\S*)")
SG_RE = re.compile(
    r"^\s*SG_\s+(?P<name>[^\s:]+)(?:\s+(?P<mux>M|m\d+M?))?\s*:\s*"
    r"(?P<start>-?\d+)\s*\|\s*(?P<len>-?\d+)\s*@\s*(?P<order>[01])\s*(?P<sign>[+-])\s*"
    r"\(\s*(?P<factor>[^,()]*)\s*,\s*(?P<offset>[^()]*)\)\s*"
    r"\[\s*(?P<min>[^|\]]*)\|(?P<max>[^\]]*)\]\s*"
    r"\"(?P<unit>[^\"]*)\"\s*(?P<rx>.*)$")
CM_RE = re.compile(
    r"CM_\s+(?:(?P<bo>BO_)\s+(?P<bo_id>\d+)\s+|(?P<sg>SG_)\s+(?P<sg_id>\d+)\s+(?P<sg_name>\S+)\s+|"
    r"(?P<bu>BU_)\s+\S+\s+|(?P<ev>EV_)\s+\S+\s+)?\"(?P<text>(?:[^\"\\]|\\.)*)\"\s*;", re.S)
BA_DEF_RE = re.compile(r"^\s*BA_DEF_\s+(?:(?P<obj>BU_|BO_|SG_|EV_)\s+)?\"(?P<name>[^\"]+)\"\s*(?P<body>[^;]*);", re.M)
BA_DEF_DEF_RE = re.compile(r"^\s*BA_DEF_DEF_\s+\"(?P<name>[^\"]+)\"\s+(?P<value>[^;]*);", re.M)
BA_BO_RE = re.compile(r"^\s*BA_\s+\"(?P<name>[^\"]+)\"\s+BO_\s+(?P<id>\d+)\s+(?P<value>[^;]*);", re.M)
BA_SG_RE = re.compile(r"^\s*BA_\s+\"(?P<name>[^\"]+)\"\s+SG_\s+(?P<id>\d+)\s+(?P<sig>\S+)\s+(?P<value>[^;]*);", re.M)
VALTYPE_RE = re.compile(r"^\s*SIG_VALTYPE_\s+(?P<id>\d+)\s+(?P<sig>[^\s:]+)\s*:?\s*(?P<type>\d+)\s*;", re.M)
MULVAL_RE = re.compile(r"^\s*SG_MUL_VAL_\s+(?P<id>\d+)\s+(?P<sig>\S+)\s+(?P<mux>\S+)\s+(?P<ranges>[^;]*);", re.M)
VAL_RE = re.compile(r"^\s*VAL_\s+(?P<id>\d+)\s+(?P<sig>\S+)\s", re.M)


# ---------------------------------------------------------------------------------------------- helpers

def read_text(path: Path) -> tuple[str, str]:
    raw = path.read_bytes()
    try:
        text, enc = raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        text, enc = raw.decode("cp1252", errors="replace"), "cp1252"
    return text.lstrip("﻿"), enc


def to_float(s: str) -> float | None:
    try:
        v = float(str(s).strip())
    except (TypeError, ValueError):
        return None
    return v


def fmt_num(v: float) -> str:
    return f"{v:.10g}"


def signal_bits(start: int, length: int, little: bool) -> list[int]:
    """Bit indices (byte*8 + bit, bit 0 = LSB of the byte) covered by a signal, DBC conventions."""
    if little:
        return list(range(start, start + length))
    bits, pos = [], start
    for _ in range(length):
        bits.append(pos)
        pos = pos + 15 if pos % 8 == 0 else pos - 1
    return bits


def range_summary(bits: list[int]) -> str:
    lo, hi = min(bits), max(bits)
    return f"bit {lo}" if lo == hi else f"bits {lo}..{hi}"


class LineIndex:
    """Offset -> 1-based line number in O(log n) (counting newlines per match is far too slow on big DBCs)."""

    def __init__(self, text: str):
        self.starts = [0] + [i + 1 for i, c in enumerate(text) if c == "\n"]

    def __call__(self, offset: int) -> int:
        return bisect.bisect_right(self.starts, offset)


def sg_colon_spacing_issue(raw_line: str) -> str:
    """Port of sgColonSpacingIssue (dbcErrors.ts): '' if OK, else 'before' / 'after' / 'before and after'."""
    t = raw_line.lstrip()
    if not t.startswith("SG_"):
        return ""
    colon = t.find(":")
    if colon <= 0:
        return ""
    before = t[colon - 1].isspace()
    after = colon + 1 < len(t) and t[colon + 1].isspace()
    if before and after:
        return ""
    if not before and not after:
        return "before and after"
    return "after" if before else "before"


# ---------------------------------------------------------------------------------------------- raw parser

def parse_raw(text: str) -> dict:
    """Light DBC parser that keeps line numbers and raw lines. Not a full grammar - just what the checks need."""
    issues: list[dict] = []
    line_of = LineIndex(text)
    # Spans of CM_ strings, so lines inside multi-line comments are not parsed as BO_/SG_
    comments = list(CM_RE.finditer(text))
    span_starts = [m.start("text") for m in comments]
    span_ends = [m.end("text") for m in comments]

    def in_comment(off: int) -> bool:
        i = bisect.bisect_right(span_starts, off) - 1
        return i >= 0 and off < span_ends[i]

    messages: list[dict] = []
    current = None
    offset = 0
    for lineno, line in enumerate(text.splitlines(keepends=True), start=1):
        line_off = offset
        offset += len(line)
        s = line.rstrip("\r\n")
        st = s.lstrip()
        if not st or in_comment(line_off):  # blank lines do not end a message block in practice
            continue
        if st.startswith("BO_ ") or st.startswith("BO_\t"):
            m = BO_RE.match(s)
            if not m:
                issues.append(issue("error", "parse-bo", lineno, f"BO_ line cannot be parsed: {st[:120]}"))
                current = None
                continue
            dbc_id = int(m.group("id"))
            current = {"line": lineno, "dbc_id": dbc_id, "extended": bool(dbc_id & 0x80000000),
                       "frame_id": dbc_id & 0x7FFFFFFF if dbc_id & 0x80000000 else dbc_id,
                       "name": m.group("name"), "dlc": int(m.group("dlc")), "sender": m.group("tx"),
                       "signals": [], "comment": "", "attrs": {}}
            messages.append(current)
            continue
        if st.startswith("SG_ ") or st.startswith("SG_\t"):
            if current is None:
                issues.append(issue("error", "parse-sg", lineno, "SG_ line outside a BO_ block"))
                continue
            m = SG_RE.match(s)
            if not m:
                issues.append(issue("error", "parse-sg", lineno, f"{current['name']}: SG_ line cannot be parsed: {st[:120]}",
                                    message=current["name"]))
                continue
            current["signals"].append({
                "line": lineno, "raw": s, "name": m.group("name"), "mux": m.group("mux") or "",
                "start": int(m.group("start")), "length": int(m.group("len")), "little": m.group("order") == "1",
                "signed": m.group("sign") == "-", "factor_s": m.group("factor").strip(),
                "offset_s": m.group("offset").strip(), "min_s": m.group("min").strip(), "max_s": m.group("max").strip(),
                "unit": m.group("unit"), "receivers": m.group("rx").strip(), "valtype": 0, "comment": "",
                "mulval": None})
            continue
        # any other keyword ends the message block
        if re.match(r"^[A-Z_]+", st):
            current = None

    by_dbc_id: dict[int, list[dict]] = {}
    for msg in messages:
        by_dbc_id.setdefault(msg["dbc_id"], []).append(msg)

    sig_index: dict[tuple[int, str], tuple[dict, dict]] = {}
    for msg in messages:
        for sig in msg["signals"]:
            sig_index.setdefault((msg["dbc_id"], sig["name"]), (msg, sig))

    def find_signal(dbc_id: int, name: str):
        return sig_index.get((dbc_id, name), (None, None))

    # Comments
    for m in comments:
        ln = line_of(m.start())
        if m.group("bo"):
            mid = int(m.group("bo_id"))
            if mid not in by_dbc_id and mid != VECTOR_INDEPENDENT_ID:
                issues.append(issue("warning", "ref-unknown", ln, f"CM_ BO_ refers to unknown message ID {mid}"))
            for msg in by_dbc_id.get(mid, []):
                msg["comment"] = m.group("text")
                msg["comment_line"] = ln
        elif m.group("sg"):
            mid = int(m.group("sg_id"))
            msg, sig = find_signal(mid, m.group("sg_name"))
            if sig is None:
                if mid != VECTOR_INDEPENDENT_ID:
                    issues.append(issue("warning", "ref-unknown", ln,
                                        f"CM_ SG_ refers to unknown signal {m.group('sg_name')} in message ID {mid}"))
            else:
                sig["comment"] = m.group("text")
                sig["comment_line"] = ln

    # Attribute definitions / defaults / values
    attr_defs = {}
    for m in BA_DEF_RE.finditer(text):
        attr_defs[m.group("name")] = {"obj": m.group("obj") or "", "body": m.group("body").strip(),
                                      "line": line_of(m.start())}
    attr_defaults = {m.group("name"): m.group("value").strip().strip('"') for m in BA_DEF_DEF_RE.finditer(text)}
    vframe_enum = DEFAULT_VFRAME_ENUM
    if "VFrameFormat" in attr_defs:
        vframe_enum = re.findall(r"\"([^\"]*)\"", attr_defs["VFrameFormat"]["body"]) or DEFAULT_VFRAME_ENUM
    for m in BA_BO_RE.finditer(text):
        mid = int(m.group("id"))
        ln = line_of(m.start())
        if mid not in by_dbc_id:
            if mid == VECTOR_INDEPENDENT_ID:
                continue
            issues.append(issue("warning", "ref-unknown", ln, f"BA_ \"{m.group('name')}\" refers to unknown message ID {mid}"))
            continue
        for msg in by_dbc_id[mid]:
            msg["attrs"][m.group("name")] = (m.group("value").strip().strip('"'), ln)
    for m in BA_SG_RE.finditer(text):
        msg, sig = find_signal(int(m.group("id")), m.group("sig"))
        if sig is None and int(m.group("id")) != VECTOR_INDEPENDENT_ID:
            issues.append(issue("warning", "ref-unknown", line_of(m.start()),
                                f"BA_ \"{m.group('name')}\" refers to unknown signal {m.group('sig')} in message ID {m.group('id')}"))
    for m in VALTYPE_RE.finditer(text):
        msg, sig = find_signal(int(m.group("id")), m.group("sig"))
        ln = line_of(m.start())
        if sig is None and int(m.group("id")) != VECTOR_INDEPENDENT_ID:
            issues.append(issue("warning", "ref-unknown", ln,
                                f"SIG_VALTYPE_ refers to unknown signal {m.group('sig')} in message ID {m.group('id')}"))
        else:
            sig["valtype"] = int(m.group("type"))
            sig["valtype_line"] = ln
    for m in MULVAL_RE.finditer(text):
        msg, sig = find_signal(int(m.group("id")), m.group("sig"))
        ln = line_of(m.start())
        if sig is None and int(m.group("id")) != VECTOR_INDEPENDENT_ID:
            issues.append(issue("warning", "ref-unknown", ln,
                                f"SG_MUL_VAL_ refers to unknown signal {m.group('sig')} in message ID {m.group('id')}"))
            continue
        ranges = []
        for part in m.group("ranges").split(","):
            rm = re.match(r"^\s*(\d+)\s*-\s*(\d+)\s*$", part)
            if rm:
                ranges.append((int(rm.group(1)), int(rm.group(2))))
        sig["mulval"] = {"parent": m.group("mux"), "ranges": ranges, "line": ln}
    for m in VAL_RE.finditer(text):
        msg, sig = find_signal(int(m.group("id")), m.group("sig"))
        if sig is None and int(m.group("id")) != VECTOR_INDEPENDENT_ID:
            issues.append(issue("warning", "ref-unknown", line_of(m.start()),
                                f"VAL_ refers to unknown signal {m.group('sig')} in message ID {m.group('id')}"))

    pm = re.search(r'^\s*BA_\s+"ProtocolType"\s+"([^"]*)"\s*;', text, re.M)
    return {"messages": messages, "issues": issues, "attr_defs": attr_defs, "attr_defaults": attr_defaults,
            "protocol_type": pm.group(1) if pm else attr_defaults.get("ProtocolType", ""),
            "vframe_enum": vframe_enum}


def issue(severity: str, check: str, line: int | None, text: str, message: str | None = None,
          signal: str | None = None) -> dict:
    return {"severity": severity, "check": check, "line": line, "message": message, "signal": signal, "text": text}


# ---------------------------------------------------------------------------------------------- checks

def check_name(value: str, long_ok: bool) -> tuple[str, str]:
    """validateMessageName / validateSignalName format part. Returns (severity, reason) or ('', '')."""
    if len(value) < 1:
        return "error", "Must be 1-32 characters."
    if not IDENT_RE.match(value):
        return "error", "Must contain only letters, digits and underscores."
    if value[0].isdigit():
        return "error", "Must not start with a digit."
    if len(value) > 32:
        if long_ok:
            return "warning", "Longer than 32 characters (allowed via the long-symbol attribute, but some tools truncate)."
        return "error", "Must be 1-32 characters."
    return "", ""


def theoretical_range(sig: dict) -> tuple[float, float] | None:
    """getTheoreticalRange + getDeclaredRange (bitMath.ts): representable range widened by one LSB."""
    if sig["valtype"] in (1, 2):
        return None
    factor, offset, length = to_float(sig["factor_s"]), to_float(sig["offset_s"]), sig["length"]
    if factor is None or offset is None or length < 1 or length > 64:
        return None
    raw_min = -(2 ** (length - 1)) if sig["signed"] else 0
    raw_max = 2 ** (length - 1) - 1 if sig["signed"] else 2 ** length - 1
    a, b = factor * raw_min + offset, factor * raw_max + offset
    # One LSB of slack (editor rule) plus a 1e-5 relative tolerance (extra) so a max written with fewer
    # significant digits (e.g. 4294970000 for 2^32) does not warn
    lo, hi = min(a, b), max(a, b)
    lsb = abs(factor)
    return lo - lsb - abs(lo) * 1e-5, hi + lsb + abs(hi) * 1e-5


def mux_selector(sig: dict, by_name: dict, root_name: str | None) -> dict[str, list[tuple[int, int]]]:
    """Port of getMuxSelector (multiplex.ts), generalized to SG_MUL_VAL_ value ranges.

    Returns {parent multiplexor name: [(lo, hi), ...]}; empty = always active.
    """
    sel: dict[str, list[tuple[int, int]]] = {}
    cur = sig
    for _ in range(32):
        if cur is None or not cur["mux"].startswith("m"):
            break
        if cur["mulval"]:
            parent = cur["mulval"]["parent"]
            ranges = cur["mulval"]["ranges"] or [(int(re.match(r"m(\d+)", cur["mux"]).group(1)),) * 2]
        else:
            parent = root_name
            v = int(re.match(r"m(\d+)", cur["mux"]).group(1))
            ranges = [(v, v)]
        if not parent or parent in sel:
            break
        sel[parent] = ranges
        cur = by_name.get(parent)
    return sel


def can_coexist(a: dict, b: dict) -> bool:
    """canSignalsCoexist (multiplex.ts): every shared parent must allow a common value."""
    for parent, ra in a.items():
        rb = b.get(parent)
        if rb is None:
            continue
        if not any(lo1 <= hi2 and lo2 <= hi1 for lo1, hi1 in ra for lo2, hi2 in rb):
            return False
    return True


def run_checks(parsed: dict) -> list[dict]:
    out: list[dict] = list(parsed["issues"])
    msgs = [m for m in parsed["messages"] if m["dbc_id"] != VECTOR_INDEPENDENT_ID]
    defs = parsed["attr_defs"]
    long_msg_ok = "SystemMessageLongSymbol" in defs
    long_sig_ok = "SystemSignalLongSymbol" in defs
    vframe_enum = parsed["vframe_enum"]
    vframe_default = parsed["attr_defaults"].get("VFrameFormat", "")
    # J1939 (PGN matching + J1939 TP) as the CSS decoders decide it: ProtocolType only (database, or message level),
    # never VFrameFormat J1939PG (a CANdb++ default that many non-J1939 DBCs carry)
    j1939_file = parsed.get("protocol_type", "").upper() == "J1939"

    def j1939_msg(msg) -> bool:
        pt = msg["attrs"].get("ProtocolType")
        return msg["extended"] and ((pt[0].upper() == "J1939") if pt else j1939_file)

    def add(sev, check, line, text, msg=None, sig=None):
        out.append(issue(sev, check, line, text, msg["name"] if msg else None, sig["name"] if sig else None))

    for msg in msgs:
        mname = msg["name"]
        # --- message name (validateMessageName, format part)
        sev, why = check_name(mname, long_msg_ok)
        if sev:
            add(sev, "msg-name", msg["line"], f"{mname}: name \"{mname}\" - {why}", msg)
        # --- CAN ID (validateMessageCanId)
        fid, ext = msg["frame_id"], msg["extended"]
        if not ext and fid > 0x7FF:
            add("error", "can-id", msg["line"],
                f"{mname}: CAN ID {msg['dbc_id']} (0x{fid:X}) - Must be 1-11 bits. For a 29-bit ID set bit 31 "
                f"(DBC ID = 0x{fid:X} + 0x80000000 = {fid | 0x80000000})", msg)
        if ext and fid > 0x1FFFFFFF:
            add("error", "can-id", msg["line"], f"{mname}: CAN ID 0x{fid:X} - Must be 1-29 bits.", msg)
        # --- VFrameFormat consistency (extra)
        vff = vframe_default
        explicit_vff = "VFrameFormat" in msg["attrs"]
        if explicit_vff:
            raw, ln = msg["attrs"]["VFrameFormat"]
            try:
                idx = int(raw)
                vff = vframe_enum[idx] if 0 <= idx < len(vframe_enum) else None
            except ValueError:
                vff = None
            if vff is None:
                add("error", "vframeformat", ln, f"{mname}: VFrameFormat value {raw} is not in the enum {vframe_enum}", msg)
                vff = ""
        msg["vframeformat"] = vff
        is_fd = "FD" in (vff or "")
        if vff and explicit_vff:  # the file default (often J1939PG) is not meant for 11-bit IDs - only check explicit values
            if ext and vff in ("StandardCAN", "StandardCAN_FD"):
                add("warning", "vframeformat", msg["line"], f"{mname}: 29-bit ID but VFrameFormat is {vff}", msg)
            if not ext and vff in ("ExtendedCAN", "ExtendedCAN_FD", "J1939PG"):
                add("warning", "vframeformat", msg["line"], f"{mname}: 11-bit ID but VFrameFormat is {vff}", msg)
        # --- DLC (validateDlc) + CAN FD length sanity (extra)
        dlc = msg["dlc"]
        if dlc < 0 or dlc > 1785:
            add("error", "dlc", msg["line"], f"{mname}: Length - Must be between 0 and 1785.", msg)
        elif dlc > 8 and (j1939_msg(msg) or "TransportProtocolType" in msg["attrs"]):
            pass  # multi-frame (J1939 TP / ISO TP) message: any length up to 1785 is fine
        elif dlc > 64:
            add("warning", "dlc", msg["line"],
                f"{mname}: Length {dlc} > 64 bytes - only decodable as a reassembled multi-frame message (J1939 TP / ISO TP)", msg)
        elif dlc > 8 and dlc not in CAN_FD_LENGTHS:
            add("warning", "dlc", msg["line"], f"{mname}: Length {dlc} is not a valid CAN FD frame length (12/16/20/24/32/48/64)", msg)
        elif dlc > 8 and not is_fd:
            add("warning", "dlc", msg["line"],
                f"{mname}: Length {dlc} > 8 but VFrameFormat is not CAN FD - fine for CAN FD or reassembled multi-frame data, "
                f"otherwise check the length", msg)
        # --- comment length (warnComment)
        if len(msg["comment"]) > 255:
            add("warning", "comment-length", msg.get("comment_line", msg["line"]),
                f"{mname}: comment - Must have 0-255 characters ({len(msg['comment'])}).", msg)

        sigs = msg["signals"]
        by_name = {}
        name_count: dict[str, int] = {}
        for s in sigs:
            name_count[s["name"]] = name_count.get(s["name"], 0) + 1
            by_name.setdefault(s["name"], s)
        roots = [s for s in sigs if s["mux"] == "M"]
        root_name = roots[0]["name"] if len(roots) == 1 else None
        has_mulval = any(s["mulval"] for s in sigs)

        for s in sigs:
            tag = f"{mname}.{s['name']}"
            ln = s["line"]
            # names (validateSignalName)
            sev, why = check_name(s["name"], long_sig_ok)
            if sev:
                add(sev, "sig-name", ln, f"{tag}: signal name \"{s['name']}\" - {why}", msg, s)
            if name_count[s["name"]] > 1:
                add("error", "sig-name-duplicate", ln, f"{tag}: signal name \"{s['name']}\" - Must be unique.", msg, s)
            # start / length (validateSignalStart / validateSignalLength)
            if s["start"] < 0:
                add("error", "sig-start", ln, f"{tag}: start - Must be 0 or greater.", msg, s)
            L = s["length"]
            if L < 1:
                add("error", "sig-length", ln, f"{tag}: length - Must be 1 or greater.", msg, s)
            elif s["valtype"] == 1 and L != 32:
                add("error", "sig-length", ln, f"{tag}: length - IEEE Float must be exactly 32 bits.", msg, s)
            elif s["valtype"] == 2 and L != 64:
                add("error", "sig-length", ln, f"{tag}: length - IEEE Double must be exactly 64 bits.", msg, s)
            elif L > 64:
                add("error", "sig-length", ln, f"{tag}: length - Must be 64 or less.", msg, s)
            # bit fit vs DLC (validateSignalBitFit)
            s["bits"] = signal_bits(s["start"], max(L, 0), s["little"]) if s["start"] >= 0 and L >= 1 else []
            if s["bits"] and (max(s["bits"]) >= dlc * 8 or min(s["bits"]) < 0):
                add("error", "sig-bit-fit", ln,
                    f"{tag}: signal exceeds Length (start {s['start']}, length {L}, "
                    f"{'little' if s['little'] else 'big'} endian, Length {dlc} bytes).", msg, s)
            # factor / offset (validateNumber) + factor 0 (extra)
            factor, offset = to_float(s["factor_s"]), to_float(s["offset_s"])
            if factor is None:
                add("error", "sig-factor", ln, f"{tag}: factor - Must be a valid number.", msg, s)
            elif factor == 0:
                add("warning", "sig-factor", ln, f"{tag}: factor is 0 - the signal always decodes to its offset", msg, s)
            if offset is None:
                add("error", "sig-offset", ln, f"{tag}: offset - Must be a valid number.", msg, s)
            # min / max (validateMin/validateMax errors, warnMin/warnMax warnings). 0 = "not set"
            vmin, vmax = to_float(s["min_s"]), to_float(s["max_s"])
            min_err = max_err = False
            if vmin is None:
                add("error", "sig-min-max", ln, f"{tag}: min - Must be a valid number.", msg, s)
                min_err = True
            if vmax is None:
                add("error", "sig-min-max", ln, f"{tag}: max - Must be a valid number.", msg, s)
                max_err = True
            if vmin is not None and vmax is not None and vmin != 0 and vmax != 0 and vmin > vmax:
                add("error", "sig-min-max", ln, f"{tag}: min {fmt_num(vmin)} - Must be smaller than Max ({fmt_num(vmax)}).", msg, s)
                min_err = max_err = True
            rng = theoretical_range(s)
            if rng:
                lo, hi = rng
                if not min_err and vmin not in (None, 0) and (vmin < lo or vmin > hi):
                    side = "below theoretical minimum" if vmin < lo else "above theoretical maximum"
                    add("warning", "sig-min-range", ln,
                        f"{tag}: min - {fmt_num(vmin)} is {side} ({fmt_num(lo if vmin < lo else hi)}).", msg, s)
                if not max_err and vmax not in (None, 0) and (vmax > hi or vmax < lo):
                    side = "above theoretical maximum" if vmax > hi else "below theoretical minimum"
                    add("warning", "sig-max-range", ln,
                        f"{tag}: max - {fmt_num(vmax)} is {side} ({fmt_num(hi if vmax > hi else lo)}).", msg, s)
            # comment length (warnComment)
            if len(s["comment"]) > 255:
                add("warning", "comment-length", s.get("comment_line", ln),
                    f"{tag}: comment - Must have 0-255 characters ({len(s['comment'])}).", msg, s)
            # float/double wire format
            if s["valtype"] in (1, 2) and not s["signed"]:
                ieee = "Float" if s["valtype"] == 1 else "Double"
                add("warning", "sig-float-sign", ln,
                    f"{tag}: SG_ wire format \"+\" (unsigned) but signal is declared as IEEE {ieee}. "
                    f"Per DBC spec, float/double must use \"-\" (--fix rewrites it).", msg, s)
            # SG_ colon spacing (error: the CSS MF4 decoders fail to decode such signals)
            sp = sg_colon_spacing_issue(s["raw"])
            if sp:
                add("error", "sg-colon-spacing", ln,
                    f"{tag}: SG_ line is missing a space {sp} the \":\" separator. Decoders (e.g. the CSS MF4 "
                    f"decoders) will fail to decode this signal (--fix rewrites it).", msg, s)
            # multiplexing
            if s["mulval"]:
                for lo_v, hi_v in s["mulval"]["ranges"]:
                    if hi_v < lo_v:
                        add("error", "mux-range", s["mulval"]["line"],
                            f"{tag}: SG_MUL_VAL_ range {lo_v}-{hi_v} is inverted (end < start).", msg, s)
                if s["mulval"]["parent"] not in by_name:
                    add("error", "mux-range", s["mulval"]["line"],
                        f"{tag}: SG_MUL_VAL_ multiplexor {s['mulval']['parent']} is not a signal of {mname}", msg, s)
            if s["mux"].startswith("m"):
                if not roots and not has_mulval and not any(x["mux"].endswith("M") for x in sigs):
                    add("error", "mux-structure", ln, f"{tag}: multiplexed signal ({s['mux']}) but {mname} has no multiplexor (M)", msg, s)
                v = int(re.match(r"m(\d+)", s["mux"]).group(1))
                parent = by_name.get(s["mulval"]["parent"]) if s["mulval"] else (roots[0] if len(roots) == 1 else None)
                if parent and 1 <= parent["length"] <= 64:
                    pmax = 2 ** parent["length"] - 1
                    if v > pmax:
                        add("error", "mux-range", ln,
                            f"{tag}: multiplexer value {v} cannot occur - multiplexor {parent['name']} is "
                            f"{parent['length']} bits (max {pmax})", msg, s)
        if len(roots) > 1 and not has_mulval:
            add("warning", "mux-structure", msg["line"],
                f"{mname}: {len(roots)} multiplexor signals (M) without SG_MUL_VAL_ - extended multiplexing needs SG_MUL_VAL_", msg)

        # --- overlap (findOverlapPairs, mux-aware)
        if dlc > 0 and len(sigs) >= 2:
            masks = []
            for s in sigs:
                mask = 0
                for b in s.get("bits", []):
                    if 0 <= b < dlc * 8:
                        mask |= 1 << b
                masks.append(mask)
            sels = [mux_selector(s, by_name, root_name) for s in sigs]
            for i in range(len(sigs)):
                if not masks[i]:
                    continue
                for j in range(i + 1, len(sigs)):
                    shared = masks[i] & masks[j]
                    if not shared or not can_coexist(sels[i], sels[j]):
                        continue
                    bits = [b for b in range(shared.bit_length()) if shared >> b & 1]
                    add("error", "sig-overlap", sigs[j]["line"],
                        f"{mname}: signals \"{sigs[i]['name']}\" (line {sigs[i]['line']}) and \"{sigs[j]['name']}\" "
                        f"overlap ({range_summary(bits)}).", msg, sigs[j])

    # --- global: duplicate names, CAN IDs, PGNs (collectGlobalIssues)
    groups: dict[str, list[dict]] = {}
    for msg in msgs:
        groups.setdefault(msg["name"], []).append(msg)
    for name, g in groups.items():
        if len(g) > 1:
            lines = ", ".join(str(m["line"]) for m in g)
            for msg in g:
                add("error", "msg-name-duplicate", msg["line"], f"{name}: name \"{name}\" - Must be unique (lines {lines}).", msg)
    idg: dict[tuple, list[dict]] = {}
    for msg in msgs:
        idg.setdefault((msg["extended"], msg["frame_id"]), []).append(msg)
    for (ext, fid), g in idg.items():
        if len(g) > 1:
            for msg in g:
                others = ", ".join(f"{m['name']} (line {m['line']})" for m in g if m is not msg)
                add("error", "can-id-duplicate", msg["line"], f"{msg['name']}: CAN ID {hex_id(fid, ext)} also used by {others}. The CSS decoders stop on "
                    f"duplicate IDs within a channel.", msg)
    pg: dict[int, list[dict]] = {}
    for msg in msgs:
        if j1939_msg(msg):
            pg.setdefault(extract_pgn(msg["frame_id"]), []).append(msg)
    for pgn, g in pg.items():
        ids = {m["frame_id"] for m in g}
        if len(g) > 1 and len(ids) > 1:  # identical IDs are already reported as can-id-duplicate
            for msg in g:
                others = ", ".join(m["name"] for m in g if m is not msg)
                add("error", "pgn-duplicate", msg["line"], f"{msg['name']}: PGN 0x{pgn:X} ({pgn}) also used by {others}. With ProtocolType J1939 the "
                    f"CSS decoders treat this as a key collision and stop (keep one message per PGN, or set "
                    f"BA_ \"ProtocolType\" \"\" for exact 29-bit ID matching).", msg)
    return out


# ---------------------------------------------------------------------------------------------- layer 1

class _Collect(logging.Handler):
    def __init__(self):
        super().__init__(logging.WARNING)
        self.records: list[str] = []

    def emit(self, record):
        self.records.append(record.getMessage())


def cantools_layer(path: Path) -> dict:
    res = {"available": True, "strict": None, "lenient": None, "load_warnings": []}
    try:
        import cantools
    except ImportError:
        res["available"] = False
        return res
    logger = logging.getLogger("cantools")
    handler = _Collect()
    logger.addHandler(handler)
    old_level, old_prop = logger.level, logger.propagate
    logger.setLevel(logging.WARNING)
    logger.propagate = False
    try:
        for strict in (True, False):
            handler.records.clear()
            try:
                db = cantools.database.load_file(str(path), strict=strict)
                r = {"ok": True, "messages": len(db.messages), "signals": sum(len(m.signals) for m in db.messages)}
            except Exception as e:  # noqa: BLE001 - report any parse/validation error verbatim
                r = {"ok": False, "error": f"{type(e).__name__}: {e}"}
            res["strict" if strict else "lenient"] = r
            if not strict:
                res["load_warnings"] = list(dict.fromkeys(handler.records))
                res["value_range"] = value_table_outside_range(db) if r["ok"] else []
    finally:
        logger.removeHandler(handler)
        logger.setLevel(old_level)
        logger.propagate = old_prop
    return res


ERROR_LABEL_RE = re.compile(r"error|invalid|not ?avail|n/a|\bsnv\b|abnormal|fault|reserved|undefined|"
                            r"open circuit|short circuit|not supported|init", re.I)


def value_table_outside_range(db) -> list[tuple[str, str, str]]:
    """(message, signal, text) for value table entries whose physical value is outside the signal's min/max.
    The CSS decoders write NULL for such values, so the state never shows up in the decoded data. Error/invalid
    codes (ERROR_LABEL_RE) are skipped: NULL is the intended result for them."""
    out = []
    for m in db.messages:
        for s in m.signals:
            lo, hi = s.minimum, s.maximum
            if not s.choices or lo is None or hi is None or (lo == 0 and hi == 0):
                continue
            bad = [(int(k), str(v)) for k, v in s.choices.items()
                   if not lo <= int(k) * s.scale + s.offset <= hi and not ERROR_LABEL_RE.search(str(v))]
            if bad:
                shown = ", ".join(f"{k} \"{v}\"" for k, v in bad[:4]) + (" ..." if len(bad) > 4 else "")
                out.append((m.name, s.name, f"{m.name}.{s.name}: value table entries outside min/max "
                            f"[{fmt_num(lo)}|{fmt_num(hi)}]: {shown} - the CSS decoders write NULL for these states"))
    return out


# ---------------------------------------------------------------------------------------------- fix

def apply_safe_fixes(text: str, parsed: dict) -> tuple[str, list[str]]:
    """Return (fixed text, change log). Only SG_ colon spacing + float/double sign."""
    fix_lines: dict[int, dict] = {}
    for msg in parsed["messages"]:
        for s in msg["signals"]:
            fix_lines[s["line"]] = s
    lines = text.splitlines(keepends=True)
    changes = []
    for i, line in enumerate(lines, start=1):
        s = fix_lines.get(i)
        if not s:
            continue
        body = line.rstrip("\r\n")
        ending = line[len(body):]
        new = body
        if sg_colon_spacing_issue(new):
            c = new.find(":")
            new = new[:c].rstrip() + " : " + new[c + 1:].lstrip()
        if s["valtype"] in (1, 2) and not s["signed"]:
            new = re.sub(r"@([01])\+", r"@\1-", new, count=1)
        if new != body:
            changes.append(f"L{i}: {body.strip()}  ->  {new.strip()}")
            lines[i - 1] = new + ending
    return "".join(lines), changes


# ---------------------------------------------------------------------------------------------- main

def lint_file(path: Path) -> dict:
    text, enc = read_text(path)
    parsed = parse_raw(text)
    issues = run_checks(parsed)
    ct = cantools_layer(path)
    vff_no_default = re.search(r'^\s*BA_DEF_\s+BO_\s+"VFrameFormat"', text, re.M) and \
        not re.search(r'^\s*BA_DEF_DEF_\s+"VFrameFormat"', text, re.M)
    if ct["available"] and vff_no_default and ct["lenient"] and not ct["lenient"]["ok"] \
            and "frame_format" in ct["lenient"]["error"]:
        # cantools limitation, not a DBC error: The CSS decoders and the DBC editor load these files (e.g. the CSS
        # J1939-73 DM1 DBCs). Report it as a warning so Python tools users know why cantools fails
        issues.append(issue("warning", "cantools-vframeformat", None,
                            "cantools cannot load this file: VFrameFormat is defined (BA_DEF_ BO_) without a default "
                            "(BA_DEF_DEF_). The CSS decoders are not affected. For Python tools (cantools) add e.g. "
                            "BA_DEF_DEF_ \"VFrameFormat\" \"J1939PG\"; (J1939) or \"StandardCAN\""))
    elif ct["available"]:
        if ct["strict"] and not ct["strict"]["ok"]:
            issues.insert(0, issue("error" if not ct["lenient"]["ok"] else "warning", "cantools-strict", None,
                                   f"cantools strict=True load failed: {ct['strict']['error']}"))
        if ct["lenient"] and not ct["lenient"]["ok"]:
            issues.insert(0, issue("error", "cantools-load", None, f"cantools strict=False load failed: {ct['lenient']['error']}"))
        for w in ct["load_warnings"]:
            issues.append(issue("warning", "cantools-load-warning", None, f"cantools: {w}"))
        for mname, sname, msg_text in ct.get("value_range", []):
            issues.append(issue("warning", "val-range", None, msg_text, mname, sname))
    issues.sort(key=lambda x: (0 if x["severity"] == "error" else 1, x["check"], x["line"] or 0))
    msgs = [m for m in parsed["messages"] if m["dbc_id"] != VECTOR_INDEPENDENT_ID]
    counts = {"errors": sum(1 for i in issues if i["severity"] == "error"),
              "warnings": sum(1 for i in issues if i["severity"] == "warning")}
    by_check: dict[str, dict] = {}
    for i in issues:
        k = f"{i['severity']}:{i['check']}"
        by_check[k] = by_check.get(k, 0) + 1
    return {"schema": "canedge-dbc-lint/1", "file": str(path), "encoding": enc,
            "stats": {"messages": len(msgs), "signals": sum(len(m["signals"]) for m in msgs),
                      "extended_ids": sum(1 for m in msgs if m["extended"]),
                      "multiplexed_messages": sum(1 for m in msgs if any(s["mux"] for s in m["signals"]))},
            "cantools": ct, "counts": counts, "by_check": by_check, "issues": issues,
            "_parsed": parsed, "_text": text}


def print_report(res: dict, limit: int) -> None:
    st = res["stats"]
    print(f"DBC lint: {res['file']}")
    print(f"  {st['messages']} messages ({st['extended_ids']} extended), {st['signals']} signals, "
          f"{st['multiplexed_messages']} multiplexed messages, encoding {res['encoding']}")
    ct = res["cantools"]
    print("Layer 1 - cantools")
    if not ct["available"]:
        print("  cantools not installed - skipped")
    else:
        for k in ("strict", "lenient"):
            r = ct[k]
            label = "strict=True " if k == "strict" else "strict=False"
            print(f"  {label}: " + (f"OK ({r['messages']} messages, {r['signals']} signals)" if r["ok"] else f"FAIL - {r['error']}"))
        if ct["load_warnings"]:
            print(f"  {len(ct['load_warnings'])} load warning(s) (listed below as cantools-load-warning)")
    print("Layer 2 - DBC editor checks + extras")
    for sev in ("error", "warning"):
        items = [i for i in res["issues"] if i["severity"] == sev]
        print(f"  {sev.upper()}S ({len(items)})")
        checks: dict[str, list[dict]] = {}
        for i in items:
            checks.setdefault(i["check"], []).append(i)
        for check, lst in checks.items():
            print(f"    [{check}] x{len(lst)}")
            for i in lst[:limit]:
                loc = f"L{i['line']}" if i["line"] else "    "
                print(f"      {loc:>7}  {i['text']}")
            if len(lst) > limit:
                print(f"      ... {len(lst) - limit} more (use --json or --limit 0 for all)")
    c = res["counts"]
    print(f"Summary: {c['errors']} error(s), {c['warnings']} warning(s)")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("dbc", type=Path)
    ap.add_argument("--json", action="store_true", help="print the full result as JSON")
    ap.add_argument("--limit", type=int, default=10, help="max issues printed per check (0 = all), text mode only")
    ap.add_argument("--fix", action="store_true", help="apply SAFE mechanical fixes and write them to --out")
    ap.add_argument("--out", type=Path, help="output path for --fix (must differ from the input)")
    args = ap.parse_args()

    if not args.dbc.is_file():
        print(f"Not found: {args.dbc}", file=sys.stderr)
        return 2
    res = lint_file(args.dbc)

    if args.fix:
        if not args.out:
            print("--fix needs --out <path> (the input is never overwritten)", file=sys.stderr)
            return 2
        if args.out.resolve() == args.dbc.resolve():
            print("--out must differ from the input file", file=sys.stderr)
            return 2
        fixed, changes = apply_safe_fixes(res["_text"], res["_parsed"])
        if not changes:
            print("No safe mechanical fixes to apply - nothing written.")
        else:
            args.out.parent.mkdir(parents=True, exist_ok=True)
            enc = "utf-8" if res["encoding"] == "utf-8" else "cp1252"
            args.out.write_bytes(fixed.encode(enc, errors="replace"))
            print(f"Applied {len(changes)} safe fix(es) -> {args.out}")
            for c in changes[:args.limit or None]:
                print(f"  {c}")
            if args.limit and len(changes) > args.limit:
                print(f"  ... {len(changes) - args.limit} more")
            print("Re-linting the fixed file:\n")
            res = lint_file(args.out)

    if args.json:
        print(json.dumps({k: v for k, v in res.items() if not k.startswith("_")}, indent=1))
    else:
        print_report(res, args.limit if args.limit > 0 else 10**9)
    return 1 if res["counts"]["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
