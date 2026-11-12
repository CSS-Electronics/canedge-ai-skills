# MF4 decoder (mdf2parquet_decode) - technical reference

AI-friendly summary of the CSS Electronics MF4 decoder docs + the decoder troubleshooting guide in the CANedge Intro
(checked against the docs for version 25.12.15). Use it to explain why a signal does not decode, why the lake looks
the way it does (folders, schema, NULLs, file counts) and how multi-frame messages are handled. How `build_lake.py`
calls the decoder: [lake-details.md](lake-details.md).

> Sources (verify against the live docs when in doubt):
> - Overview + CLI: https://canlogger.csselectronics.com/tools-docs/decoders_mf4/decoders/
> - Parquet output: https://canlogger.csselectronics.com/tools-docs/decoders_mf4/decoders/parquet/index.html
> - DBC support: https://canlogger.csselectronics.com/tools-docs/decoders_mf4/decoders/dbc/index.html
> - Output organization: https://canlogger.csselectronics.com/tools-docs/decoders_mf4/decoders/organization/index.html
> - Transport protocol: https://canlogger.csselectronics.com/tools-docs/decoders_mf4/transportprotocol/index.html
> - Troubleshooting: CANedge Intro, "MF4 decoder troubleshooting" (in `canedge-knowledge`)

## 1. Inputs

- Log files from CANedge devices: `MF4` (regular), `MFC` (compressed), `MFE` (encrypted), `MFM` (compressed +
  encrypted), plus MF4 files written by `mdf2mdf` (from 24.12.19). Not MDF from other tools (asammdf, MATLAB, ...)
- Encrypted files need a password file (`-p`, default `passwords.json` in the working directory):
  `{"default": "<password>", "AABBCCDD": "<device-specific password>"}`
- The decoder download also has a CSV variant (same DBC logic). Not to be confused with the raw-frame converters
  (`mdf2csv`, `mdf2mdf`: finalize / decompress / decrypt, see canedge-mdf2csv)

## 2. Command line

```text
mdf2parquet_decode [--verbosity=<0-4>] [--non-interactive] [-X] [-b <bytes>] [-p <passwords.json>]
                   [--concat | --no-concat] [--pgn-src-merge | --no-pgn-src-merge] [--pgn-dst-merge | --no-pgn-dst-merge]
                   [--dbdump] [--dbc-can<N>=<file>]... [--dbc-lin<N>=<file>]... [-O <output dir>] [-i <files/folders>]
```

| Option | Meaning |
|---|---|
| `--dbc-can<N>=` / `--dbc-lin<N>=` | DBC for CAN/LIN channel N; repeat for several DBCs on one channel (the same ID in several DBCs only with different message names, section 4). Channel 9 = internal GNSS/IMU of CANedge GNSS variants; 11-14 (15-18, ...) = CANmod.router S1-S4 after MUX-TP demux (section 5) |
| (no `--dbc-*`) | Auto-discovery in the working directory: Files matching `^(can\|lin)(\d+)-(\S*)\.dbc$`, e.g. `can1-obd.dbc`, `lin1-truck.dbc`. The data lake automation uses the same prefix rule; the CANedge Intro recommends only letters, numbers and dashes in DBC file names |
| `-i` | Input files/folders (must have a common root); positional paths work too (also drag-and-drop on Windows) |
| `-O` | Output directory (must exist). `-X`: no `_out` suffix on the output root for folder input |
| `-p` | Password file for MFE/MFM (default `passwords.json` in the working directory) |
| `--concat` (default) / `--no-concat` | Merge consecutive output files of one rule directory |
| `--no-pgn-src-merge` (default) / `--pgn-src-merge` | J1939: One output per source address (`_S<SA>`), or merge all SAs |
| `--pgn-dst-merge` (default) / `--no-pgn-dst-merge` | J1939 PDU1: Merge destination addresses, or split per DA |
| `--verbosity=<n>` | 0-4, default 2; 4 reports details like DLC mismatches |
| `--dbdump` | Dump the parsed DBCs as JSON into the working directory (troubleshooting) |
| `-b` | Buffer size in bytes (0 disables, -1 = whole file) |
| `-d` | Deletes the input files after conversion - never use it on the user's log files |

The run summary includes transport-protocol counts (succeeded / failed / unfinished).

## 3. DBC support

- **ID format** from the `BusType` / `ProtocolType` attributes: LIN (`BusType` LIN), CAN (`CAN`/`CAN FD`), PGN
  (`CAN`/`CAN FD` + `ProtocolType` `J1939`). `BusType` undefined or empty = CAN. With PGN format, ALL extended-ID
  messages are matched as PGNs (`VFrameFormat` is not used)
- **Keywords used**: `BO_`, `SG_`, `BA_`, `BA_DEF_`, `BA_DEF_DEF_`, `SG_MUL_VAL_`, `SIG_VALTYPE_`. Invalid or
  unsupported signals are ignored as if not present and parsing continues
- **Multiplexing**: Standard and extended. Extended multiplexing needs ranges (`1-1`, `1-10`); value lists
  (`1;3;5-10`) are not supported
- **Encoding**: Intel/Motorola, signed/unsigned, 32-bit IEEE float and 64-bit IEEE double (`SIG_VALTYPE_`; the
  length must be exactly 32/64 bit)
- **Transport protocol**: `ProtocolType` `J1939` enables J1939-21 TP (PGNs 0xEB00/0xEC00 are treated as transport
  frames). `TransportProtocolType` selects the others: `ISOTP`, `NMEATP` (NMEA 2000 fast packet),
  `MUXTP#<S1>:<S2>:<S3>:<S4>` (CANmod.router)
