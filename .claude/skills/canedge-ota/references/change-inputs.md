# Change inputs (canedge-ota plan)

Applied per device in this order, after the automatic config migration (new firmware MAJOR.MINOR) and before the
credential step and validation:

| Input | Use | Notes |
|---|---|---|
| `--config FILE` (+ `--keep PATH ...`) | A full config prepared with canedge-config, for all selected devices | Must be the device type + target version (`config-VV.VV.json`). The keep paths come from each device's current config: Default `general.device.meta`, `general.security`, `log.encryption`, `connect` (connection, credentials, meta stay per device) |
| `--partial FILE` | The same partial JSON for all devices | Objects merge, arrays replace (a filter list is replaced as a whole). No `kpub` |
| `--set PATH=VALUE` | One field for all devices | Typed like the current value (`--set log.file.split_size=64`); a new field: JSON (`true`, `5`, `"x"`) |
| `--table CSV` | Per-device values | First column the device ID, other columns JSON paths. Empty cell = no change. Several tables merge (devices.csv + secrets.csv) |
| `--change-func FILE.py` | Logic no table expresses | canedge_manager signature, see below |
| `--encrypt` | Encrypt every credential section | Needs the plaintext of fields that are encrypted already |

Paths: `a.b.c`, list items `a.list[0].b` (the item must exist; `[n]` with n = length appends). Examples:
`general.device.meta`, `connect.s3.server.bucket`, `connect.s3.server.endpoint`, `connect.wifi.accesspoint[0].pwd`,
`can_1.phy.bit_rate_std`, `log.file.split_size`. Look the field up in the schema / Docs before using it (canedge-config,
canedge-knowledge `[Configuration]:`).

Values supplied by any input are treated as plaintext by the credential step (encrypted on upload where the
section's `keyformat` is 1).

## Which input when

- A config optimized with canedge-config for a group of devices: `--config` (validated + delivered as is, with the
  per-device fields kept; no need to turn it into Python)
- A few fields on many devices: `--set` / `--partial`
- Different values per device (bucket moves, meta names, APNs): `--table`
- Conditional or structural logic (e.g. "enable filter X only where CAN2 is used"): `--change-func`
- A schema migration (new MAJOR.MINOR): Automatic (vendored functions). A missing hop: Write
  `vendor/canedge_manager/config/config_func_<a>_XX_<b>_XX.py` (same signature) and add it to `MIGRATIONS` in
  `ota_lib.py`; prefer upstream's when available

## Change function

```python
from collections import OrderedDict


def config_func(tools, index, device_type, device_id, config_old: OrderedDict, config_new: OrderedDict, row=None):
    """tools.security: canedge_manager encryption (rarely needed: the credential step encrypts plaintext values).
    index: The device's position in the plan. device_type: canedge_manager CANedgeType. config_old: The device's
    config (migrated + earlier inputs applied), a copy. config_new: The target firmware's default config (or a copy
    of config_old). row: The device's --table row as {path: value} (optional parameter).
    Return the new config, or None for no change."""
    cfg = config_old
    if row and row.get("site"):
        cfg["general"]["device"]["meta"] = f"{row['site']};{device_id[-4:]}"
    return cfg
```

Keep it pure (no S3, no network): The plan validates its output for every device. If the function sets a new
`general.security.kpub` itself (its own encryption), the credential step is skipped for that device.
