# dbc_lint.py checks

Two layers. Layer 1 is cantools; layer 2 runs on a light raw-text parser (so every issue has a line number and
the checks still run when cantools cannot load the file). Layer 2 is ported from the CSS Electronics online DBC
editor (`dbc-editor/src/validation/dbcErrors.ts` + `columnValidators.ts`, helpers in `bit-utils/bitMath.ts`,
`multiplex.ts`, `canId.ts`); "extra" checks are additions for use outside the GUI.

Severity: **error** = will break decoding or violates the DBC format; **warning** = review, may be intended.

## Layer 1 - cantools

| Check | Severity | Meaning |
|---|---|---|
| `cantools-strict` | warning (error if the lenient load also fails) | `load_file(strict=True)` failed. Only the FIRST problem is reported, and it can be a symptom (e.g. "signals Blank and Blank overlap" when the real cause is duplicate signal names) - layer 2 names the root cause |
| `cantools-load` | error | Even `strict=False` fails: syntax error (cantools reports line + column) |
| `cantools-load-warning` | warning | cantools load warnings, e.g. "Overwriting message X with Y ... identical frame ids" (only one of them is usable in cantools-based tools) |

## Layer 2 - ported from the DBC editor

| Check | Severity | Source | Rule |
|---|---|---|---|
| `msg-name` | error | `validateMessageName` | 1-32 chars, letters/digits/underscore, no leading digit. Over 32 chars is a warning if the file defines `SystemMessageLongSymbol` (extra) |
| `msg-name-duplicate` | error | `collectGlobalIssues` | Message names must be unique |
| `can-id` | error | `validateMessageCanId` | 11-bit IDs <= 0x7FF (an ID > 0x7FF without bit 31 set is a missing extended flag), 29-bit IDs <= 0x1FFFFFFF |
| `can-id-duplicate` | error | `collectGlobalIssues` | Same ID + same extendedness used twice. The CSS decoders stop on duplicate IDs within a channel |
| `pgn-duplicate` | error | `collectGlobalIssues` (J1939 rule changed) | J1939 = `ProtocolType "J1939"` (database, or message level), as the CSS decoders decide it - not `VFrameFormat` J1939PG (the DBC editor's basis; many non-J1939 DBCs carry it as default): two different 29-bit IDs with the same PGN (`extractPgn`). The CSS decoders treat this as a key collision and refuse the DBC |
| `dlc` | error | `validateDlc` | Length 0-1785 |
| `comment-length` | warning | `warnComment` | Message/signal comment > 255 chars |
| `sig-name` | error | `validateSignalName` | Same rules as message names |
| `sig-name-duplicate` | error | `validateSignalName` | Signal names unique within a message |
| `sig-start` | error | `validateSignalStart` | Start bit >= 0 |
| `sig-length` | error | `validateSignalLength` | 1-64 bits; IEEE float exactly 32, double exactly 64 (from `SIG_VALTYPE_`) |
| `sig-bit-fit` | error | `validateSignalBitFit` | All bits inside the message length (Motorola bits walked in sawtooth order) |
| `sig-factor` / `sig-offset` | error | `validateNumber` | Must be numbers |
| `sig-min-max` | error | `validateMin` / `validateMax` | Numbers; min <= max when both are non-zero (0 = "not set") |
| `sig-min-range` / `sig-max-range` | warning | `warnMin` / `warnMax` + `getDeclaredRange` | Non-zero min/max outside the representable physical range (+1 LSB slack; extra: 1e-5 relative slack for rounded numbers). Typical bug: max written as the raw max (65535) instead of the physical max |
| `val-range` | warning | extra (cantools) | Value table entry outside the signal's min/max (e.g. `2 "Outside"` with max 1). The CSS decoders write NULL for it, so that state never shows in the decoded data. Error/invalid labels (error, invalid, not available, fault, reserved, sensor open/short circuit, ...) are skipped: NULL is intended for them |
| `cantools-vframeformat` | warning | extra (cantools) | `VFrameFormat` defined without `BA_DEF_DEF_` default: cantools 45 cannot load the file (the CSS decoders can, e.g. the J1939-73 DM1 DBCs). Replaces the cantools-load errors for this case |
| `sig-float-sign` | warning | wire-format check | `SIG_VALTYPE_` float/double with `+` in the SG_ line (spec requires `-`). `--fix` rewrites it |
| `sg-colon-spacing` | error | `sgColonSpacingIssue` | `SG_ Name: 0|8...` without spaces around `:` - the CSS MF4 decoders silently fail to decode such signals. `--fix` rewrites it |
| `mux-range` | error | SG_MUL_VAL_ guard | Inverted `SG_MUL_VAL_` range (end < start); extra: multiplexor in SG_MUL_VAL_ is not a signal of the message; extra: `m<N>` value larger than the multiplexor can hold |
| `sig-overlap` | error | `findOverlapPairs` + `canSignalsCoexist` | Two signals share bits and can be active in the same frame. Mux-aware: signals with different values on a shared multiplexor may overlap (generalized to SG_MUL_VAL_ value ranges) |

## Layer 2 - extras

| Check | Severity | Rule |
|---|---|---|
| `parse-bo` / `parse-sg` | error | BO_/SG_ line the raw parser cannot read, or SG_ outside a BO_ block |
| `vframeformat` | error / warning | Explicit `BA_ "VFrameFormat"` value outside the enum (error); 29-bit ID with a Standard* format or 11-bit ID with Extended*/J1939PG (warning). The file default is not checked (CSS J1939 templates default to J1939PG) |
| `dlc` | warning | DLC 9-64 that is not a CAN FD length (12/16/20/24/32/48/64), DLC > 8 without a CAN FD VFrameFormat, DLC > 64 (multi-frame only). Skipped for J1939 files and messages with `TransportProtocolType` |
| `sig-factor` | warning | Factor 0 (signal always decodes to its offset) |
| `mux-structure` | error / warning | `m<N>` signals without any multiplexor (error); several `M` signals without SG_MUL_VAL_ (warning) |
| `ref-unknown` | warning | CM_/BA_/VAL_/SIG_VALTYPE_/SG_MUL_VAL_ referring to a message ID or signal that does not exist (VECTOR__INDEPENDENT_SIG_MSG is ignored) |

## Not checked (decide with data or the user)

- Whether min/max are realistic: the CSS decoders write NULL for values outside them, so a too-narrow range
  silently drops data - check on data ([plausibility.md](plausibility.md): NULL shares, values at the DBC max)
- DBC DLC vs the real frame length on the bus (the decoder skips messages whose DBC DLC exceeds the payload):
  compare with raw stats from canedge-raw-overview
- Scale, offset, sign, byte order correctness: On data ([plausibility.md](plausibility.md))
- Channel prefix of the file name (`can1-`, `can2-`, ...): see canedge-workspace conventions

## --fix (safe mechanical fixes only)

Writes to `--out` (never the input) and re-lints the result:

- `SG_` colon spacing -> ` : `
- `+` -> `-` on float/double signals

Everything else changes semantics and is left to the user/agent (edit the DBC text, or rebuild via
`dbc_build.py`, then prove the result with `dbc_diff.py`).