- **Selective decoding**: `MessageIgnore` (1 = ignore) and `SignalIgnore` exclude messages/signals without
  deleting them (smaller output)
- **Min/max**: Decoded values outside a signal's DBC min/max are written as NULL. Arbitrary min/max values in a DBC
  silently remove valid data

## 4. Common reasons a signal does not decode (troubleshooting guide)

- Wrong or missing channel prefix (auto-discovery / automation) or the wrong `--dbc-canN`: The DBC is never applied
- **Duplicates within one DBC**: The same CAN ID twice in one DBC, or (with `ProtocolType "J1939"`) two 29-bit IDs
  with the same PGN, make the decoder refuse the whole DBC ("Failed to load DBC file"; stricter than asammdf). Fix:
  Keep one definition, or switch the DBC to exact 29-bit matching (`BA_ "ProtocolType" "";`). J1939 = ProtocolType
  only, not VFrameFormat J1939PG
- **The same ID in several DBCs on one channel**: The decoder accepts it and decodes into one folder per message
  name. Different message names = separate folders: fine (e.g. the J1939-73 DM1 one-DBC-per-DTC workaround). The SAME
  message name = one folder: Different signals give corrupt/mixed Parquet although decoding "works" - not allowed.
  `canedge-workspace` `dbc_groups.py check` (and `build_lake.py`) report both cases
- **Signals beyond the frame**: A DBC length longer than the logged frames alone is fine (the signals that fit are
  decoded). Signals that end beyond the frame's data length are not decoded: The decoder drops those signals or the
  whole message (both seen with 25.12.15), and with mixed frame lengths on one ID it can skip some frames. Fix the
  DBC to the real frames; `canedge-dbc` `dbc_coverage.py` lists these messages per channel
- **Variable-length IDs** (one ID sent with several frame lengths, signals beyond the shortest): The decoder decodes
  signals in DBC order up to the first one that does not fit and writes each resulting column set to the SAME file:
  An unreadable Parquet file, or a readable one with only one frame length (seen with 25.12.15). Fix: Split the
  message. One message with the signals that fit the shortest frame (DLC = shortest), plus per longer length a
  message with the same ID, a unique name and only the extra signals, in its own DBC on the same channel (as the
  J1939-73 DM1 workaround; e.g. `can2-x.dbc` + `can2-x-long.dbc`). Without the extra signals: The short message only.
  `dbc_coverage.py` flags these (MIXED LENGTHS), `build_lake.py` stops on them
- Values filtered by min/max (see 3), or the ID is not in the raw data at all (check with canedge-mdf2csv)

## 5. Transport protocol (multi-frame messages)

- ISO-TP (ISO 15765-2), J1939-21 TP, NMEA-TP (IEC 61162-3 fast packet), MUX-TP (CANmod.router); configured via the
  DBC (section 3); supported since decoder 24.10.17
- MUX-TP: The router DBC's tunnel message (default 0x10 on CAN2, `MUXTP#11:12:13:14`) is demuxed into the original
  frames on the listed channels (S1-S4), which are then decoded with the DBCs of those channels (`--dbc-can11=`
  ...)
- Broadcast and one-to-one sessions; flow-control messages are generally ignored; interleaved sessions are supported
- Headers are stripped and the payload reassembled before the decoding rules apply; the message gets the timestamp
  of its final segment
- Incomplete sessions are dropped. Sessions continue across consecutive log files; a transition between
  non-consecutive files aborts all active sessions. A new session on the same message can make the previous one fail

## 6. Output organization

```
DEVICE/ITF_CHN_MESSAGE[_MULTIPLEXOR_M<SWITCH>][SA][DA]/YYYY/MM/DD/SESSION_SPLIT.parquet
e.g. AABBCCDD/CAN1_GnssTime/2020/01/01/00000001_00000001.parquet
     AABBCCDD/CAN1_EEC1_S00/...   (J1939 EEC1 from source address 0x00, default --no-pgn-src-merge)
```

- Partitioned by device, decoding rule (interface + channel + message, multiplexor switch, J1939 SA/DA as per the
  merge options) and record date (one input file can write several days). Nested multiplexing repeats the
  multiplexor block
- All files below one rule directory share one schema: That is what lets Athena/BigQuery/Synapse (and DuckDB) treat
  each `<device>/<message>/` folder as one table
- File names come from the CANedge session/split counters, so output files never collide. A concatenated file is
  named after its first input
- `--concat`: Only consecutive input files (no gaps) are merged; a missing or late file gives several files per day.
  Decoding one input file per call also prevents concatenation (the data lake automation decodes file by file)

## 7. Parquet schema

- `t`: Int64 timestamp (`MICROS`, UTC), snappy compression
- One column per signal: double or NULL, snappy compression; names from the DBC (Athena/Glue lower-cases them)
- Row groups of 1,000,000 records; records where all signal values are NULL are not written

## 8. Performance (docs benchmark, 10 M records, Linux x86_64 Xeon 3.4 GHz)

- 7 s / 309 MB RAM (one input, one output), 8 s / 116 MB (many inputs, one output), 11 s / 1131 MB (one input,
  many outputs). Measured end to end on a laptop via `build_lake.py`: ~12 MB of log files per second
- Linux with many output files: Raise the open-file limit (`ulimit -n`)
- In the cloud automation, the Linux build runs once per uploaded log file (Lambda / Cloud Function / Container App)
  with the DBCs and `passwords.json` from the input bucket; see `canedge-knowledge` for the data lake setup
