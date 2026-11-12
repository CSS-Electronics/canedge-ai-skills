"""Simulate a Configuration File on real log files: Filters, prescalers and control signals, frame by frame.

Input: Typically ONE CANedge log file (MF4/MFC/MFE/MFM) recorded with default (accept-all) filters; IDs blocked at
recording time are invisible. Each file is converted with the canedge-mdf2csv converter to a temporary CSV (deleted
at once). Every frame then runs through the config like on the device:
  - Filters (same logic as the config editor's filter builder, filterEvaluator.js): Per channel (CAN1 -> can_1,
    CAN2 -> can_2, CAN9 -> can_internal), enabled filters top to bottom, the FIRST match decides; the ID format must
    match; no match = not logged; remote frames are rejected if remote_frames is 0
  - Prescalers per ID: count N logs frames 1, N+1, 2N+1, ...; time T logs a frame >= T ms after the last logged
    one; data logs a frame when the masked bytes differ from the last logged frame. Max 100 prescaled IDs per
    channel, 15 on CAN9 (first seen); further IDs are logged unprescaled
  - Control signals (CANedge Docs, "Control signal"): Per channel section, control_rx_state gates logging and
    control_tx_state the transmit list; initial state = general.rx_state / tx_state. The start / stop message
    is matched by id + id_mask (mask filter rule, same ID format) on the source channel (chn 0 = CAN9, 1, 2),
    using only frames accepted by that channel's filters + prescalers. The value is decoded with cantools from
    the config's signal fields (DBC convention, unsigned, raw x factor + offset). Ranges are inclusive; if both
    match: start inside stop -> start wins, stop inside start -> stop wins (partial overlap: start, warned).
    The state carries over to the next split of the same session and restarts with a new session
  - Channels without a filter section (LIN) are kept; rx_state 0 without a control signal = nothing logged

Sizes: Frames weighted by data length (the config editor's MF4 weights, canedge-workspace tools.frame_weight), scaled so
the recorded total equals the real file size. Rates per recorded time, MB = 1e6 bytes (the editor divides by 1024^2,
so its MB/min reads ~5% lower). Compression turned on by the new config: 50% smaller (the editor's figure, not
measured); the page's headline, bars and log size KPIs include it, the measured reduction shows below the headline.

Per ID also: Median period and data changes (frames whose payload differs from the previous frame = what a data
prescaler on all bytes would log) to find prescaler candidates. Run with the current config as --config (no
--baseline) for the plain per-ID table of a log file.

--project: Match the IDs against the device's DBC files (project dbc/, per dbc-groups.json if present; exact ID,
J1939 by PGN, J1939 TP frames), via canedge-workspace dbc_groups.py; also names control signals. Optional.

CANmod.router config (detected from phy.can.route; the router report): The log files (CANedge + router) are demuxed
with mdf2mdf (mapping: --muxtp, else the project's router DBC, else the config's tunnel ID with the default channels
0x10 -> CAN11-14, 0x12 -> CAN15-18) and only the 4 channels of this router are simulated: CAN11 = S1 (phy.can_s1
filters), ... Router filters: mask-only, accept-only, the frame format must match, first match decides, no match =
rejected; prescalers as on the CANedge (cfg_common.py). Sizes = the router's share of the CANedge log file: The
tunnel frames' share of each file (weighted by data length) spread over the demuxed frames, so "after" = the CANedge
log size of this router's data with the new router filters. Total frames/s are compared to the ~6000 frames/s
guideline. Control signals, compression and remote frames do not apply.

Usage (from the repo root):
  simulate_config.py <log file(s)/folder> --config new.json [--baseline old.json] [--project projects/<name>]
                     [--html report.html] [--explore-mb 6] [--top 30] [--json] [--tool <mdf2csv>]
                     [--passwords passwords.json] [--muxtp 010:11]   (--muxtp: CANmod.router configs only)

--html Explore tab: Pick an ID and see each signal (DBC, else per data byte) as recorded vs logged under a rule the user
sets live (accept, reject, count / time / data prescaler; default = the new config). Values hold until the next logged
frame; logging-off periods of a control signal shown grey (and applied, can be switched off). Covers the busiest
window that fits --explore-mb (max 2M frames).
--html also writes report.json next to the page: The page's data without the Explore frames (KPIs, per-ID rows, notes,
config paths + CRC32 of the simulated config, the baseline and the config the log files were recorded with), so the
agent answers questions about the page from it.
"""
from __future__ import annotations

import argparse
import base64
import datetime as dt
import json
import sys
import zlib
from collections import defaultdict
from pathlib import Path

import numpy as np

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]
sys.path[:0] = [str(HERE), str(SKILLS / "canedge-workspace" / "scripts"), str(SKILLS / "canedge-reports" / "scripts")]
from cfg_common import (  # noqa: E402
    CHANNEL_TO_SECTION, MAX_PRESCALER_IDS, MAX_PRESCALER_IDS_PER_CHANNEL, enabled_filters, filter_matches, first_match, g, hexint,
    j1939_tp,
    crc32_hex, j1939_pgn, load_config, mask_secrets, prescaler_text, signal_bytes,
    ROUTER_FPS_GUIDE, is_router, router_channel_map, router_filters, router_match, router_tunnel,
)
import muxtp  # noqa: E402
from dbc_groups import fmt_can_id  # noqa: E402
from mf4_files import describe, fill_headers, find_log_files  # noqa: E402
from tools import EST_ANCHORS, mdf2csv_tables, password_file, passwords_refusal, resolve  # noqa: E402
from paths import open_line  # noqa: E402

COMPRESSION = 0.5  # MFC ~ 50% of MF4, as in the config editor's filter builder (not measured here)
SMALL_FILE_BYTES = 500_000  # uncompressed MF4 below this: The ~100 kB file header skews the sizes
TP_LABELS = {"ISOTP": "ISO-TP", "NMEATP": "NMEA 2000 Fast Packet", "J1939TP": "J1939 TP"}
TP_PREFIX_LABELS = {"MUXTP": "CANmod.router link"}   # MUXTP#11:12:13:14 (router DBC): The router's tunnel frames
COLS = ["TimestampEpoch", "BusChannel", "ID", "IDE", "DataLength", "RTR", "EDL", "DataBytes"]
SECTIONS = (("can_internal", "CAN9"), ("can_1", "CAN1"), ("can_2", "CAN2"))
CHN = {0: "CAN9", 1: "CAN1", 2: "CAN2"}
MAX_EXPLORE_FRAMES = 2_000_000  # Explore tab: Frames the page decodes at most (browser memory + time)
BINS = (1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600)


# --------------------------------------------------------------------------- #
# Filters + prescalers
# --------------------------------------------------------------------------- #

def decide_router(cfg: dict, rt: dict, channel: str, can_id: int, ide: int, fd: bool) -> dict:
    """Decision for an ID under a CANmod.router config: rt = demuxed channel -> section (CAN11 -> can_s1)."""
    sec = rt.get(channel)
    if sec is None:
        return {"fate": "accepted", "reason": "not a channel of this router"}
    s = sec.replace("can_s", "S")
    flt = router_filters(cfg, sec)
    if flt is None:
        return {"fate": "accepted", "reason": f"{s}: no filter list"}
    if not flt:
        return {"fate": "rejected", "reason": f"{s}: no enabled filters"}
    m = router_match(flt, can_id, ide, fd)
    if m is None:
        return {"fate": "rejected", "reason": f"{s}: no matching filter"}
    name = f" {m['name']}" if m.get("name") else ""
    if m.get("prescaler_type", 0):
        return {"fate": "prescaled", "reason": f"{prescaler_text(m)} ({s} filter #{m['_index']}{name})", "filter": m}
    return {"fate": "accepted", "reason": f"{s} filter #{m['_index']}{name}"}


def decide(cfg: dict | None, channel: str, can_id: int, ide: int, fd: bool = False, rt: dict | None = None) -> dict:
    """Decision for an ID under a config (CANedge Docs terms): {'fate': accepted|prescaled|rejected, 'reason', 'filter'}.
    rt = CANmod.router channel map (router configs)."""
    if cfg is None:
        return {"fate": "accepted", "reason": "as recorded"}
    if rt is not None:
        return decide_router(cfg, rt, channel, can_id, ide, fd)
    sec = CHANNEL_TO_SECTION.get(channel)
    if sec is None or sec not in cfg:
        return {"fate": "accepted", "reason": "no filter section for channel"}
    if g(cfg, sec, "general", "rx_state") == 0 and g(cfg, sec, "control", "control_rx_state") != 1:
        return {"fate": "rejected", "reason": "rx disabled"}
    flt = enabled_filters(cfg, sec)
    if flt is None:
        return {"fate": "accepted", "reason": "no filter list"}
    if not flt:
        return {"fate": "rejected", "reason": "no enabled filters"}
    m = first_match(flt, can_id, ide)
    if m is None:
        return {"fate": "rejected", "reason": "no matching filter"}
    name = f" {m['name']}" if m.get("name") else ""
    if m.get("type", 0) == 1:
        return {"fate": "rejected", "reason": f"filter #{m['_index']}{name}"}
    if m.get("prescaler_type", 0):
        return {"fate": "prescaled", "reason": f"{prescaler_text(m)} (filter #{m['_index']}{name})", "filter": m}
    return {"fate": "accepted", "reason": f"filter #{m['_index']}{name}"}


