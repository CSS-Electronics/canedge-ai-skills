---
name: canedge-dbc
description: >
  Review, fix, create and compare DBC files: Lint (cantools + the CSS DBC editor checks), build a DBC from a
  PDF/Excel spec, diff two DBCs, find overlaps between DBCs on a channel, and check on decoded data whether
  signals look plausible. Use for "check/fix my DBC", "create a DBC from this PDF/Excel", "compare two DBCs",
  "do my DBC files overlap", "which signals look wrong".
---

# canedge-dbc

Deterministic validation for syntax (cantools + checks ported from the CSS Electronics DBC editor), AI + data for
semantics (does the DBC decode real logs to sensible values). Uses cantools for parsing/writing.

Read [canedge-workspace conventions](../canedge-workspace/references/conventions.md) first. DBC-to-device
mapping: If the project has a `dbc-groups.json`, use it and never test a DBC on data from a device it is not mapped
to. Without one, ask the user once before testing on data whether the DBCs apply to all devices (the usual case).
Linting, building and diffing need no mapping. The channel comes from the `can1-`/`can2-`/`can9-` file name prefix
(no prefix = CAN1 + CAN2); `can11-`, `can12-`, ... = CANmod.router channels after demux (the router DBC
`can2-canmod-router.dbc` in the project makes `dbc_coverage.py` use demuxed raw stats; workspace conventions 5b).

## Scripts

Run from the repo root with `.venv\Scripts\python.exe .claude/skills/canedge-dbc/scripts/<script>` (`--help` for
all options, `--json` for machine-readable output).

| Script | Use it for |
|---|---|
| `dbc_lint.py <file.dbc> [--fix --out <fixed.dbc>]` | Errors vs warnings with line numbers. `--fix` = safe mechanical fixes only, written to a new file |
| `dbc_info.py <file.dbc> [--message <name\|id>] [--find <text>]` | Message table (ID, DLC, extended/FD, cycle time, mux), signal details, signal search |
| `dbc_build.py --spec <spec.json\|csv> --out <new.dbc>` | Build a DBC from a structured spec, then lint it |
| `dbc_coverage.py --log <files> --project <p> \| --dbc <files>` | Which DBC messages are on the bus (raw IDs + frame lengths, no decoding) and which of them decode to nothing or lose signals: Signals beyond the frame length, mixed frame lengths, messages without signals; plus bus IDs no DBC matches |
| `dbc_diff.py <a.dbc> <b.dbc> [--ignore comments,...] [--html <page>]` | Semantic diff: messages/signals added, removed, changed fields; likely renames. `--html`: Before/after page for the user (Diff tab = side by side as in the DBC editor, Changes tab = the semantic changes) |

## Workflow

1. **Review / lint**: `dbc_lint.py` on each DBC. Explain errors first (they break decoding), then warnings.
   `dbc_info.py` for an overview when the user asks what is in the file. With 2+ DBCs on a channel also run
   the overlap check across files (canedge-workspace): `dbc_groups.py check --project <p>` (per device group) or
   `dbc_groups.py check --dbc <files or folder>` (loose files = one device). This is the usual cause when decoded
   data "looks weird" although each DBC lints clean
