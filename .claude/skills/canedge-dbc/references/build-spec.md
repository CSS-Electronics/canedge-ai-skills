# DBC build spec (input for dbc_build.py)

Extract the protocol document (PDF, Excel, Word, web page) into this format first, then run
`dbc_build.py --spec <file> --out <new.dbc>`. JSON for anything non-trivial, CSV for flat signal lists
(e.g. an Excel sheet exported as CSV).

Reading the document: Open PDFs with the agent's file reader (tables + page images). Read Excel with `openpyxl`
(in `requirements.txt`) from a small script - the sheet's columns rarely match this format 1:1. For a structured
CAN matrix (one row per signal), write a converter script that maps the columns and logs every fix; for a short or
messy sheet, write the spec by hand.

Typical problems in real spec sheets (check each, log what you changed, never fix silently):
- Columns that contradict a formula/comment in the same row (e.g. resolution 0.02 A/bit vs `PH=0.1*INT-1000`):
  The variant that matches the stated physical min/max wins; otherwise ask
- Mixed bit numbering in one sheet (e.g. `3` = byte 3 and `1.2` = byte 1, bit 2 zero-based): Resolve it with
  signals whose layout is known (J1939 SPNs, the same ID in another document, packed fields that must not overlap)
- The same CAN ID defined twice, 29-bit IDs marked as standard, hidden Unicode characters in numbers
- Value labels in another language: Translate them (DBC tools handle non-ASCII labels poorly)
- A range narrower than the value table (e.g. range 0..7 with gear codes 124-126): Widen it, or those states
  decode to NULL
- Signals marked "not available" / without ID: Leave them out and list them for the user
- Lists of PGNs/SPNs without bit layouts (e.g. FMS requirement sheets): Not buildable from the document alone -
  the layouts come from the J1939 DBC / FMS standard

## JSON

```json
{
  "version": "",
  "nodes": ["BMS", "VCU"],
  "defaults": {"byte_order": "little", "extended": false, "sender": "BMS"},
  "messages": [
    {"id": "0x123", "name": "BMS_Status", "dlc": 8, "sender": "BMS", "cycle_time_ms": 100,
     "comment": "Battery status",
     "signals": [
       {"name": "PackVoltage", "lsb_start": 8, "length": 16, "byte_order": "big",
        "factor": 0.1, "offset": 0, "min": 0, "max": 1000, "unit": "V", "receivers": ["VCU"]},
       {"name": "PackCurrent", "start": 16, "length": 16, "signed": true, "factor": 0.1, "unit": "A"},
       {"name": "State", "start": 32, "length": 2, "values": {"0": "Off", "1": "Precharge", "2": "On", "3": "Fault"}}
     ]},
    {"id": "0x124", "name": "BMS_Cells", "dlc": 8, "signals": [
       {"name": "CellMux", "start": 0, "length": 8, "multiplexer": true},
       {"name": "Cell1_V", "start": 8, "length": 16, "factor": 0.001, "unit": "V", "mux_id": 0},
       {"name": "Cell2_V", "start": 8, "length": 16, "factor": 0.001, "unit": "V", "mux_id": 1}
     ]},
    {"id": "0x18FEF100", "extended": true, "name": "CCVS1", "frame_format": "J1939PG", "signals": [
       {"name": "WheelBasedVehicleSpeed", "start": 8, "length": 16, "factor": 0.00390625, "unit": "km/h"}]},
    {"id": "0x1A0", "name": "FD_Frame", "fd": true, "dlc": 64, "signals": [
       {"name": "FloatVal", "start": 0, "length": 32, "float": true}]}
  ]
}
```

### Message fields

| Field | Required | Notes |
|---|---|---|
| `id` | yes | `"0x123"`, `291` or a DBC-style ID with bit 31 set (`0x98FEF100` = extended) |
| `name` | yes | C identifier, max 32 chars recommended |
| `extended` | no | Default: `true` if the ID is > 0x7FF, else `defaults.extended` / false |
| `dlc` | no | Bytes. Default 8 (64 for `fd`). J1939 TP / ISO TP messages may be up to 1785 |
| `fd` | no | CAN FD frame: writes `VFrameFormat` StandardCAN_FD / ExtendedCAN_FD |
| `frame_format` | no | `StandardCAN`, `ExtendedCAN`, `J1939PG`, `StandardCAN_FD`, `ExtendedCAN_FD` |
| `sender` | no | Transmitting node, added to `BU_` automatically |
| `cycle_time_ms` | no | Written as the `GenMsgCycleTime` attribute |
| `comment` | no | Message comment (keep <= 255 chars) |

### Signal fields

| Field | Required | Notes |
|---|---|---|
| `name` | yes | Unique within the message |
| `start` | one of | DBC start bit as written in the DBC file: Intel = LSB, Motorola = MSB (sawtooth numbering) |
| `lsb_start` | one of | Start given as the LSB position (sawtooth numbering). Converted to the DBC MSB start for big endian |
| `length` | yes | Bits (float = 32, double = 64) |
| `byte_order` | no | `little`/`intel` (default) or `big`/`motorola` |
| `signed` | no | Two's complement. Default false |
| `float` | no | IEEE float (32) / double (64); implies signed (`-` in the SG_ line, as the spec requires) |
| `factor`, `offset` | no | physical = raw x factor + offset. Default 1, 0 |
| `min`, `max` | no | Physical range. Omitted = `[0|0]`, or the representable range with `--fill-minmax`. Note: the CSS decoders write NULL for values outside min/max, so never guess a narrow range |
| `unit` | no | e.g. `"km/h"`, `"degC"`, `"%"` |
| `values` | no | Value table: `{"0": "Off", "1": "On"}` (keys may be hex strings like `"0x1F"`) |
| `comment` | no | Signal comment |
| `receivers` | no | List of node names |
| `multiplexer` | no | `true` on the multiplexor signal (`M`) |
| `mux_id` | no | Multiplexer value(s) for a multiplexed signal: `3` or `[3, 4]` |
| `mux_signal` | no | Parent multiplexor name; only needed if a message has several multiplexors |

## CSV

One row per signal; message columns repeat (the first non-empty value per message wins). Delimiter `,`, `;`
or tab (auto-detected). Column names (case-insensitive):

`message_id, message_name, extended, dlc, sender, cycle_time_ms, fd, frame_format, message_comment,
signal_name, start, lsb_start, length, byte_order, signed, float, factor, offset, min, max, unit, values,
multiplexer, mux_id, receivers, signal_comment`

- Booleans: `1/true/yes/y/x` = true, anything else false
- `values`: `0=Off;1=Precharge;2=On`
- `receivers`: space, comma or `|` separated
- `mux_id`: `3` or `3,4`

## Bit numbering in spec documents (convert before building)

| Document says | Use |
|---|---|
| "Intel / little endian, start bit N" | `start: N` |
| "Motorola, start bit N" where N is the MSB (Vector DBC convention) | `start: N`, `byte_order: big` |
| "Motorola, LSB at bit N" (some CANdb++ views, many OEM sheets) | `lsb_start: N`, `byte_order: big` |
| "Byte B, bit b" (0-based byte, bit 0 = LSB of the byte) | bit position = `B*8 + b` |
| "Byte 1-2" (1-based bytes, J1939 SPN style) | subtract 1 from the byte number first |
| J1939 SPN "start position 4.1" (byte.bit, 1-based) | `start: (4-1)*8 + (1-1)` = 24, little endian |

When in doubt, build, then check one known frame by hand (`dbc_info.py --message`), or verify on data
([plausibility.md](plausibility.md)).
