"""Build the dashboard facts: Small, additive tables that the report.html app filters and sums in the browser.

Built from the same per-minute device model as stats.json (raw_aggregate.device_model), so both give the same
numbers. All times are UTC epoch seconds. Tables are columnar (one list per column) to keep the HTML small:

  buckets  device x time bucket (finest of 1 min .. 1 day that keeps <= 70k rows): recorded / silent / low / high seconds,
           frames per channel (silent / active), busiest minute (all buses and per channel), log file bytes
  trips    one row per trip (all trips, not only the longest); its log files = the files overlapping it
  files    one row per log file: device, first/last frame, size, meta name, header readable, session/split/extension (<device>/<session>/<split>.<ext>), firmware, config CRC32
  ids      device x day (week / 30 days for large fleets) x channel x ID: frames, est. storage bytes, max unique payloads, period, files containing it

Minute states: Silent (recording, no frames on any bus) or active. Averages use active time only.
"""
from __future__ import annotations

import argparse
from collections import defaultdict

import numpy as np

from raw_stats import STATIC_MAX_CHANGES_PCT, est_storage
from raw_aggregate import MIN_RATE_COVER_S, ch_key, device_model, rnd, router_info, ts, weighted_median

SCHEMA = "canedge-raw-facts/1"
BUCKET_CHOICES = (60, 300, 900, 3600, 6 * 3600, 86400)  # finest that fits the budget: 1 min for small datasets
MAX_BUCKET_ROWS = 70_000  # keeps the embedded data at a few MB
ID_GRAINS = (86400, 7 * 86400, 30 * 86400)  # Storage tab time resolution: day, else week / 30 days for large fleets
MAX_ID_ROWS = 100_000
DAY = 86400


