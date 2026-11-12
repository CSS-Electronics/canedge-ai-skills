---
name: canedge-mdf2mdf
description: >
  Demux CANmod.router data and convert CANedge log files (MFC, MFE, MFM, MF4) to plain MF4 with the CSS mdf2mdf
  converter: Router channels S1-S4 as CAN11-14 (CAN15-18 for a second router), decompress / decrypt / finalize
  for asammdf or Vector/PEAK tools. Use for "demux my CANmod.router data", "I use a CANmod.router", "convert MFC to
  MF4", "decrypt my log files for asammdf", "which mdf2mdf flags do I need". Raw stats: canedge-raw-overview,
  decoding: canedge-mdf2parquet (both demux on the fly).
---

# canedge-mdf2mdf

The official CSS Electronics MF4 to MF4 converter. It demuxes CANmod.router data (MUX-TP) and decompresses,
decrypts and finalizes log files. Never use other tools (asammdf, custom parsers) to demux or decrypt.

## Tool

| Platform | Location |
|---|---|
| Windows x64, Linux x64 / ARM64 | `.claude/skills/canedge-mdf2mdf/bin/mdf2mdf[.exe]` (24.12.19), downloaded by `install.bat` / `install.sh` (`canedge-workspace/scripts/install_tools.py`) |
| macOS / other | No build: Use a Linux VM/container, or set `CANEDGE_MDF2MDF=<path>` / `--tool` |

Docs: https://canlogger.csselectronics.com/tools-docs/converters_mf4/converters/mdf/index.html

## CANmod.router in short

- The router sends its 4 secondary channels (S1-S4) to the CANedge CAN2 as CAN FD **tunnel frames** on one CAN ID
  (default **0x10**, router config `phy.can.route.mux.s2p.message.id`). A raw log then shows CAN2 with one big ID
  0x010 (64-byte FD frames) instead of the router traffic
- **Demux** = unpack the tunnel frames into the original frames on virtual channels: 0x10 -> CAN11-14 (S1-S4), a
  second, daisy-chained router (tunnel ID 0x12) -> CAN15-18. CAN11+ avoids clashing with CAN9 (GNSS/IMU). The tunnel
  frames are removed; CAN1, CAN9 and other CAN2 traffic (e.g. other CANmods) stay
- **The router DBC is the switch**: `assets/can2-canmod-router.dbc` (message 0x10 with
  `TransportProtocolType "MUXTP#11:12:13:14"`, 0x12 with `"MUXTP#15:16:17:18"`) in the project's `dbc/` folder makes every canedge-* script demux
  that device's files (canedge-workspace `muxtp.py`), and makes the MF4 decoders demux + decode in one step. The
  user's DBCs for the router channels need `can11-`, `can12-`, ... prefixes
- Other tunnel IDs (not 0x10/0x12): `muxtp.py dbc --id 010:11 --id 014:15 -o <project>/dbc/can2-canmod-router.dbc`
  writes a matching router DBC (replace the default one). Confirm the IDs with the user (router config)

Detection: `raw_stats.py` / `csv_summary.py` report "looks like CANmod.router data that is not demuxed" when CAN2
carries CAN FD frames on 0x010 / 0x012. Ask the user before adding the router DBC.

## When to write demuxed files

The canedge-* scripts demux on the fly (temporary MF4, deleted), so **no copy is needed** for raw overviews, config
simulation or decoding. Write files only when the user wants them, e.g. for asammdf, Vector/PEAK tools or to share:

```
.venv\Scripts\python.exe .claude/skills/canedge-mdf2mdf/scripts/mdf2mdf.py --project projects/<name>
.venv\Scripts\python.exe .claude/skills/canedge-mdf2mdf/scripts/mdf2mdf.py <files/folders> --muxtp default
.venv\Scripts\python.exe .claude/skills/canedge-mdf2mdf/scripts/mdf2mdf.py <files/folders> --no-demux --passwords passwords.json
```

- Passwords (MFE/MFM): `--passwords <path>`, else `<project>/passwords.json`; refused up front without one
- Mapping: `--project` (router DBC per device), `--muxtp [canN:]ID:S1[,S2,S3,S4]` (`010:11` = 0x010 -> CAN11-14;
  `default` = `010:11`; repeatable), or `--no-demux` (only MFC/MFE/MFM -> MF4)
- Output: `<project>/temp/mf4-demuxed/<DEVICE>/<SESSION>/<SPLIT>.MF4` (`--no-demux`: `.../temp/mf4/`; no project:
  `TEMP/...`). The CANedge layout and the MF4 header are kept, so other scripts can read the copies
- Prints the frames per channel of each output (`--summary`, default up to 10 files). Existing outputs are skipped
  (`--force` to redo)

## mdf2mdf CLI (direct use)

```
mdf2mdf.exe --non-interactive -O <out-folder> --muxtp-can2=010#11:12:13:14 <file.MF4>
mdf2mdf.exe --non-interactive -O <out-folder> --muxtp-can2=010#11:12:13:14 --muxtp-can2=012#15:16:17:18 <file.MF4>
mdf2mdf.exe --non-interactive -O <out-folder> -p passwords.json <file.MFE>
```

| Option | Meaning |
|---|---|
| `--muxtp-can<N>=<ID>#<S1>:<S2>:<S3>:<S4>` | Demux tunnel frames on channel N, ID hex (3 digits 11-bit, 8 digits 29-bit), into channels S1-S4 (decimal; not N). Repeat for several tunnel IDs |
| `-O, --output-directory` | Output folder: Always set it, outside the user's log folders. Output file: `<stem>_out.MF4` |
| `-p, --password-file` | Passwords for encrypted files (`{"default": "...", "AABBCCDD": "..."}`) |
| `-i`, `-X`, `-b`, `--verbosity`, `--non-interactive`, `-v` | As for mdf2csv (`canedge-mdf2csv`) |
| `-d, --delete-converted` | **Never use**: Deletes the input log files |

## MF4 header of MFC/MFE/MFM files

The header (serial, firmware, config CRC32, meta name, bit-rates) of compressed/encrypted files is only readable
after finalizing. The scripts do this themselves (canedge-workspace `mf4_files.fill_headers()`: one file per
device/session); the whole file is needed (MFC carries a file checksum). mdf2mdf 24.12.19 drops the LIN bit-rates.

## Rules

- Input log files are read-only: Never pass `-d`, never write into the log folders
- The demuxed MF4 is decompressed: It can be 2x the MFC size. Tell the user the size before writing > ~1 GB
- Demux mapping and DBC prefixes must agree (`muxtp.py show --project <p>` prints the mapping and flags)
- State the mdf2mdf version and the mapping when reporting results

## Related skills

- `canedge-raw-overview` / `canedge-mdf2csv` - raw frames of the router channels (demux on the fly)
- `canedge-mdf2parquet` - decode router channels (router DBC + `can11-` DBCs)
- `canedge-config` - CANmod.router config: Filters/prescalers for S1-S4 (router report)
- `canedge-knowledge` - CANmod.router facts (`references/canmod/canmod-router.md`)
