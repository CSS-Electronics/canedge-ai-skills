---
name: canedge-troubleshoot
description: >
  Troubleshoot a CANedge that does not record, records no useful data, does not upload to S3, did not apply a new
  Configuration File, shows wrong timestamps, or whose signals do not decode: Collect the evidence, run the checks
  per symptom with the other canedge-* skills and report the most likely root cause + fix. Includes a device.json
  check (SD card, firmware, config CRC32, SIM, TLS certificates). Use for "my device is not logging / not
  uploading", "why is there no data", "did my device load the new config", "what does this device.json tell us".
---

# canedge-troubleshoot

A workflow, not a tool: It decides which evidence a symptom needs and runs the checks of the other skills in a
sensible order. Shared rules: [canedge-workspace conventions](../canedge-workspace/references/conventions.md)
(data safety, credentials, UTC). Device facts (LEDs, fields, limits): `canedge-knowledge`.

## Script

`scripts/check_device_json.py <device.json> [--config config-VV.VV.json] [--expected-id ID] [--json]` (run with
`.venv\Scripts\python.exe` from the repo root): Device ID, firmware, the CRC32 of the loaded config vs `--config`,
SD card (a CSS card or not), SD wear + space, GNSS/cellular module firmware (blank = failed module update), SIM
(blank ICCID = SIM not readable), TLS certificates vs an https endpoint, last reset cause. Exit 1 = warnings. ICCID
and IMEI are masked. device.json is in the SD card root and (CANedge2/3) in the S3 device folder: canedge-s3
`s3_object.py get <device>/device.json` (+ `<device>/config-VV.VV.json`).

## Workflow

1. **Symptom + evidence**: Ask what the user expected and what they see, then collect only what the symptom below
   needs, in one question: Device type + firmware, the Configuration File, device.json, one recent log file (path),
   bucket access (data lake profile, `--env`). Say where each comes from (SD root, S3 device folder, CANcloud)
2. **Run the checks** for the symptom, cheapest first. Stop when one explains the symptom, then confirm it
3. **Report**: Per check the evidence (value + source) and the verdict, the most likely root cause first, then the
   fix. Config changes go through canedge-config (check-edit-check loop), never by hand. Log the finding in the
   project's `project.md` if there is a project

| Symptom | Checks (skill: script) |
|---|---|
| **Not recording** (no new files on SD or S3) | `check_device_json.py` (SD card, firmware, config CRC32) -> canedge-config `check_config.py` on the config (errors = the device rejects it and runs its default config) -> canedge-knowledge: Docs `[LEDs]:`, Intro `[Logging troubleshooting tips]:` (power, wiring, SD card) |
| **Files, but no useful data** (tiny files, silent bus, only CAN9, only `Dir` = 1 frames) | canedge-mdf2csv `csv_summary.py` on a log file (few files) or canedge-raw-overview (many) -> bit-rate in the MF4 header (`mf4_files.py`) vs the bus, `phy.mode` (silent mode cannot ACK), only the logger's own requests = no ECU answers (wiring, termination, bit-rate, ignition), filters that reject everything, a control signal that keeps logging off (canedge-config `simulate_config.py`) |
| **CANmod.router: No / missing router data** (CAN2 = only CAN FD ID 0x010, or no CAN11-14 after demux) | One big FD ID 0x010 on CAN2 = router data that is not demuxed yet (workspace conventions 5b, canedge-mdf2mdf). No tunnel frames at all: `check_config.py <router config> --pair <CANedge config>` (tunnel accepted unprescaled, CAN2 1M/4M not auto-detect, not Monitoring mode, S1-S4 filters enabled, `rx_msg` on); a channel missing after demux = its S-channel bit-rate/mode or filters (router config) |
| **Not uploading** (CANedge2/3) | canedge-s3-overview or `s3_inventory.py --prefix <device>/` (last logged vs last uploaded vs last heartbeat: logs but no uploads = connectivity) -> `check_device_json.py --config` (SIM, TLS certificates, module firmware) -> `check_config.py` warnings (APN, TLS/port, WiFi, S3 endpoint) -> canedge-knowledge Intro `[WiFi/S3 troubleshooting]:` / `[LTE/S3 troubleshooting]:`, `[Cloud server TLS]:` |
| **Config not applied** | CRC32 of the intended file (`check_config.py` prints it) vs `cfg_crc32` in device.json vs the config CRC32 in new log file headers (`mf4_files.py`) vs the S3 overview sync column; file name `config-VV.VV.json` must match the firmware major.minor; `check_config.py` errors = rejected |
| **Signals missing or wrong** | canedge-mdf2parquet step 5 (no message folder = DBC does not match the IDs/channel; NULL = range/DLC), canedge-dbc `dbc_lint.py` + `dbc_groups.py check` (overlaps), the data check in canedge-dbc (lake-query `--catalog` + `--stats`) |
| **Data lake shows nothing new** | canedge-s3 (did files arrive? `s3_inventory.py --upload-start`), then canedge-lake-admin `lake_ops.py diagnose <key>` for a recent file (function result + errors, Parquet output, tables) and `status`; files but no tables = re-run the table mapping (`lake_ops.py start --job map-tables`, on request); decoder errors: Intro `[MF4 decoder troubleshooting]:` |
| **Wrong timestamps** | MF4 header start + first frame (`mf4_files.py`, `csv_summary.py`) vs the S3 `Timestamp` (approximate before FW 01.09.01, see canedge-s3) vs the RTC settings (canedge-knowledge: RTC, GNSS time sync; some users set the RTC to local time) |

## Rules

- Evidence before conclusions: Name the value and where it came from; say "likely" until a check confirms it
- Change one thing at a time, and check its effect (new log file header, device.json, S3 upload) before the next
- Never edit or deploy a Configuration File outside canedge-config's check loop; never print keys or passwords
- device.json holds the SIM ICCID and IMEI: Show them masked (the script does)
- Raw log files stay read-only (canedge-workspace rules)

## Related skills

- `canedge-config` - config checks, edits, simulation; `canedge-knowledge` - LEDs, fields, troubleshooting pages
- `canedge-s3`, `canedge-s3-overview` - uploads, heartbeats, device.json + configs in the bucket
- `canedge-mdf2csv`, `canedge-raw-overview` - what is on the bus; `canedge-workspace` - `mf4_files.py` (headers)
- `canedge-mdf2parquet`, `canedge-dbc`, `canedge-lake-query` - decoding problems
