---
name: canedge-mdf2csv
description: >
  Convert CANedge log files (MF4, MFC, MFE, MFM) to raw CAN / CAN FD / LIN frames (CSV) with the CSS mdf2csv
  converter, no DBC needed: CLI options, CSV columns, quick per-file summaries. Use for "convert this log file
  to CSV", "which CAN IDs are in this file", "how often is ID X sent", "decrypt / decompress my log files".
  Overview across many files: canedge-raw-overview. DBC decoding: canedge-mdf2parquet.
---

# canedge-mdf2csv

The official CSS Electronics MF4 converter for raw data. It reads every CANedge log file format
(incl. compressed and encrypted files), is fast and needs no DBC. Do not use GUI tools or custom MF4
parsers for raw frames.

## Tool

| Platform | Location |
|---|---|
| Windows x64, Linux x64 / ARM64 | `.claude/skills/canedge-mdf2csv/bin/mdf2csv[.exe]` (24.12.19), downloaded by `install.bat` / `install.sh` (`canedge-workspace/scripts/install_tools.py`) |
| macOS / other | No build: Use a Linux VM/container, or set `CANEDGE_MDF2CSV=<path>` / `--tool` |

The docs page also has separate downloads of the converters to ASC (Vector) and TRC (PEAK). The converter to MF4
(`mdf2mdf`: Finalize, decompress/decrypt, demux CANmod.router data) is installed with skill `canedge-mdf2mdf`.
Docs: https://canlogger.csselectronics.com/tools-docs/converters_mf4/converters/

**CANmod.router**: `mdf2csv` cannot demux. Raw router data is CAN FD tunnel frames on CAN2 (ID 0x010; a second
router 0x012). Demux first with `mdf2mdf` (skill `canedge-mdf2mdf`), then convert the demuxed MF4: The router
channels S1-S4 then show as `BusChannel` 11-14 (15-18 for a second router). `csv_summary.py --muxtp default` (or
`--project` with the router DBC) does both in one step.

## Usage

```
mdf2csv.exe --non-interactive -O <out-folder> <file.MF4>            one file
mdf2csv.exe --non-interactive -O <out-folder> -i <folder>           all log files below a folder
mdf2csv.exe --non-interactive -O <out-folder> -p passwords.json <file.MFE>   encrypted files
```

| Option | Meaning |
|---|---|
| `-O, --output-directory` | Output folder. Always set it (the project's `temp/`, or `TEMP/` without a project), so nothing is written next to the user's log files |
| `-i, --input-files` | Input files/folders (must have a common root); positional paths work too |
| `-p, --password-file` | Passwords for encrypted files (default: `passwords.json` in the working directory) |
| `-X, --no-append-root` | Folder input: No extra `<folder>_out` level in the output |
| `-b, --buffer` | Buffer size in bytes (0 disables, -1 = whole file) |
| `--non-interactive`, `--verbosity=<0-4>` | No progress output (use in scripts) / log level |
| `-v, --version` | Tool version (state it when reporting results) |
| `-d, --delete-converted` | **Never use**: Deletes the input log files after conversion |

`passwords.json` (device ID keys take precedence over `default`). The scripts (`csv_summary.py`, raw stats, ...) use
`--passwords <path>`, else `projects/<name>/passwords.json`, and never the working directory's file:

```json
{"default": "thedefaultpasswordforall", "AABBCCDD": "devicespecificpasswordex"}
```

## Output

- One CSV per bus type and input file: `<split>_CAN.csv` (CAN + CAN FD, all CAN channels) and
  `<split>_LIN.csv`. A file without LIN data gives no LIN CSV
- Folder input: The output mirrors the input tree below `<out-folder>/<folder>_out/` (`-X` drops the `_out` level)
- Many files from several devices: mdf2csv uses one CPU core, so run one call per device folder in parallel (Python
  `ThreadPoolExecutor`, at most 4; works the same on Windows and Linux, no GNU parallel needed), each with its own
  `-O <out>/<DEVICE>` (with `-X` the outputs of several devices would collide). Measured: 3 devices 49 s -> 12 s.
  Raw statistics across many files: `canedge-raw-overview` `raw_stats.py` (already parallel)
- Separator `;`, one row per frame, sorted by time. Data frames only (no error frames)

| Column | CAN | LIN | Meaning |
|---|---|---|---|
| `TimestampEpoch` | x | x | UTC Unix time in seconds (CANedge resolution 50 &#181;s) |
| `BusChannel` | x | x | 1, 2 (CAN1/CAN2, LIN1/LIN2); 9 = internal GNSS/IMU (CANedge GNSS variants); 11-14, 15-18, ... = CANmod.router S1-S4 after demux (mdf2mdf) |
| `ID` | x | x | Hex without `0x` (e.g. `081`, `18FEF100`) |
| `IDE` | x | | 1 = 29-bit (extended) ID |
| `DLC` / `DataLength` | x | `DataLength` only | DLC code vs. number of data bytes (differ for CAN FD > 8 bytes) |
| `Dir` | x | x | 1 = transmitted by the CANedge itself (e.g. OBD2 requests) |
| `EDL`, `BRS`, `ESI` | x | | CAN FD frame / bit-rate switch / error state indicator |
| `RTR` | x | | Remote frame |
| `DataBytes` | x | x | Payload as hex string (e.g. `0000000100000400`) |

## Rules

- Input log files are read-only: Never pass `-d`, never move/rename/delete logs; write output outside the log folders
- CSV is 3-10x the MF4 size (a 50 MB log is ~3M rows). Convert per file into a temp folder, summarize
  with a script and delete the temp CSV. Before converting > ~1 GB, tell the user the size and ask
- Never load CSV rows into the conversation. Use `scripts/csv_summary.py` or your own pandas/pyarrow
  script and print aggregates only
- Times are UTC. Show local time to the user (see the `canedge-workspace` conventions)

## Script

`scripts/csv_summary.py <log or CSV files/folders> [--top 20] [--json] [--muxtp default | --project <p>]` (run with
`.venv\Scripts\python.exe`): Per file: Time span, frames per channel, unique IDs, and the top IDs by frame count with
their median period and number of distinct payloads. Log files are converted to a temp folder that is deleted
afterwards. Use it for quick questions about a few files. `--muxtp` / `--project`: CANmod.router demux first.

## Related skills

- `canedge-raw-overview` - standardized raw data report across 100s of files (activity, frames/s, storage)
- `canedge-mdf2parquet` - DBC-decode log files into signals
- `canedge-knowledge` - log file format, sessions/splits, configuration
