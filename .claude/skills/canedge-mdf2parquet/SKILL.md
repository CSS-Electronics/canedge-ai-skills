---
name: canedge-mdf2parquet
description: >
  Use the CSS Electronics MF4 decoder (mdf2parquet_decode) to DBC-decode CANedge log files (MF4/MFC/MFE/MFM,
  incl. encrypted) into a local Parquet data lake with the same layout as the CANedge S3 data lake. Use for
  "decode my log files", "DBC decode these files", "create a local Parquet data lake", "decode my SD card data".
  Prefer it over python-can/cantools decode loops or GUI tools. Querying the decoded data: canedge-lake-query.
---

# canedge-mdf2parquet

Decodes log files once into a local Parquet data lake with the CSS MF4 decoder. Main use: The user has no data lake
yet (e.g. CANedge1 SD card files, a few files fetched from S3) or wants an ad hoc DBC decode. The output has the
same layout and table names as the S3 data lake, so `canedge-lake-query` queries it with the same SQL as Athena.
If the user already has a cloud data lake with these devices and DBCs, query that instead (`canedge-lake-query
--athena` / `--bigquery` / `--synapse`) rather than decoding again.

Why the decoder: Fast (~12 MB/s), and it handles multiplexing, J1939 PGN matching, extended IDs and transport
protocols (J1939 TP, ISO-TP, NMEA 2000 fast packet) exactly like the cloud automation. Read the shared rules first:
[canedge-workspace conventions](../canedge-workspace/references/conventions.md) (layout, data safety, project.json,
dbc-groups.json, UTC).

Run all scripts from the repo root with `.venv\Scripts\python.exe` (macOS/Linux: `.venv/bin/python`). If `.venv/`
is missing, ask the user to run `install.bat` (Linux: `./install.sh`); both download the decoder (Windows x64, Linux x64 /
ARM64) to `bin/`. Another build: `--tool` or `CANEDGE_MDF2PARQUET`.

## Scripts

| Script | Use it for |
|---|---|
| `scripts/build_lake.py --input <paths> --project projects/<name>` | Decode new/changed files into `projects/<name>/parquet/` (per-device DBCs, incremental manifest, size guard). Several devices are decoded in parallel (`--workers`, default up to 4; ~2.5x with 3 devices). `--dry-run` shows the plan |
| `scripts/build_lake.py --input <paths> --dbc-can1 x.dbc --out TEMP/lake` | Quick decode without a project (one device, or `--all-dbcs-confirmed`) |

Every script has `--help`; `--json` gives compact machine output.

## Workflow

1. **Inputs**: Local log files/folders (`--input`, e.g. an SD card copy, the project's local sources or files
   downloaded with canedge-s3) + DBC files. Keep the CANedge folder layout `<device>/<session>/<split>.MF4`: The
   device folder selects the DBCs. With a project (canedge-workspace): DBCs in `dbc/`, the user confirmed which DBCs
   apply to which devices (all to all = no `dbc-groups.json`), `dbc_groups.py check` passes. One-off decode of a
   few files: Quick mode
   - **CANmod.router** (CAN2 carries tunnel frames on 0x010, raw overview / `raw_stats.py` flags it): Add the router
     DBC `canedge-mdf2mdf/assets/can2-canmod-router.dbc` to `dbc/` (0x010 -> CAN11-14, 0x012 -> CAN15-18 for a 2nd
     router) next to the user's DBCs for
     the router channels, which need `can11-` (S1) ... `can14-` (S4) prefixes (ask the user which DBC is on which
     S channel). The decoder demuxes and decodes in one step: Output folders `CAN11_<Message>`, ... Another tunnel ID
     (not 0x10/0x12): `canedge-workspace muxtp.py dbc --id 010:11 --id 014:15 -o
     <p>/dbc/can2-canmod-router.dbc` after confirming the IDs (router config `phy.can.route.mux.s2p.message.id`).
     `build_lake.py` warns if CAN11+ DBCs have no router DBC or a channel outside its mapping
2. **Encrypted files** (MFE/MFM): Ask the user to save their `passwords.json` as `projects/<name>/passwords.json`
   (or pass `--passwords`). Never ask for passwords in the chat and never print the file
