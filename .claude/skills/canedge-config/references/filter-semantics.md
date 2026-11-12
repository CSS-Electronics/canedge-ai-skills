# CANedge filters, prescalers, transmit lists and control signals (FW 01.09)

Sources: The 01.09 JSON Schemas in `assets/schema-files/`, the CANedge config editor
(`filterEvaluator.js`, filter builder, warning checks) and the CANedge Intro ("Reduce your log file
size", OBD2, J1939, GNSS/IMU sections). Items marked *derived* follow from these rules but are not
stated in the docs.

## 1. Where things live

| Section | Content |
|---|---|
| `can_internal` | Internal GNSS/IMU messages (CAN9 in the log files). `general`, `filter.id`, `control` (no `phy`, no transmit) |
| `can_1`, `can_2` | `general` (rx_state/tx_state), `phy` (mode, bit-rate), `filter` (remote_frames, id[]), `transmit[]`, `control` |
| `lin_1`, `lin_2` | `phy`, `frames`, `transmit` (no ID filters) |
| `log` | `file` (split_size MB 1-512, split_time_period/offset s, cyclic), `compression.level`, `encryption`, `error_frames` |
| `routing[]` | Max 32 rules, route frames from CAN-internal/CAN1/CAN2/LIN to CAN1/CAN2 |

File name: `config-VV.VV.json` with VV.VV = the firmware major.minor (e.g. `config-01.09.json`). The
device only loads the matching name (after a MAJOR/MINOR firmware update without a matching file it
creates a default config). Keep config variants apart by folder (e.g. `optimized/config-01.09.json`), never by
a renamed file (`config-01.09-optimized.json` is ignored by the device and a common user mistake).

## 2. ID filters (`can_X.filter.id[]`)

| Field | Values |
|---|---|
| `state` | 0 disabled (ignored), 1 enabled |
| `type` | 0 acceptance, 1 rejection |
| `id_format` | 0 = 11-bit, 1 = 29-bit. A filter only applies to frames of the same ID format |
| `method` | 0 range (`f1` <= ID <= `f2`), 1 mask (`f1 & f2 == ID & f2`, f1 = ID, f2 = mask) |
| `f1`, `f2` | Hex strings without 0x |
| `prescaler_type` | Acceptance filters only: 0 none, 1 count, 2 time, 3 data |
| `prescaler_value` | count 1-256 / time 1-4194304 ms / data: hex byte mask (string, max 16 hex chars) |
| `name` | Optional, max 16 printable ASCII chars |

Evaluation (per channel, per frame):
1. Remote (RTR) frames are discarded before the filters if `filter.remote_frames` = 0
2. Enabled filters are checked top to bottom; **the first match decides** (accept or reject)
3. **No match = not logged.** A list with only disabled filters logs nothing on that channel
4. Limits: Max 128 11-bit and 64 29-bit filters per channel (schema max 192 entries)

Consequences:
- A broad filter placed above a specific one shadows it (the specific prescaler is never used).
  `check_config.py` flags this. Put specific filters (prescale, reject) **at the top**, the
  catch-all accept (e.g. range 0-1FFFFFFF) **at the bottom** (the filter builder's "append to top")
- "Record only these IDs" = acceptance filters without a catch-all. Anything not listed is lost,
  including IDs nobody has looked at yet: List the dropped IDs for the user (with DBC files, dropping
  undecodable IDs is the default proposal, unless the user wants to keep them)
- The default config accepts everything: `AllStandardID` range 0-7FF (11-bit) and
  `AllExtendedID` range 0-1FFFFFFF (29-bit); some example configs use mask f1 7FF / f2 0 instead

## 3. Prescalers (acceptance filters only)

Prescaler state is kept **per ID**, so one filter matching many IDs prescales each ID separately.
The device tracks prescaler state for **max 100 unique IDs per CAN channel (15 for CAN-internal)**: The first
IDs accepted by a filter with prescaling; IDs beyond that are logged
unprescaled (`simulate_config.py` counts this from measured data).

| Type | Behaviour | JSON |
|---|---|---|
| Count (1) | Logs the 1st, then every Nth frame of the ID | `"prescaler_type": 1, "prescaler_value": 10` |
| Time (2) | Logs a frame if >= value ms passed since the last logged frame of the ID. Not resampled to a fixed period | `"prescaler_type": 2, "prescaler_value": 1000` |
| Data (3) | Logs a frame if any masked data byte changed vs the last logged frame. Mask bit i = byte i (FF = first 8 bytes, 03 = bytes 0-1, empty = any byte) | `"prescaler_type": 3, "prescaler_value": "FF"` |

- With a time value equal to a multiple of the period (100 ms message, 1000 ms prescaler), timing jitter
  makes the interval alternate (1000/1100 ms). Use a value slightly below the multiple (e.g. 95 ms for a
  steady 10 Hz). Measured on a real 10 ms ID: 90-98 ms gives 100 ms intervals (9.5 Hz), 100 ms gives
  ~9.0 Hz with a 108 ms median interval
- Time vs count: Count (1 of N) gives the same result on strictly periodic IDs, but follows any change of
  the source rate and needs N per ID. Time sets the output rate, so one value (e.g. 95 ms = max ~10 Hz)
  works for IDs with different periods under one range/mask filter. Prefer time when the user wants a
  lower rate. Both remove real samples: Only after the user confirms (a data prescaler on all bytes keeps
  every value change and only drops repeats)
- Data prescalers suit status/static messages. Avoid them on messages with counters or checksums
  (every frame changes) unless the mask excludes those bytes. Docs warning: Data prescaling assumes a
  constant payload length per ID (avoid it on IDs with varying DLC, see the per-ID DLC in the raw stats)
- The first frame of an ID is always logged, whatever the prescaler (Docs)
- Data masks are byte level (Docs): Bit i = data byte i, the rightmost bit = byte 0, up to 16 hex digits (64 bytes,
  CAN FD). `0B` = bytes 0, 1 and 3 (not bits), `0300` = bytes 8-9, `8000000000000000` = byte 63; empty = all bytes;
  bytes beyond the frame are ignored. A single bit cannot be watched: Every signal in a masked byte triggers a
  logged frame, so "log only when signal Y changes" saves little if a counter, checksum or noisy analog value
  shares Y's bytes (`data_mask.py` lists them; measured: a CANmod.input analog input changes in 99% of frames)
- Data prescaler on a signal you analyze with time aggregation: Values are only logged on change,
  so "last value holds" logic is needed in the analysis

Example (CANedge Intro): Only 70B, 72A, 18FFA43D; 70B every 500 ms, 72A every 2nd frame:
```json
"id": [
  {"name": "70B 500ms", "state": 1, "type": 0, "id_format": 0, "method": 0, "f1": "70B", "f2": "70B", "prescaler_type": 2, "prescaler_value": 500},
  {"name": "72A 1of2",  "state": 1, "type": 0, "id_format": 0, "method": 0, "f1": "72A", "f2": "72A", "prescaler_type": 1, "prescaler_value": 2},
  {"name": "18FFA43D",  "state": 1, "type": 0, "id_format": 1, "method": 0, "f1": "18FFA43D", "f2": "18FFA43D", "prescaler_type": 0}
]
```

## 4. J1939 PGN filters (mask method, as built by the editor's filter builder)

- PDU2 (PF >= F0): `f1` = PGN << 8, `f2` = `3FFFF00` (any source address, any priority).
  EEC1 (PGN F004): `{"id_format": 1, "method": 1, "f1": "F00400", "f2": "3FFFF00"}`
- PDU1 (PF < F0): `f1` = (PGN & 3FF00) << 8, `f2` = `3FF0000` (any destination + source).
  TP.CM (EC00): `"f1": "EC0000", "f2": "3FF0000"`
- All proprietary B (FF00-FFFF): `"f1": "FF0000", "f2": "3FF0000"`
- Keep TP.CM (EC00) and TP.DT (EB00) if multi-frame data matters (e.g. DM1 with >1 DTC, VIN),
  and DM1 (FECA) for fault codes. They are rarely in the user's signal list but cheap (low rate)

### Transport protocols (multi-frame messages)

The decoder reassembles multi-frame messages from **all** their segments (incomplete sessions are dropped), so
never reject or prescale these IDs unless the user explicitly wants to lose that data. A data prescaler breaks
them too (a repeated segment would be skipped):
- **J1939 TP** (also NMEA 2000 / ISOBUS networks): TP.CM `EC00`, TP.DT `EB00`, ETP.CM `C800`, ETP.DT `C700`.
  PDU1 PGNs: Keep them with mask filters `EC0000` / `EB0000` and mask `3FF0000` (any source + destination).
  They carry e.g. DM1 with 2+ DTCs, VIN, software IDs
- **ISO-TP** (UDS, OBD2 multi-frame): The response ID carries all segments (first + consecutive frames); keep the
  request and flow control frames too if they are logged. DBC: `TransportProtocolType` `ISOTP` on the message
- **NMEA 2000 Fast Packet**: The PGN's own ID carries all segments (sequence counter in byte 0). DBC:
  `TransportProtocolType` `NMEATP` on the message (CSS NMEA 2000 DBC)
- `simulate_config.py` tags these IDs "TP" (J1939 TP always; ISO-TP / Fast Packet from the DBC with `--project`)
  and warns if the config rejects or prescales them

## 5. Transmit list (`can_X.transmit[]`)

Fields: `name`, `state`, `id_format`, `frame_format` (0 CAN, 1 FD, 2 RTR), `brs` (FD only), `log`
(1 = include the transmitted frame in the log), `period` ms (multiple of 10, 0 = single shot),
`delay` ms (multiple of 10, < period if periodic), `id` (hex), `data` (hex, max 8 bytes CAN /
64 bytes FD).

- Requires `phy.mode` = 0 (Normal). Restricted/Monitoring never transmit
- Max 224 messages and 4096 data bytes across CAN1 + CAN2; period <= delay is rejected
- OBD2 request example: `{"name": "Vehicle_Speed", "state": 1, "id_format": 0, "frame_format": 0, "brs": 0, "log": 0, "period": 5000, "delay": 1500, "id": "7DF", "data": "02010D5555555555"}`

Period/delay recipe (the config editor's OBD tool; use it for any request list): Requests take turns,
one every `spacing` ms (>= 250 ms recommended; faster may give response errors):
- N requests: `period` = N x spacing for all; `delay` = 0, spacing, 2 x spacing, ... (request i at i x spacing)
- Multi-frame responses (ISO-TP: VIN, DTCs, UDS): Add a Flow Control frame 50 ms after the request (same
  period, delay + 50; data `3000000000000000`) to the physical ID: 7E0 (11-bit) / 18DA00F1 (29-bit), and
  send the request itself to that physical ID too
- Each PID is then updated every `period` ms: Many PIDs = slow updates (e.g. 20 PIDs x 300 ms = 6 s)

Request patterns:
- OBD2 (service 01): ID 7DF (11-bit, cars) or 18DB33F1 (29-bit, trucks/buses), data `02 01 <PID> 55 55 55 55 55`
- WWH-OBD / OBDonUDS: data `03 22 F4 <PID> 55 55 55 55`
- Stored DTCs (service 03): data `01 03 55 55 55 55 55 55`
- UDS ReadDataByIdentifier: Single Frame `03 22 <DID hi> <DID lo> 55..` to the ECU's request ID + Flow Control
  (see the Intro's UDS section and EV partial configs)
- J1939 request (PGN EA00): 29-bit ID `18EA<DA><SA>`, 3 data bytes = requested PGN, least significant byte
  first (e.g. `E5FE00` requests PGN FEE5)
- Responses: Keep them in the filters. The OBD tool's response filter: 11-bit range 7E8-7E8 (engine ECU
  only; 7E8-7EF covers all ECUs) or 29-bit mask f1 `18DAF100` / f2 `3FF0000` (any ECU)
- Transmitting while the vehicle is off can wake ECUs and drain the battery: Gate transmission with
  a control signal (`control_tx_state`), e.g. GPS speed

## 6. Control signal (`can_X.control`)

Fields: `control_rx_state` (1 = the signal controls reception = logging), `control_tx_state`
(1 = controls the transmit list), `start` and `stop`, each with `message` (`chn` 0 internal / 1 / 2,
`id_format`, `id`, `id_mask`), `signal` (`type` 0 unsigned, `byteorder` 0 Motorola / 1 Intel,
`bitpos`, `length`, `factor`, `offset`) and `trigger_low` / `trigger_high` (physical value).

Device rules (CANedge Docs "Control signal" + "Signal interpretation"; `simulate_config.py` follows them):
- One control section per channel: To gate several channels, add the same control to each section
- Start/stop message: `id` + `id_mask` match like a mask filter, same ID format (J1939: mask out the
  source address, e.g. `3FFFF00`). Start and stop may use different messages, from any channel (`chn`)
- Only frames **accepted by the source channel's filters + prescalers** count (`check_config.py` flags a
  control message the filters reject; a prescaler on it delays the trigger)
- Signal = DBC convention: Copy bitpos, length, factor, offset from the DBC `SG_` line; byte order
  `@1` = Intel (1), `@0` = Motorola (0). **Unsigned only** (`+`). Value = raw x factor + offset. A signed
  signal (`-`, e.g. a current) read unsigned: |value| >= a is one range (raw a .. 2^n - a, x factor); stop on the
  near-zero band of one side (raw 0 .. a-1), the small negative band then holds the state
- Ranges include low and high. Non-overlapping: Each evaluated on its own (on when the value enters the
  start range, off when it enters the stop range; a gap between them = hysteresis). Choosing thresholds: Get all
  relevant data. Clean integer signal (e.g. speed in km/h from an ECU): Start 1 to max, stop 0 to 0. Jittery
  signal (e.g. GNSS speed): A small gap (start >= 0.2 m/s, stop <= 0.19 m/s). Short on-periods in the
  simulation = flicker: Harmless for logging (no file splits), so prefer more data; confirm with the user. Start range inside the
  stop range: start wins (on only inside the start range). Stop range inside the start range: stop wins.
  Partial overlap: Not defined, avoid
- `general.rx_state` / `tx_state` are the **initial** states at power-up. With a control signal you
  normally set the initial rx_state to 0 (off until the start trigger). No file split on state changes;
  the state carries over to the next split of the session
- An all-zero signal (length 0 or factor 0) is a constant and never triggers as intended (editor warning)

J1939 engine speed (CANedge Intro, `j1939-engine-speed-control-signal-can1.json`): Log CAN1 only
while EEC1 engine speed >= 200 rpm:
```json
"can_1": {"general": {"rx_state": 0, "tx_state": 1},
  "control": {"control_rx_state": 1, "control_tx_state": 0,
    "start": {"message": {"chn": 1, "id_format": 1, "id": "F00400", "id_mask": "3FFFF00"},
              "signal": {"type": 0, "byteorder": 1, "bitpos": 24, "length": 16, "factor": 0.125, "offset": 0},
              "trigger_high": 9999, "trigger_low": 200},
    "stop":  {"message": {"chn": 1, "id_format": 1, "id": "F00400", "id_mask": "3FFFF00"},
              "signal": {"type": 0, "byteorder": 1, "bitpos": 24, "length": 16, "factor": 0.125, "offset": 0},
              "trigger_high": 199, "trigger_low": 0}}}
```
GNSS speed (`assets/control-signal-internal-gps-01.09.json`, from the config editor): `chn` 0, ID `6B`,
mask `7FF`, Intel, bitpos 1, length 20, factor 0.001 (m/s); start 0.2-1000, stop 0-0.19; gates logging on
all channels and the transmit lists on CAN1/CAN2.

Use case reasoning: A control signal removes everything outside the trigger window. Example: "Log
only while driving" loses faults that occur while parked and charging (EV), during pre-heating, or
in the seconds before the engine starts. Ask what events matter before proposing one.

## 7. Other size levers

- Compression (`log.compression.level` 256/512/1024): 50-70% smaller, files become `.MFC` (the CSS
  converters and decoders read them; asammdf needs `mdf2mdf` first). **Recommend it in every optimized config**:
  1024 B window, 256 B for very high rates (3+ MB/min, where a large window can slow down e.g. uploads)
- Error frames (`log.error_frames.state` 1) can add many frames on a faulty bus
- CAN-internal messages (`can_internal` filters, CAN9 in the log files) are recorded whenever the logger is
  powered, also while the vehicle is parked: E.g. TimeExternal (`0x005`, 1 Hz; off in the default config, meant
  for checking the time sync) or GNSS/IMU. They add data and keep the raw overview's bus activity "on" (endless
  trips). Disable the ones not needed, or gate CAN9 with a control signal
- Cyclic logging (`log.file.cyclic` 1) deletes the oldest files when the SD is full: The SD then
  holds the last N days instead of stopping
- Split size: Small splits (e.g. 5 MB) for CANedge2/3 with intermittent connectivity (no resumable
  uploads); split time < 60 s adds overhead

## 8. Size estimate model (`simulate_config.py`)

- Exact, not modelled: Every frame of the log file runs through the filters and the per-ID prescaler state,
  ported from the config editor's filter builder (`filterEvaluator.js`). Prescaler state restarts per file
- Size: Frames weighted by data length (the editor's MF4 weights: <= 8 B 1.0, 12 B 1.75, 16 B 2.12, 64 B
  4.16; workspace `tools.frame_weight`), scaled so the recorded total equals the real file size. Source files
  that are compressed (MFC) give compressed sizes
- Rates per recorded time: MB/min (the filter builder's unit) and MB/h, MB = 1e6 bytes (the editor divides
  by 1024^2: Its MB/min reads ~5% lower). Multiply by the expected hours of use yourself (SD card days,
  monthly upload volume)
- Compression in the new config: Shown as 50% smaller (the editor's figure; the real ratio depends on the data)
- Why not estimate from aggregated stats: On a 28 min EV log, an average-period model overestimated time
  prescalers by 68% (IDs only on the bus part of the time) and a unique-payload model missed IDs whose 2
  payloads toggle on every frame (a data prescaler keeps 100% of them). The per-ID `changes` % (frames with
  a new payload) shows the data prescaler effect directly
- Frames the device transmits itself (transmit list with `log` 1) are treated like received frames
- Only IDs present in the log file are evaluated; data recorded with filters hides the blocked IDs
- Explore tab (`--html`): The same prescaler rules in the page (JS port, checked against `prescale()` per ID on load),
  on the whole log file or the busiest window that fits `--explore-mb`; prescalers start at the window start.
  Signals decoded with cantools (a preview, not the MF4 decoder; multi-frame TP messages show raw bytes). A control
  signal removes the frames in its logging-off periods after the prescaler, as in the simulation (shown grey, can be
  switched off)

## 9. Deployment

CANedge1: Copy the file to the SD card root as `config-VV.VV.json`. CANedge2/CANedge3: Upload to the
device folder on S3 (`<device ID>/config-VV.VV.json`) for an OTA update, via CANcloud, or across a
fleet with the OTA batch manager (`canedge_manager`). The device reports the loaded config CRC32 (also in the MF4 header), so you
can confirm which config produced which log files.

## 10. CANmod.router (S1-S4 filters, CANedge + router)

Source: CANmod.router Docs ("Filter", "Message Prescaling"), schema 01.02 (`assets/schema-files/CANmod.router/`),
CANedge Intro ("CANmod.router"), the CANmod config editor's filter builder.

- **Setup**: The router on CANedge CAN2 sends S1-S4 as CAN FD tunnel frames on one ID (`phy.can.route.mux.s2p.message.id`,
  default `10` = 0x10, FD + BRS; FW 01.01: `mux.output.message`). A 2nd (daisy-chained) router uses another ID
  (0x12 by convention; never the p2s ID 0x11). After demux: 0x10 -> CAN11-14, 0x12 -> CAN15-18 (S1-S4)
- **Filters** `phy.can_s1..can_s4.filter[]` (1-32 per channel): `{name (<= 16), state, id_format (0 11-bit / 1
  29-bit), frame_format (0 standard / 1 FD / 2 both; 01.02+), f1 (ID hex), f2 (mask hex), prescaler_type,
  prescaler_value}`. No `type` (accept only) and no `method` (mask only): `(f1 & f2) == (id & f2)`. Top to bottom,
  the first match decides (and its prescaler), no match = rejected, no enabled filter = everything rejected.
  Accept all: f1 `7FF` mask `0` and f1 `1FFFFFFF` mask `0` (the defaults, `assets/canmod-router-default-filters-01.02.json`)
- **Dropping IDs** = keep-list: List the IDs to keep (exact masks `7FF` / `1FFFFFFF`, J1939 PGN masks as section 4,
  neighbouring IDs with one mask), nothing else. Or keep accept-all at the bottom and put prescaled IDs above it.
  `scripts/router_filters.py` writes these (32-limit and duplicates checked)
- **Prescalers**: As on the CANedge (count 1-256, time 1-4194304 ms, data mask up to 16 hex digits = 64 bytes; the
  first frame of an ID is always accepted), max 100 prescaled IDs per channel (first seen)
- **CANedge side**: CAN2 at the router's primary bit-rate (default 1M / FD 4M, not auto-detect), Normal or
  Restricted mode, and the tunnel ID accepted **without** prescaler (like a transport protocol: every segment is
  needed). Filter and prescale per ID on the router, never the tunnel on the CANedge. CANedge control signals see
  only physical CAN1/CAN2 frames (the tunnel), not S1-S4 signals
- **Bandwidth**: Keep the total of all router channels below ~6000 frames/s after filters (log size, upload, file
  rollover)
