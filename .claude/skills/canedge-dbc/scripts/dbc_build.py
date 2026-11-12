"""Build a DBC file from a structured spec (JSON or CSV), validate it with dbc_lint and print a summary.

Typical use: The agent first extracts a PDF / Excel / Word protocol spec into the spec format, then builds the DBC
here instead of hand-writing DBC syntax. The spec format is documented in references/build-spec.md.

Usage:
  python dbc_build.py --spec spec.json --out new.dbc [--fill-minmax] [--overwrite] [--json]
  python dbc_build.py --spec spec.csv  --out new.dbc

Supports: Standard/extended IDs, CAN FD (VFrameFormat), J1939PG frame format, little/big endian (big endian start as
the DBC MSB start bit, or as "lsb_start"), signed, IEEE float/double, value tables, comments, units, receivers,
simple multiplexing (one multiplexor per message) and the GenMsgCycleTime attribute.

--fill-minmax fills missing signal min/max with the representable physical range (from length/signed/factor/offset).
The output file is never overwritten unless --overwrite is passed. Exit code: 0 = built + no lint errors,
1 = built but lint found errors, 2 = spec error.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import dbc_lint  # noqa: E402  (same skill)

TRUE = {"1", "true", "yes", "y", "x"}
FRAME_FORMATS = ["StandardCAN", "ExtendedCAN", "reserved", "J1939PG", "reserved", "reserved", "reserved", "reserved",
                 "reserved", "reserved", "reserved", "reserved", "reserved", "reserved", "StandardCAN_FD", "ExtendedCAN_FD"]


class SpecError(Exception):
    pass


def as_bool(v, default=False) -> bool:
    if v is None or v == "":
        return default
    if isinstance(v, bool):
        return v
    return str(v).strip().lower() in TRUE


def as_num(v, field: str, where: str, default=None):
    if v is None or v == "":
        return default
    try:
        f = float(v)
    except (TypeError, ValueError):
        raise SpecError(f"{where}: {field} {v!r} is not a number")
    return int(f) if f.is_integer() and abs(f) < 1e15 else f  # ints print as "5", not "5.0", in the DBC


def parse_id(v, where: str) -> int:
    if isinstance(v, int):
        return v
    s = str(v).strip()
    try:
        return int(s, 16) if s.lower().startswith("0x") or re.search(r"[a-fA-F]", s) else int(s)
    except ValueError:
        raise SpecError(f"{where}: id {v!r} is not a valid CAN ID (use hex 0x.. or decimal)")


def byte_order(v, where: str) -> str:
    s = str(v if v not in (None, "") else "little").strip().lower()
    if s in ("little", "intel", "little_endian", "le", "1"):
        return "little_endian"
    if s in ("big", "motorola", "big_endian", "be", "0"):
        return "big_endian"
    raise SpecError(f"{where}: byte_order {v!r} must be little/intel or big/motorola")


def msb_from_lsb(lsb: int, length: int) -> int:
    """Big endian: DBC start bit (MSB, sawtooth numbering) from the LSB position."""
    pos = lsb
    for _ in range(length - 1):
        pos = pos - 15 if pos % 8 == 7 else pos + 1
    return pos


def to_key(k) -> int:
    s = str(k).strip()
    return int(s, 16) if s.lower().startswith("0x") else int(float(s))


def parse_values(v) -> dict[int, str]:
    if not v:
        return {}
    if isinstance(v, dict):
        return {to_key(k): str(t) for k, t in v.items()}
    out = {}
    for part in str(v).split(";"):
        if "=" in part:
            k, t = part.split("=", 1)
            out[to_key(k)] = t.strip()
    return out


def load_spec(path: Path) -> dict:
    if path.suffix.lower() == ".json":
        return json.loads(path.read_text(encoding="utf-8"))
    if path.suffix.lower() != ".csv":
        raise SpecError("spec must be .json or .csv")
    text = path.read_text(encoding="utf-8-sig")
    dialect = csv.Sniffer().sniff(text.splitlines()[0], delimiters=",;\t")
    msgs: dict[tuple, dict] = {}
    nodes: set[str] = set()
    for i, row in enumerate(csv.DictReader(text.splitlines(), dialect=dialect), start=2):
        row = {k.strip().lower(): (v or "").strip() for k, v in row.items() if k}
        if not row.get("message_id") and not row.get("message_name"):
            continue
        key = (row.get("message_id"), row.get("message_name"))
        m = msgs.setdefault(key, {"id": row.get("message_id"), "name": row.get("message_name"), "signals": []})
        for src, dst in (("extended", "extended"), ("dlc", "dlc"), ("sender", "sender"), ("cycle_time_ms", "cycle_time_ms"),
                         ("message_comment", "comment"), ("fd", "fd"), ("frame_format", "frame_format")):
            if row.get(src) and dst not in m:
                m[dst] = row[src]
        if m.get("sender"):
            nodes.add(m["sender"])
        if row.get("signal_name"):
            sig = {k: row.get(k) for k in ("start", "lsb_start", "length", "byte_order", "signed", "float", "factor",
                                           "offset", "min", "max", "unit", "values", "multiplexer", "mux_id")}
            sig["name"] = row["signal_name"]
            sig["comment"] = row.get("signal_comment") or row.get("comment")
            sig["receivers"] = [r for r in re.split(r"[ ,|]+", row.get("receivers", "")) if r]
            sig["_row"] = i
            m["signals"].append(sig)
    return {"nodes": sorted(nodes), "messages": list(msgs.values())}


def build(spec: dict, fill_minmax: bool):
    from cantools.database.can import Database, Message, Node, Signal
    from cantools.database.conversion import BaseConversion

    defaults = spec.get("defaults", {})
    messages, frame_formats, notes = [], {}, []
    node_names = list(dict.fromkeys(spec.get("nodes", [])))
    for mi, m in enumerate(spec.get("messages", [])):
        where = f"message {m.get('name') or mi}"
        if not m.get("name"):
            raise SpecError(f"{where}: missing name")
        if len(m["name"]) > 32:
            raise SpecError(f"{where}: name longer than 32 characters (the CSS decoders would use it cut to 32)")
        fid = parse_id(m.get("id"), where)
        extended = as_bool(m.get("extended"), as_bool(defaults.get("extended"), fid > 0x7FF))
        if fid & 0x80000000:  # DBC-style ID with the extended flag
            fid, extended = fid & 0x1FFFFFFF, True
        fd = as_bool(m.get("fd"), as_bool(defaults.get("fd")))
        dlc = as_num(m.get("dlc"), "dlc", where, 64 if fd else 8)
        sender = m.get("sender") or defaults.get("sender")
        if sender and sender not in node_names and sender != "Vector__XXX":
            node_names.append(sender)
        ff = m.get("frame_format") or defaults.get("frame_format")
        if ff and ff not in FRAME_FORMATS:
            raise SpecError(f"{where}: frame_format {ff!r} must be one of StandardCAN, ExtendedCAN, J1939PG, StandardCAN_FD, ExtendedCAN_FD")
        if fd and not ff:
            ff = "ExtendedCAN_FD" if extended else "StandardCAN_FD"
        signals = []
        muxers = [s["name"] for s in m.get("signals", []) if as_bool(s.get("multiplexer"))]
        for si, s in enumerate(m.get("signals", [])):
            sw = f"{where} signal {s.get('name') or si}" + (f" (CSV row {s['_row']})" if s.get("_row") else "")
            if not s.get("name"):
                raise SpecError(f"{sw}: missing name")
            if len(s["name"]) > 32:
                raise SpecError(f"{sw}: name longer than 32 characters (the CSS decoders would use it cut to 32)")
            length =as_num(s.get("length"), "length", sw)
            if length is None:
                raise SpecError(f"{sw}: missing length")
            order = byte_order(s.get("byte_order") or defaults.get("byte_order"), sw)
            start = as_num(s.get("start"), "start", sw)
            lsb_start = as_num(s.get("lsb_start"), "lsb_start", sw)
            if start is None and lsb_start is None:
                raise SpecError(f"{sw}: missing start (or lsb_start)")
            if start is None:
                start = lsb_start if order == "little_endian" else msb_from_lsb(lsb_start, length)
                if start < 0:
                    raise SpecError(f"{sw}: lsb_start {lsb_start} with length {length} runs past byte 0 (big endian: the "
                                    f"MSB would sit before the frame start) - check the bit numbering")
            is_float = as_bool(s.get("float"))
            signed = as_bool(s.get("signed")) or is_float
            factor = as_num(s.get("factor"), "factor", sw, 1)
            offset = as_num(s.get("offset"), "offset", sw, 0)
            vmin, vmax = as_num(s.get("min"), "min", sw), as_num(s.get("max"), "max", sw)
            if fill_minmax and not is_float and (vmin is None or vmax is None):
                raw_lo = -(2 ** (length - 1)) if signed else 0
                raw_hi = 2 ** (length - 1) - 1 if signed else 2 ** length - 1
                a, b = factor * raw_lo + offset, factor * raw_hi + offset
                vmin = as_num(round(min(a, b), 12), "min", sw) if vmin is None else vmin
                vmax = as_num(round(max(a, b), 12), "max", sw) if vmax is None else vmax
            mux_ids = s.get("mux_id")
            if isinstance(mux_ids, str):
                mux_ids = [int(x) for x in re.split(r"[ ,;]+", mux_ids) if x] if mux_ids.strip() else None
            elif isinstance(mux_ids, int):
                mux_ids = [mux_ids]
            mux_signal = s.get("mux_signal") or (muxers[0] if mux_ids and len(muxers) == 1 else None)
            if mux_ids and not mux_signal:
                raise SpecError(f"{sw}: mux_id given but the message has no single multiplexer signal (set mux_signal)")
            rx = s.get("receivers") or []
            for r in rx:
                if r not in node_names and r != "Vector__XXX":
                    node_names.append(r)
            signals.append(Signal(
                name=s["name"], start=start, length=length, byte_order=order, is_signed=signed,
                conversion=BaseConversion.factory(factor, offset, parse_values(s.get("values")) or None, is_float),
                minimum=vmin, maximum=vmax, unit=s.get("unit") or None, comment=s.get("comment") or None,
                receivers=rx, is_multiplexer=as_bool(s.get("multiplexer")), multiplexer_ids=mux_ids,
                multiplexer_signal=mux_signal))
        cycle = as_num(m.get("cycle_time_ms"), "cycle_time_ms", where)
        messages.append(Message(frame_id=fid, name=m["name"], length=dlc, signals=signals, comment=m.get("comment") or None,
                                senders=[sender] if sender else None, cycle_time=cycle, is_extended_frame=extended,
                                is_fd=fd, strict=False, sort_signals=None))
        if ff:
            frame_formats[(fid | 0x80000000) if extended else fid] = ff
    db = Database(messages=messages, nodes=[Node(n) for n in node_names], version=spec.get("version", ""),
                  strict=False, sort_signals=None)
    text = db.as_dbc_string(sort_signals=None)
    if frame_formats:
        text = add_vframeformat(text, frame_formats)
        notes.append(f"VFrameFormat set on {len(frame_formats)} message(s)")
    return text, notes


def add_vframeformat(text: str, formats: dict[int, str]) -> str:
    """Insert the VFrameFormat attribute (cantools does not write it) in the right section order."""
    lines = text.split("\n")

    def first(prefixes, after=0):
        for i in range(after, len(lines)):
            if lines[i].startswith(prefixes):
                return i
        return len(lines)
    enum = ",".join(f'"{x}"' for x in FRAME_FORMATS)
    i = first(("BA_DEF_DEF_", "BA_ ", "VAL_ ", "SIG_VALTYPE_", "SG_MUL_VAL_"))
    lines.insert(i, f'BA_DEF_ BO_  "VFrameFormat" ENUM  {enum};')
    j = first(("BA_ ", "VAL_ ", "SIG_VALTYPE_", "SG_MUL_VAL_"), i + 1)
    lines.insert(j, 'BA_DEF_DEF_  "VFrameFormat" "StandardCAN";')
    k = first(("VAL_ ", "SIG_VALTYPE_", "SG_MUL_VAL_"), j + 1)
    for n, (dbc_id, ff) in enumerate(formats.items()):
        lines.insert(k + n, f'BA_ "VFrameFormat" BO_ {dbc_id} {FRAME_FORMATS.index(ff)};')
    return "\n".join(lines)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", type=Path, required=True, help="spec .json or .csv (see references/build-spec.md)")
    ap.add_argument("--out", type=Path, required=True, help="output .dbc")
    ap.add_argument("--fill-minmax", action="store_true", help="fill missing min/max with the representable range")
    ap.add_argument("--overwrite", action="store_true", help="allow overwriting an existing --out file")
    ap.add_argument("--json", action="store_true", help="print the lint result as JSON")
    args = ap.parse_args()
    if args.out.exists() and not args.overwrite:
        print(f"{args.out} exists - pass --overwrite to replace it", file=sys.stderr)
        return 2
    try:
        spec = load_spec(args.spec)
        text, notes = build(spec, args.fill_minmax)
    except SpecError as e:
        print(f"Spec error: {e}", file=sys.stderr)
        return 2
    except Exception as e:  # noqa: BLE001 - cantools validation errors (e.g. bad lengths)
        print(f"Build failed: {type(e).__name__}: {e}", file=sys.stderr)
        return 2
    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(text.replace("\r\n", "\n"), encoding="utf-8", newline="\r\n")  # cantools gives \r\n already
    res = dbc_lint.lint_file(args.out)
    st, c = res["stats"], res["counts"]
    if args.json:
        print(json.dumps({k: v for k, v in res.items() if not k.startswith("_")}, indent=1))
    else:
        print(f"Built {args.out}: {st['messages']} messages ({st['extended_ids']} extended, "
              f"{st['multiplexed_messages']} multiplexed), {st['signals']} signals")
        for n in notes:
            print(f"  {n}")
        print(f"Lint: {c['errors']} error(s), {c['warnings']} warning(s)")
        for i in res["issues"][:30]:
            print(f"  {i['severity']:<7} [{i['check']}] L{i['line'] or '-'} {i['text']}")
        if len(res["issues"]) > 30:
            print(f"  ... run dbc_lint.py {args.out} for all {len(res['issues'])} issues")
    return 1 if c["errors"] else 0


if __name__ == "__main__":
    sys.exit(main())
