---
name: canedge-ota
description: >
  Update CANedge2/3 devices over the air via their S3 bucket, safely and in batches: Firmware (official packages,
  mandatory steps, automatic config migration), config changes on one or many devices (a field, a prepared config,
  per-device values, a change function), credential encryption, moving devices to another bucket/server. Every write
  is plan -> OK -> apply -> verify. Use for "update device X to firmware 01.09", "push this config to my devices",
  "change X on all devices", "encrypt the passwords", "move these devices to a new bucket", "did my devices take the
  update". Writing the config itself: canedge-config; bucket root control files: canedge-lake-admin.
---

# canedge-ota

Writes `<device>/config-VV.VV.json` and `<device>/firmware.bin` the way the device expects them (Docs
`[S3 management]:` COTA/FOTA). Built on the vendored [canedge_manager](vendor/canedge_manager/UPSTREAM.md)
(encryption, firmware parsing, config migrations). Needs a profile whose keys can write the input bucket. Not for
CANedge1 (no S3) or CANmod. Shared rules: [conventions.md](../canedge-workspace/references/conventions.md).

## Commands

`.venv\Scripts\python.exe .claude/skills/canedge-ota/scripts/ota.py [--env NAME] [--bucket NAME] [--json] <cmd>`:

| Command | Does |
|---|---|
| `status [--devices ...]` | Type, firmware, config + loaded (CRC), heartbeat age, meta, encrypted sections, pending firmware |
| `fw list --type T` / `fw get --type T --version V` / `fw info FILE` | Official packages from the bundled Docs (MD5 checked, `data/firmware/`) |
| `plan --project P (--devices ... \| --all \| --table T) [inputs] [--fw-version V \| --fw FILE] --label L` | Gates, migration, changes, encryption, validation, masked diff, new-server check. Writes `P/ota/<utc>-<label>/` only (exit 1 = some blocked) |
| `apply --plan DIR [--limit N]` | The only S3 write: Re-checks each device (device.json, CRC drift, pending firmware), config before firmware, never deletes, skips applied devices |
| `verify --plan DIR [--target-env NAME]` | applied / pending / failed? / not applied, from device.json |

Inputs (combinable): `--config FILE` (prepared config; each device keeps meta, security, log encryption, connect),
`--partial FILE`, `--set PATH=VALUE`, `--table CSV` (device ID + JSON path columns), `--change-func FILE.py`,
`--encrypt`. Details: [change-inputs.md](references/change-inputs.md). Custom batch scripts:
[custom-scripts.md](references/custom-scripts.md). Rules + background: [ota-rules.md](references/ota-rules.md).

## Workflow

1. `status`; mention offline devices, devices not in sync, blocks
2. `plan` in a project. Report blocked devices with their reason; fix the input and re-plan, never work around a gate
3. Show the user the summary table, one representative diff and the warnings; ask for an explicit OK
4. Canary: `apply --limit 1`, `verify` after the next heartbeat until `applied`, then `apply` the rest and `verify`
5. Note the plan folder + result in `project.md`

- **Firmware**: One step per plan; a new MAJOR.MINOR migrates the config automatically. CE3G mandatory steps
  (01.07.03, 01.08.05, 01.09.02) are enforced; for 01.09.02 tell the user the Docs recommend the SD card
- **Credentials**: Changing an encrypted credential needs the plaintext of all encrypted fields: Write a
  `P/ota/secrets.csv` template, the user fills it in, pass it with `--table`. Never ask for, print or read secrets
- **Bucket move**: [bucket-migration.md](references/bucket-migration.md)

## Rules

- S3 writes only via `apply` on a plan the user OKed; no deletes; no edits to plan files (apply refuses them)
- Canary first; stop the rollout on a `failed?` device. Wrong S3/WiFi settings disconnect a device until the SD card
  fixes it: Say so before such changes
- `general.device.meta`: Letters, digits, `;`, spaces, max 30
- A blocked migration (missing hop, or a vendored function failing on a config): Tell the user; fixes go upstream
  (canedge_manager) or into a tested local function (change-inputs.md)

## Related

`canedge-config` (write/check the config), `canedge-lake-admin` (bucket root; lake setup after a move),
`canedge-s3` / `canedge-s3-overview` (read the bucket, fleet firmware/sync view), `canedge-s3-migrate` (old log
files after a move), `canedge-troubleshoot` (device not uploading), `canedge-knowledge` (`[S3 management]:`,
`[Download firmware]:`, `### Security`)