def build_facts(stats: list[dict], o: argparse.Namespace) -> dict:
    by_dev: dict[str, list[dict]] = defaultdict(list)
    for s in stats:
        by_dev[s["file"]["device"]].append(s)
    devices = sorted(by_dev)
    models = {d: device_model(by_dev[d], o) for d in devices}
    channels = sorted({c for m in models.values() for c in m["chans"]}, key=ch_key)

    bucket = BUCKET_CHOICES[-1]
    for b in BUCKET_CHOICES:
        if sum(len(np.unique(m["minutes"] * 60 // b)) for m in models.values()) <= MAX_BUCKET_ROWS:
            bucket = b
            break

    bk = {"dev": [], "t": [], "rec": [], "act": [], "pk": [], "bytes": []}
    fr = {ch: {"a": [], "pk": []} for ch in channels}  # frames in active minutes (silent minutes have none)
    trips = {"dev": [], "start": [], "end": [], "min": [], "fps": []}  # trip log files: From `files` (overlap) in the app
    files = {"dev": [], "first": [], "last": [], "size": [], "session": [], "split": [], "ext": [], "fw": [], "crc": [],
             "meta": [], "hdr": []}
    meta_names: list[str] = []
    meta_index: dict[str, int] = {}
    meta = {}
    ids_acc: dict[tuple, dict] = {}
    id_dlc: dict[str, set] = defaultdict(set)

    for di, dev in enumerate(devices):
        m = models[dev]
        minutes, cov, code = m["minutes"], m["cov"], m["code"]
        meta[dev] = {"channels": m["chans"],
                     "bitrates": {c: sorted({s["file"]["bitrates"].get(c) for s in m["stats"]
                                             if s["file"]["bitrates"].get(c)}) for c in m["chans"]}}
        router = router_info(m["stats"])  # CANmod.router: channel labels + un-demuxed tunnel frames (app notes)
        if router:
            meta[dev]["router"] = router
        if not len(minutes):
            continue
        # log file bytes per minute: Each file's size spread over its minutes by their frame share
        bytes_min = np.zeros(len(minutes))
        for s in m["stats"]:
            tot = np.zeros(0)
            start = None
            for c in s["channels"].values():
                if c.get("bins"):
                    b = np.asarray(c["bins"]["frames"], dtype=float)
                    tot = b if not len(tot) else tot + b
                    start = int(ts(c["bins"]["start_utc"]) // 60)
            if start is None or tot.sum() <= 0:
                continue
            mm = start + np.arange(len(tot))
            k = np.clip(np.searchsorted(minutes, mm), 0, len(minutes) - 1)
            ok = minutes[k] == mm
            np.add.at(bytes_min, k[ok], s["file"]["size_bytes"] * tot[ok] / tot.sum())

        bstart = (minutes * 60 // bucket) * bucket
        keys, inv = np.unique(bstart, return_inverse=True)
        n = len(keys)
        add = lambda w: np.bincount(inv, weights=w, minlength=n)  # noqa: E731
        bk["dev"] += [di] * n
        bk["t"] += keys.astype(int).tolist()
        bk["rec"] += np.round(add(cov)).astype(int).tolist()
        bk["act"] += np.round(add(np.where(code == 1, cov, 0))).astype(int).tolist()
        pk = np.zeros(n)  # busiest minute: The same recorded minutes as the averages (raw_aggregate per_min)
        np.maximum.at(pk, inv, m["rate"])
        bk["pk"] += np.round(pk).astype(int).tolist()
        bk["bytes"] += np.round(add(bytes_min)).astype(int).tolist()
        for ch in channels:
            f = m["fr"].get(ch)
            if f is None:
                for k_ in ("a", "pk"):
                    fr[ch][k_] += [0] * n
                continue
            for k_, c_ in (("a", 1),):
                fr[ch][k_] += np.round(add(np.where(code == c_, f, 0))).astype(int).tolist()
            pkc = np.zeros(n)
            np.maximum.at(pkc, inv, m["per_min"](f))
            fr[ch]["pk"] += np.round(pkc).astype(int).tolist()

        for g in m["segments"]:
            trips["dev"].append(di)
            trips["start"].append(int(ts(g["start_utc"])))
            trips["end"].append(int(ts(g["end_utc"])))
            trips["min"].append(g["minutes"])
            trips["fps"].append(int(round(g["fps_mean"] or 0)))

        for s in m["stats"]:
            f = s["file"]
            t0 = ts(s["time"]["first_utc"])
            files["dev"].append(di)
            files["first"].append(int(t0))
            files["last"].append(int(ts(s["time"]["last_utc"])))
            files["size"].append(f["size_bytes"])
            files["session"].append(f.get("session") or 0)
            files["split"].append(f.get("split") or 0)
            files["ext"].append(f.get("format") or "MF4")
            files["fw"].append(f.get("firmware") or "")
            files["crc"].append(f.get("config_crc32") or "")
            mn = f.get("meta")  # device meta name from the header; -1 = not set (or header not readable)
            if mn and mn not in meta_index:
                meta_index[mn] = len(meta_names)
                meta_names.append(mn)
            files["meta"].append(meta_index[mn] if mn else -1)
            files["hdr"].append(0 if f.get("header") == "unsupported" else 1)
            day = int(t0 // DAY * DAY)
            for i in s["ids"]:
                key = (di, day, i["channel"], i["id"])
                a = ids_acc.setdefault(key, {"fr": 0, "est": 0, "up": 0, "cap": 0, "chg": 0, "per": [], "w": [], "nf": 0})
                a["fr"] += i["frames"]
                a["chg"] += i.get("payload_changes", i["frames"])  # frames with a new payload (static ID basis)
                a["est"] += est_storage(i["frames"], i["payload_bytes"])
                a["up"] = max(a["up"], i["unique_payloads"])
                a["cap"] |= int(bool(i.get("unique_payloads_capped")))
                a["per"].append(i["period_ms_median"])
                a["w"].append(i["frames"])
                a["nf"] += 1
                id_dlc[f"{dev}|{i['channel']}|{i['id']}"] |= set(i["dlc"])

    id_grain = ID_GRAINS[0]
    for g in ID_GRAINS:  # coarsen the ID time grain until the table fits the budget
        id_grain = g
        if len({(k[0], k[1] // g, k[2], k[3]) for k in ids_acc}) <= MAX_ID_ROWS:
            break
    if id_grain > DAY:
        merged: dict[tuple, dict] = {}
        for (di, day, ch, idn), a in ids_acc.items():
            m_ = merged.setdefault((di, day // id_grain * id_grain, ch, idn), {"fr": 0, "est": 0, "up": 0, "cap": 0, "chg": 0, "per": [], "w": [], "nf": 0})
            for k_ in ("fr", "est", "nf", "chg"):
                m_[k_] += a[k_]
            m_["up"], m_["cap"] = max(m_["up"], a["up"]), m_["cap"] | a["cap"]
            m_["per"] += a["per"]
            m_["w"] += a["w"]
        ids_acc = merged
    id_names = sorted({k[3] for k in ids_acc})
    id_index = {v: k for k, v in enumerate(id_names)}
    ids = {"dev": [], "day": [], "ch": [], "id": [], "fr": [], "est": [], "up": [], "cap": [], "chg": [], "per": [], "nf": []}
    for (di, day, ch, idn), a in sorted(ids_acc.items()):
        per = weighted_median(a["per"], a["w"])
        ids["dev"].append(di)
        ids["day"].append(day)
        ids["ch"].append(channels.index(ch))
        ids["id"].append(id_index[idn])
        ids["fr"].append(a["fr"])
        ids["est"].append(a["est"])
        ids["up"].append(a["up"])
        ids["cap"].append(a["cap"])
        ids["chg"].append(a["chg"])
        ids["per"].append(None if per is None else round(per, 3))
        ids["nf"].append(a["nf"])

    return {"schema": SCHEMA, "bucket_s": bucket, "id_grain_s": id_grain, "min_rate_cover_s": MIN_RATE_COVER_S,
            "static_max_changes_pct": STATIC_MAX_CHANGES_PCT,
            "devices": devices, "channels": channels,
            "id_names": id_names, "meta_names": meta_names, "id_dlc": {k: ",".join(map(str, sorted(v))) for k, v in id_dlc.items()},
            "segment_merge_min": o.segment_merge_min, "trip_min_min": o.trip_min_min, "meta": meta,
            "buckets": {**bk, "fr": fr}, "trips": trips, "files": files, "ids": ids}


def facts_summary(f: dict) -> str:
    return (f"facts: {len(f['devices'])} devices, {len(f['buckets']['t']):,} buckets of {f['bucket_s'] // 60} min, "
            f"{len(f['trips']['start']):,} trips, {len(f['files']['first']):,} files, {len(f['ids']['fr']):,} ID rows")


__all__ = ["build_facts", "facts_summary", "rnd"]
