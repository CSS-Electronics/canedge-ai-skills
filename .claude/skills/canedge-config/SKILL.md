---
name: canedge-config
description: >
  Explain, check, troubleshoot and edit CANedge and CANmod.router Configuration Files (config-VV.VV.json): Any
  setting, plus guided workflows for filters + prescalers (file size), transmit lists (OBD2/UDS/J1939 requests),
  control signals (start/stop logging) and CANmod.router S1-S4 filters (separate router report). Validates against
  the device JSON Schema + the config editor checks and simulates a config on real log data. Use for "check my
  config", "optimize my config / reduce file size", "optimize my CANmod.router filters", "only log while driving",
  "create a transmit list", "why does my device reject this config".
---

# canedge-config

Helps users with anything in the Configuration File: Explain settings, find mistakes, and write
ready-to-deploy configs. Filters, transmit lists and control signals get dedicated workflows below because
users find them hardest; for every other setting, use the field semantics in `canedge-knowledge` (CANedge
Docs: Every config field, default and limit) and the same check-edit-check loop. Complements the CANedge
config editor (and its filter builder, whose filter logic `simulate_config.py` mirrors). Log files are
converted with the `canedge-mdf2csv` converter; DBC matching uses the DBC groups of `canedge-workspace` (`dbc_groups.py`).

Conventions (paths, data safety, venv): [canedge-workspace/references/conventions.md](../canedge-workspace/references/conventions.md).
Filter/prescaler semantics, J1939 PGN masks, transmit + control signal JSON snippets, other size levers:
[references/filter-semantics.md](references/filter-semantics.md). Every check `check_config.py` runs, by ID, with
the context to give the user: [references/warning-checks.md](references/warning-checks.md).

## Scripts

Run from the repo root with `.venv\Scripts\python.exe .claude/skills/canedge-config/scripts/<script>` (`--help`, `--json`).