2. **Fix**: Apply `--fix` for mechanical issues. For anything semantic (duplicate IDs, overlaps, wrong ranges)
   propose the change, let the user decide, write a new file (e.g. `<name>-fixed.dbc`), re-lint and show
   `dbc_diff.py original fixed --html <page>` as proof that only the intended fields changed. Give the user the
   page (`projects/<p>/reports/<yyyymmdd>-dbc-changes/report.html`, without a project
   `TEMP/reports/<yyyymmdd>-dbc-changes/report.html`; never next to the user's DBC) as the bold **Open report**
   link from the printed `OPEN` line, for every DBC edit, like the config skill's Diff tab
3. **Create from a document**: Extract the PDF/Excel/Word tables into the spec format
   ([references/build-spec.md](references/build-spec.md)), mind the bit numbering table there, run
   `dbc_build.py`, then `dbc_info.py --message` on a few messages to cross-check against the document. If an
   official DBC exists, `dbc_diff.py official.dbc new.dbc` must report IDENTICAL
4. **Coverage on raw data** (no decoding needed): `dbc_coverage.py --log <data folder>` if the raw stats are cached
   (canedge-raw-overview ran: seconds, all sessions), else `--log <a few typical files>`; before a large decode
   or when expected messages are missing: NOT DECODED / AT RISK rows = signals beyond the real frame length (fix the
   DBC to the frames, never the other way round); MIXED LENGTHS = split the message (canedge-mdf2parquet
   decoder-technical.md section 4)
5. **Semantic check on data**: Needs a decoded lake: The user's cloud data lake, or a local one (create it with
   canedge-mdf2parquet using the confirmed DBC mapping). With canedge-lake-query on one device and a short window:
   `lake_schema.py --catalog --dbc` (units, always-NULL signals) + `--stats <table>` (NULL share, min/max); judge
   the ranges with [references/plausibility.md](references/plausibility.md) and explain each suspect with its likely cause
6. **Confirm a suspected scale/unit error** with a reference signal: `lake_query.py resample` of both on the same
   window, median ratio + correlation in pandas (e.g. wheel speed vs GNSS speed). ~10, 3.6, 1.609, -1 or 256 names
   the cause

The free [online DBC editor](https://www.csselectronics.com/pages/dbc-editor-can-bus-database) shows the same
editor checks visually - suggest it when the user wants to inspect or edit a DBC by hand.

## Rules

- Never overwrite the user's DBC: fixes go to a new file (`--out`); `dbc_build.py` refuses to overwrite unless `--overwrite`
- The CSS decoders write NULL for values outside the DBC min/max: Never "fix" a range by narrowing it without data,
  and read NULL shares as possible range problems ([references/plausibility.md](references/plausibility.md))
- A plausibility flag is a lead, not proof: Confirm with a reference signal, the raw bytes (canedge-mdf2csv) or the
  user's documentation before changing a DBC
- Duplicate CAN IDs / overlapping J1939 PGNs WITHIN one DBC stop the CSS decoders (`dbc_lint.py` and
  `dbc_groups.py check` errors): Treat them as blocking. J1939 = `ProtocolType "J1939"` only (not VFrameFormat)
- ACROSS DBCs on the same channel, the same ID/PGN is fine if the message names differ: Each DBC decodes into its
  own Parquet folder. CSS uses this on purpose (J1939-73 DM1: one DBC per DTC slot, same ID, names `DM01_DTC_01`,
  `DM01_DTC_02`, ...) - never flag it. With the SAME message name the data shares a folder: Corrupt/mixed Parquet
  (different signals) or inconsistent values (other bits/scaling) although decoding "works" - not allowed. `dbc_groups.py check` reports exactly these.
  J1939 DBCs (`ProtocolType "J1939"`) match on the PGN, so a same-named OEM message with another source address
  counts too. Different channels (`can1-` vs `can2-`) or devices (separate DBC groups) never overlap
- Never load Parquet rows into the conversation; use the script summaries

## References

- [references/lint-checks.md](references/lint-checks.md) - every lint check, severity and its DBC editor source
- [references/build-spec.md](references/build-spec.md) - spec format (JSON/CSV) + bit numbering conversions
- [references/plausibility.md](references/plausibility.md) - data check steps, physical ranges, scale factor causes

## Related skills

- `canedge-workspace` - projects, `dbc-groups.json` (DBC-to-device mapping), `dbc_groups.py check` for overlaps
  between DBCs on the same channel
- `canedge-mdf2parquet` - creates a local decoded lake for the data check
- `canedge-lake-query` - the data check queries (catalog, stats, resample), local or cloud
- `canedge-mdf2csv` - raw frames/stats when a flag needs checking at byte level
- `canedge-reports` - the diff page kit (`dbc_diff.py --html`) and optional reports
