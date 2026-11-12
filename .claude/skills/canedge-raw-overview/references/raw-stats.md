# Raw stats: Schemas + heuristics

Three JSON layers, all regenerable from the log files:

| Layer | Path | Written by | Read by |
|---|---|---|---|
| Per-file stats (`canedge-raw-stats/1`) | `data/raw-stats/<DEVICE>/<SESSION 8>/<SPLIT 8>.json` | `raw_stats.py` | `raw_aggregate.py` |
| Aggregate (`canedge-raw-aggregate/2`) | `stats.json` next to the report (or `--out`) | `raw_aggregate.py` / `build_dashboard.py` | The agent (read it in full for follow-up questions) |
| Dashboard facts (`canedge-raw-facts/1`) | Embedded in `report.html` (`const FACTS`) | `facts.py` via `build_dashboard.py` | The dashboard app (`assets/app.js`) |

All times are UTC ISO strings ending in `Z`. Device = MF4 header serial number (folder name as fallback).

## 1. Per-file stats (canedge-raw-stats/1)

| Field | Meaning |
|---|---|
| `file.path`, `size_bytes`, `mtime` | Absolute input path, size and modification time. **Cache key**: Reprocessed only if size or mtime differ |
| `file.device`, `session`, `split` | From the MF4 header (fallback: `<device>/<session>/<split>.MF4` path). Files without session/split go to `<DEVICE>/00000000/<stem>.json` |
| `file.path_device` | Only present if the folder name differs from the header serial (e.g. renamed S3 prefix) |
| `file.format`, `firmware`, `hardware`, `config_crc32` | Extension and header fields (`null` if the firmware does not write them) |
| `file.meta` | Device meta name = header `comment` (Configuration File `general.device.meta`); `null` = not set |
| `file.header`, `file.id_source` | `mf4` = header read, `finalized` = MFC/MFE/MFM header from a finalized copy (mdf2mdf) of one file of the session, `unsupported` = not read (`--no-headers`, mdf2mdf failed, or cached before; meta/firmware/CRC32 `null`, no bus load); `id_source` = where device/session/split came from (`header`, or `path` as fallback). Header fields are refreshed on cached stats on every `raw_stats.py` run (no re-conversion) |
| `file.bitrates` | Non-zero header bit-rates, e.g. `{"CAN1": 500000}`. No CAN FD data-phase rate in the header |
| `tool.mdf2csv` | Converter version |
| `time.first_utc`, `last_utc`, `duration_s` | First/last frame of the file (all channels). The header start time can be the session start, so it is not used |
| `frames_total` | Data frames, all channels |
| `bin_s` | 60. Bins are aligned to whole UTC minutes (`bins.start_utc` = first frame floored to the minute) |
| `channels.<CANx/LINx>.frames`, `unique_ids` | Totals per channel. `CAN9` = internal GNSS/IMU, treated like any other channel |
| `channels.*.error_frames` | Always `null`: mdf2csv 24.12.19 does not export error frames |
| `channels.*.tx_frames` | Frames transmitted by the logger (`Dir` = 1), only if > 0 |
| `channels.*.busload_mean_pct`, `busload_max_pct` | Only if the header has a bit-rate for the channel (CAN only). Max over the same bins as the mean (coverage floored at 1 s) |
| `channels.*.bins.frames`, `bins.busload_pct` | Per 60 s bin. Bus load of a partial edge bin is relative to its covered seconds. Omitted (with a `warnings` entry) if the file spans > 100k bins (bad timestamps) |
| `ids[]` | One entry per channel + ID, sorted by `est_storage_bytes` |
| `ids[].id`, `ide`, `fd` | `0x` + upper-case hex, 4 digits (11-bit) or 8 digits (29-bit); LIN IDs 2 digits (cache format; `stats.json` and the dashboard show 11-bit IDs with 3 digits, as all skills do). `fd` = any frame with EDL |
| `ids[].frames`, `first_utc`, `last_utc` | Count and first/last timestamp in the file |
| `ids[].dlc`, `data_length_max` | Sorted distinct DLC values and max data bytes |
| `ids[].period_ms_median`, `period_ms_p95` | Median / p95 of consecutive timestamp deltas (`null` for single frames) |
| `ids[].unique_payloads`, `unique_payloads_capped` | Distinct payloads, capped at 1000 (`capped: true` = 1000 or more) |
| `ids[].payload_changes` | Frames whose payload differs from the previous frame of the ID (the first frame counts) = frames a data prescaler would log. Basis of "static" (< 5% of frames). Older cached stats without it are recomputed |
| `ids[].payload_bytes`, `est_storage_bytes` | Sum of data bytes; storage estimate (section 3) |

