---
name: canedge-knowledge
description: >
  Authoritative CANedge context (CANedge1/2/3 incl. GNSS/IMU variants; CAN, CAN FD, LIN) from the bundled
  official CANedge Intro + CANedge Docs: Configuration fields (meaning, defaults, limits), hardware, LEDs,
  recording, log file format, SD/WiFi/LTE/S3 transfer, firmware, protocols (OBD2, UDS, J1939, NMEA 2000,
  CANopen, LIN), GNSS/IMU and the software tools (MF4 converters/decoders, data lakes, dashboards, asammdf,
  Python), plus the CANmod Intro and curated CANmod.router facts (with a CANedge: tunnel, demux, S1-S4 filters).
  Use whenever a task needs CANedge facts instead of model memory.
version: 1.4.0
allowed-tools:
  - Read
  - Grep
  - Glob
---

# CANedge Knowledge

Context for the CSS Electronics CANedge data-logger family (CANedge1 / CANedge2 / CANedge3, CAN / CAN FD / LIN loggers). It bundles LLM-friendly snapshots of the two official documentation sets: the CANedge Intro and the CANedge Docs.

The bundled files in `references/` (relative to this skill directory) are the authoritative source: prefer them over web search or model memory. If something is not covered, say so rather than inventing specifics.

## What this skill contains

