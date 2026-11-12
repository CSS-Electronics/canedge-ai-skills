"""Semantic diff of two DBC files (cantools, strict=False): messages/signals added, removed and changed fields.

Compares what matters for decoding, not the text: Messages are matched by (extended, CAN ID) - and by name when
an ID is used more than once - signals by name within a message. Use it to prove that a DBC built from a PDF/Excel
spec matches an official DBC, or to review what changed between two DBC versions.

Usage:
  python dbc_diff.py a.dbc b.dbc [--json] [--ignore comments,units,minmax,receivers,senders,choices,cycle_time]
                                 [--limit 50] [--html changes.html]

--html: One self-contained page for the user (open in a browser): Diff tab = both files side by side (diff2html, as
in the DBC editor and the config editor's Review changes), Changes tab = the semantic changes as a table.

Compared fields:
  message: name, DLC, extended, CAN FD, cycle time, senders, comment
  signal:  start, length, byte order, signed, float, factor, offset, min, max, unit, value table, multiplexer role
           (M / mux ids / parent), receivers, comment
Removed/added signals with the same start|length|order in the other file are reported as likely renames.
Numbers are compared with a relative tolerance of 1e-9. Exit code: 0 = identical semantics, 1 = differences.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(SKILLS / "canedge-workspace" / "scripts"), str(SKILLS / "canedge-reports" / "scripts")]
from dbc_groups import fmt_can_id, load_dbc  # noqa: E402
from paths import open_line  # noqa: E402

IGNORABLE = {"comments", "units", "minmax", "receivers", "senders", "choices", "cycle_time"}


def load(path: Path):
    return [m for m in load_dbc(path).messages if m.name != "VECTOR__INDEPENDENT_SIG_MSG"]


def hex_id(m) -> str:
    return fmt_can_id(m.frame_id, m.is_extended_frame)


def same(a, b) -> bool:
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return math.isclose(float(a), float(b), rel_tol=1e-9, abs_tol=1e-12)
    return a == b


def msg_fields(m, ignore: set) -> dict:
    f = {"name": m.name, "dlc": m.length, "extended": m.is_extended_frame, "fd": m.is_fd}
    if "cycle_time" not in ignore:
        f["cycle_time_ms"] = m.cycle_time
    if "senders" not in ignore:
        f["senders"] = sorted(m.senders or [])
    if "comments" not in ignore:
        f["comment"] = m.comment or ""
    return f


def sig_fields(s, ignore: set) -> dict:
    f = {"start": s.start, "length": s.length, "byte_order": s.byte_order, "signed": s.is_signed,
         "float": s.is_float, "factor": s.scale, "offset": s.offset,
         "mux": ("M" if s.is_multiplexer else "") + (f"m{sorted(s.multiplexer_ids)}" if s.multiplexer_ids else ""),
         "mux_parent": s.multiplexer_signal or ""}
    if "minmax" not in ignore:
        f["min"], f["max"] = s.minimum, s.maximum
    if "units" not in ignore:
        f["unit"] = s.unit or ""
    if "choices" not in ignore:
        f["values"] = {int(k): str(v) for k, v in (s.choices or {}).items()}
    if "receivers" not in ignore:
        f["receivers"] = sorted(r for r in (s.receivers or []) if r != "Vector__XXX")
    if "comments" not in ignore:
        f["comment"] = s.comment or ""
    return f


def field_changes(a: dict, b: dict) -> list[dict]:
    out = []
    for k in a:
        if not same(a[k], b.get(k)):
            out.append({"field": k, "a": a[k], "b": b.get(k)})
    return out


def match_messages(ma: list, mb: list):
    """Yield (a, b) pairs; None on one side = removed/added."""
    def key(m):
        return (m.is_extended_frame, m.frame_id)
    ga, gb = {}, {}
    for m in ma:
        ga.setdefault(key(m), []).append(m)
    for m in mb:
        gb.setdefault(key(m), []).append(m)
    pairs = []
    for k in sorted(set(ga) | set(gb)):
        la, lb = list(ga.get(k, [])), list(gb.get(k, []))
        if len(la) == 1 and len(lb) == 1:
            pairs.append((la[0], lb[0]))
            continue
        for m in list(la):  # several messages on one ID: pair by name
            hit = next((x for x in lb if x.name == m.name), None)
            if hit:
                pairs.append((m, hit))
                la.remove(m)
                lb.remove(hit)
        for x, y in zip(la, lb):
            pairs.append((x, y))
        pairs += [(m, None) for m in la[len(lb):]] + [(None, m) for m in lb[len(la):]]
    return pairs


def diff(a_path: Path, b_path: Path, ignore: set) -> dict:
    ma, mb = load(a_path), load(b_path)
    res = {"a": str(a_path), "b": str(b_path), "ignored": sorted(ignore),
           "messages_a": len(ma), "messages_b": len(mb), "removed": [], "added": [], "changed": []}
    for a, b in match_messages(ma, mb):
        if b is None:
            res["removed"].append({"id": hex_id(a), "name": a.name, "signals": len(a.signals)})
            continue
        if a is None:
            res["added"].append({"id": hex_id(b), "name": b.name, "signals": len(b.signals)})
            continue
        entry = {"id": hex_id(a), "name": a.name, "fields": field_changes(msg_fields(a, ignore), msg_fields(b, ignore)),
                 "signals_removed": [], "signals_added": [], "signals_changed": [], "renames": []}
        sa = {s.name: s for s in a.signals}
        sb = {s.name: s for s in b.signals}
        for n in sa:
            if n not in sb:
                entry["signals_removed"].append(n)
            else:
                ch = field_changes(sig_fields(sa[n], ignore), sig_fields(sb[n], ignore))
                if ch:
                    entry["signals_changed"].append({"signal": n, "fields": ch})
        entry["signals_added"] = [n for n in sb if n not in sa]
        for n in entry["signals_removed"]:
            geo = (sa[n].start, sa[n].length, sa[n].byte_order)
            for m in entry["signals_added"]:
                if (sb[m].start, sb[m].length, sb[m].byte_order) == geo:
                    entry["renames"].append({"a": n, "b": m})
        if entry["fields"] or entry["signals_removed"] or entry["signals_added"] or entry["signals_changed"]:
            res["changed"].append(entry)
    res["identical"] = not (res["removed"] or res["added"] or res["changed"])
    res["counts"] = {"messages_removed": len(res["removed"]), "messages_added": len(res["added"]),
                     "messages_changed": len(res["changed"]),
                     "signals_removed": sum(len(c["signals_removed"]) for c in res["changed"]),
                     "signals_added": sum(len(c["signals_added"]) for c in res["changed"]),
                     "signals_changed": sum(len(c["signals_changed"]) for c in res["changed"])}
    return res


def text_diff(a_path: Path, b_path: Path) -> dict:
    """Unified line diff of the two files as text (line endings + trailing spaces ignored), for diff2html."""
    from dashboard import line_diff  # noqa: E402 - canedge-reports
    from dbc_lint import read_text
    names = [a_path.name, b_path.name] if a_path.name != b_path.name else [f"{p.parent.name}/{p.name}" for p in (a_path, b_path)]
    return line_diff(*[[ln.rstrip() for ln in read_text(p)[0].splitlines()] for p in (a_path, b_path)], names)


def build_html(res: dict, a_path: Path, b_path: Path) -> str:
    """Self-contained page: assets/dbc-diff.html + .js on the canedge-reports dashboard kit + diff2html (vendored)."""
    import datetime as dt
    from dashboard import build_html as kit_html, diff_parts  # noqa: E402
    assets = Path(__file__).resolve().parent.parent / "assets"
    text = text_diff(a_path, b_path)
    data = {"a": {"name": a_path.name, "messages": res["messages_a"]}, "b": {"name": b_path.name, "messages": res["messages_b"]},
            "ignored": res["ignored"], "semantic": res, "text": text,
            "notes": ["Original = the first file (left), new = the second file (right)",
                      "Changes compare what the decoders use: Messages by CAN ID, signals by name. "
                      "Likely renames = a removed + an added signal with the same start, length and byte order"],
            "generated": dt.datetime.now(dt.timezone.utc).strftime("%y/%m/%d %H:%M UTC")}
    return kit_html("DBC changes", assets / "dbc-diff.js", {"D": data}, template=assets / "dbc-diff.html",
                    extra=diff_parts())


def short(v) -> str:
    s = json.dumps(v, default=str) if isinstance(v, (dict, list)) else str(v)
    return s if len(s) <= 60 else s[:57] + "..."


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("a", type=Path)
    ap.add_argument("b", type=Path)
    ap.add_argument("--ignore", default="", help=f"comma list of: {', '.join(sorted(IGNORABLE))}")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--limit", type=int, default=50, help="max changed messages printed (text mode)")
    ap.add_argument("--html", type=Path, help="also write a before/after page (Diff + Changes tabs) to this file")
    args = ap.parse_args()
    ignore = {x.strip() for x in args.ignore.split(",") if x.strip()}
    bad = ignore - IGNORABLE
    if bad:
        print(f"Unknown --ignore value(s): {', '.join(bad)}", file=sys.stderr)
        return 2
    for p in (args.a, args.b):
        if not p.is_file():
            print(f"Not found: {p}", file=sys.stderr)
            return 2
    try:
        res = diff(args.a, args.b, ignore)
    except Exception as e:  # noqa: BLE001
        print(f"cantools cannot load a file: {e}. Run dbc_lint.py on it.", file=sys.stderr)
        return 2
    if args.html:
        args.html.parent.mkdir(parents=True, exist_ok=True)
        args.html.write_text(build_html(res, args.a, args.b), encoding="utf-8")
        print(f"Page: {args.html.resolve()}", file=sys.stderr if args.json else sys.stdout)
        print(open_line(args.html, "report"), file=sys.stderr if args.json else sys.stdout)
    if args.json:
        print(json.dumps(res, indent=1, default=str))
        return 0 if res["identical"] else 1
    c = res["counts"]
    print(f"A: {res['a']} ({res['messages_a']} messages)\nB: {res['b']} ({res['messages_b']} messages)")
    if ignore:
        print(f"Ignored: {', '.join(sorted(ignore))}")
    if res["identical"]:
        print("IDENTICAL semantics (all compared message and signal fields match)")
        return 0
    print(f"DIFFERENT: messages -{c['messages_removed']} +{c['messages_added']} ~{c['messages_changed']}, "
          f"signals -{c['signals_removed']} +{c['signals_added']} ~{c['signals_changed']}")
    for m in res["removed"]:
        print(f"  - {m['id']} {m['name']} ({m['signals']} signals) only in A")
    for m in res["added"]:
        print(f"  + {m['id']} {m['name']} ({m['signals']} signals) only in B")
    for e in res["changed"][:args.limit]:
        print(f"  ~ {e['id']} {e['name']}")
        for f in e["fields"]:
            print(f"      message.{f['field']}: {short(f['a'])} -> {short(f['b'])}")
        for n in e["signals_removed"]:
            print(f"      - {n}")
        for n in e["signals_added"]:
            print(f"      + {n}")
        for r in e["renames"]:
            print(f"      likely rename: {r['a']} -> {r['b']} (same start/length/order)")
        for s in e["signals_changed"]:
            print(f"      ~ {s['signal']}: " + "; ".join(f"{f['field']} {short(f['a'])} -> {short(f['b'])}" for f in s["fields"]))
    if len(res["changed"]) > args.limit:
        print(f"  ... {len(res['changed']) - args.limit} more changed messages (--json for all)")
    return 1


if __name__ == "__main__":
    sys.exit(main())