## 2. Aggregate (canedge-raw-aggregate/2)

Top level: `selection` (inputs, time filter, `files_with_stats`, `files_missing_stats` + examples), `settings`,
`tools`, `totals` (devices, files, size, first/last, recorded + bus-active hours, frames, periods without data,
unique IDs), `devices`, `comparison` (only with > 1 device). All times UTC; reports convert to local time.

| `devices.<ID>` field | Meaning |
|---|---|
| `files`, `sessions`, `session_count` | Sessions listed when <= 10, else `{count, first, last}` |
| `size_bytes`, `size_gb`, `bytes_per_frame` | Actual input size and actual MF4 bytes per frame (use for size planning) |
| `est_storage_bytes`, `est_storage_vs_size_pct` | Sum of the per-ID storage estimates and its ratio to the real size |
| `first_utc`, `last_utc`, `span_h` | First/last frame and the wall-clock span between them |
| `logged_h`, `active_h` | Recorded time (logger on, section 3) and the part of it with frames on a bus (not silent) |
| `firmware`, `hardware`, `config_crc32`, `meta` | `{changed, values: [{value, files, first_utc, last_utc}]}` in time order - `changed: true` flags an update/reconfiguration/rename (`unknown` = header not readable, meta `(not set)`) |
| `config_periods` | Consecutive files with the same meta, config CRC32 and firmware: `{from_utc, to_utc, files, sessions (distinct count), session_range, meta, config_crc32, firmware, changed: [meta, config, fw]}` (what differs from the previous period; a meta change always comes with a config change) |
| `header_unsupported_files` | Files whose header is not read (MFC/MFE/MFM with `--no-headers`, mdf2mdf failed, or stats cached before headers were read: `--force`) |
| `recording.spans_utc`, `spans_total` | Recording intervals merged when < `--merge-s` (120 s) apart (max 200 listed, `spans_truncated`) |
| `recording.no_data`, `no_data_total` | Periods without log data between spans, longer than `--gap-h` (1 h): `{from_utc, to_utc, hours}`. Normal for loggers that only run with the ignition - never call them "gaps" or errors |
| `states` | `fps_active_mean` (all buses, frames / active time), `fps_max` (busiest minute, all channels), `states.silent` / `states.active` (`h`, `pct` of recorded time; active also `fps_median`) |
| `states.trips` | Trip summary over all trips: `count`, `total_h`, `median_min`, `longest_min`, `days_with_data` (UTC days with recording), `per_day` (trips per day with data), `last_start_utc` |
| `states.segments`, `segments_total` | The **trips** shown in the report (active = frames on any channel, except those in `settings.activity_ignore`, e.g. CAN9): Active periods, joined across interruptions (silent or no data) <= `--segment-merge-min` (5), at least `--trip-min-min` (3) min long: `{start_utc, end_utc, minutes, fps_mean, files[] (`<device>/<session>/<split>.<ext>`, max 10), files_more}` - max 200 listed |
| `channels.<ch>` | `bitrates`, `frames`, `tx_frames`, `unique_ids`, `fps_active_mean` (frames / bus-active time), `fps_max` (busiest minute: frames / recorded seconds of that minute, min 1 s - the same minutes as the averages), plus the estimates `busload_mean_pct` (duration-weighted) and `busload_max_pct` (not shown in the report) |
| `fps_timeline` | Recorded-time timeline for charts: `t_utc[]` (point start), `fps.<ch>[]` (average per point), `fps_peak.<ch>[]` (busiest minute per point), `minutes_per_point`, `break_before[]` (point indexes preceded by a period without data). Periods without data are removed from the axis |
| `ids.total`, `ids.listed`, `ids.omitted` | All IDs are counted; only the top `--top` (30) by storage are listed, the rest is summarized |
| `ids.top[]` | `channel`, `id`, `frames`, `rate_hz` (frames / device bus-active time), `period_ms_median`/`p95` (frames-weighted median of the per-file values), `dlc`, `unique_payloads` (max per file), `payload_changes_pct` (all files), `est_storage_bytes`, `est_storage_pct` (share of the device total), `first_utc`, `last_utc`, `files`, `files_pct`, `sessions`, optional `fd`, `unique_payloads_capped` |
| `static_candidates[]` | Up to 20 static IDs (`payload_changes_pct` < 5), by storage share |
| `path_device` | Folder names that differ from the header serial |