def prescale(f: dict, t: np.ndarray, data: np.ndarray) -> np.ndarray:
    """Positions of the frames one ID's prescaler logs (t in ms, sorted) - port of filterEvaluator applyPrescaler."""
    pt, pv = f.get("prescaler_type", 0), f.get("prescaler_value")
    if pt == 1:
        return np.arange(0, len(t), max(1, int(pv or 1)))
    if pt == 2:  # whole us (as the page's JS): In float epoch ms, a frame exactly T after the last one can miss ">= T"
        tu, T, pos, out = np.round(t * 1000).astype(np.int64), round(float(pv or 1) * 1000), 0, []
        while pos < len(t):
            out.append(pos)
            pos = int(np.searchsorted(tu, tu[pos] + T, side="left"))
        return np.array(out, dtype=np.int64)
    if pt == 3:
        # Frames not logged have the same masked bytes as the last logged one, so "changed vs last logged" =
        # "changed vs the previous frame". Mask bit i = data byte i; empty = all bytes
        mask = int(str(pv), 16) if pv else (1 << 64) - 1
        pos = [i for i in range(64) if mask >> i & 1]
        masked = data if len(pos) == 64 else np.array(["".join(d[2 * i:2 * i + 2] for i in pos) for d in data], dtype=object)
        return np.concatenate([[0], np.flatnonzero(masked[1:] != masked[:-1]) + 1]).astype(np.int64)
    return np.arange(len(t))


def accept(cfg: dict | None, F: dict, rt: dict | None = None) -> tuple[np.ndarray, list]:
    """Per-frame mask of frames accepted by the filters + prescalers, and the decision per ID group."""
    ok = np.zeros(len(F["ts"]), dtype=bool)
    dec = [decide(cfg, c, i, e, bool(fd), rt) for (c, e, i), fd in zip(F["gkeys"], F["gfd"])]
    first = defaultdict(list)  # device limit: The first 100 IDs per channel (15 on CAN9) that reach a prescaler get one
    for k, d in enumerate(dec):
        if d["fate"] == "prescaled":
            first[F["gkeys"][k][0]].append((F["ts"][F["gidx"][k][0]], k))
    for ch, lst in first.items():
        lim = MAX_PRESCALER_IDS.get(ch, MAX_PRESCALER_IDS_PER_CHANNEL)
        for _, k in sorted(lst)[lim:]:
            dec[k] = {"fate": "accepted", "reason": f"over the {lim}-ID prescaler limit of {ch}"}
    for k, ((c, e, i), idx) in enumerate(zip(F["gkeys"], F["gidx"])):
        sec = CHANNEL_TO_SECTION.get(c)
        if cfg is not None and sec in cfg and g(cfg, sec, "filter", "remote_frames") == 0:
            idx = idx[F["rtr"][idx] == 0]  # remote frames are discarded before the filters
        if dec[k]["fate"] == "rejected" or not len(idx):
            continue
        ok[idx[prescale(dec[k]["filter"], F["ts"][idx], F["data"][idx])] if dec[k]["fate"] == "prescaled" else idx] = True
    return ok, dec


# --------------------------------------------------------------------------- #
# Control signals
# --------------------------------------------------------------------------- #

def signal_decoder(sig: dict):
    """Decoder bytes -> physical value for a config signal, via cantools and a one-signal DBC (the device uses
    the DBC convention). Returns (decode, bytes needed)."""
    import cantools
    bitpos, length, bo = int(sig.get("bitpos", 0)), int(sig.get("length", 0)), int(sig.get("byteorder", 1))
    if length <= 0:
        return None, 0
    dbc = (f'VERSION ""\nBS_:\nBU_: X\nBO_ 1 M: 64 X\n SG_ S : {bitpos}|{length}@{bo}+ '
           f'({sig.get("factor", 0)},{sig.get("offset", 0)}) [0|0] "" X\n')
    msg = cantools.database.load_string(dbc, database_format="dbc", strict=False).messages[0]
    need = max(signal_bytes(bitpos, length, bo)) + 1
    return (lambda b: float(msg.decode(b + bytes(64 - len(b)), decode_choices=False)["S"])), need


def control_states(cfg: dict, F: dict, ok: np.ndarray, carry: dict, new_session: bool) -> dict:
    """Per controlled section: {'channel', 'kinds', 'rx'|'tx': (transition times ms, states), 'value': ...}.
    carry holds the end states of the previous file (same session)."""
    out = {}
    for sec, ch in SECTIONS:
        ctrl = g(cfg, sec, "control") or {}
        kinds = [k for k, key in (("rx", "control_rx_state"), ("tx", "control_tx_state")) if ctrl.get(key) == 1]
        if not kinds:
            continue
        trig = {}
        for which in ("start", "stop"):
            m, s = (ctrl.get(which) or {}).get("message") or {}, (ctrl.get(which) or {}).get("signal") or {}
            src = CHN.get(m.get("chn", 0), "CAN9")
            f = {"method": 1, "id_format": m.get("id_format", 0), "f1": m.get("id", "0"), "f2": m.get("id_mask", "7FF")}
            sel = np.flatnonzero(ok & (F["chlabel"] == src))
            sel = sel[[filter_matches(f, int(F["idint"][j]), int(F["ide"][j])) for j in sel]] if len(sel) else sel
            dec, need = signal_decoder(s)
            vals, keep, cache = [], [], {}
            for j in sel:
                d = F["data"][j]
                if dec is None or len(d) // 2 < need:
                    continue  # signal outside the frame's data (device behaviour undefined): not evaluated
                if d not in cache:
                    cache[d] = dec(bytes.fromhex(d))
                vals.append(cache[d])
                keep.append(j)
            lo, hi = float(ctrl.get(which, {}).get("trigger_low", 0)), float(ctrl.get(which, {}).get("trigger_high", 0))
            trig[which] = {"idx": np.array(keep, dtype=np.int64), "val": np.array(vals), "lo": lo, "hi": hi,
                           "src": f"{src} 0x{hexint(m.get('id', '0')):X} mask {str(m.get('id_mask', '')).upper()}",
                           "sig": s, "m": m}
        st, sp = trig["start"], trig["stop"]
        start_in_stop = sp["lo"] <= st["lo"] and st["hi"] <= sp["hi"]
        stop_in_start = st["lo"] <= sp["lo"] and sp["hi"] <= st["hi"]
        # Events in time order: +1 = start condition, -1 = stop condition (a frame can match both)
        ev = defaultdict(lambda: [False, False])
        for which, k in (("start", 0), ("stop", 1)):
            t = trig[which]
            for j, v in zip(t["idx"], t["val"]):
                if t["lo"] <= v <= t["hi"]:
                    ev[int(j)][k] = True
        order = sorted(ev, key=lambda j: F["ts"][j])
        res = {"channel": ch, "kinds": kinds, "trig": trig, "overlap": not (start_in_stop or stop_in_start)
               and max(st["lo"], sp["lo"]) <= min(st["hi"], sp["hi"])}
        for kind in kinds:
            init = carry.get((sec, kind)) if not new_session and (sec, kind) in carry else \
                int(g(cfg, sec, "general", f"{kind}_state") == 1)
            times, states, cur = [-np.inf], [init], init
            for j in order:
                s_on, s_off = ev[j]
                nxt = (1 if (start_in_stop or not stop_in_start) else 0) if (s_on and s_off) else 1 if s_on else 0
                if nxt != cur:
                    times.append(F["ts"][j]); states.append(nxt); cur = nxt
            res[kind] = (np.array(times), np.array(states))
            carry[(sec, kind)] = cur
        out[sec] = res
    return out


def state_at(step: tuple, t: np.ndarray) -> np.ndarray:
    times, states = step
    return states[np.searchsorted(times, t, side="right") - 1]


# --------------------------------------------------------------------------- #
# Files
# --------------------------------------------------------------------------- #

