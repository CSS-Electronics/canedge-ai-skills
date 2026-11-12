# CANmod.router with a CANedge (curated facts)

Hand-curated from: CANedge Intro "CANmod.router" (`intro/canedge2_intro_llm.txt`, grep `^## CANmod.router`), the
CANmod.router Docs (FW 01.02.02, https://canlogger.csselectronics.com/canmod-router-docs/: "Filter", "Message
Prescaling", "Configuration"), the router JSON Schema 01.02 (`canedge-config/assets/schema-files/CANmod.router/`),
the MF4 to MF4 converter docs (MUX-TP) and the CANmod.router product page. Setup and troubleshooting: also
`canmod_intro_llm.txt` (CANmod Intro, all CANmod variants).

## What it does

- Routes traffic between a primary bus (CAN-P, DB9) and 4 secondary buses (CAN-S1..S4, DB25), each galvanically
  isolated, CAN / CAN FD
- **Mux mode** (`phy.can.route.mode` 0, the CANedge use case): Traffic of S1-S4 is packed into CAN FD **tunnel
  frames** on CAN-P with one ID (`phy.can.route.mux.s2p.message`: default ID `10` = 0x10, 11-bit, FD, BRS; FW 01.01:
  `mux.output.message`). Which traffic is tunnelled: `mux.s2p.filter` (`rx_msg` 1, `tx_ack` 0, `err` 0 by default).
  Frames from CAN-P to S1-S4 (e.g. CANedge transmit lists) use the p2s IDs (`mux.p2s[]`, default `11` = 0x11, max 8)
- **Direct mode** (`mode` 1, FW 01.02.01+): Gateway, frames are forwarded as plain frames (per-channel
  `direct.p2s.can_sN.filter[]` max 4, `direct.s2p.can_sN.map[]` max 16 ID re-mappings). No tunnel, no demux
- Primary bus default 1M / FD 4M; secondary default 250K / FD 1M. 1 ms cross-channel timing precision (product page)

## With a CANedge

- Connect to the CANedge CH2 (the router can be powered from the CANedge 5V on CH2). CANedge CH2: Bit-rate 1M / FD
  4M set manually (auto-detect does not work with only a CANmod on the channel), mode Normal or Restricted,
  termination as for any CANmod
- Raw logs show CAN2 with one CAN FD ID (0x10) carrying everything: **Demux** before raw analysis. The CANedge filters
  must accept the tunnel ID **without prescaler** (every segment is needed). Filter / prescale per ID on the router
- Daisy chain / several routers: Each router needs its own tunnel ID (convention: 0x10, 0x12, ...; never the p2s ID
  0x11). "Daisy chain two CANmod.routers to log 9 x CAN" (Intro); more are possible, limited by the total frame rate
- Bandwidth: Keep the total of all router channels below ~6000 frames/s after filters (log size, upload; the
  product page estimates ~6k fps ~ 5 MB/min ~ 7 GB/day compressed)

## Demux

- `mdf2mdf --muxtp-can2=010#11:12:13:14 -i <file>` (channel N of the tunnel, ID hex 3 or 8 digits, then the 4 target
  channels in decimal; repeat the flag per tunnel ID): 0x10 -> CAN11-14, 0x12 -> CAN15-18. Output `<stem>_out.MF4`.
  The Intro and the product page recommend 11+ (matches the router DBC, no clash with CAN9 GNSS/IMU)
- MF4 decoders (`mdf2parquet_decode`, data lake automation): Add the router DBC `can2-canmod-router.dbc` (messages
  0x10 `MUXTP#11:12:13:14` and 0x12 `MUXTP#15:16:17:18`, attribute `TransportProtocolType`) next to the other DBCs;
  they demux and decode in one step (MUX-TP since decoder 24.10.17). DBCs for the router channels: `can11-` ...
  `can14-` prefixes (2nd router: `can15-` ... `can18-`). A `dbc-groups.json` with these needs automation v4.3.2+
- The default router DBC assumes CH2 and tunnel IDs 0x10 / 0x12: Another tunnel ID needs an edited DBC
  (canedge-workspace `muxtp.py dbc`)
- USB streaming (no CANedge): SavvyCAN "virtual channels", `python-can-csscan-mux`

## S1-S4 filters (router config)

- `phy.can_s1..can_s4.filter[]`, 1-32 per channel: `name` (<= 16), `state`, `id_format` (0 11-bit / 1 29-bit),
  `frame_format` (0 standard / 1 FD / 2 both; 01.02), `f1` (ID hex), `f2` (mask hex), `prescaler_type` (0 none /
  1 count 1-256 / 2 time 1-4194304 ms / 3 data mask <= 16 hex digits)
- Docs: "A message is accepted if it passes one or more filters"; processed top to bottom, "Processing stops on the
  first filter match"; "Messages matching no filters are rejected as default"; "If there are no enabled filters in
  the list then all messages are rejected". Match: `filter_id & filter_mask == message_id & filter_mask`. Mask only,
  accept only (no rejection filters, unlike the CANedge; the product page lists "rejection" filters, but the schema
  has no filter type field)
- Prescaling as on the CANedge: "The first message with a given ID is always accepted"; "A maximum of 100 unique
  message IDs can be prescaled for each CAN-bus channel"; data prescaler assumes a constant payload length
- Transmit: `phy.can_sN.transmit[]` max 16 (period/delay in 10 ms steps, delay < period if periodic)
- Config file: `config-01.02.json` on the router's CONFIG drive (USB); the Rule Schema and default config are on the
  DEVICE drive. An invalid config stops the device until a valid one is provided (LEDs show it)

## Pitfalls

- S channel in Monitoring mode: No ACK, so a single node on a bench never gets its frames through (fine on a vehicle)
- A second router configured with tunnel ID 0x11 collides with the default p2s ID 0x11
- After demux, other CANmods on CH2 stay on CAN2
- Decoder MUX-TP: Incomplete tunnel sessions are dropped; sessions continue only across consecutive log files