`comparison`: Keyed by ID string (channel ignored): `ids_common_to_all` and per device `ids`, `only_here_count`,
`only_here_top_by_frames` (max 30).

## 2b. Dashboard facts (canedge-raw-facts/1)

Built from the same per-device minute model as `stats.json` (`raw_aggregate.device_model`), so the dashboard's
unfiltered view matches `stats.json`. Tables are columnar (one list per column, same length); devices, channels and
IDs are integer indexes into `devices`, `channels`, `id_names` (meta names into `meta_names`, -1 = none). All times are UTC epoch seconds. Every number is
additive (sum) or max-able, so the app can filter by device + time and recompute every table in the browser.

| Key | Content |
|---|---|
| `bucket_s` | Time bucket size: The finest of 1 min / 5 min / 15 min / 1 h / 6 h / 1 day that keeps `buckets` <= 70k rows. The time filter snaps to it |
| `id_grain_s` | `ids` time grain: 1 day, else 1 week / 30 days to keep <= 100k rows |
| `meta.<device>` | `channels`, `bitrates` |
| `buckets` | Per device x bucket: `rec` / `act` (seconds recorded / bus active), `pk` (busiest minute, all buses, FPS), `bytes` (log file bytes, spread over each file's minutes by frame share), `fr.<ch>.a` (frames, all in active minutes) and `fr.<ch>.pk` (busiest minute per channel). The dashboard divides by `act` (averages over active time only) and does not show silent time. Maxima use the same recorded minutes as the averages (incl. partial minutes at file edges, rate floored at 1 s) |
| `trips` | One row per trip (all of them): `dev`, `start`, `end`, `min`, `fps` (avg, all buses). Its log files = the `files` rows overlapping it (computed in the app) |
| `files` | One row per log file: `dev`, `first`, `last` (frames), `size`, `session`, `split`, `ext` (MF4/MFC/MFE/MFM), `fw`, `crc`, `meta`, `hdr` (1 = header readable). The app shows them as `<device>/<session>/<split>.<ext>` in hover tooltips and the trips table |
| `ids` | Per device x grain x channel x ID: `fr` (frames), `est` (est. storage bytes), `up` (max unique payloads), `cap` (capped), `per` (frames-weighted median period ms), `nf` (files containing it) |
| `id_dlc` | `"<device>|<channel>|<id>"` -> distinct DLCs |

Filter semantics in the app: Buckets and ID rows are included when their start lies in the range; trips and files when
they overlap it. Medians are not additive, so the dashboard shows means and maxima per selection (per-state medians
stay in `stats.json`).

## 3. Heuristics

**One basis for all statistics**: Averages, maxima, medians and states all use the same
recorded minutes (incl. partial minutes at log file edges). Per-minute FPS = frames / max(recorded seconds, 1), so
a fraction of a second cannot create a spike. Never compute one statistic on a filtered subset of minutes and
another on all of them.

**Recorded time**: Each file's first..last frame, plus the time between consecutive splits of the same
session (split N and N+1 both present). A new session starts at every power-on, so within a session the
logger was on, even if the bus was silent. Between sessions (or missing splits) nothing is known: That is
a period without data (logger off, files not included, or deleted by cyclic logging).

**Activity** (per device, per recorded minute; all channels incl. CAN9 = internal GNSS/IMU, so a GNSS logger is
active whenever it logs GNSS/IMU data - disable that in the Configuration File if it should not count):
**active** = frames on at least one channel, **silent** = recording but no frames on any
bus (bus asleep or disconnected). The dashboard only shows active vs. no activity (silent and "no data" look the
same to a user); `stats.json` keeps the silent hours for troubleshooting ("was the logger on?"). Frames sent by the
logger (e.g. OBD2 requests) count as activity. **Trips** = active periods joined across interruptions of up to
`--segment-merge-min` (5) minutes, lasting at least `--trip-min-min` (3) minutes; shorter activity (e.g. a wake-up
while parked) is not a trip.

**Frames/s**: `fps_active_mean` and ID `rate_hz` divide by bus-active time, so long silent periods do not
dilute them. An ID that only exists in part of the active time still shows a lower `rate_hz` than its nominal rate -
use `period_ms_median` for the transmit rate.

**Bus load** (estimate, kept in stats.json only; per frame time on the bus, summed per 60 s bin, divided by
the covered seconds):

| Frame | Bits |
|---|---|
| Classical, 11-bit | 47 + 8n + 0.10 x (34 + 8n) |
| Classical, 29-bit | 67 + 8n + 0.10 x (54 + 8n) |
| CAN FD | Nominal rate: 17 (11-bit) / 36 (29-bit) arbitration bits x 1.1 + 13 tail bits. Data rate (if BRS, else nominal): (5 + 8n) x 1.1 + 4 (stuff count) + CRC 17 (n <= 16) / 21 + ceil((4 + CRC) / 4) fixed stuff bits |

n = data bytes; 47/67 include SOF, arbitration, control, CRC, delimiters, ACK, EOF and 3 bits intermission;
the 34/54 bits + data are the stuffable part, 10% is an average stuff-bit share (real buses range ~3-20%,
e.g. J1939 0xFF padding stuffs more). CAN FD data-phase bit-rate defaults to 2 Mbit/s (`--fd-data-bitrate`).
Expect +-10% vs a hardware bus-load meter. No bit-rate in the header (CAN9, LIN, bit-rate 0) = no bus load.

**Storage estimate**: `est_storage_bytes = frames x weight(average data length) x 17` (`raw_stats.est_storage`).
Weights from MF4 size measurements, the same as the CANedge config editor's filter builder: <= 8 data bytes 1.0
(fixed-size record, so DLC 1 costs as much as DLC 8), 12 B 1.75, 16 B 2.12, 64 B 4.16, linear in between; a classic
CAN frame = 17 bytes. Readers recompute it from `frames` + `payload_bytes`, so cached stats always use the current
model. It is a relative weight for "which IDs fill the SD card": For absolute sizes use `bytes_per_frame` x frames
(real size, incl. record overhead and MFC compression; static IDs compress far better than the estimate assumes).

**Static IDs**: Fewer than 5% of the ID's frames carry a new payload (`payload_changes` / `frames`, vs the
previous frame of the ID; `raw_stats.STATIC_MAX_CHANGES_PCT`). That is exactly what a data prescaler on all bytes
would log, so a high storage share makes them candidates for a data prescaler (log on change, every value change
kept). Same basis as canedge-config `simulate_config.py` ("changes %"). Unlike a count of distinct payloads, it
also handles IDs with many values that rarely change (static) and IDs whose 2 values toggle on every frame (not
static).

## 4. Edge cases

- Encrypted files (MFE/MFM) need a password file: `projects/<name>/passwords.json` or `--passwords <path>` (refused up front without). MFC/MFE/MFM header fields come from a finalized copy of one file per device/session (canedge-workspace conventions section 8); a loose file outside the `<device>/<session>/` layout is finalized on its own and its stats get the header's device/session/split
- A file with an RTC jump (e.g. 1970 to 2024) spans > 100k bins: Bins are omitted, totals still valid
- Same device/session/split in two folders (SD copy + S3 copy): One cache entry. Only the last copy is processed (the others are reported as skipped duplicates, so parallel workers never write the same file)
- `--start/--end` in the aggregate filter whole files (overlap), not frames
- Memory: Each worker holds one file's frames in RAM (pandas). Very large splits need RAM accordingly; use `--workers 1` then