def read_frames(path: Path, tool: Path, passwords: str | None, mx: list | None = None, mdf2mdf: Path | None = None,
                keep: set | None = None) -> dict:
    """Convert one log file to temporary CSVs (deleted at once) and return all frames as arrays, grouped by ID.
    mx = CANmod.router demux mapping (mdf2mdf first), keep = only these channels (None = all)."""
    try:
        tables = mdf2csv_tables(path, tool, passwords, columns=COLS, **({"muxtp": mx, "mdf2mdf": mdf2mdf} if mx else {}))
    except RuntimeError as e:
        sys.exit(f"{path}: {e}")
    parts = defaultdict(list)
    for bus, t in tables:
        n = t.num_rows
        ids = np.array(t["ID"].to_pylist(), dtype=object)
        uid, inv = np.unique(ids, return_inverse=True)
        parts["ts"].append(t["TimestampEpoch"].to_numpy() * 1000.0)
        parts["chlabel"].append(np.array([f"{bus}{c}" for c in t["BusChannel"].to_pylist()], dtype=object))
        parts["ide"].append(t["IDE"].fill_null(0).to_numpy().astype(np.int64) if bus == "CAN" else np.zeros(n, np.int64))
        parts["rtr"].append(t["RTR"].fill_null(0).to_numpy().astype(np.int64) if bus == "CAN" else np.zeros(n, np.int64))
        parts["edl"].append(t["EDL"].fill_null(0).to_numpy().astype(np.int64) if bus == "CAN" else np.zeros(n, np.int64))
        parts["idint"].append(np.array([int(u, 16) for u in uid], dtype=np.int64)[inv] if n else np.zeros(0, np.int64))
        parts["w"].append(np.interp(t["DataLength"].fill_null(0).to_numpy(), *zip(*EST_ANCHORS)))
        parts["data"].append(np.array(t["DataBytes"].fill_null("").to_pylist(), dtype=object))
    F = {k: np.concatenate(v) for k, v in parts.items()}
    if keep is not None:
        sel = np.isin(F["chlabel"], sorted(keep))
        F = {k: v[sel] for k, v in F.items()}
    keys = np.array([f"{c}|{e}|{i}" for c, e, i in zip(F["chlabel"], F["ide"], F["idint"])], dtype=object)
    order = np.lexsort((F["ts"], keys))
    uk, starts, F["gi"] = np.unique(keys[order], return_index=True, return_inverse=True)
    F["gi"] = F["gi"][np.argsort(order)]  # group index per frame (original order)
    bounds = list(starts) + [len(order)]
    F["gidx"] = [order[bounds[k]:bounds[k + 1]] for k in range(len(uk))]  # frames of each ID, time-sorted
    F["gkeys"] = [(c, int(e), int(i)) for c, e, i in (k.split("|") for k in uk)]
    F["gfd"] = [bool(F["edl"][ix].max()) if len(ix) else False for ix in F["gidx"]]  # CAN FD ID (router frame_format)
    return F


def tunnel_share(path: Path, tool: Path, passwords: str | None, tunnel: tuple[str, int, bool]) -> float:
    """Share of a raw (not demuxed) log file's size that the CANmod.router tunnel frames take: Their weight (data
    length, tools.EST_ANCHORS) / the weight of all frames. tunnel = (channel, ID, extended), e.g. ("CAN2", 0x10, False)."""
    tables = mdf2csv_tables(path, tool, passwords, columns=["BusChannel", "ID", "IDE", "DataLength"])
    tot = tun = 0.0
    ch, tid, ext = tunnel
    for bus, t in tables:
        w = np.interp(t["DataLength"].fill_null(0).to_numpy(), *zip(*EST_ANCHORS))
        tot += float(w.sum())
        if bus != "CAN":
            continue
        sel = ((np.array([f"CAN{c}" for c in t["BusChannel"].to_pylist()]) == ch)
               & (t["IDE"].fill_null(0).to_numpy() == int(ext))
               & (np.array([int(x, 16) if x else -1 for x in t["ID"].to_pylist()]) == tid))
        tun += float(w[sel].sum())
    return tun / tot if tot else 0.0


def router_setup(cfg: dict, base: dict | None, args, meta: dict) -> dict:
    """CANmod.router simulation context: Demux mapping (--muxtp > project router DBC > the config's tunnel ID with
    the default channels), this router's channel map (CAN11 -> can_s1, ...) and its tunnel on the CANedge."""
    t = router_tunnel(cfg)
    if t is None:
        sys.exit("The CANmod.router config is not in mux mode (phy.can.route.mode 0) or has no tunnel message ID: "
                 "The CANedge only records S1-S4 as tunnel frames in mux mode")
    try:
        mx = [muxtp.parse_spec(x) for x in args.muxtp or []]
    except ValueError as e:
        sys.exit(str(e))
    source = "--muxtp" if mx else ""
    if not mx and args.project:
        mx = muxtp.for_device(args.project, meta["device"], alt_device=meta.get("path_device"))
        source = "project router DBC" if mx else ""
    tid, ext = t
    entry = next((e for e in mx if int(e["id"], 16) == tid and bool(e["ext"]) == ext), None)
    chmap = router_channel_map(cfg, mx)
    if chmap is None:
        sys.exit(f"Tunnel ID 0x{tid:X} of the router config has no demux channels: Pass --muxtp {tid:03X}:<first channel>")
    if entry is None:   # this router's tunnel is not in the mapping: demux it with the default channels (on CAN2)
        entry = {"channel": "CAN2", "id": f"{tid:08X}" if ext else f"{tid:03X}", "ext": ext,
                 "channels": [int(c[3:]) for c in chmap]}
        mx = mx + [entry]
        source = (source + " + " if source else "") + f"config tunnel ID 0x{tid:X} on CAN2 with default channels"
    if base is not None and router_tunnel(base) != t:
        print("NOTE: The baseline router config uses another tunnel ID: Its filters are applied to the same channels",
              file=sys.stderr)
    return {"mx": mx, "chmap": chmap, "tunnel": (entry["channel"], tid, ext), "source": source,
            "mdf2mdf": resolve("mdf2mdf"), "empty_files": 0}


def router_notes(router: dict, tb: dict, ta: dict) -> list[str]:
    ch = ", ".join(f"{c} = {s.replace('can_s', 'S')}" for c, s in router["chmap"].items())
    out = [f"CANmod.router: {ch} (demuxed from {router['tunnel'][0]} tunnel ID 0x{router['tunnel'][1]:X}; "
           f"mapping: {router['source']}). Sizes = this router's share of the CANedge log file (its tunnel frames), "
           "estimated per demuxed frame"]
    if ta["frames_per_s"] > ROUTER_FPS_GUIDE:
        out.insert(0, f"{ta['frames_per_s']:,.0f} frames/s after the router filters: Above the ~{ROUTER_FPS_GUIDE:,} "
                      "frames/s guideline for all router channels together (log file size, upload): Filter or prescale more")
    elif tb["frames_per_s"] > ROUTER_FPS_GUIDE:
        out.append(f"Before: {tb['frames_per_s']:,.0f} frames/s (above the ~{ROUTER_FPS_GUIDE:,} frames/s guideline), "
                   f"after: {ta['frames_per_s']:,.0f}")
    if router["empty_files"]:
        out.append(f"{router['empty_files']} log file(s) without frames on this router's channels were skipped")
    return out


def dbc_matcher(project: Path, meta: dict):
    """(match(channel, ide, id) -> DBC message name | False, tp(channel, ide, id) -> transport protocol label | None,
    mapping, note) from the project's dbc-groups.json. TP = the DBC message's TransportProtocolType attribute
    (e.g. ISOTP, NMEATP), matched like the message itself (exact ID, or PGN for J1939/NMEA DBCs)."""
    from dbc_groups import dbcs_for_device, load_dbc, message_layouts, pgn_of  # noqa: E402
    mapping, note = dbcs_for_device(project, meta["device"], alt_device=meta.get("path_device"))
    if mapping is None:
        return None, None, None, f"No DBC files for {meta['device']}: {note}"

    # Per channel: exact (extended, ID), J1939 PGN (decoder default: priority, SA and the PDU1 DA ignored), and
    # whether J1939 TP.CM/TP.DT frames carry DBC messages > 8 bytes (the decoder reassembles them)
    idx = {}
    for ch, paths in mapping.items():
        exact, pgn, tp_j1939 = {}, {}, False
        for p in paths:
            for (ext, fid), d in message_layouts(p).items():
                exact[(ext, fid)] = d["name"]
                if d["j1939"]:
                    pgn[pgn_of(fid)] = d["name"]
                    tp_j1939 |= d["layout"][0] > 8
        idx[ch] = (exact, pgn, tp_j1939)

    def match(c, e, i):
        if c not in idx:
            return False
        exact, pgn, tp_j1939 = idx[c]
        name = exact.get((bool(e), i)) or (pgn.get(pgn_of(i)) if e else None)
        if not name and e and tp_j1939 and pgn_of(i) in (0xEC00, 0xEB00):
            name = "J1939 TP"
        return name or False

    tp = {ch: {} for ch in mapping}
    for ch, paths in mapping.items():
        for p in paths:
            try:
                db = load_dbc(p)
            except Exception:  # noqa: BLE001 - message_layouts falls back to a light parser for matching
                continue
            j1939 = {k for k, d in message_layouts(p).items() if d["j1939"]}
            for m in db.messages:
                a = m.dbc.attributes.get("TransportProtocolType") if m.dbc else None
                v = str(a.value).strip() if a is not None else ""
                if v:
                    ide = int(m.is_extended_frame)
                    key = (("pgn", pgn_of(m.frame_id)) if (m.is_extended_frame, m.frame_id) in j1939
                           else ("id", ide, m.frame_id))
                    tp[ch][key] = TP_LABELS.get(v.upper()) or next(
                        (lb for pre, lb in TP_PREFIX_LABELS.items() if v.upper().startswith(pre)), v)
    tp_of = lambda c, e, i: tp.get(c, {}).get(("id", e, i)) or (tp.get(c, {}).get(("pgn", pgn_of(i))) if e else None)  # noqa: E731
    files = "; ".join(f"{ch}: {', '.join(p.name for p in ps)}" for ch, ps in mapping.items())
    return match, tp_of, mapping, f"DBC files ({note}): {files}"


