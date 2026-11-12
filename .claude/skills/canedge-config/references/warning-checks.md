# Config checks (check_config.py)

**Every check in this file runs automatically** when you run `check_config.py`: Nothing here has to be checked by
hand. Each finding prints with its check ID in brackets (`x [filter_limits] ...` = error, `! [file_split_value] ...`
= warning). Look the ID up below for the context to give the user.

## Severity: What to do with each result

| Result | Printed as | Exit | What the agent does |
|---|---|---|---|
| JSON syntax error | `JSON syntax : FAILED` | 1 | Fix it. Never hand over the file |
| Schema error | `Schema : N ERROR(S)` | 1 | Fix it. Never hand over the file |
| Device error | `x [id] ...` under `Errors` | 1 | The device rejects the config. Fix it. Never hand over the file |
| Warning | `! [id] ...` under `Warnings` | 0 | The config loads, but something is likely wrong or has a side effect. Tell the user about each one: What it means for them (the "Context + fix" column) and the fix. Fix it if it contradicts their goal, or keep it if they confirm it is intended. Never drop a warning silently |
| Note | `Note: ...` | 0 | Information, e.g. compression is off or the schema check was skipped |

Why errors are critical: A device that rejects its config (schema or device error) replaces it with the **default
configuration** (Memory LED blinks 5 times). That means accept-all filters, no transmit lists and, on a CANedge2/3, no
WiFi/S3 details: The device goes offline and cannot receive a fixed config over the air. Someone has to update the
SD card on site.

The last line sums it up: `Result : DO NOT DEPLOY`, `NOT VERIFIED` (no bundled schema for the version: Say so, do
not call the file validated) or `Valid`. With `--json`: `errors` / `warnings` (each `{check, message}`), `deployable`.

## Device errors

The config editor shows these as warnings, but the device rejects the config. The JSON Schema cannot catch them
(cross-field rules, byte totals, filter counts per ID format), so `check_config.py` treats them as errors.

| ID | Triggers when | Fix |
|---|---|---|
| `transmit_period_delay` | A `can_{1,2}.transmit[]` entry has `period > 0` and `period <= delay` | Make `period` larger than `delay` (filter-semantics.md section 5) |
| `transmit_exceed_limits` | More than 224 transmit messages or 4096 data bytes across CAN1 + CAN2 | Remove or shorten entries |
| `filter_limits` | More than 128 11-bit or 64 29-bit filters on a channel (disabled filters count). The schema allows 192 in total, so it does not catch this | Merge IDs into range/mask filters, remove filters |
| `file_split_offset_period` | `log.file.split_time_offset` > `split_time_period` | Set the offset to at most the period |

## Warnings: Config editor checks

Ported 1:1 from the config editor (`config-editor-base/src/editorBase/actions.js` -> `runConfigurationWarningChecks`),
with the same messages. They run for CANedge configs (the config has a `can_2` section).