| Script | Use it for |
|---|---|
| `check_config.py <config.json> [--pair <other config>] [--project <p>]` | Summary (log settings, filter tables, transmit lists, control signals; secrets masked) + all validation checks (see Validation below). Exit 1 = do not deploy. CANmod.router configs too (schema + `router_*` checks); `--pair` = the other config of a CANedge + router setup (tunnel accepted unprescaled, bit-rates, modes) |
| `simulate_config.py <log file> --config new.json [--baseline old.json] [--project projects/<name>] [--html report.html]` | Exact frame-by-frame simulation of a config on a log file: Filters + prescalers (same logic as the config editor's filter builder) and control signals (device rules, signal decoded the DBC way). MB/min + frames/s before/after, reduction %, per ID: share, period, data changes, decision; control: on-time + switches per channel. `--project`: DBC match per ID + signal names. `--html`: One self-contained page for the user (size impact, KPIs, timeline with FPS before/after, control signal + on/off, CAN ID table, Explore tab, Diff tab vs `--baseline` as in the config editor's Review changes). Explore tab: Pick an ID, see each signal (DBC, else per data byte) as recorded vs as logged under a rule the user changes live (accept, reject, count / time / data prescaler; default = `--config`), then copy the changes into the chat. Whole log file if it fits `--explore-mb` (default 6), else the busiest window; a control signal's logging-off periods are applied + shown grey (can be switched off). `--html` also writes `report.json` next to the page: What the page shows (`page`, without the Explore frames; the ID table is `result.ids`) + the full result incl. the CRC32 of the simulated config, the baseline and the config the log was recorded with (`result`). Answer questions about the page from it |
| `router_filters.py --config <router config> --out <new> --set S1 "103:time=100,pgn:FEF1,dbc:<file>" ...` | CANmod.router S1-S4 filters from a keep list (exact IDs, ID/mask, J1939 PGNs, a DBC's messages, `all`, `none`) with prescalers: Mask-only accept filters, 32-limit, duplicates removed, replace / top / bottom |
| `trigger_candidates.py <log file>` | No DBC: Bytes/bits whose value separates busy from idle bus periods, with ready-to-use control fields |
| `data_mask.py --dbc <file> --message <name or ID> --signals <a,b>` | "Log this message only when signal(s) X change": The bytes the signals cover, the data prescaler mask (bit i = byte i), the other signals in those bytes (they trigger too) and a filter line (J1939: PGN mask). Byte level only, also CAN FD (up to 64 bytes) |

Asset: `assets/control-signal-internal-gps-01.09.json` - the config editor's partial config: Log (and transmit) only
while moving, by GNSS speed (CAN9 `0x6B`, start >= 0.2 m/s, stop <= 0.19 m/s) on all channels.

## Validation

`check_config.py` runs every check automatically. You never go through a checklist by hand. Act on its output:

- **Errors** (exit 1, `Result : DO NOT DEPLOY`): JSON syntax, JSON Schema (device type from the content, firmware
  version from the file name) and device errors (`x [id]`: Rules the schema cannot express, e.g. transmit period <=
  delay, 128/64 filter limits). The device would reject the config and fall back to the default config (a
  CANedge2/3 then goes offline). Fix every one and re-run. Never hand over a file with exit 1
- **Warnings** (`! [id]`, exit 0): The config editor's warnings + this skill's filter/control checks. Tell the user
  about each one in plain words: What it means for their setup and the fix (look the ID up in
  [warning-checks.md](references/warning-checks.md)). Fix it if it contradicts their goal, or keep it once they
  confirm. Never drop one silently
- `NOT VERIFIED` (no bundled schema for that firmware): Say so; do not call the file validated
- `simulate_config.py` adds the traffic-dependent checks (prescaler ID limit, transport protocol IDs, control
  message never seen) at the top of its notes: Report them like warnings

## Workflow

1. Locate the user's config (SD root, S3 device folder, or a file they name). Copy it to
   `projects/<name>/config/original/config-VV.VV.json`; never edit the original. Note in `project.md` where it came from
   (path, CRC32: The page shows the CRC32, so "is this the user's config" is a lookup). Run `check_config.py` on it and
   tell the user about any errors/warnings in their current config. Several devices/configs: Suggest starting
   with one (recommend which and why) and align with the user on it first, so they are not flooded with pages and
   variants. Then decide per device, with the user, whether the same changes fit (other vehicles or applications
   may need another approach). Several at once only if the user asks
2. Ask about the use case: Which signals/events matter, which channels, rates needed, idle/charging phases,
   upload volume or SD constraints. Then go to the job below

**Any other setting** (explain, troubleshoot, change): Look up the field in `canedge-knowledge` (meaning,
default, limits, firmware version), check the current value in the `check_config.py` summary or the JSON,
and explain or edit (step 8). Connectivity problems: Start from the editor warnings (TLS/port, APN, WiFi,
encrypted keys without public key, AWS endpoint syntax) before guessing

**Filters + prescalers (file size)**
3. Pick one log file recorded with **default (accept-all) filters**: One the user names, or suggest one (a
   busy period, e.g. driving, not a parked vehicle; canedge-raw-overview helps to find one across many files).
   It should be several MB: Below 0.5 MB (uncompressed) the ~100 kB MF4 header skews the sizes (flagged).
   Use this test set for every variant of the config. Need more phases (e.g. drive, charge, parked for a control
   signal)? Prefer consecutive splits of one session over files picked from different days; if you must combine
   files, list each with its time and phase in the reply
4. Optional, only if the user has DBC files: Put ALL of them in the project's `dbc/` folder (not a subset picked
   by file name, also when the config is for one device) and pass `--project`. The DBC match decides which IDs
   are dropped, so settle the DBC-to-device mapping BEFORE the first simulation (canedge-workspace conventions
   section 5): Reuse a confirmed `dbc-groups.json` (this or an earlier project); else, if there is more than one
   DBC per channel or device names in the file names, run `dbc_groups.py check --dbc <all DBCs>` (IDs the DBCs
   define differently = the DBCs differ per device) and `propose`, then STOP: End the turn with the proposed
   mapping as a question (plus what you found so far), and save the answer as the project's `dbc-groups.json`.
   Do not simulate on an assumed mapping and ask afterwards; picking the DBCs by file name is assuming it. DBC
   errors (e.g. duplicate IDs) do not block this; they are fixed with canedge-dbc before decoding. J1939 DBCs match by PGN (any source address); J1939 TP frames count
   as decodable when the DBC has multi-frame messages. Without DBCs everything else works the same