def signal_name(mapping, trig: dict) -> tuple[str, str]:
    """(label, detail) of a control trigger: '<channel> 0x<ID> <DBC message> <DBC signal> [unit]' if the project's DBC
    has it, else '<channel> 0x<ID> bits <bitpos>|<length>'. The ID mask is shown only if it is not a full single-ID mask."""
    m, s = trig["m"], trig["sig"]
    full = 0x1FFFFFFF if m.get("id_format") == 1 else 0x7FF
    mask = hexint(m.get("id_mask", "0"))
    src = f"{CHN.get(m.get('chn', 0), 'CAN9')} 0x{hexint(m.get('id', '0')):X}" + ("" if mask & full == full else f" (mask {mask:X})")
    raw = (f"{src} bits {s.get('bitpos')}|{s.get('length')}", f"Signal: bit position {s.get('bitpos')}, length {s.get('length')}, "
           f"factor {s.get('factor')}, offset {s.get('offset')}")
    if not mapping:
        return raw
    from dbc_groups import load_dbc
    fid = hexint(m.get("id", "0"))
    for p in mapping.get(CHN.get(m.get("chn", 0), "CAN9"), []):
        try:
            db = load_dbc(p)
        except Exception:  # noqa: BLE001
            continue
        for msg in db.messages:
            if (msg.frame_id & hexint(m.get("id_mask", "0"))) == (fid & hexint(m.get("id_mask", "0"))):
                for sg in msg.signals:
                    if sg.start == s.get("bitpos") and sg.length == s.get("length"):
                        return (f"{src} {msg.name} {sg.name}" + (f" [{sg.unit}]" if sg.unit else ""),
                                f"DBC message {msg.name}, signal {sg.name}")
    return raw


# --------------------------------------------------------------------------- #
# Explore tab: Per ID the frames of one time window + the signal values, for the page's live prescaler preview
# --------------------------------------------------------------------------- #

def dbc_messages(mapping):
    """(channel, ide, id) -> cantools message (exact ID, or PGN for J1939 DBCs, as dbc_matcher matches) or None."""
    from dbc_groups import load_dbc, message_layouts, pgn_of  # noqa: E402
    idx = {}
    for ch, paths in (mapping or {}).items():
        exact, pgn = {}, {}
        for p in paths:
            try:
                db = load_dbc(p)
            except Exception:  # noqa: BLE001 - not loadable by cantools: Its IDs show raw bytes
                continue
            j1939 = {k for k, d in message_layouts(p).items() if d["j1939"]}
            for m in db.messages:
                k = (m.is_extended_frame, m.frame_id)
                exact[k] = m
                if k in j1939:
                    pgn[pgn_of(m.frame_id)] = m
        idx[ch] = (exact, pgn)

    def get(c, e, i):
        exact, pgn = idx.get(c, ({}, {}))
        return exact.get((bool(e), i)) or (pgn.get(pgn_of(i)) if e else None)
    return get


def id_text(c: str, e: int, i: int) -> str:
    """ID as in the rows / page: CAN 0x + 3 or 8 hex digits, LIN 0x + 2."""
    return fmt_can_id(i, bool(e)) if c.startswith("CAN") else f"0x{i:02X}"


class Blob:
    """Typed arrays in one binary blob: put() returns [byte offset, byte length] for the page; z() = zlib-compressed,
    base64 (the page inflates it with DecompressionStream). Times + run-length indexes go in as deltas (compress well)."""
    def __init__(self):
        self.parts, self.n = [], 0

    def put(self, a, dtype: str) -> list[int]:
        b = np.ascontiguousarray(a, dtype=dtype).tobytes()
        self.parts.append(b); self.n += len(b)
        return [self.n - len(b), len(b)]

    def z(self) -> str:
        return base64.b64encode(zlib.compress(b"".join(self.parts), 6)).decode()


def explore_window(ts: np.ndarray, budget: int) -> tuple[float, float]:
    """[a, b] ms: The whole recording if it has <= budget frames, else the busiest window that holds <= budget frames
    (window length = the recording's average time per budget frames, shrunk until the busiest one fits)."""
    ts = np.sort(ts)
    if len(ts) <= budget:
        return float(ts[0]), float(ts[-1])
    w, k = (ts[-1] - ts[0]) * budget / len(ts), 0
    for _ in range(12):
        cnt = np.searchsorted(ts, ts + w, side="right") - np.arange(len(ts))
        k = int(np.argmax(cnt))
        if cnt[k] <= budget:
            break
        w *= 0.98 * budget / cnt[k]
    return float(ts[k]), float(ts[k] + w)


