# Custom batch scripts on ota_lib (canedge-ota)

For jobs the CLI does not cover in one call (select devices by a rule, compute values per device, plan many
buckets), write a script in `projects/<p>/temp/` that imports `ota_lib` and ENDS WITH A PLAN. It never writes to S3:
The user reviews the plan, then `ota.py apply --plan DIR` does the writes with all re-checks.

```python
import sys
from pathlib import Path
sys.path.insert(0, ".claude/skills/canedge-ota/scripts")
import ota_lib as ol

P = Path("projects/2026-11-12-fleet-update")
b = ol.Bucket(env="my-bucket")                         # or Bucket(env=..., bucket="other-bucket")
rows = ol.fleet_status(b)                              # type, fw_ver, cfg_name, meta, heartbeat_days, ...
devices = [r["device"] for r in rows if r["type"] == "CANedge2" and ol.vkey(r["fw_ver"]) < ol.vkey("01.09.03")]

fws = ol.resolve_firmware(None, "01.09.03", {"CANedge2"})     # official package, MD5 checked
ch = ol.Changes(sets=["log.file.split_size=64"])              # same arguments as the CLI inputs
plan_dir = ol.make_plan(b, devices, ch, fws, P / "ota", label="ce2-0109")
print("PLAN", plan_dir)                                        # then show plan.md and ask for the OK
```

Building blocks:

| Function | Does |
|---|---|
| `Bucket(env, bucket)` | S3 access with a profile; `.devices()`, `.get(key)`, `.list(prefix)` (reads; `.put` is for apply only) |
| `device_state(b, dev)` | Fresh device.json, heartbeat time, folder files, current config bytes |
| `fleet_status(b, devices)` | The `status` rows |
| `fw_catalog(type)`, `fw_get(type, version)`, `fw_load(path)`, `fw_path_check(type, cur, new)` | Firmware packages + rules |
| `migrate_chain(cfg, type_code, dev, kpub, cur_rev, target_rev)` | Run the vendored migrations |
| `Changes(sets, partial, tables, config, keep, change_func, encrypt)` | Change inputs (`tables` = CSV paths; or set `ch.table = {dev: {path: value}}` directly) |
| `credentials_step(before, after, kpub, encrypt)` | Keep or re-encrypt credentials |
| `make_plan(b, devices, ch, fws, out_root, label)` | The plan (all gates + validation) |
| `load_plan(dir)`, `verify_plan(dir, b, target)` | Read a plan, check the result |

Per-device values computed in the script: Build `ch.table` in memory (`{"AABBCCDD": {"general.device.meta":
"Site1"}}`) instead of writing a CSV; secrets still come from a user-filled CSV (`ol.load_table(path)`), never from
the chat. Several buckets: One `make_plan` per bucket (one plan = one bucket).
