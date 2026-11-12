# Lake details: How build_lake.py runs the decoder, manifest

Decoder behavior itself (DBC rules, NULLs, transport protocol, output layout, troubleshooting):
[decoder-technical.md](decoder-technical.md).

## Decoder call (mdf2parquet_decode 25.12.15)

- `build_lake.py` runs: `mdf2parquet_decode --non-interactive --verbosity=2 -X --no-concat -O <lake>
  [--password-file=<abs>] --dbc-can1=<abs> ... -i <files>` with an empty working directory (the decoder would
  otherwise pick up DBC files / `passwords.json` from its cwd). Never `-d`
- The decoder writes straight into the lake: `<lake>/<device>/<CANx_Message>/<yyyy>/<mm>/<dd>/<session>_<split>.parquet`,
  `<device>` = the MF4 header serial. Existing files with the same name are overwritten, so re-decoding a file is
  safe. `build_lake.py` uses the device folder of the input (`<device>/<session>/<split>.MF4`) only to pick the
  DBCs; if it differs from the header serial, the output lands under the header serial
- Several `-i` files must share one parent folder ("No common root", exit 132), so batches are per folder. A bad
  file fails the whole batch (exit 8; the decoder still writes the other files). `build_lake.py` then stops, marks
  the batch's files `error` and prints the decoder's error lines (`E`/`F` log level, they name the file). The user
  decides: Fix and re-run (the batch is decoded again and overwrites its outputs), or `--exclude <file>`
- `--no-concat` keeps one output file per input file, like the cloud automation, so file counts match the S3 data
  lake
- DBC channel mapping: `build_lake.py` applies the prefix rule (`can1-`/`can2-`/`can9-`/`lin1-`/`lin2-`, and
  `can11-`, `can12-`, ... for CANmod.router channels; no prefix = CAN1 + CAN2, like the automation) and passes
  explicit `--dbc-canX=` arguments; `--dbc-canX` itself works with any file name
- CANmod.router: The router DBC on CAN2 (`TransportProtocolType "MUXTP#11:12:13:14"` on message 0x10) makes the
  decoder demux the tunnel frames into CAN11-14 before decoding them with the `--dbc-can11=` ... DBCs (verified with
  decoder 25.12.15: folders `CAN11_<Message>`). The tunnel message itself has no signals (no folder)
- Throughput: ~12-15 MB/s of log files per decoder process (J1939 sample: 131 MB in ~11 s). The decoder uses one
  CPU core, so `build_lake.py` runs one decoder per device in parallel (`--workers`, default `min(devices, 4, CPUs/2)`;
  `--workers 1` = one after another). Measured on 3 EV devices, 451 MB, 16 logical CPUs: 33.7 s -> 14.3 s, identical
  Parquet files, rows and manifest. RAM: Up to ~1 GB per decoder (decoder-technical.md section 8)
- Parallel only per device: Each device writes only `<lake>/<device>/`, its batches run one after another, the
  manifest is updated under a lock. Files outside the CANedge layout (device `UNKNOWN`, may be the same device as a
  folder) switch the run to one device at a time. A decoder error lets running batches finish but starts no new
  ones; all failures are printed (JSON: `stopped` + `also_stopped`). The sample decodes and the size guard estimate
  stay sequential (conservative)

## Encrypted log files (MFE/MFM)

- Password file: `--passwords <path>`, or in project mode `projects/<name>/passwords.json` if present (projects/
  is gitignored). Format: `{"default": "<password>", "AABBCCDD": "<device password>"}`; device keys win
- Without a password file, `build_lake.py` stops before decoding (exit 3) and lists the encrypted files. The
  passwords are never printed. A wrong password shows up as a decoder error for that file (status `error`)

## Manifest (`<lake>/_manifest.json`)

```json
{"schema": "canedge-parquet-manifest/2", "decoder": "25.12.15.a87dc0b4",
 "devices": {"A1B2C3D4": {"dbc_fingerprint": "e3b0...", "dbc_note": "dbc-groups.json group 0",
             "dbc_files": {"CAN1": ["can1-j1939.dbc"]}, "decoder": "25.12.15.a87dc0b4",
             "meta": "Truck 1"}},
 "files": {"C:/.../A1B2C3D4/00000001/00000001.MF4": {"size": 5242880, "mtime": 1729000000.0, "device": "A1B2C3D4",
           "dbc_fingerprint": "e3b0...", "status": "ok", "decoded_utc": "..."}}}
```

- A file is decoded again when it is new, its size/mtime changed, or its last status was `error` (`returncode`
  + decoder `output` are stored). Files with status `error` can still have output in the lake (the other files of
  a failed batch, or a partly readable file)
- DBC fingerprint = sha256 over channel + file name + content of the device's DBCs. A changed fingerprint
  flags the device (exit code 3); `--rebuild --device X` deletes `<lake>/<X>/` and decodes all its files again
- Rows, time span and messages per device: canedge-lake-query (`lake_schema.py`), not the manifest
- The manifest holds the decoder version and the DBC files per device: Quote them in a report's provenance
