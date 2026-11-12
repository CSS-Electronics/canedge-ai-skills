# Vendored: canedge_manager

Source: https://github.com/CSS-Electronics/canedge_manager, commit `8f18a77` "Fix migration crash on filters without
a name" (branch `fix/migration-unnamed-filters`, on top of master `881616e`, CANedge module version 00.00.04). The
fix is a pull request to master; once merged, master and this copy are the same. MIT License (LICENSE).

Copied unchanged (line endings converted to LF):

- `canedge_manager.py`: `CANEdgeSecurity` (credential encryption), `CANedgeType`, `CANedgeTools` and the firmware
  header parsing (`CANedge._CANedge__parse_fw_bin`). The `CANedge` class itself is not used by the skill (it needs
  the old firmware file, does one migration step and writes without checks); `ota_lib.py` does the orchestration.
- `config/config_func_01_06_XX_01_07_XX.py`, `config_func_01_07_XX_01_08_XX.py`, `config_func_01_08_XX_01_09_XX.py`:
  The configuration migration functions, chained by `ota_lib.migrate_chain()`.

Left out: The CLI (`canedge_manager_cli.py`), `servers.json`, the migration functions before 01.06 and `test/` (it
needs a MinIO server). The migration fixtures from upstream's `test/test_01_08/` and `test/test_01_09/` are in
`tests/fixtures/ota/` and run in `tests/ota_tests.py`.

To update (e.g. after the pull request is merged, or for a new firmware MAJOR.MINOR):

1. Copy the files above from upstream master, converting line endings to LF, plus any new `config_func_<old>_XX_<new>_XX.py`
   (add it to `MIGRATIONS` in `ota_lib.py`) and its upstream test fixtures into `tests/fixtures/ota/`
2. Update the commit in "Source" above
3. Run `tests/ota_tests.py`

Once canedge_manager is pip-installable, replace this folder with the package (`ota_lib.py` imports
`canedge_manager` and the `config_func_*` files from here).