def explore_data(parts: list[dict], decisions: dict, rows: list[dict], msg_of, budget: int, dcache: dict,
                 ctl_steps: dict | None = None) -> dict:
    """Data of the Explore tab. parts: Per log file ts (ms), data, rtr, gkeys, gidx. Remote frames are left out (no
    data). All arrays live in one compressed blob ('z', see Blob). Per ID (in the order of rows): Frame times
    (uint32 deltas, 'unit' us ticks after t0), a per-frame mask of the data bytes
    that changed vs the previous frame (bit i = byte i; first frame all bits; what a data prescaler tests), the
    default decision of the new config and 'py' = the frames prescale() logs in the window (the page checks its JS
    port against it). Signals: The DBC message's signals (cantools, unsigned/signed + factor/offset as in the DBC),
    else one per data byte, as run lengths (frame index deltas where the value changes, value; float32 if lossless). With
    multiplexed or short frames: 'pats' (signal indexes per presence pattern) + 'pid' (pattern per frame). dcache:
    Decoded payloads, reused across calls. Control signal (ctl_steps of the new config): 'ctl' = per gated channel the
    logging-off intervals [from, to) in ticks; 'py_ctl' = frames logged with the prescaler AND logging on (the device
    state per frame, state_at) for the page's check."""
    ts_all = np.concatenate([p["ts"][p["rtr"] == 0] for p in parts])
    a, b = explore_window(ts_all, budget)
    unit = max(1, int(np.ceil((b - a) * 1000 / 2 ** 32)))   # us per tick: 1 us up to 71 min
    blob = Blob()
    per = defaultdict(lambda: ([], [], []))
    for p in parts:
        for gk, idx in zip(p["gkeys"], p["gidx"]):
            t = p["ts"][idx]
            sel = idx[(t >= a) & (t <= b) & (p["rtr"][idx] == 0)]
            if len(sel):
                per[gk][0].append(p["ts"][sel]); per[gk][1].append(p["data"][sel]); per[gk][2].append(p["gate"][sel])
    by_row = {(r["channel"], r["id"]): r for r in rows}
    row = lambda k: by_row[(k[0], id_text(*k))]  # noqa: E731
    out = []
    for gk in sorted(per, key=lambda k: -row(k)["mb_min_before"]):
        c, e, i = gk
        t, data, gate = (np.concatenate(x) for x in per[gk])
        o = np.argsort(t, kind="stable")
        t, data, gate, r = t[o], data[o], gate[o], row(gk)
        uniq, inv = np.unique(data, return_inverse=True)
        maxlen = max(1, max(len(d) // 2 for d in uniq))
        um = np.full((len(uniq), maxlen), -1, dtype=np.int16)   # bytes per unique payload, -1 = not sent
        for k, d in enumerate(uniq):
            bb = bytes.fromhex(d)
            um[k, :len(bb)] = list(bb)
        M = um[inv]
        bits = np.zeros(len(t), dtype=np.uint64)
        bits[0] = np.uint64((1 << maxlen) - 1) if maxlen < 64 else np.uint64(2 ** 64 - 1)
        if len(t) > 1:
            diff = M[1:] != M[:-1]
            for j in range(maxlen):
                bits[1:] |= diff[:, j].astype(np.uint64) << np.uint64(j)
        wide = maxlen > 8
        chg = (np.stack([bits & np.uint64(0xFFFFFFFF), bits >> np.uint64(32)], axis=1).ravel().astype(np.uint32)
               if wide else bits.astype(np.uint8))
        msg = None if r["tp"] else msg_of(c, e, i) if msg_of else None
        if msg is not None:
            vm = np.full((len(uniq), len(msg.signals)), np.nan)
            for k, d in enumerate(uniq):
                if (id(msg), d) not in dcache:
                    try:
                        dcache[(id(msg), d)] = msg.decode(bytes.fromhex(d), decode_choices=False, allow_truncated=True)
                    except Exception:  # noqa: BLE001 - payload the DBC cannot decode: No values for this frame
                        dcache[(id(msg), d)] = {}
                v = dcache[(id(msg), d)]
                for j, sg in enumerate(msg.signals):
                    x = v.get(sg.name)
                    if isinstance(x, (int, float)):
                        vm[k, j] = float(x)
            meta = [(sg.name, sg.unit or "", signal_bytes(sg.start, sg.length, 1 if sg.byte_order == "little_endian" else 0))
                    for sg in msg.signals]
        else:
            vm = np.where(um >= 0, um, np.nan).astype(float)
            meta = [(f"B{j}", "", [j]) for j in range(maxlen)]
        # Which signals a frame carries (multiplexing, short frames): One pattern ID per frame. Values are held
        # forward over the frames without the signal, so the run lengths only break on a new value
        keep = [j for j in range(len(meta)) if not np.isnan(vm[:, j]).all()]
        pres = ~np.isnan(vm[:, keep])
        pats, pid_u = np.unique(pres, axis=0, return_inverse=True)
        pid_u = pid_u.ravel()
        V = vm[inv][:, keep]
        sigs = []
        for s, j in enumerate(keep):
            v = V[:, s]
            have = ~np.isnan(v)
            fill = np.maximum.accumulate(np.where(have, np.arange(len(v)), -1))
            vf = np.where(fill >= 0, v[np.maximum(fill, 0)], np.nan)
            same = (vf[1:] == vf[:-1]) | (np.isnan(vf[1:]) & np.isnan(vf[:-1]))
            ix = np.concatenate([[0], np.flatnonzero(~same) + 1])
            vals = vf[ix]
            f32 = bool(np.all((vals.astype(np.float32).astype(float) == vals) | np.isnan(vals)))
            name, unit_s, byts = meta[j]
            sigs.append({"name": name, "unit": unit_s, "bytes": byts, "idx": blob.put(np.diff(ix, prepend=0), "<u4"),
                         "val": blob.put(vals, "<f4" if f32 else "<f8"), "f32": f32})
        pat = ({"pats": [[s for s in range(len(keep)) if row_[s]] for row_ in pats],
                "pid": blob.put(pid_u[inv], "u1" if len(pats) < 256 else "<u2"), "pid16": len(pats) >= 256}
               if len(pats) > 1 else {})
        d = decisions[gk]
        f = d.get("filter") if d["fate"] == "prescaled" else None
        pos = np.zeros(0, np.int64) if d["fate"] == "rejected" else prescale(f, t, data) if f else np.arange(len(t))
        py, py_ctl = len(pos), int(gate[pos].sum())
        out.append({"channel": c, "id": r["id"], "dbc": r["dbc"] if msg is not None else None, "tp": r["tp"],
                    "fate": d["fate"], "rule": d["reason"], "pt": f.get("prescaler_type", 0) if f else 0,
                    "pv": f.get("prescaler_value") if f else None, "n": len(t), "maxlen": maxlen, "wide": wide,
                    "ts": blob.put(np.diff(np.round((t - a) * 1000 / unit).astype(np.int64), prepend=0), "<u4"),
                    "chg": blob.put(chg, "<u4" if wide else "u1"),
                    "py": py, "py_ctl": py_ctl, "signals": sigs, **pat})
    tick = lambda x: int(np.round((x - a) * 1000 / unit))  # noqa: E731 - same rounding as the frame times
    ctl = {}
    for (sec, kind), steps in (ctl_steps or {}).items():
        if kind != "rx":
            continue
        off = []
        for times, states, f0, f1 in steps:   # per log file: state k holds from times[k] (-inf = file start) to times[k+1]
            for k, st in enumerate(states):
                lo = f0 if k == 0 else times[k]
                hi = times[k + 1] if k + 1 < len(times) else f1 + 1e-3
                if not st and hi > a and lo <= b:
                    off.append([max(-1, tick(lo)), tick(hi) if hi <= b else 2 ** 32])
        ctl[dict(SECTIONS)[sec]] = off
    return {"t0": a / 1000.0, "t1": b / 1000.0, "unit_us": unit, "frames": int(sum(x["n"] for x in out)),
            "total_frames": int(len(ts_all)), "windowed": bool(len(ts_all) > budget), "budget": budget, "ids": out,
            "ctl": ctl, "z": blob.z()}


def explore_page(parts: list[dict], decisions: dict, rows: list[dict], msg_of, max_mb: float, ctl_steps: dict) -> dict:
    """explore_data() within max_mb of page data: Starts with MAX_EXPLORE_FRAMES, shrinks the window until it fits."""
    budget, cache = MAX_EXPLORE_FRAMES, {}
    for _ in range(5):
        ex = explore_data(parts, decisions, rows, msg_of, budget, cache, ctl_steps)
        mb = len(json.dumps(ex, separators=(",", ":"))) / 1e6
        if mb <= max_mb or budget <= 5_000:
            return {**ex, "mb": round(mb, 1)}
        budget = max(5_000, int(budget * 0.92 * max_mb / mb))
    return {**ex, "mb": round(mb, 1)}


# --------------------------------------------------------------------------- #
# Page
# --------------------------------------------------------------------------- #

def cfg_label(path: str) -> str:
    """'<folder>/config-VV.VV.json' (variants live in folders, the file name is always the device's)."""
    p = Path(path)
    return f"{p.parent.name}/{p.name}" if p.parent.name else p.name


def config_diff(old: str, new: str) -> dict:
    """Line diff as in the config editor's 'Review changes': A unified diff (difflib, same as the editor's JS port)
    of both files as 2-space JSON (whitespace changes ignored), rendered by diff2html. Secrets are masked; the CRC32 is
    the one of the actual file (as in the MF4 header / config editor)."""
    from dashboard import line_diff  # noqa: E402 - canedge-reports
    texts = [json.dumps(mask_secrets(load_config(Path(p))[0]), indent=2, ensure_ascii=False).splitlines() for p in (old, new)]
    return {**line_diff(*texts, [cfg_label(old), cfg_label(new)]), "crc32": [crc32_hex(Path(p).read_bytes()) for p in (old, new)]}


def build_html(result: dict, rows: list[dict], source: str, args, cfg: dict, timeline: dict | None, tz: str | None,
               explore: dict | None = None) -> tuple[str, dict]:
    """Self-contained page: assets/config-effect.html + .js on the canedge-reports dashboard kit (dashboard.py).
    Returns (html, the page's data D)."""
    from dashboard import build_html, diff_parts  # noqa: E402

    bt, at = result["before"], result["after"]
    k = bt["mb_per_min"] / max(sum(r["mb_min_before"] for r in rows), 1e-12)  # rounding-safe scale to the total
    data = {
        "device": result["device"], "source": source, "recorded_min": round(result["recorded_s"] / 60),
        "ids": len(rows), "config_name": cfg_label(args.config),
        "baseline_name": cfg_label(args.baseline) if args.baseline else "as recorded",
        "config_crc32": result["config_crc32"],
        "baseline_crc32": result["baseline_crc32"] or ", ".join(result["recorded_config_crc32"]) or None,
        "before": bt, "after": at, "reduction_pct": result["reduction_pct"], "counts": result["counts"],
        "breakdown": {
            "prescaled": k * sum(max(0.0, r["mb_min_before"] - r["mb_min_accepted"]) for r in rows if r["fate"] == "prescaled"),
            "rejected": k * sum(r["mb_min_before"] for r in rows if r["fate"] == "rejected"),
            "control": k * sum(max(0.0, r["mb_min_accepted"] - r["mb_min_after"]) for r in rows if r["fate"] != "rejected")},
        "has_dbc": result["decodable_ids_rejected"] is not None,
        "decodable_after_pct": result["decodable_share_after_pct"], "decodable_rejected": len(result["decodable_ids_rejected"] or []),
        "channels": [{"channel": c, "before": sum(r["mb_min_before"] for r in rows if r["channel"] == c),
                      "after": sum(r["mb_min_after"] for r in rows if r["channel"] == c)} for c in sorted({r["channel"] for r in rows})],
        "rows": [{"channel": r["channel"], "id": r["id"], "dbc": r["dbc"], "fps_before": r["fps_before"],
                  "fps_after": r["fps_after"], "mb_before": r["mb_min_before"], "mb_after": r["mb_min_after"],
                  "fate": r["fate"], "rule": r["reason"], "tp": r["tp"]} for r in rows],
        "control": result["control"], "timeline": timeline, "tz": tz, "explore": explore, "router": result.get("router"),
        "diff": config_diff(args.baseline, args.config) if args.baseline else None,
        "notes": [n for n in result["notes"] if not n.startswith("DBC files")],
        "generated": dt.datetime.now(dt.timezone.utc).strftime("%y/%m/%d %H:%M UTC"),
    }
    # Compression (estimate): The headline, bars and log size KPIs include it when the new config turns it on
    # (the measured reduction shows below the headline); off = a muted "if enabled" hint; on in both = no step
    if not result.get("router") and not result["source_compressed"] and (result["compression_estimated"] or not result["compression_level"]):
        on = result["compression_estimated"]
        data["compression"] = {
            "enabled": on, "mb_per_min": at["mb_per_min"] * COMPRESSION, "mb_per_h": at["mb_per_h"] * COMPRESSION,
            "total_pct": round(100 * (1 - at["mb_per_min"] * COMPRESSION / bt["mb_per_min"]), 1) if bt["mb_per_min"] else None,
            "note": (f"Compression ({result['compression_level']} byte window): Shown as {1 - COMPRESSION:.0%} smaller "
                     "(typical, not measured)") if on else
                    f"Compression is not enabled: {1 - COMPRESSION:.0%} smaller if enabled (typical, not measured)"}
        if on:
            data["notes"].append(data["compression"]["note"])
    title = "CANmod.router config impact" if result.get("router") else "Config impact"
    html = build_html(title, HERE.parent / "assets" / "config-effect.js", {"D": data, "CFG": {"tz": tz}},
                      template=HERE.parent / "assets" / "config-effect.html", kit=["series"],
                      extra={**diff_parts(bool(data["diff"])),
                             "__EXPLORE_JS__": (HERE.parent / "assets" / "config-explore.js").read_text(encoding="utf-8")})
    return html, data


# --------------------------------------------------------------------------- #
# Main
# --------------------------------------------------------------------------- #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("logs", nargs="+", type=Path, help="Log file(s) or folder (typically one file)")
    ap.add_argument("--config", required=True, help="Configuration File to evaluate (the new one)")
    ap.add_argument("--baseline", help="Old config: 'before' = frames filtered by it (default: as recorded)")
    ap.add_argument("--project", type=Path, help="Project (dbc/ + optional dbc-groups.json): Match IDs to the device's DBC files")
    ap.add_argument("--top", type=int, default=30, help="Rows in the printed per-ID table (default 30, by size before; --json lists all IDs)")
    ap.add_argument("--json", action="store_true", help="Print JSON for the agent")
    ap.add_argument("--html", help="Also write a self-contained page (e.g. <project>/reports/<yyyymmdd>-config-<variant>/report.html)")
    ap.add_argument("--tool", help="mdf2csv path (default: canedge-mdf2csv/bin, or CANEDGE_MDF2CSV)")
    ap.add_argument("--passwords", help="passwords.json for encrypted files (MFE/MFM; default <project>/passwords.json)")
    ap.add_argument("--explore-mb", type=float, default=6, help="Max data of the page's Explore tab in MB (default 6): "
                    "A longer log file = the busiest time window that fits (max 2M frames). 0 = no Explore tab")
    ap.add_argument("--tz", help="Page time zone (IANA, e.g. Europe/Berlin). Default: project display_tz, else the viewer PC time zone")
    ap.add_argument("--muxtp", action="append", help="CANmod.router configs: Demux mapping [canN:]ID:S1[,S2,S3,S4] "
                    "(e.g. 010:11; default: the project's router DBC, else the config's tunnel ID -> CAN11-14 / 15-18)")
    args = ap.parse_args()

    files = find_log_files(args.logs)
    if not files:
        sys.exit("No log files (MF4/MFC/MFE/MFM) found")
    metas = sorted((describe(f) for f in files), key=lambda m: (m["session"] or 0, m["split"] or 0, m["path"]))
    devices = sorted({m["device"] for m in metas})
    if len(devices) > 1:
        sys.exit(f"Log files of several devices {devices}: Run once per device")
    args.passwords = password_file(args.passwords, args.project)
    refusal = passwords_refusal([m["path"] for m in metas], args.passwords, args.project)
    if refusal:
        sys.exit(refusal)
    if any(m["header"] == "unsupported" for m in metas):  # MFC/MFE/MFM: Recorded config CRC32 from a finalized copy
        for e in fill_headers(metas, args.passwords)["failed"]:
            print(f"WARNING: Header not readable: {e}", file=sys.stderr)
    cfg = load_config(Path(args.config))[0]
    configs = {"before": load_config(Path(args.baseline))[0] if args.baseline else None, "after": cfg}
    tool = resolve("mdf2csv", args.tool)
    router = router_setup(cfg, configs["before"], args, metas[0]) if is_router(cfg) else None
    if router is None and (is_router(configs["before"]) or args.muxtp):
        sys.exit("--baseline is a CANmod.router config but --config is not (or --muxtp without a router config): "
                 "Compare configs of the same device")
    rts = {name: (router["chmap"] if router and c is not None else None) for name, c in configs.items()}
    router_bytes = 0.0

    acc: dict = defaultdict(lambda: {"frames": 0, "w": 0.0, "changes": 0, "periods": [],
                                     "before": [0, 0.0], "accepted": [0, 0.0], "after": [0, 0.0]})
    carry = {name: {} for name in configs}
    ctl_sum: dict = {}
    tl_frames = []  # per file: (ts, chlabel, logged before, logged after) for the timeline
    ex_parts, ex_dec = [], {}  # per file: The frames for the Explore tab; decision per ID (new config)
    ctl_series = defaultdict(list)  # control value points per signal: (ts, value)
    ctl_steps = defaultdict(list)  # (section, kind) -> [(times, states, t0, t1)]
    recorded_s, prev = 0.0, None
    for m in metas:
        if router:
            F = read_frames(Path(m["path"]), tool, args.passwords, router["mx"], router["mdf2mdf"], set(router["chmap"]))
            if not len(F["ts"]):
                router["empty_files"] += 1
                continue
            router_bytes += m["size_bytes"] * tunnel_share(Path(m["path"]), tool, args.passwords, router["tunnel"])
        else:
            F = read_frames(Path(m["path"]), tool, args.passwords)
        new_session = not (prev and prev["session"] == m["session"] and m["split"] == (prev["split"] or 0) + 1)
        prev = m
        t0, t1 = float(F["ts"].min()), float(F["ts"].max())
        recorded_s += (t1 - t0) / 1000.0
        logged, gate = {}, np.ones(len(F["ts"]), dtype=bool)  # gate: logging on per frame (new config's control signal)
        for name, c in configs.items():
            ok, dec = accept(c, F, rts[name])
            mask = ok.copy()
            if c is not None:  # control signals: frames on a channel whose logging is off are not logged
                states = control_states(c, F, ok, carry[name], new_session)
                for sec, res in states.items():
                    if "rx" in res:
                        on = state_at(res["rx"], F["ts"])
                        mask &= ~((F["chlabel"] == res["channel"]) & (on == 0))
                        if name == "after":
                            gate &= ~((F["chlabel"] == res["channel"]) & (on == 0))
                    if name == "after":
                        for kind in res["kinds"]:
                            ctl_steps[(sec, kind)].append((res[kind][0], res[kind][1], t0, t1))
                        s = ctl_sum.setdefault(sec, {"res": res})
                        s["res"] = res
                        for which in ("start", "stop"):
                            tr = res["trig"][which]
                            key = (tr["src"], json.dumps(tr["sig"], sort_keys=True))
                            ctl_series[key] += list(zip(F["ts"][tr["idx"]].tolist(), tr["val"].tolist()))
                            s.setdefault("matched", {}).setdefault(which, 0)
                            s["matched"][which] += len(tr["idx"])
            logged[name] = mask
            if name == "after":
                a_ok, a_dec = ok, dec
        gi = F["gi"]
        for key, k in (("before", "before"), ("accepted", None), ("after", "after")):
            msk = a_ok if key == "accepted" else logged[k]
            cnt = np.bincount(gi, weights=msk.astype(float), minlength=len(F["gkeys"]))
            wsum = np.bincount(gi, weights=F["w"] * msk, minlength=len(F["gkeys"]))
            for n, gk in enumerate(F["gkeys"]):
                acc[gk][key][0] += int(cnt[n]); acc[gk][key][1] += float(wsum[n])
        for n, (gk, idx) in enumerate(zip(F["gkeys"], F["gidx"])):
            a = acc[gk]
            a["frames"] += len(idx); a["w"] += float(F["w"][idx].sum())
            a["changes"] += 1 + int((F["data"][idx][1:] != F["data"][idx][:-1]).sum())
            if len(idx) > 1:
                a["periods"].append(float(np.median(np.diff(F["ts"][idx]))))
            a["fate"], a["reason"] = a_dec[n]["fate"], a_dec[n]["reason"]
        tl_frames.append((F["ts"], F["chlabel"], logged["before"], logged["after"]))
        if args.html and args.explore_mb > 0:
            ex_parts.append({**{k: F[k] for k in ("ts", "data", "rtr", "gkeys", "gidx")}, "gate": gate})
            ex_dec.update(zip(F["gkeys"], a_dec))
    if not acc or not recorded_s:
        sys.exit("No frames in the log files")
    match, tp_dbc, mapping, dbc_note = dbc_matcher(args.project, metas[0]) if args.project else (None, None, None, None)

    # bytes per weight unit: The real file size over all frames; router: the tunnel frames' share of the file size over
    # the demuxed frames of this router
    calib = ((router_bytes if router else sum(m["size_bytes"] for m in metas))
             / max(sum(a["w"] for a in acc.values()), 1e-9))
    mb_min = lambda w: w * calib / 1e6 / (recorded_s / 60)  # noqa: E731
    rows = []
    for (c, e, i), a in acc.items():
        rows.append({
            "channel": c, "id": id_text(c, e, i),
            "pgn": f"{j1939_pgn(i):04X}" if e else None, "frames": a["frames"],
            "fps_before": round(a["before"][0] / recorded_s, 2), "fps_after": round(a["after"][0] / recorded_s, 2),
            "mb_min_before": round(mb_min(a["before"][1]), 5), "mb_min_accepted": round(mb_min(a["accepted"][1]), 5),
            "mb_min_after": round(mb_min(a["after"][1]), 5),
            "fate": a["fate"], "reason": a["reason"], "dbc": match(c, e, i) if match else None,
            "tp": (j1939_tp(e, i) if c.startswith("CAN") else None) or (tp_dbc(c, e, i) if tp_dbc else None),
            "period_ms": round(float(np.median(a["periods"])), 2) if a["periods"] else None,
            "data_changes_pct": round(100 * a["changes"] / a["frames"], 1),
        })
    rows.sort(key=lambda r: -r["mb_min_before"])
    total_b = sum(r["mb_min_before"] for r in rows) or 1e-12
    for r in rows:
        r["share_before_pct"] = round(100 * r["mb_min_before"] / total_b, 2)

    def totals(k: str) -> dict:
        w = sum(a[k][1] for a in acc.values())
        return {"frames_per_s": round(sum(a[k][0] for a in acc.values()) / recorded_s, 1),
                "ids_logged": sum(1 for a in acc.values() if a[k][0]),
                "mb_per_min": round(mb_min(w), 3), "mb_per_h": round(mb_min(w) * 60, 1)}

    tb, ta = totals("before"), totals("after")
    red = round(100 * (1 - ta["mb_per_min"] / tb["mb_per_min"]), 1) if tb["mb_per_min"] else 0.0

    # Control summary: Share of the recorded time each controlled channel logs / transmits
    def on_share(sec: str, kind: str) -> float:
        on = tot = 0.0
        for times, states, a, b in ctl_steps[(sec, kind)]:
            edges = np.concatenate([[a], np.clip(times[1:], a, b), [b]])
            on += float(np.sum(np.diff(edges) * states))
            tot += b - a
        return round(100 * on / tot, 1) if tot else 0.0

    def short_on(sec: str, kind: str, max_s: float = 2.0) -> int:
        """On-periods shorter than max_s (inside the file, not cut by its ends): Many = a flickering trigger signal."""
        n = 0
        for times, states, a, b in ctl_steps[(sec, kind)]:
            t = times[1:]
            for k in range(1, len(states)):
                if states[k] and k < len(t) and (t[k] - t[k - 1]) / 1000.0 < max_s:
                    n += 1
        return n

    control, warnings = [], []
    for sec, s in ctl_sum.items():
        res = s["res"]
        tr = res["trig"]
        item = {"channel": res["channel"], "controls": res["kinds"],
                "start": {"source": tr["start"]["src"], **dict(zip(("signal", "detail"), signal_name(mapping, tr["start"]))),
                          "range": [tr["start"]["lo"], tr["start"]["hi"]], "frames": s["matched"]["start"]},
                "stop": {"source": tr["stop"]["src"], **dict(zip(("signal", "detail"), signal_name(mapping, tr["stop"]))),
                         "range": [tr["stop"]["lo"], tr["stop"]["hi"]], "frames": s["matched"]["stop"]}}
        for kind in res["kinds"]:
            item[f"{kind}_on_pct"] = on_share(sec, kind)
            item[f"{kind}_switches"] = sum(len(st[0]) - 1 for st in ctl_steps[(sec, kind)])
            item[f"{kind}_short_on"] = short_on(sec, kind)
        if not s["matched"]["start"]:
            warnings.append(f"{res['channel']}: No accepted frame matches the start message {tr['start']['src']} "
                            "(not in the data, rejected by the source channel's filters, or too short)")
        if res["overlap"]:
            warnings.append(f"{res['channel']}: Start and stop ranges partially overlap (undefined on the device; "
                            "simulated as start)")
        control.append(item)

    m0 = metas[0]
    source = (f"{m0['device']}/{m0['session']:08d}/{m0['split']:08d}.{m0['format']}"
              if len(metas) == 1 and m0["session"] is not None and m0["split"] is not None else f"{len(metas)} log files")
    notes = [f"Exact simulation on {source} ({recorded_s / 60:.0f} min recorded): Real sizes vary with how the vehicle is used",
             "IDs that are not in the log files cannot be evaluated: Use data recorded with default (accept-all) filters"]
    if control:
        notes.append("Control signal: The simulation starts in the configured initial state")
    notes += warnings
    src_compressed = any(m["format"] in ("MFC", "MFM") for m in metas)
    if src_compressed:
        notes.append("The source files are compressed (MFC/MFM): Sizes are compressed sizes")
    small = [m for m in metas if m["format"] in ("MF4", "MFE") and m["size_bytes"] < SMALL_FILE_BYTES]
    if small:
        notes.append(f"Small log file{'s' if len(small) > 1 else ''} (< 0.5 MB): The ~100 kB MF4 header is a large share, "
                     "so the sizes and the reduction are rough. Use a log file of several MB")
    tp_hit = [r for r in rows if r["tp"] and r["fate"] in ("rejected", "prescaled")]
    if tp_hit:
        notes.insert(0, f"Transport protocol IDs rejected or prescaled ({len(tp_hit)}, e.g. {tp_hit[0]['channel']} "
                        f"{tp_hit[0]['id']} {tp_hit[0]['tp']}): Their multi-frame messages can no longer be reassembled. "
                        "Only do this if intended")
    over = [r for r in rows if str(r["reason"]).startswith("over the ")]
    if over:
        notes.insert(0, f"Prescaler limit exceeded ({len(over)} IDs, e.g. {over[0]['channel']} {over[0]['id']}): The device "
                        f"prescales only the first IDs per channel ({over[0]['reason']}), the rest are logged at full rate. "
                        "Prescale fewer IDs (those with the largest share) or reject unneeded IDs")
    comp_level = g(cfg, "log", "compression", "level") or 0
    base_level = (g(configs["before"], "log", "compression", "level") or 0) if configs["before"] else 0
    comp_est = bool(comp_level) and not src_compressed and not base_level   # the new config turns compression on
    if router:
        notes[1:1] = router_notes(router, tb, ta)
    if not comp_level and not src_compressed and not router:
        notes.append("Compression is off: Enable it (level 1024, or 256 for 3+ MB/min). Shown as 50% smaller (typical, not measured)")
    if comp_level and base_level and not src_compressed:
        notes.append("Compression is on in both configs: Sizes are compared uncompressed (the reduction is the same compressed)")
    if dbc_note:
        notes.append(dbc_note)
    after_mb = sum(r["mb_min_after"] for r in rows) or 1e-12
    result = {
        "device": devices[0], "files": [m["path"] for m in metas], "config": args.config, "baseline": args.baseline,
        "config_crc32": crc32_hex(Path(args.config).read_bytes()),
        "baseline_crc32": crc32_hex(Path(args.baseline).read_bytes()) if args.baseline else None,
        "recorded_config_crc32": sorted({m["config_crc32"] for m in metas if m.get("config_crc32")}),
        "recorded_s": round(recorded_s, 1), "before": tb, "after": ta, "reduction_pct": red,
        "compression_level": comp_level, "source_compressed": src_compressed, "compression_estimated": comp_est,
        "after_with_compression_mb_per_min": round(ta["mb_per_min"] * COMPRESSION, 3) if comp_est else None,
        "reduction_incl_compression_pct": (round(100 * (1 - ta["mb_per_min"] * COMPRESSION / tb["mb_per_min"]), 1)
                                           if comp_est and tb["mb_per_min"] else None),
        "tp_ids_affected": [{k: r[k] for k in ("channel", "id", "tp", "fate", "reason")} for r in tp_hit],
        "router": ({"tunnel": f"{router['tunnel'][0]} 0x{router['tunnel'][1]:03X}", "demux": muxtp.key(router["mx"]),
                    "channels": {ch: sec.replace("can_s", "S") for ch, sec in router["chmap"].items()},
                    "fps_guideline": ROUTER_FPS_GUIDE, "mapping_source": router["source"]} if router else None),
        "counts": {f: sum(1 for r in rows if r["fate"] == f) for f in ("accepted", "prescaled", "rejected")},
        "control": control,
        "decodable_ids_rejected": [{k: r[k] for k in ("channel", "id", "dbc", "reason", "fps_before")} for r in rows
                                  if r["dbc"] and r["fate"] == "rejected"] if match else None,
        "decodable_share_after_pct": round(100 * sum(r["mb_min_after"] for r in rows if r["dbc"]) / after_mb, 1) if match else None,
        "notes": notes, "ids": rows,
    }

    if args.html:
        timeline = build_timeline(tl_frames, ctl_series, ctl_steps, control, mapping)
        from display_time import DisplayTime  # noqa: E402
        pcfg = (json.loads((args.project / "project.json").read_text(encoding="utf-8"))
                if args.project and (args.project / "project.json").exists() else None)
        tz = DisplayTime.from_project(pcfg, args.tz).page_tz()
        explore = (explore_page(ex_parts, ex_dec, rows, dbc_messages(mapping) if mapping else None, args.explore_mb, ctl_steps)
                   if ex_parts else None)
        page, D = build_html(result, rows, source, args, cfg, timeline, tz, explore)
        Path(args.html).parent.mkdir(parents=True, exist_ok=True)
        Path(args.html).write_text(page, encoding="utf-8")
        # report.json = what the page shows (without the Explore frames; its ID table = result.ids) + the full result
        side = Path(args.html).with_name("report.json")
        side.write_text(json.dumps({"page": {k: v for k, v in D.items() if k not in ("explore", "rows")}, "result": result},
                                   indent=1, default=str), encoding="utf-8")
        print(f"HTML: {args.html} ({len(page.encode('utf-8')) / 1024:.0f} KB, single file)", file=sys.stderr)
        print(f"JSON: {side} (what the page shows: Answer questions about it from this file)", file=sys.stderr)
        print(open_line(args.html, "report"), file=sys.stderr)
    if args.json:
        print(json.dumps(result, indent=1))
        return 0

    print(f"Device {devices[0]}: {source}, {recorded_s / 60:.1f} min recorded, {len(rows)} IDs")
    print(f"{'':8} {'frames/s':>10} {'IDs':>5} {'MB/min':>9} {'MB/h':>8}")
    for lbl, t in (("before", tb), ("after", ta)):
        print(f"{lbl:8} {t['frames_per_s']:>10} {t['ids_logged']:>5} {t['mb_per_min']:>9} {t['mb_per_h']:>8}")
    c = result["counts"]
    if result["reduction_incl_compression_pct"] is not None:
        print(f"Reduction incl. compression: {result['reduction_incl_compression_pct']}% "
              f"(~{result['after_with_compression_mb_per_min']} MB/min, compression = 50% estimate)")
    print(f"Reduction{' excl. compression' if comp_est else ''}: {red}% (accepted {c['accepted']}, "
          f"prescaled {c['prescaled']}, rejected {c['rejected']} IDs)")
    for it in control:
        on = ", ".join(f"{k} on {it[f'{k}_on_pct']}% of the time ({it[f'{k}_switches']} switches, "
                       f"{it[f'{k}_short_on']} on-periods < 2 s)" for k in it["controls"])
        print(f"Control {it['channel']}: {on}. Start {it['start']['signal']} in {it['start']['range']}, "
              f"stop {it['stop']['signal']} in {it['stop']['range']}")
    if match:
        print(f"Decodable share of the size after: {result['decodable_share_after_pct']}%")
        if result["decodable_ids_rejected"]:
            print(f"!! {len(result['decodable_ids_rejected'])} DECODABLE IDs WOULD BE REJECTED: "
                  + ", ".join(f"{x['channel']} {x['id']} {x['dbc']}" for x in result["decodable_ids_rejected"][:15]))
    print(f"\nTop {min(args.top, len(rows))} IDs by size before (changes = frames with a new payload):")
    print(f"  {'ch':5} {'id':11} {'share%':>7} {'period':>7} {'changes':>8} {'fps bef':>8} {'fps aft':>8}  {'dbc':16} decision")
    for r in rows[: args.top]:
        dbc = "-" if r["dbc"] is None else (r["dbc"] or "no")[:16]
        print(f"  {r['channel']:5} {r['id']:11} {r['share_before_pct']:>7} {r['period_ms'] or '-':>7} {r['data_changes_pct']:>7}% "
              f"{r['fps_before']:>8} {r['fps_after']:>8}  {dbc:16} {r['fate']}: {r['reason']}" + (f"  [TP: {r['tp']}]" if r['tp'] else ""))
    for n in notes:
        print(f"Note: {n}")
    return 0


def build_timeline(tl_frames, ctl_series, ctl_steps, control, mapping) -> dict:
    """Bins for the page: FPS per channel before/after, control values (mean per bin) and on/off intervals."""
    t_min = min(float(f[0].min()) for f in tl_frames) / 1000.0
    t_max = max(float(f[0].max()) for f in tl_frames) / 1000.0
    span = max(t_max - t_min, 1.0)
    bin_s = next((b for b in BINS if span / b <= 400), BINS[-1])
    t0 = np.floor(t_min / bin_s) * bin_s
    n = int(np.ceil((t_max - t0) / bin_s)) + 1
    chans = sorted({c for f in tl_frames for c in np.unique(f[1])})
    fps = {k: {c: np.zeros(n) for c in chans} for k in ("before", "after")}
    seen = np.zeros(n, dtype=bool)
    for ts, ch, lb, la in tl_frames:
        b = ((ts / 1000.0 - t0) // bin_s).astype(int)
        seen[np.unique(b)] = True
        for c in chans:
            sel = ch == c
            fps["before"][c] += np.bincount(b[sel & lb], minlength=n)[:n]
            fps["after"][c] += np.bincount(b[sel & la], minlength=n)[:n]
    rnd = lambda a: [round(float(x) / bin_s, 2) if seen[i] else None for i, x in enumerate(a)]  # noqa: E731
    signals = []
    for (src, sig), pts in ctl_series.items():
        if not pts:
            continue
        t, v = np.array(pts).T
        b = ((t / 1000.0 - t0) // bin_s).astype(int)
        sums, cnt = np.bincount(b, weights=v, minlength=n)[:n], np.bincount(b, minlength=n)[:n]
        it = next(c for c in control for w in ("start", "stop") if c[w]["source"] == src)
        w = "start" if it["start"]["source"] == src else "stop"
        label, detail = it[w]["signal"], it[w]["detail"]
        if any(s["label"] == label for s in signals):
            continue
        ranges = [c[w]["range"] + [w] for c in control for w in ("start", "stop") if c[w]["signal"] == label]
        signals.append({"label": label, "detail": detail, "values": [round(float(s / k), 4) if k else None for s, k in zip(sums, cnt)],
                        "ranges": [list(r) for r in dict.fromkeys(tuple(r) for r in ranges)]})   # dedup, keep order
    states = []
    for (sec, kind), steps in ctl_steps.items():
        on = []
        for times, st, a, b in steps:
            edges = np.concatenate([[a], np.clip(times[1:], a, b), [b]]) / 1000.0
            on += [[float(edges[k]), float(edges[k + 1])] for k in range(len(st)) if st[k] and edges[k + 1] > edges[k]]
        ch = next(c["channel"] for c in control if CHANNEL_TO_SECTION.get(c["channel"]) == sec)
        states.append({"label": f"{ch} {'logging' if kind == 'rx' else 'transmit list'}", "on": on})
    return {"t0": float(t0), "bin_s": bin_s, "xs": [float(t0 + k * bin_s) for k in range(n)], "channels": chans,
            "before": {c: rnd(fps["before"][c]) for c in chans}, "after": {c: rnd(fps["after"][c]) for c in chans},
            "signals": signals, "states": states}


if __name__ == "__main__":
    sys.exit(main())