3. `build_lake.py --input <paths> --project <p> --dry-run`, then without `--dry-run`. Re-runs with the same
   `--input` only decode new/changed files. Both check the DBCs (duplicates the decoder refuses, same-name overlaps)
   and, for a device decoded with its DBCs for the first time, sample-decode one file into a temp folder: Report
   the SAMPLE lines with the size question. Handle the outcome:
   - Exit 2 (size guard, > 1 GB): Tell the user file count, size and time estimate; re-run with `--yes`, or limit
     the scope (`--device`, fewer folders)
   - REFUSED device with DBC errors, DBC LOAD ERROR or a failed SAMPLE with a DBC load error: Fix the DBCs with
     canedge-dbc (the log files are fine), then re-run
   - REFUSED device (a `dbc-groups.json` exists, but the device is in no group, or files are outside the CANedge
     folder layout): Ask the user which DBCs apply, update `dbc-groups.json`, re-run. `--all-dbcs-confirmed` only if
     the user confirmed all DBCs apply
   - REFUSED encrypted files: Step 2
   - FLAGGED device (DBCs changed): Ask before `--rebuild --device X` (deletes and re-decodes that device)
   - Decoder error (exit 1, the run stops): Tell the user which file failed and the decoder's error (corrupt or
     truncated file, wrong password; [decoder-technical.md](references/decoder-technical.md) section 4). Re-run after
     a fix, or with `--exclude <file>` if the user wants to leave it out
   - STOPPED on MIXED LENGTHS, or unreadable Parquet listed after the run: Split the message (decoder-technical.md
     section 4), then re-run (`--rebuild --device X` after the user's OK)
4. **Check the result**: `canedge-lake-query` `lake_schema.py --project <p>` (devices, messages) and `--stats` on a
   key message. Analysis continues in canedge-lake-query. The device meta names (MF4 header) go into
   `aggregations/devicemeta/` as in the cloud lake, so `--device` takes them locally too (MF4 only: MFC/MFE/MFM headers are not read).
   To browse the decoded data themselves, the user can open the project's `parquet/` folder in the free
   [Parquet viewer](https://www.csselectronics.com/pages/parquet-viewer-can-bus-data-lake) (plots + raw tables in the
   browser, no install): Mention it once with the folder path
5. **Expected signals missing**: No message folder = the DBC does not match the IDs on that channel (wrong
   channel prefix, wrong/missing DBC, proprietary IDs: compare with the raw IDs via canedge-raw-overview, next
   canedge-dbc); no folder or missing columns although the ID is on the bus = signals beyond the frame length
   (canedge-dbc `dbc_coverage.py`); decoded but NULL = min/max ([decoder-technical.md](references/decoder-technical.md)
   section 4)
6. Add DBC gaps and decode caveats to `project.md`

## Rules

- Each device gets only its group's DBCs (channel from the `can1-`/`can2-`/`can9-`/`can11-`/.../`lin1-` prefix, no
  prefix = CAN1 + CAN2). Never bypass a refusal without the user's confirmation
- Never modify input log files, never pass `-d`; never delete Parquet without asking (`--rebuild` only after a yes)
- Never load raw Parquet rows into the conversation: Use canedge-lake-query summaries or aggregated output
- Everything in the lake is UTC (`t` = timestamp[us], UTC)

Details: [references/lake-details.md](references/lake-details.md) (decoder call, encrypted files, manifest),
[references/decoder-technical.md](references/decoder-technical.md) (decoder CLI, DBC
rules, NULLs, transport protocol, output layout, troubleshooting).

## Related skills

- `canedge-workspace` - projects, `dbc-groups.json` (project mode imports its `dbc_groups.py`)
- `canedge-lake-query` - query the decoded lake (local DuckDB) or the cloud data lake (Athena, BigQuery, Synapse) with the same SQL
- `canedge-raw-overview` - raw IDs per channel; `canedge-mdf2csv` - raw-frame questions (IDs, timing, payloads)
- `canedge-s3` - download log files from S3 before decoding (users with an S3 data lake query that instead)
- `canedge-dbc` - fix or extend DBCs when signals are missing