| Folder | What it is | Use it for |
|---|---|---|
| `references/intro/` | **CANedge Intro** ("Intro and Tools"), one file per base device | Getting started and the practical "how do I" workflow: config editor usage, protocol recipes, recording, data transfer / S3 setup, and all software / API / automation tools |
| `references/docs/` | **CANedge Docs** (the technical user manual, firmware 01.09.03), one file per device variant | The detailed reference: every configuration section field by field, hardware, log file / filesystem format, S3 / TLS internals, firmware changelog, bit timing |
| `references/canmod/` | `canmod_intro_llm.txt` (**CANmod Intro**: configure, install, CANmod.router secondary CAN troubleshooting, SavvyCAN/Python USB streaming) + `canmod-router.md` (curated **CANmod.router** facts: mux/direct mode, tunnel ID, CANedge setup, demux, S1-S4 filter + prescaler rules, limits, pitfalls; FW 01.02) | CANmod questions; any CANedge + CANmod.router question (start with `canmod-router.md`, then the Intro's `^## CANmod.router` section). The CANmod Docs (config fields of gps/input/temp) are not bundled: https://www.csselectronics.com/pages/can-bus-hardware-software-docs#canmod-resources |

## CANedge Intro vs CANedge Docs: which to read

The two sets supplement each other. They overlap on the basics, but differ in purpose and depth:

- **The Intro is the quickstart + tools manual.** It explains what the user needs to do to get started (configure, record, transfer, process), the protocol-specific recipes, and it is the home of the software documentation and automation workflows. It is task-oriented and links into the Docs for detail.
- **The Docs are the in-depth technical reference.** Each configuration section is documented field by field (JSON path, type, default, min/max, options, auto-generated from the schema) followed by a "Configuration explained" part, plus hardware and file-format detail the Intro does not contain.

Pick the set by the question:

| Read the **Intro** for | Read the **Docs** for |
|---|---|
| Getting started, the overall workflow, "how do I ..." | "What exactly does field X do / what are its default, limits, options?" |
| Config editor tools (online/offline editor, OBD / filter builder / encryption / bit-timing helpers, partial configs, schema loader) | Every config section in detail: general, logging, RTC (incl. GNSS time sync), 5 V output, CAN (general, PHY, filter, transmit), control signals, LIN (PHY, frame table, transmit), routing, GNSS (system, alignment, geofence, dynamic model, sensor fusion, invalid outputs), connect (WiFi / cellular), protocol (S3, web server) |
| Firmware update workflow and config migration (editor, CANcloud, canedge_manager OTA) | Firmware changelog (what changed per version) and firmware packages |
| Protocol recipes: OBD2 (incl. manual PID validation), UDS, J1939, NMEA 2000, CANopen, CCP/XCP, LIN, CANmod, internal GNSS/IMU usage | Signal interpretation (the decode structure used by control signals, routing, GNSS etc.), internal signals |
| Recording checklist, logging troubleshooting, reducing file size, practical tips & tricks | Hardware: specification (tech specs), installation requirements, connectors / pinout, LEDs (behaviour per state), SD-card, enclosure, label, legal / certifications |
| Data transfer setup: SD extraction, S3 servers (AWS / Google / Azure / MinIO, CloudFormation / Terraform), HTTPS/TLS certificate setup, WiFi access points, SIM card, web-server mode, WiFi/LTE troubleshooting | S3 internals: S3 summary, server types, S3 security, how the device interacts with S3 (S3 management: uploads, device file, config/firmware OTA mechanics), TLS cipher support, web-server API |
| S3 tools: CANcloud, mounting S3 as a drive, S3 API, OTA batch manager | Filesystem layout, device file, log file format (MF4 structure, naming, splitting, compression, encryption) |
| All software / API tools: asammdf GUI, MF4 converters, MF4 decoders, Parquet data lakes (+ advanced: backlog, trip summaries, events, custom signals, device DBCs), Grafana / Power BI dashboards, Python, MATLAB | Bit-timing tables (CAN / CAN FD on the 40 MHz clock), glossary |

Rules of thumb:

- **Default for support / how-to questions: start with the Intro**, then go to the Docs only when you need exact field semantics, limits, hardware facts or file-format detail.
- **Software, tools and automation: the Intro only.** The Docs do not cover asammdf, decoders, data lakes, dashboards or the S3 tools.
- **Config-field meaning, defaults, ranges, max counts (e.g. filters, transmit messages), LED meaning, connector pinout, spec numbers, file naming / MF4 layout, firmware changes: the Docs.** Quote these verbatim from the Docs rather than from the Intro.
- **Troubleshooting often needs both**: the Intro for the practical checklist (wiring, termination, bit-rate, access point, SIM), the Docs for the exact device behaviour (LED codes, field constraints, how uploads / heartbeats work).
- The Docs snapshots are for firmware **01.09.03**. If the device runs older firmware, check the Docs firmware changelog (`[Download firmware]:` section) before assuming a field or behaviour exists.

## Which variant file to open

The content within each set is largely identical across device variants: the files differ mainly in the connectivity- and GNSS-specific sections. **Do not read several variants for the same topic.** Open the one file matching the device in question, and only that one.

| Device | Intro file | Docs file | Product page |
|---|---|---|---|
| CANedge1 | `intro/canedge1_intro_llm.txt` | `docs/canedge1_llm.txt` | [can-logger-sd-canedge1](https://www.csselectronics.com/products/can-logger-sd-canedge1) |
| CANedge1 GNSS | `intro/canedge1_intro_llm.txt` | `docs/canedge1_gnss_llm.txt` | [can-logger-sd-canedge1-gnss-imu](https://www.csselectronics.com/products/can-logger-sd-canedge1-gnss-imu) |
| CANedge2 | `intro/canedge2_intro_llm.txt` | `docs/canedge2_llm.txt` | [can-bus-data-logger-wifi-canedge2](https://www.csselectronics.com/products/can-bus-data-logger-wifi-canedge2) |
| CANedge2 GNSS | `intro/canedge2_intro_llm.txt` | `docs/canedge2_gnss_llm.txt` | [can-bus-data-logger-wifi-canedge2-gnss-imu](https://www.csselectronics.com/products/can-bus-data-logger-wifi-canedge2-gnss-imu) |
| CANedge3 GNSS | `intro/canedge3_intro_llm.txt` | `docs/canedge3_gnss_llm.txt` | [can-bus-data-logger-4g-lte-canedge3-gnss](https://www.csselectronics.com/products/can-bus-data-logger-4g-lte-canedge3-gnss) |

(File paths relative to `references/`.)

Notes:

- There is one Intro per base device; it covers the GNSS variant too (the "Using the internal GNSS/IMU" page is in all three Intros).
- The CANedge3 only exists as a GNSS variant, so there is no `canedge3_llm.txt`.
- What differs between the variant files:
  - **Intro**: CANedge1 has no transfer-over-air pages. CANedge2 adds S3 server / tools, WiFi access points, USB 3G/4G router, web server, WiFi/S3 troubleshooting. CANedge3 adds SIM card, LTE/S3 troubleshooting and the "route data to client-specific output buckets" data-lake page (and has no WiFi / web-server pages)
  - **Docs**: CANedge1 has no Connect / Protocol / S3 / TLS sections. CANedge2 adds WiFi, S3, web server (+ API), TLS cipher support. CANedge3 GNSS adds Cellular and SIM-card and has no WiFi / web server. GNSS variants add the GNSS config sections (satellite system, alignment, geofence, dynamic model, sensor fusion, invalid outputs) and RTC sync via GNSS. Specification, LEDs, label and firmware changelog are variant-specific
- Picking the file:
  - Use the variant the user named (a CANedge3 question goes to the CANedge3 files, etc.).
  - For shared topics (CAN / LIN config, filters, transmit lists, logging, MF4 format, decoding) when no variant is named, use one file: `canedge2_intro_llm.txt` for the Intro and `canedge2_gnss_llm.txt` for the Docs (the broadest sets: base logger + connectivity (+ GNSS)).
  - For variant-specific facts (specs, LEDs, connectivity, TLS ciphers, firmware changelog) always use the exact variant's Docs file, never a sibling's.
  - When unsure which variant the user means, state the connectivity delta rather than guessing.

## How to use the files

All Intro and Docs files are very large (Intro 3,800-5,200 lines / 240-330 KB; Docs 5,300-7,700 lines / 240-345 KB). Never read one in full. Instead:

1. Search (grep) first to locate the section, using the structure of each set:
   - **Docs**: each section starts with a marker line `[Title]:` followed by `# Title` (e.g. `^\[Filter\]:$`, `^\[CAN transmit\]:$`, `^\[LEDs\]:$`, `^\[Specification\]:$`, `^\[Log file\]:$`, `^\[S3 management\]:$`). The section list is at the top of each file. Config fields appear as `**Name** \`json.path\`` (e.g. search `can.filter.id`, `connect.s3.server`, `connect.cellular.apn`, `log.file.split`)
   - **Intro**: same convention as the Docs: each page starts with a marker line `[Title]:`, then `Path:` (breadcrumb) and `URL:` (live page), then `# Title` (e.g. `^\[Log OBD data\]:$`, `^\[Parquet data lake\]:$`, `^\[WiFi/S3 troubleshooting\]:$`). The indented section list with one-line descriptions is at the top of each file, followed by `## Source files` (links to the CloudFormation templates and GitHub repos). Repeated titles are qualified, e.g. `[Customize your dashboard (Grafana-Athena dashboards)]:`. The AWS data lake's deployed resources (buckets, Lambda, Glue jobs/triggers, Athena, SNS, IAM users/roles) are listed under `### Deployed resources` (in its `## Technical` section) in `[Amazon Parquet data lake]:`. The data lake output layout and table naming (`<deviceid>/<message>/YYYY/MM/DD/...`, `tbl_<deviceid>_<message>`) are under `### Data lake structure` (in its `## Technical` section) in `[Parquet data lake]:`, and the input-bucket control files (`passwords.json`, `events.json`, `routing.json` ...) are tabled under `### Control files (JSON)` in `[Parquet data lake - advanced topics]:`. The automation source code lives in the public GitHub repos listed under `## Source files` (fetch on demand); MF4 decoder internals are in the MF4 decoder docs linked from the Intro
   - Topic keywords work in both: `S3`, `WiFi`, `LTE`, `SIM`, `heartbeat`, `prescal`, `filter`, `transmit`, `GNSS`, `IMU`, `MF4`, `DBC`, `firmware`, `bit-rate`
2. Read with an offset/limit once the search reports line numbers, fetching ~30-100 lines around the match (a Docs config section with its field tables can be 100-300 lines; read the field tables and the relevant "Configuration explained" subsection, not the whole section).
3. Quote numbers and specs verbatim (intervals, sizes, ranges, defaults, max counts). Do not paraphrase them.
4. When citing sources, use the `URL:` line of the Intro section (links inside the Intro files are absolute live URLs). Docs pages live at `https://canlogger.csselectronics.com/canedge-docs/<ce1|ce1-gnss|ce2|ce2-gnss|ce3-gnss>/...` (the files contain the exact URLs). The Intro and all documentation downloads are on the CSS Electronics docs hub: https://www.csselectronics.com/pages/can-bus-hardware-software-docs

## Security / compliance

For the technical security controls, use the Docs sections `[S3 security]:` and `[TLS cipher support]:`, and the encryption / key-format parts of the config sections. For the overall framing, see the secure CAN logging / telematics intro article: https://www.csselectronics.com/pages/secure-can-bus-logging-telematics-intro

## Worked examples

User asks "how do I set up my CANedge3 to upload to AWS S3?" (how-to: Intro)

```
grep -n "^\[Cloud servers\]:$\|^\[SIM card\]:$" references/intro/canedge3_intro_llm.txt
# -> read the setup pages, quote the steps; link the CloudFormation template mentioned there
```

User asks "what's the max number of CAN ID filters, and what does the 'prescaler' type do?" (field detail: Docs)

```
grep -n "^\[Filter\]:$" references/docs/canedge2_gnss_llm.txt   # no variant named -> CANedge2 GNSS
# -> read the field tables (can.filter.id ... "Max 128 11-bit filters and 64 29-bit filters")
#    and the "Message prescaling" subsection; quote verbatim
```

User asks "my CANedge2 LED is blinking X after inserting the SD card, what does it mean?" (hardware: Docs, then Intro for the fix)

```
grep -n "^\[LEDs\]:$" references/docs/canedge2_llm.txt
# -> interpret the LED state; then grep "^\[Logging troubleshooting tips\]:$" in intro/canedge2_intro_llm.txt for the practical checklist
```

User asks "how do I DBC-decode my logs into a Parquet data lake on Azure?" (software: Intro only)

```
grep -n "^\[Azure Parquet data lake\]:$" references/intro/canedge2_intro_llm.txt
```

User asks "does the CANedge1 support WiFi upload?"

```
# CANedge1 is SD-only. Answer from intro/canedge1_intro_llm.txt and point to CANedge2/CANedge3 for remote upload.
```

## Updating

The `references/` files are point-in-time snapshots. The latest Intro and Docs "Docs for AI" text files are published on https://www.csselectronics.com/pages/can-bus-hardware-software-docs. To update, replace the files in `references/intro/` or `references/docs/` (keep the filenames) and update the firmware version noted above.