5. Per-ID table: `simulate_config.py <log file> --config <current> [--project ...]`. Default proposal (unless
   the user said otherwise):
   - **Transport protocol IDs (tagged TP)**: Never reject or prescale them, also not as "undecodable" or
     "static" (J1939 TP.CM changes rarely, but every segment is needed): J1939 TP.CM/TP.DT/ETP (always tagged)
     and DBC messages with a `TransportProtocolType` (ISO-TP, NMEA 2000 Fast Packet, and the CANmod.router tunnel
     on CAN2 as "CANmod.router link"; tagged with `--project`).
     Only if the user explicitly asks; `simulate_config.py` warns when the config does it
   - **Compression**: Enable it (`log.compression.level` 1024, 256 for 3+ MB/min) unless the user has a reason
     not to (e.g. asammdf without `mdf2mdf`). The page's headline % and log size KPIs include it (~50%, an
     estimate); the measured % (filters, prescalers, control signal) is shown below the headline. Give both
   - **Undecodable IDs** (with DBCs): Reject them in the FIRST draft config (rejection filters), not as a later
     option: Often the largest cut. In the proposal, list how many IDs and their share, and that they may hold
     data nobody has a DBC for yet (e.g. an ID another device's DBC defines); keep them only if the user asks
   - **Static IDs** (`changes` < 5%, the same rule as the raw overview): Data prescaler (mask all bytes: every
     value change is logged, only the repeats go, so you no longer see that an unchanged message is still
     sent). Toggling values show ~100% changes and do not reduce
   - **Other duplicate-heavy IDs** (not static, but a large share with e.g. < 60% changes: fast messages
     that often repeat the same payload): Suggest a data prescaler and offer to show the impact
     (simulate a variant config); it keeps every value change, so it is usually an easy yes
   - **"Log X only when signal Y changes"**: `data_mask.py` gives the mask. The device watches whole bytes, so tell
     the user which other signals share those bytes (each of their changes logs a frame too; a counter, checksum
     or noisy analog value there = little or no saving) and confirm the effect in the simulation and the Explore
     tab ("Watch a signal", changes per signal in the masked bytes)
   - **Time prescalers**: Never by default, since they remove real samples. Suggest them where they would
     help most (short periods, large share) with the resulting rate, and only add them after the user confirms
6. Propose in plain words first (which IDs, which filter/prescaler, why), citing share, period, changes (+ DBC)
7. Write the new config (step 8), then `simulate_config.py <test set> --baseline config/original/... --config
   config/<variant>/... [--project ...] --html projects/<name>/reports/<yyyymmdd>-config-<variant>/report.html` (several devices:
   `config-<device>-<variant>`);
   give the user the reduction % (incl. compression, plus the measured % without it) and the bold **Open report** link (printed `OPEN` line); iterate. Every page: Baseline = the original, same
   test set, so each page shows the total effect (one step's effect = the difference between two pages; say it)
   - Prescaler choices: Point the user to the page's **Explore** tab (each signal as recorded vs as logged). They paste
     back "Config changes from the Explore tab" lines (`<channel> <ID> <message>: <old rule> -> <new rule>`): Apply
     each as a specific filter above the broader ones (data mask = bit i for byte i), run `check_config.py`, re-run
     the simulation and give them the new page

**CANmod.router filters (router report)** - CANedge + CANmod.router setups (CAN2 carries the router's tunnel
frames, 0x10). The router config is a separate file (`config-01.02.json` on the router's CONFIG drive) and gets its
own report; the CANedge config only has to log the tunnel (rules: filter-semantics.md section 10)
1. Copy both configs: `config/router-original/config-01.02.json` (router) and `config/original/config-VV.VV.json`
   (CANedge). `check_config.py <router> --pair <CANedge>` (pairing: tunnel accepted unprescaled, bit-rates, modes).
   Put the router DBC (`canedge-mdf2mdf/assets/can2-canmod-router.dbc`) and the user's router-channel DBCs
   (`can11-`... `can14-`, ask which DBC is on which S channel) in `dbc/` (canedge-workspace conventions, CANmod.router)
2. Log file recorded with the router's default (accept-all) filters, as in step 3 below. Per-ID table of the
   router channels: `simulate_config.py <log> --config config/router-original/config-01.02.json --project <p>`
   (router mode is automatic: Demux with mdf2mdf, CAN11-14 = S1-S4; sizes = the router's share of the CANedge log)
3. Propose per S channel as in step 5 (undecodable IDs out, data prescaler on static IDs, time prescalers only
   after the user confirms). Router filters cannot reject: Keeping IDs = a keep-list. Write it with
   `router_filters.py --config config/router-original/config-01.02.json --out config/router-<variant>/config-01.02.json
   --set S1 "..." ...` (never by hand), then `check_config.py` on it until it exits 0
4. `simulate_config.py <log> --baseline config/router-original/config-01.02.json --config
   config/router-<variant>/config-01.02.json --project <p> --html <p>/reports/<yyyymmdd>-router-<variant>/report.html`
   ("CANmod.router config impact": Same KPIs, Explore and Diff tabs, channels shown as CAN11 (S1)). Report the
   reduction and the total frames/s vs the ~6000 frames/s guideline (warned in the notes)
5. Deployment: The router reads `config-VV.VV.json` (its firmware version, e.g. 01.02) from its CONFIG drive (USB);
   an invalid file stops the router (CANmod.router Docs). Changing the tunnel ID needs a matching router DBC
   (`muxtp.py dbc`) for demux and decoding

**Transmit lists**: Build the list by hand from filter-semantics.md section 5 (period/delay recipe of the
editor's OBD tool, OBD2 / UDS / J1939 request patterns) and `canedge-knowledge` (PIDs, UDS DIDs). Needs `phy.mode` 0 (Normal); keep the response IDs in the filters; `check_config.py`
catches period <= delay and the 224 / 4096 limits. Warn about waking ECUs / battery drain with the ignition
off and offer a control signal (`control_tx_state`) to gate it

**Control signals**
- Clarify with the user first: What to cut (parked, charging, idle, outside an event) and what that loses
  (faults while parked or charging, pre-heating); source (CAN9 GNSS, CAN1, CAN2); which channels it gates
  (one control section per channel); logging, transmit list or both; start/stop thresholds (see below) and the
  initial state (`rx_state` 0 = off until the first start)
- **Only if it pays off:** A control signal adds risk (data missed around start/stop, e.g. a fault frame just
  before the stop) and a setting users rarely revisit. Propose one only if the simulation shows a large saving
  (not a few %) on data that contains the phases it cuts (parked, charging, idle: A driving-only file shows almost
  none), or if it solves a concrete problem the user has (battery drain from a transmit list, endless trips because
  the bus stays awake, upload volume while parked). Otherwise tell the user it is not worth it and why
- **Is the off-time waste?** Before proposing a gate, check in the data what it would drop: Fault/DTC/status
  signals that change while off, and events within seconds after a stop (there is no stop delay: The stop can
  fire just before e.g. a fault frame). Show it and ask the user whether that time is waste (static, no activity)
  or possibly relevant. Present the gate as its own decision, never as a quick add-on to the lossless steps
- Pick the trigger:
  - **Default: GNSS speed** (GNSS variants): Merge `assets/control-signal-internal-gps-01.09.json`, adjust
    thresholds/channels; `can_internal` must accept `0x6B`
  - **Signal named by the user, DBC given**: Copy the SG_ fields (bitpos, length, byte order, factor, offset)
    into the control signal; message `id` + `id_mask` (J1939: mask out the source address). Unsigned only
  - **DBC given, signal unknown**: Decode candidate signals with canedge-mdf2parquet, look at them with the
    user via canedge-lake-query (e.g. speed, gear, ignition, charge state) and pick one
  - **No DBC**: `trigger_candidates.py <log file>` (file with busy and idle periods); prefer a high score
    with as many switches as busy/idle; confirm the candidate with the user
- Thresholds: Capture all relevant data. A clean integer signal: Start at the first step above idle, stop only at
  idle (e.g. speed start 1 to max, stop 0 to 0). A hysteresis gap only for jittery signals (GNSS speed: start 0.2 m/s)
- `check_config.py` (flags a control message the source channel's filters reject, partial range overlap),
  then `simulate_config.py ... --html`: On-time, switches, short on-periods (< 2 s = flicker) and the timeline.
  Some flicker is harmless (no file splits): Prefer more data if the difference is a few % of on-time. This is a
  key decision: Show the user both variants (tight vs with a gap: on-time + switches) and let them choose.
  Rules: filter-semantics.md section 6

**Encryption + OTA updates**: This skill never writes to S3. Delivering a finished config to devices over the air
(one or many, incl. per-device credentials, encryption and firmware updates): `canedge-ota` (`plan --config
<this config>` keeps each device's connection, credentials and meta). A single device by hand: CANcloud or the SD card

**Write + hand over**
8. Edit a copy as `projects/<name>/config/<variant>/config-VV.VV.json`: One folder per variant, named for
   everything it contains (e.g. `optimized/`, then `optimized-speed-control/`; several devices:
   `config/<device>/<variant>/`), the file always named exactly as the device expects, never
   `config-01.09-optimized.json` (users copy renamed files to the SD card, and the device ignores them). The agent
   edits the JSON directly; J1939 PGN masks and filter JSON in filter-semantics.md. Run `check_config.py` on it
   until it exits 0 (see Validation). Summarize the changes for the user (filters added/removed/moved, settings
   changed) and any remaining warnings
9. Deployment: Copy the file as is (no renaming) to the SD root (CANedge1) or upload it to the S3 device
    folder / CANcloud (CANedge2/3). Confirm via the config CRC32 in new log file headers
    (`check_config.py` prints the CRC32 of the file)

## Rules

- Never overwrite the user's config: Always write a new file, in its own folder, named exactly `config-VV.VV.json`
- Run `check_config.py` after every edit; never hand over a file that fails it (exit 1). Report every warning to
  the user with its impact + fix
- File name `config-VV.VV.json` must match the device firmware major.minor (check the MF4 header
  firmware with canedge-workspace `mf4_files.py`)
- Mind first-match-wins order (specific filters on top, catch-all at the bottom), the 128/64 filter
  limits and the 100 prescaled IDs per channel limit (15 on CAN9)
- Ground every filter/prescaler in the simulated effect on real data (+ DBC match). Never silently drop IDs
  (list what is rejected); never assume a DBC applies to every device or channel
- Count/time prescalers reduce the sample rate of real signals: Only with the user's confirmation
- CAN9 in log files = the `can_internal` section (GNSS/IMU); CAN11-14 (CAN15-18) = CANmod.router S1-S4 after demux
  = the router config's `phy.can_s1..4` sections, never a CANedge section
- Results come from one log file: Say so, and that other driving/idle patterns give other sizes
- Never print secrets (`check_config.py` masks passwords/keys); do not write to S3

## Related skills

- `canedge-mdf2csv` - the converter `simulate_config.py` runs on the log file (required)
- `canedge-mdf2mdf` - CANmod.router demux (router mode of `simulate_config.py`), the router DBC
- `canedge-raw-overview` - pick a representative log file across many (activity, top IDs)
- `canedge-mdf2parquet` - DBC decoding
- `canedge-reports` - shared page CSS, fonts, uPlot, the stacked FPS chart (`assets/stacked-bars.js`) + diff2html
- `canedge-workspace` - conventions, projects, DBC mapping + matching (`dbc_groups.py`, used by
  `simulate_config.py --project`), MF4 header (firmware, config CRC32)
- `canedge-knowledge` - config field details, OBD2/UDS/J1939 setup, GNSS/IMU, OTA updates
- `canedge-dbc` - check or create the DBC files used for DBC matching and control signals
- `canedge-troubleshoot` - device not recording / uploading / applying a config: The workflow + device.json check
