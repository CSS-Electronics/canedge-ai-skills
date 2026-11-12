"""DBC meaning for lake tables: The DBC message behind each message table, with its comment and per signal the unit,
value table and comment. Lake first: Only tables that exist in the lake are annotated (a DBC often has far more
messages than the lake holds), and only the lake's columns.

Reuses canedge-dbc dbc_info.py (cantools load, signal rows) and canedge-workspace dbc_groups.py (channel from the
file name prefix, DBC groups per device).

  idx = DbcIndex("projects/<name>")      # project: dbc/ + dbc-groups.json (none = all DBCs for all devices)
  idx = DbcIndex("projects/<name>/dbc")  # or any folder / single .dbc file: All DBCs apply to all devices
  info = idx.match("12345678", "can1_eec1_s00")   # {dbc, message, id, comment, signals: {name: {unit, ...}}} | None

Signal names are the decoder's: A signal with a SystemSignalLongSymbol attribute is the SG_ name (the lake column),
not the long name cantools reports (kept as "long_name").
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]
for _p in (SKILLS / "canedge-dbc" / "scripts", SKILLS / "canedge-workspace" / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from dbc_groups import channels_for, group_for, load_groups  # noqa: E402
from dbc_info import hex_id, load, sig_row  # noqa: E402

LONG_SIG_RE = re.compile(r'BA_\s+"SystemSignalLongSymbol"\s+SG_\s+(\d+)\s+(\w+)\s+"([^"]*)"\s*;')


def decoder_names(path: str | Path) -> dict[tuple[int, str], str]:
    """(frame id, cantools signal name) -> SG_ name, for signals with a SystemSignalLongSymbol attribute. cantools
    reports the long name; the CSS decoders (and so the lake columns) use the SG_ name."""
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return {(int(i) & 0x7FFFFFFF, long): short for i, short, long in LONG_SIG_RE.findall(text) if long and long != short}


class DbcIndex:
    def __init__(self, path: str | Path):
        p = Path(path)
        if (p / "dbc").is_dir():                    # a project folder
            self.dir, self.groups = p / "dbc", load_groups(p)
        elif p.is_dir():
            self.dir = p
            self.groups = [{"devices": ["*"], "dbc_files": sorted(f.name for f in p.glob("*.dbc"))}]
        elif p.is_file():
            self.dir, self.groups = p.parent, [{"devices": ["*"], "dbc_files": [p.name]}]
        else:
            raise FileNotFoundError(f"No DBC folder or file at {p}")
        if not any(g.get("dbc_files") for g in self.groups):
            raise FileNotFoundError(f"No .dbc files in {self.dir}")
        self.errors: dict[str, str] = {}            # DBC file -> load error (the file is skipped)
        self._dbs: dict[str, object] = {}
        self._short: dict[str, dict] = {}           # DBC file -> decoder_names()
        self._msgs: dict[tuple[int, str], dict] = {}

    def files(self, device: str) -> list[str]:
        """The DBC files that apply to a device (its group, or all)."""
        g = self._group(device)
        return list(self.groups[g]["dbc_files"]) if g is not None else []

    def _group(self, device: str) -> int | None:
        hit = group_for(self.groups, device)
        return hit[0] if hit else None

    def _db(self, fname: str):
        if fname not in self._dbs:
            try:
                self._dbs[fname] = load(self.dir / fname)
                self._short[fname] = decoder_names(self.dir / fname)
            except Exception as e:  # noqa: BLE001 - a broken DBC: skip it, report it (dbc_lint.py has the details)
                self._dbs[fname] = None
                self.errors[fname] = f"{e.__class__.__name__}: {str(e).splitlines()[0][:200]}"
        return self._dbs[fname]

    def _messages(self, group: int, channel: str) -> dict:
        """Lower-case message name -> (file, cantools message) for one DBC group + channel."""
        key = (group, channel)
        if key not in self._msgs:
            out = {}
            for f in self.groups[group]["dbc_files"]:
                if channel in channels_for(f) and (db := self._db(f)) is not None:
                    for m in db.messages:
                        out.setdefault(m.name.lower(), (f, m))
            self._msgs[key] = out
        return self._msgs[key]

    def match(self, device: str, table_message: str) -> dict | None:
        """The DBC message of a lake table (message part of tbl_<device>_<message>, e.g. can1_eec1_s00): Channel from
        the prefix, then the longest DBC message name that the rest starts with (the decoder appends e.g. a source
        address _s00 or a multiplexor _<mux>_m<n>)."""
        g = self._group(device)
        ch, _, rest = table_message.lower().partition("_")
        if g is None or not rest:
            return None
        msgs = self._messages(g, ch.upper())
        name = max((n for n in msgs if rest == n or rest.startswith(n + "_")), key=len, default=None)
        if name is None:
            return None
        f, m = msgs[name]
        short = self._short.get(f, {})
        sigs = [_signal(s, short.get((m.frame_id, s.name))) for s in m.signals]
        return {"dbc": f, "message": m.name, "id": hex_id(m), "comment": m.comment or "",
                "suffix": rest[len(name):].lstrip("_"),
                "signals": {s["name"].lower(): s for s in sigs}}


def _signal(s, decoder_name: str | None = None) -> dict:
    """decoder_name: The SG_ name of a signal with a long name (SystemSignalLongSymbol); it is the lake column."""
    r = sig_row(s)
    out = {"name": decoder_name or r["name"], "unit": r["unit"], "comment": r["comment"],
           "value_table": r["value_table"], "min": r["min"], "max": r["max"]}
    if decoder_name:
        out["long_name"] = r["name"]
    return out


def text_hit(pattern, info: dict | None, column: str) -> str | None:
    """Where a pattern matches the DBC side of a lake column: 'unit', 'comment' or 'values' (value table labels)."""
    s = (info or {}).get("signals", {}).get(column)
    if not s:
        return None
    if s["unit"] and pattern.search(s["unit"]):
        return "unit"
    if s["comment"] and pattern.search(s["comment"]):
        return "comment"
    if any(pattern.search(v) for v in s["value_table"].values()):
        return "values"
    return None


def short(text: str, n: int = 90) -> str:
    t = " ".join(str(text or "").split())
    return t if len(t) <= n else t[:n - 3] + "..."


def values_text(vt: dict, n: int = 6) -> str:
    items = list(vt.items())
    return ", ".join(f"{k}={v}" for k, v in items[:n]) + (f", ... ({len(items)})" if len(items) > n else "")
