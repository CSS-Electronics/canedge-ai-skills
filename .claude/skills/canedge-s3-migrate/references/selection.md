# canedge-s3-migrate: Which files get selected

## Objects

- Log files: `<device>/<session>/<split>[-<suffix>].<MF4|MFC|MFE|MFM>` (suffix ignored), device = 8 hex chars,
  session/split = 8 digits. Everything else (device.json, `config-XX.YY.json`, firmware, DBCs, data lake JSON
  files) is "other": Copied only with `--include-other`, never deleted (a device reads its config from the bucket)
- `--devices` / `--exclude-devices` filter by the device folder

## Logging time per file

| Source | Used |
|---|---|
| `Timestamp` metadata, `fw` >= 01.09.01 or `net` = `sd_manual` (file-created / first frame, UTC) | Yes, exact |
| `Timestamp` metadata, older or no `fw` | Yes, as +/-14 h (`approx-timestamp`) |
| `LastModified` (upload time), key suffix | Never as logging time. `LastModified` only as the upload rule below |
| The log file itself | Never read on S3 (MFC/MFE cannot be parsed anyway). SD-card data without metadata: Upload it with `canedge-s3-sd-upload` |

Why approximate before FW 01.09.01 (firmware changelog): The value was the upload time (< 01.02.03), then the
file MODIFIED time (~ file end, until 01.08.04), and local time from 01.03.01 until 01.08.05, also when it ends
with `Z` (01.08.01-01.08.05). Values are read as written (`Z` or not) and never corrected. The cutoff rule stays
safe with them: A start is always <= the value, so a later file only proves the cutoff if value + 14 h <= cutoff,
and wrong guesses only ever KEEP files.

Not trusted (flagged, ignored as evidence):
- `implausible`: Before 2019 or more than 1 day in the future (RTC never set or reset)
- `out-of-order`: Starts more than 1 h before an earlier file of the same device (recording order = session,
  then split). One wrong clock must not pull newer files into the archive

## Cutoff rule (`--before`)

A file is selected only if its data ends before the cutoff.

**Upload rule (first, no HEAD):** A file with `LastModified` (S3's clock) before the cutoff was uploaded before
it, so all its data was logged before it: Selected as `uploaded before cutoff`, whatever its `Timestamp` says
(missing, RTC not set, local time). This saves the HEAD request for most files of an archive cutoff. A
duplicate upload group is selected if any copy was uploaded before the cutoff, and these files also count as
evidence for the earlier files below. On copied/imported buckets (`LastModified` = copy time) the rule rarely
applies, and the `Timestamp` rule decides.

**Timestamp rule (files uploaded after the cutoff, HEADed):** A file's data ends before the next file of the same
device starts (within a session and across sessions), so: **Selected if any LATER file of the device (in
recording order) is known to start at or before the cutoff.** Duplicate uploads of one session/split (lost
upload confirmation, same file twice with different suffixes) are decided together.

Selected files: `uploaded before cutoff` or `ends before cutoff`. Kept files get a reason:

| Reason | Meaning |
|---|---|
| `newest file` | The device's last file, uploaded after the cutoff: No later file can prove that it ended |
| `after cutoff` | The file, or an earlier one, starts after the cutoff |
| `contains the cutoff` | Starts before the cutoff, but no later file proves it ended before it (e.g. the last split before a gap, or a session that ran past the cutoff) |
| `near cutoff (approximate timestamp)` | The +/-14 h window of an approximate timestamp contains the cutoff |
| `undetermined` | No usable `Timestamp` on the file or around it: Stays in the source |
| `source in GLACIER/DEEP_ARCHIVE (restore first)` | Cannot be copied without a restore |
| `larger than 5 GB (copy manually)` | Above the single-copy limit (not expected for CANedge files) |

The rule is conservative by design: At most one file per device near the cutoff stays in the source. Re-run
`plan` with a later cutoff in the future to move it.
