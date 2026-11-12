# OTA rules (canedge-ota)

Sources: CANedge Docs `[S3 management]:` (COTA, FOTA, server certificates), `[Download firmware]:` (mandatory
steps, packages), `### Security` (credential encryption) in `canedge-knowledge/references/docs/`; the CANcloud OTA
batch manager (same gates).

## How the device picks up changes

- The device checks its S3 folder at the `connect.s3.sync.ota` interval and writes `device.json` (the heartbeat) at
  `connect.s3.sync.heartbeat`. `device.json` reports `fw_ver`, `cfg_name`, `cfg_crc32` (CRC32 of the config file
  bytes it loaded), `kpub` (its public key) and `log_meta`
- **Config**: A changed `<dev>/config-VV.VV.json` (same name as `cfg_name`) is downloaded and applied; the device
  reboots. Without a config in S3 the device uploads its own
- **Firmware, same MAJOR.MINOR** (01.09.01 -> 01.09.03): `firmware.bin` only, the config stays
- **Firmware, new MAJOR.MINOR** (01.08 -> 01.09): `config-<new>.json` must be in the folder BEFORE `firmware.bin`,
  else the update aborts. The device deletes `firmware.bin` after the download, also when it aborts: After an abort,
  check the config and upload the firmware again (re-plan)
- After a firmware update the device uploads the new `schema-VV.VV.json`

## Gates (plan blocks the device)

- `device.json` missing, invalid, id not matching its folder (a cloned SD card), unknown type
- `fw_ver`, `cfg_name`, `sch_name`, `cfg_ver` disagree on MAJOR.MINOR: The device may be mid-update
- The config named in `device.json` is not in the folder
- Firmware: Other device type (skipped, not blocked), a skipped mandatory step, a CE3G downgrade, a config downgrade
  (no backward migration), a `firmware.bin` already pending
- No migration hop to the target MAJOR.MINOR
- Validation errors: The target schema (firmware-embedded, else the device's own `schema-VV.VV.json`) + the bundled
  canedge-config schema and reject checks
- Credentials: A needed plaintext missing, a value too long to encrypt, no device `kpub`
- S3 server change: The new server/keys fail a test read, or the new bucket already holds a config for the device

Warnings (not blocking): No heartbeat for 7+ days, the device has not loaded the current S3 config yet (a pending
change gets replaced), canedge-config warnings (e.g. TLS/port mismatch, missing APN), firmware notes, a firmware
downgrade, an existing `config-<new>.json` that gets replaced.

## Mandatory firmware steps

From the Docs (01.06+): CANedge3 GNSS 01.07.03 (GNSS module), 01.08.05 (improved download), 01.09.02 (cellular
module, ~50 MB, ~1 h, SD card recommended; remote only after 01.08.05 / 01.09.01). CE3G cannot be downgraded.
CANedge1/2 steps end at 01.04; CE1G/CE2G have none. When the Docs add a step: Update `MANDATORY_FW_STEPS` in
`ota_lib.py` (and the CANcloud table `otaBatch/fwPaths.js`).

## Credential encryption

- Sections (each one flag + its value fields): `log.encryption` (pwd, when `state` 1), `connect.wifi` (every
  `accesspoint[i].pwd`), `connect.cellular` (pin, when not blank), `connect.s3.server` (secretkey),
  `connect.webserver` (password; `security` 1 plain / 2 encrypted). Flag `keyformat` 0 plain / 1 encrypted
- Scheme (vendored `CANEdgeSecurity`): A new server key pair per encryption, ECDH (P-256) with the device `kpub`,
  HMAC-SHA256("config") -> AES-128-CTR per field, base64(iv + ciphertext). The server public key goes into
  `general.security.kpub`; all encrypted fields of a config share it
- Consequences:
  - Changes that do not touch an encrypted section keep its ciphertext and `kpub` byte for byte (meta, filters,
    firmware migrations): No plaintext needed
  - Changing or adding an encrypted field needs a new key pair, so EVERY encrypted section is re-encrypted from
    plaintext. Values given in the change inputs are plaintext; encrypted fields on the device without a plaintext
    block the device. Plain sections next to encrypted ones can change without re-encryption
  - A shared partial or `--set` can never carry a `kpub` or ciphertext (device-specific)
- Plaintext secrets live in a CSV in the project (`projects/` is gitignored) that the user fills in; plan files
  hold the uploaded configs (encrypted where the config is encrypted) and the `before-*` configs

## Canary and rollout

1 device first (`apply --limit 1`), `verify` until `applied`, then the rest. Large fleets: In waves
(`--limit 10`, verify, ...). A device that stays `pending` for long is offline or cannot reach the bucket: It applies
the change when it connects. `failed?` = `firmware.bin` was taken but `fw_ver` did not change: Wait one more
heartbeat, then check the config (`canedge-troubleshoot`), re-plan and apply again.

Rollback of a config: The plan folder holds each device's `before-config-VV.VV.json`: Plan `--config` with it for
that device (same version) or ask the user to restore it.
