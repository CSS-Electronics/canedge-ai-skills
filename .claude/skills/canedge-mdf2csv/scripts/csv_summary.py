"""Summarize raw CAN/LIN frames per file without loading them into the conversation.

Input: CANedge log files/folders (MF4/MFC/MFE/MFM, converted with mdf2csv into a temp folder that is
deleted afterwards) and/or mdf2csv CSV files (*_CAN.csv / *_LIN.csv). Per file it prints the time span
(UTC), frames per channel, unique IDs and the top IDs by frame count with their median period and
number of distinct payloads. Input files are never modified.

CANmod.router: --muxtp 010:11 (or --project with the router DBC in its dbc/) demuxes log files first with mdf2mdf,
so the router channels show as CAN11-14 instead of tunnel frames on CAN2 (canedge-workspace muxtp.py). Without it, a
note flags files whose CAN2 looks like un-demuxed router data.

Usage:
  python csv_summary.py <files/folders> [--top 20] [--passwords passwords.json] [--tool <mdf2csv>] [--json]
                        [--muxtp 010:11 | --project projects/<name>]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
from dbc_groups import fmt_can_id  # noqa: E402
from mf4_files import LOG_EXTS  # noqa: E402
from tools import mdf2csv_tables, password_file, passwords_refusal, read_mdf2csv, resolve  # noqa: E402
import muxtp  # noqa: E402


def iso(t: float) -> str:
    return dt.datetime.fromtimestamp(t, dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def summarize(name: str, bus: str, table, top: int) -> dict:
    """One mdf2csv table (bus = CAN | LIN): Time span, frames per channel, top IDs."""
    df = table.to_pandas()
    if df.empty:
        return {"file": name, "frames": 0}
    df["ch"] = bus + df["BusChannel"].astype(str)
    df = df.sort_values("TimestampEpoch", kind="mergesort")
    df["IDE"] = df["IDE"].fillna(0).astype(int) if "IDE" in df else 0
    g = df.groupby(["ch", "IDE", "ID"], sort=False)
    ids = g.agg(frames=("TimestampEpoch", "size"), payloads=("DataBytes", "nunique"))
    ids["period_ms"] = g["TimestampEpoch"].apply(lambda s: s.diff().median() * 1000)
    ids = ids.sort_values("frames", ascending=False).reset_index()
    return {
        "file": name,
        "first_utc": iso(df["TimestampEpoch"].iloc[0]), "last_utc": iso(df["TimestampEpoch"].iloc[-1]),
        "duration_s": round(float(df["TimestampEpoch"].iloc[-1] - df["TimestampEpoch"].iloc[0]), 3),
        "frames": int(len(df)),
        "channels": {ch: {"frames": int(n), "unique_ids": int(df.loc[df["ch"] == ch, "ID"].nunique())}
                     for ch, n in df["ch"].value_counts().sort_index().items()},
        "tx_frames": int(df["Dir"].sum()) if "Dir" in df else 0,
        "top_ids": [{"channel": r.ch, "id": "0x" + r.ID if bus == "LIN" else fmt_can_id(int(r.ID, 16), bool(r.IDE)),
                     "frames": int(r.frames), "period_ms": None if r.period_ms != r.period_ms else round(float(r.period_ms), 2),
                     "unique_payloads": int(r.payloads)} for r in ids.head(top).itertuples()],
    }


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("paths", nargs="+", type=Path)
    ap.add_argument("--top", type=int, default=20, help="IDs listed per file (default 20)")
    ap.add_argument("--passwords", help="passwords.json for encrypted files (default <project>/passwords.json)")
    ap.add_argument("--tool", help="mdf2csv path (default: canedge-mdf2csv/bin, or CANEDGE_MDF2CSV)")
    ap.add_argument("--muxtp", action="append",
                    help="CANmod.router demux: [canN:]ID:S1[,S2,S3,S4], e.g. 010:11 or 'default' (repeatable)")
    ap.add_argument("--project", type=Path, help="Take the demux mapping from the project's router DBC (and its passwords.json)")
    ap.add_argument("--json", action="store_true")
    a = ap.parse_args()
    try:
        mx = [muxtp.parse_spec(x) for x in a.muxtp or []] or (muxtp.for_project(a.project) if a.project else [])
    except ValueError as e:
        ap.error(str(e))

    files = []
    for p in a.paths:
        files += sorted(f for f in (p.rglob("*") if p.is_dir() else [p])
                        if f.suffix.lower() in LOG_EXTS or f.suffix.lower() == ".csv")
    if not files:
        sys.exit("No log or CSV files found")
    pw = password_file(a.passwords, a.project)
    refusal = passwords_refusal(files, pw, a.project)
    if refusal:
        sys.exit(refusal)
    out = []
    for f in files:
        try:
            if f.suffix.lower() == ".csv":
                bus = "LIN" if f.stem.endswith("_LIN") else "CAN"
                out.append({"input": str(f), "csv": [summarize(f.name, bus, read_mdf2csv(f), a.top)]})
                continue
            tables = mdf2csv_tables(f, resolve("mdf2csv", a.tool), pw, muxtp=mx,
                                    mdf2mdf=resolve("mdf2mdf") if mx else None)
            r = {"input": str(f), "csv": [summarize(f"{f.stem}_{bus}.csv", bus, t, a.top) for bus, t in tables]}
            if mx:
                r["demux"] = muxtp.key(mx)
            out.append(r)
        except Exception as e:  # noqa: BLE001 - report and continue
            out.append({"input": str(f), "error": f"{type(e).__name__}: {e}"})
    for r in out:  # CAN2 tunnel frames of a CANmod.router that were not demuxed
        hint = [i for c in r.get("csv", []) for i in c.get("top_ids", []) if i["channel"] == "CAN2"
                and i["id"] in ("0x010", "0x012") and not r.get("demux")]
        if hint:
            r["router_hint"] = ("CAN2 " + ", ".join(i["id"] for i in hint) + " may be CANmod.router tunnel frames: "
                                "Demux with --muxtp default (or the project's router DBC) to see the router channels")
    if a.json:
        print(json.dumps(out, indent=1))
        return 0
    for r in out:
        print(f"\n{r['input']}")
        if "error" in r:
            print(f"  ERROR {r['error']}")
            continue
        if r.get("demux"):
            print(f"  demuxed (mdf2mdf {r['demux']})")
        if r.get("router_hint"):
            print(f"  NOTE: {r['router_hint']}")
        for c in r["csv"]:
            if not c["frames"]:
                print(f"  {c['file']}: no frames")
                continue
            chans = ", ".join(f"{ch} {v['frames']:,} frames / {v['unique_ids']} IDs" for ch, v in c["channels"].items())
            print(f"  {c['file']}: {c['first_utc']} -> {c['last_utc']} UTC ({c['duration_s'] / 60:.1f} min), {chans}"
                  + (f", {c['tx_frames']:,} sent by the logger" if c["tx_frames"] else ""))
            for i in c["top_ids"]:
                print(f"    {i['channel']} {i['id']:>10}  {i['frames']:>9,} frames  period {i['period_ms']} ms  "
                      f"{i['unique_payloads']} payloads")
    return 0


if __name__ == "__main__":
    sys.exit(main())