| ID | Triggers when | Context + fix for the user |
|---|---|---|
| `transmit_monitoring` | Non-empty `can_{1,2}.transmit` and `phy.mode` is not 0 (Normal) | Restricted/Monitoring mode never transmits, so the requests (e.g. OBD2) get no responses. Fix: `phy.mode` 0. Normal mode also acknowledges frames and signals errors on the bus: Fine for requests, but mention it for buses that should be listened to only |
| `transmit_monitoring_routing` | An enabled route targets CAN1/2, but that channel is not in Normal mode | Nothing is routed onto that channel. Fix: Destination channel `phy.mode` 0 (same caveat as above) |
| `filters_disabled` | No enabled filter on CAN1 or CAN2 | Nothing is recorded on that channel. Fine if the channel is unused on purpose, otherwise enable the filters |
| `config_tls` | S3 endpoint `https://` with port 80 | The device likely cannot connect. Fix: Port 443 (or the server's TLS port) |
| `config_tls_port` | S3 endpoint `http://` with port 443 | The device likely cannot connect. Fix: Port 80, or `https://` for TLS |
| `aws_endpoint` | Endpoint contains `amazonaws` but is not `http(s)://s3.<region>.amazonaws.com` | Typical mistake: The bucket name or a wrong region in the endpoint. Fix: `https://s3.<region>.amazonaws.com`, bucket in its own field |
| `s3_encrypted_no_kpub` | S3 `keyformat` 1 (encrypted), but `general.security.kpub` is empty | The device cannot decrypt the secret key, so it cannot connect. Fix: Add the user public key from the config editor's encryption tool, or use a plain key (`keyformat` 0) |
| `wifi_encrypted_no_kpub` | WiFi `keyformat` 1, but `kpub` is empty | The device cannot decrypt the WiFi password. Fix: As above |
| `rtc_adjustment` | `rtc.adjustment` > 400 s (CANedge2/3) | The field is for drift correction, not time zones (use `rtc.timezone`; the data stays UTC). A large offset can break S3 uploads, as request signing needs the correct time |
| `file_split_value` | `0 < split_time_period < 60` s | Many small files: More storage overhead, slower upload and processing. Fine for a short test; otherwise use e.g. 300-900 s |
| `missing_apn` | S3 endpoint set, cellular APN blank | The CANedge3 likely cannot connect. Fix: Ask the user for their SIM provider's APN |
| `missing_wifi` | S3 endpoint set, no WiFi access points | The CANedge2 cannot connect. Fix: Add the SSID + password |
| `missing_s3_if_wifi` | WiFi access points set, but no S3 endpoint | The device connects but has nowhere to upload. Fix: Add the S3 server details |
| `incorrect_apn_telekom` | APN `internet.v6.telekom` | Not compatible with the device. Fix: `internet.telekom` |
| `incorrect_apn_supersim` | APN `super` with an EU S3 region | Works, but slower. Fix: `de1.super` for optimal speed in the EU |
| `incorrect_apn_spaces` | APN starts/ends with a space | Almost certainly a typo that stops the connection. Fix: Trim it |
| `incorrect_roaming_supersim` | Super SIM APN with `roaming` 0 | A Super SIM connects via roaming, so the device cannot connect. Fix: `roaming` 1 |
| `gnss_estimate` | `gnss.alignment.method` 1 (Estimate) | While estimating, all other GNSS/IMU outputs are off: No position/speed/IMU data. Use it only to find the alignment angles, then enter them with Manual (0). A GNSS speed control signal will never trigger in this mode |
| `control_signal_zero` | Enabled control signal with `factor` 0 or `length` 0 | The signal value is constant, so the control signal never switches. Fix: Copy the signal from the DBC |

## Warnings: This skill's checks

| ID | Triggers when | Context + fix for the user |
|---|---|---|
| `file_name` | No version (`config.json`) or a suffix (`config-01.09-optimized.json`) | The device only loads `config-VV.VV.json` matching its firmware and ignores the file. Fix: Exact name, variants in folders (`optimized/config-01.09.json`). On the user's original file: Just tell them |
| `filter_shadowed` | An enabled filter is fully covered by an earlier enabled filter of the same ID format | It is never reached (first match wins), so e.g. a prescaler or reject filter has no effect. Typical: Catch-all accept above specific filters. Fix: Move the specific filters to the top |
| `filter_empty_range` | Range filter with f1 > f2 | Matches nothing. Fix: Swap the values |
| `filter_11bit_value` | 11-bit filter with f1/f2 above 7FF | Likely a 29-bit ID in an 11-bit filter. Fix: `id_format` 1, or the correct ID |
| `control_initial_rx_state` | `control_rx_state` 1 and `general.rx_state` 1 | Logging runs from power-up until the first stop trigger. Often intended (no data lost at start-up); confirm with the user. For "off until the start trigger": `rx_state` 0 |
| `control_range_overlap` | Start and stop use the same message + signal, and their ranges partially overlap (nested ranges are fine: The inner range wins, per the Docs) | Device behaviour for values in both ranges is not defined. Fix: Separate or nest the ranges |
| `control_message_rejected` | The start/stop message is rejected (or not matched) by its source channel's filters | The control signal only sees accepted frames, so it never triggers. Fix: Accept the control message ID in the source channel's filters (`can_internal` for GNSS speed `0x6B`) |

## CANmod.router checks (`router_*`)

Run on a CANmod.router config (`phy.can.route`). The `--pair` checks need both configs (`check_config.py
<router config> --pair <CANedge config>`, either way round) or, for the tunnel rules, `--project` with the router DBC.

| ID | Triggers when | Context + fix for the user |
|---|---|---|
| `router_transmit_period_delay` (error) | S1-S4 transmit entry with period > 0 and period <= delay | Invalid: The router schema states "if period > 0, delay < period" (as the CANedge, which rejects such configs). Fix: Smaller delay |
| `router_filters_disabled` | An S channel has no enabled filter | The router forwards nothing from that channel (no match = rejected). Intended only if the channel is unused. Fix: Enable a filter (accept all: f1 `7FF` mask `0` + f1 `1FFFFFFF` mask `0`) |
| `router_filter_shadowed` | An enabled filter is fully covered by an earlier one (same ID format, frame format both or equal) | Never reached (first match wins), so its prescaler has no effect. Typical: Accept-all above specific filters. Fix: Specific filters on top (`router_filters.py --mode top`) |
| `router_transmit_mode` | Enabled S-channel transmit entries, but the channel is not in Normal mode | Nothing is transmitted. Fix: `phy.can_sN.phy.mode` 0 |
| `router_direct_mode` | `route.mode` 1 (Direct) | S1-S4 frames reach the CANedge as plain frames on CAN2 (their own IDs), not as tunnel frames: No demux, decode them as CAN2. Fine if intended |
| `router_tunnel_rx_off` | `mux.s2p.filter.rx_msg` 0, or the CANedge channel's `rx_state` off | No S1-S4 data reaches the CANedge log. Fix: `rx_msg` 1 / `rx_state` 1 |
| `router_tunnel_collision` | The tunnel ID (s2p) equals a p2s ID | The router and the CANedge use the same ID in both directions. Fix: Distinct IDs (default s2p 10, p2s 11; a 2nd router s2p 12, not 11 as some examples show) |
| `router_tunnel_rejected` | The CANedge filters on the tunnel's channel reject (or do not match) the tunnel ID | No router data is logged. Fix: Accept the tunnel ID (0x10) without prescaler, above any reject filter |
| `router_tunnel_prescaled` | The CANedge prescales the tunnel ID | Tunnel segments are lost, so demuxed frames go missing (like a TP). Fix: Never prescale the tunnel; prescale per ID on the router (S1-S4 filters) |
| `router_bitrate_autodetect` | CANedge channel on bit-rate auto-detect with a router config given | Auto-detect does not work with only a CANmod on the channel (CANedge Intro). Fix: Set the router's primary bit-rate (default 1M / FD 4M) |
| `router_bitrate_mismatch` | CANedge channel bit-rate (std or FD) differs from the router's primary bus | No data. Fix: Same bit-rates on both |
| `router_canedge_monitoring` | CANedge channel in Monitoring mode | It does not acknowledge the router's frames (the only other node on that bus). Fix: Normal or Restricted |

Notes (no warning): S channels in Monitoring mode (fine on a vehicle, fails on a single-node bench), a custom
tunnel ID or 0x12 (the router DBC must match: `muxtp.py dbc`).

## Checked by simulate_config.py instead

These depend on the traffic, so `simulate_config.py` checks them on a log file. They appear at the top of its
`notes` (console, `--json` and the HTML page); treat them like warnings and tell the user:

- **Prescaler limit:** More than 100 prescaled IDs on a channel (15 on CAN9). The device prescales the first ones
  it sees, the rest are logged at full rate, so the file is larger than planned
- **Transport protocol IDs** (J1939 TP, ISO-TP, NMEA 2000 Fast Packet) rejected or prescaled: Multi-frame messages
  can no longer be reassembled
- **Control signal:** No accepted frame matches the start message (it never starts); partially overlapping ranges
- **CANmod.router** (router configs): Total frames/s after the router filters above ~6000 (guideline for all router
  channels together); the router's tunnel ID tagged "CANmod.router link" (TP) when a CANedge config rejects or
  prescales it (with `--project` and the router DBC)

## Keeping in sync

Port changes in `config-editor-base/src/editorBase/actions.js` to `check_config.py` (limits: Transmit 224 / 4096
bytes, filters 128 / 64, RTC 400 s, split 60 s) and add the check to the right table here (a new check that makes the
device reject the config goes into `REJECT_CHECKS`). New firmware: Add `schema-VV.VV.json` to
`assets/schema-files/<DeviceType>/` (CANedge1, CANedge1 GNSS, CANedge2, CANedge2 GNSS, CANedge3 GNSS, CANmod.router;
router schemas from the CANmod.router firmware zip / `config-editor-base/dist/schema/CANmod.router/`).
