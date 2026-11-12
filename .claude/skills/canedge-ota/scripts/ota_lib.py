"""Over-the-air (OTA) updates of CANedge devices via their S3 bucket: The importable core of canedge-ota.

Every write follows plan -> user OK -> apply -> verify. Custom batch scripts import this module and end with
make_plan(); only `ota.py apply` writes to S3 (the harness asks before each call).

Built on the vendored canedge_manager (vendor/canedge_manager, see UPSTREAM.md): Its credential encryption
(CANEdgeSecurity), device types, firmware header parsing and configuration migration functions. The safety rules
follow the CANcloud OTA batch manager and the CANedge Docs (S3 management: COTA/FOTA; Firmware: mandatory steps).

  Bucket(env, bucket)              S3 access with a data lake profile (credentials/<name>.env)
  device_state(b, dev)             device.json + folder objects + the current config bytes, fresh from S3
  fw_catalog(type) / fw_get / fw_load / fw_path_check
  migrate_chain(...)               Chained config migrations (01.06 -> 01.07 -> 01.08 -> 01.09)
  Changes / apply_changes(...)     prepared config (+ keep-list), partial, --set, per-device table, change function
  credentials_step(...)            Keep ciphertext untouched, or re-encrypt ALL encrypted fields from plaintext
  make_plan / apply_plan / verify_plan / fleet_status
"""
from __future__ import annotations

import concurrent.futures as cf
import copy
import csv
import datetime as dt
import difflib
import hashlib
import importlib.util
import inspect
import json
import re
import sys
import urllib.request
import zipfile
from collections import OrderedDict
from pathlib import Path

HERE = Path(__file__).resolve().parent
SKILLS = HERE.parents[1]
VENDOR = HERE.parent / "vendor" / "canedge_manager"
for _p in (VENDOR, SKILLS / "canedge-workspace" / "scripts", SKILLS / "canedge-config" / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))
from canedge_manager import CANedge, CANEdgeSecurity, CANedgeType  # noqa: E402  (vendored)
from cfg_common import SCHEMA_ROOT, crc32_hex, mask_secrets  # noqa: E402
import check_config  # noqa: E402

DOCS = SKILLS / "canedge-knowledge" / "references" / "docs"
FW_DIR = Path("data/firmware")          # downloaded firmware packages (shared cache)

# device.json "type" -> the device names used by canedge-config (schema folders) and the Docs
TYPE_NAMES = {"0000001D": "CANedge1", "0000005D": "CANedge1 GNSS", "0000001F": "CANedge2",
              "0000005F": "CANedge2 GNSS", "0000003D": "CANedge3", "0000007D": "CANedge3 GNSS"}
DOC_FILES = {"CANedge1": "canedge1_llm.txt", "CANedge1 GNSS": "canedge1_gnss_llm.txt", "CANedge2": "canedge2_llm.txt",
             "CANedge2 GNSS": "canedge2_gnss_llm.txt", "CANedge3 GNSS": "canedge3_gnss_llm.txt"}
FW_HOST = "https://canlogger.csselectronics.com/"

# Vendored migration functions: (from MAJOR.MINOR, to MAJOR.MINOR) -> file in vendor/canedge_manager/config
MIGRATIONS = {("01.06", "01.07"): "config_func_01_06_XX_01_07_XX.py",
              ("01.07", "01.08"): "config_func_01_07_XX_01_08_XX.py",
              ("01.08", "01.09"): "config_func_01_08_XX_01_09_XX.py"}

# CANedge Docs "Download firmware" > "Mandatory update steps" within 01.06+ (CE1/CE2 steps end at 01.04; CE1G/CE2G
# have none) - same table as CANcloud otaBatch/fwPaths.js. Re-check when the Docs add a step
MANDATORY_FW_STEPS = {"CANedge3 GNSS": ["01.07.03", "01.08.05", "01.09.02"]}
NO_DOWNGRADE = {"CANedge3 GNSS"}
FW_NOTES = {("CANedge3 GNSS", "01.09.02"):
            "01.09.02 is a ~50 MB update incl. the cellular module: ~1 h, power must not be interrupted. The Docs "
            "recommend updating it from the SD card; if remote, the device must run 01.08.05 or 01.09.01 first"}

DEVICE_RE = re.compile(r"^[0-9A-F]{8}$")
PUT_RE = re.compile(r"^[0-9A-F]{8}/(config-\d{2}\.\d{2}\.json|firmware\.bin)$")   # the only keys apply writes
STALE_DAYS = 7
# Prepared configs (--config): These paths are taken from each device's current config (per-device values)
DEFAULT_KEEP = ["general.device.meta", "general.security", "log.encryption", "connect"]
# Encrypted form max length per credential section (schema maxLength, verified 01.07-01.09, config-editor-tools)
ENCRYPTED_MAX = {"log": 108, "wifi": 64, "s3": 150, "cellular": 32, "webserver": 64}


class OtaError(Exception):
    pass


def utc_now() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def stamp() -> str:
    return utc_now().strftime("%Y%m%dT%H%M%SZ")


def rev_of(name: str | None) -> str | None:
    """'config-01.09.json' / 'schema-01.09.json' / '01.09.03' -> '01.09'."""
    m = re.search(r"(\d{2}\.\d{2})", name or "")
    return m.group(1) if m else None


def vkey(v: str) -> tuple:
    return tuple(int(x) for x in re.findall(r"\d+", v or ""))


def load_json(raw: bytes):
    return json.loads(raw.decode("utf-8-sig"), object_pairs_hook=OrderedDict)


def dump_config(cfg) -> bytes:
    """Same layout as canedge_manager / CANcloud (2-space indent). The device reports the CRC32 of these bytes."""
    return json.dumps(cfg, indent=2).encode("utf-8")


# ---------------------------------------------------------------- JSON paths ("a.b[0].c")
def parse_path(s: str) -> tuple:
    out = []
    for part in s.strip().split("."):
        m = re.fullmatch(r"([^\[\]]+)((?:\[\d+\])*)", part)
        if not m:
            raise OtaError(f"Bad path '{s}' (use a.b.c or a.list[0].b)")
        out.append(m.group(1))
        out += [int(i) for i in re.findall(r"\[(\d+)\]", m.group(2))]
    return tuple(out)


def path_str(p) -> str:
    s = ""
    for k in p:
        s += f"[{k}]" if isinstance(k, int) else (("." if s else "") + k)
    return s


_MISSING = object()


def get_path(d, p, default=None):
    cur = d
    for k in p:
        if isinstance(k, int):
            if not isinstance(cur, list) or k >= len(cur):
                return default
        elif not isinstance(cur, dict) or k not in cur:
            return default
        cur = cur[k]
    return cur


def has_path(d, p) -> bool:
    return get_path(d, p, _MISSING) is not _MISSING


def set_path(d, p, value) -> None:
    cur = d
    for i, k in enumerate(p[:-1]):
        nxt = p[i + 1]
        if isinstance(k, int):
            if not isinstance(cur, list) or k >= len(cur):
                raise OtaError(f"{path_str(p)}: list index {k} does not exist")
        elif k not in cur or not isinstance(cur[k], (dict, list)):
            cur[k] = [] if isinstance(nxt, int) else OrderedDict()
        cur = cur[k]
    last = p[-1]
    if isinstance(last, int):
        if not isinstance(cur, list) or last > len(cur):
            raise OtaError(f"{path_str(p)}: list index {last} does not exist")
        if last == len(cur):
            cur.append(value)
        else:
            cur[last] = value
    else:
        cur[last] = value


def coerce(raw: str, current):
    """A --set / table cell value, typed like the value it replaces (meta '123' stays a string)."""
    if not isinstance(raw, str):
        return raw
    if isinstance(current, bool):
        return raw.strip().lower() in ("1", "true", "yes")
    try:
        if isinstance(current, int):
            return int(raw.strip(), 0)
        if isinstance(current, float):
            return float(raw)
    except ValueError:
        raise OtaError(f"'{raw}' is not a number (the field holds {current!r})") from None
    if isinstance(current, str):
        return raw
    try:
        return json.loads(raw, object_pairs_hook=OrderedDict)
    except ValueError:
        return raw


def deep_merge(base, patch):
    """Partial config merge as in CANcloud: Objects merge recursively, arrays and values are replaced."""
    if not isinstance(base, dict) or not isinstance(patch, dict):
        return copy.deepcopy(patch)
    out = copy.deepcopy(base)
    for k, v in patch.items():
        out[k] = deep_merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def find_kpub(obj, path="") -> list[str]:
    """Paths of non-empty kpub values (device-specific: Never part of a shared change)."""
    out = []
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{path}.{k}" if path else k
            if k == "kpub" and isinstance(v, str) and v:
                out.append(p)
            out += find_kpub(v, p)
    elif isinstance(obj, list):
        for i, v in enumerate(obj):
            out += find_kpub(v, f"{path}[{i}]")
    return out


# ---------------------------------------------------------------- S3
class Bucket:
    """One input bucket via a data lake profile (env.py). Reads + the two allowlisted writes of apply."""

    def __init__(self, env: str | None = None, bucket: str | None = None, s3=None):
        if s3 is not None:                        # tests: a fake client
            self.s3, self.name, self.env, self.cloud = s3, bucket, env, "s3"
            return
        from env import aws_client, lake, require_s3, use_env
        use_env(env)
        require_s3()
        info = lake()
        self.s3, self.env, self.cloud = aws_client("s3"), env, info["cloud"]
        self.name = bucket or info["input_bucket"]

    @staticmethod
    def _missing(e) -> bool:
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", ""))
        return code in ("404", "NoSuchKey", "NotFound")

    def get(self, key: str) -> tuple[bytes, dict] | tuple[None, None]:
        try:
            r = self.s3.get_object(Bucket=self.name, Key=key)
        except Exception as e:  # noqa: BLE001
            if self._missing(e):
                return None, None
            raise
        return r["Body"].read(), {"modified": r.get("LastModified")}

    def head(self, key: str) -> dict | None:
        try:
            return self.s3.head_object(Bucket=self.name, Key=key)
        except Exception as e:  # noqa: BLE001
            if self._missing(e):
                return None
            raise

    def put(self, key: str, data: bytes) -> int:
        """PUT one allowlisted object, then HEAD it: Returns the size S3 reports (None = gone already)."""
        if not PUT_RE.match(key):
            raise OtaError(f"Refusing to write {key}: Only <device>/config-VV.VV.json and <device>/firmware.bin")
        ctype = "application/json" if key.endswith(".json") else "application/octet-stream"
        self.s3.put_object(Bucket=self.name, Key=key, Body=data,
                           **({} if self.cloud == "google" else {"ContentType": ctype}))
        h = self.head(key)
        return h["ContentLength"] if h else None

    def list(self, prefix: str = "", delimiter: str = "/") -> tuple[list[str], list[dict]]:
        dirs, objs = [], []
        for page in self.s3.get_paginator("list_objects_v2").paginate(Bucket=self.name, Prefix=prefix,
                                                                       Delimiter=delimiter):
            dirs += [p["Prefix"][len(prefix):].rstrip("/") for p in page.get("CommonPrefixes", [])]
            objs += page.get("Contents", [])
        return dirs, objs

    def devices(self) -> list[str]:
        return sorted(d for d in self.list()[0] if DEVICE_RE.match(d))


def device_state(b: Bucket, dev: str) -> dict:
    """Fresh from S3: device.json (+ its LastModified = last heartbeat), the folder's root objects and the bytes of
    the config the device.json names."""
    _, objs = b.list(f"{dev}/")
    files = {o["Key"].split("/", 1)[1]: o for o in objs}
    raw, info = b.get(f"{dev}/device.json")
    dj = None
    if raw is not None:
        try:
            dj = json.loads(raw.decode("utf-8-sig"))
        except ValueError:
            dj = {"_invalid": True}
    cfg_name = (dj or {}).get("cfg_name")
    cfg_raw = b.get(f"{dev}/{cfg_name}")[0] if cfg_name else None
    hb = info["modified"] if info else None
    return {"id": dev, "device_json": dj, "heartbeat": hb, "files": files, "cfg_name": cfg_name, "cfg_raw": cfg_raw,
            "firmware_pending": "firmware.bin" in files}


def heartbeat_age_days(hb) -> float | None:
    if hb is None:
        return None
    if isinstance(hb, str):
        hb = dt.datetime.fromisoformat(hb.replace("Z", "+00:00"))
    return (utc_now() - hb).total_seconds() / 86400


def gate_device(st: dict) -> tuple[list[str], list[str]]:
    """(blocks, warnings) from device.json alone (CANcloud deviceBaseGates)."""
    dj, dev = st["device_json"], st["id"]
    if dj is None:
        return ["No device.json in the device folder (the device never connected, or not a device folder)"], []
    if dj.get("_invalid"):
        return ["device.json is not valid JSON"], []
    blocks, warns = [], []
    if dj.get("id") != dev:
        blocks.append(f"device.json id {dj.get('id')} does not match its folder {dev} (cloned SD card?)")
    if dj.get("type") not in TYPE_NAMES:
        blocks.append(f"Unknown device type {dj.get('type')!r} (CANmod and other devices are not supported)")
    revs = {rev_of(dj.get("cfg_name")), rev_of(dj.get("sch_name")), dj.get("cfg_ver"), rev_of(dj.get("fw_ver"))}
    if len(revs) != 1 or None in revs:
        blocks.append(f"device.json is inconsistent (fw_ver {dj.get('fw_ver')}, cfg_name {dj.get('cfg_name')}, "
                      f"sch_name {dj.get('sch_name')}, cfg_ver {dj.get('cfg_ver')}): The device may be mid-update")
    if st["cfg_name"] and st["cfg_raw"] is None:
        blocks.append(f"{st['cfg_name']} is not in the device folder (the device uploads it on its next connect)")
    age = heartbeat_age_days(st["heartbeat"])
    if age is not None and age > STALE_DAYS:
        warns.append(f"Last heartbeat {age:.0f} days ago: The device picks up the change when it connects again")
    if st["cfg_raw"] is not None and dj.get("cfg_crc32") and crc32_hex(st["cfg_raw"]) != dj["cfg_crc32"].upper():
        warns.append(f"The device has not loaded the current {st['cfg_name']} in S3 yet (device CRC "
                     f"{dj['cfg_crc32']}, S3 CRC {crc32_hex(st['cfg_raw'])}): A pending change gets replaced")
    return blocks, warns


# ---------------------------------------------------------------- firmware
def fw_catalog(type_name: str) -> list[dict]:
    """Firmware packages of a device type from the bundled CANedge Docs: [{version, url, md5}], newest first."""
    f = DOCS / DOC_FILES.get(type_name, "")
    if type_name not in DOC_FILES or not f.is_file():
        return []
    out, inside = [], False
    for line in f.read_text(encoding="utf-8").splitlines():
        if line.startswith("## ") and "firmware packages" in line:
            inside = True
            continue
        if inside and line.startswith("#"):
            break
        m = re.match(r"\* \[firmware-(\d{2}\.\d{2}\.\d{2})\.zip\]\((\S+)\) \(MD5: `([0-9a-f]{32})`\)", line.strip())
        if inside and m:
            out.append({"version": m.group(1), "url": m.group(2), "md5": m.group(3)})
    return sorted(out, key=lambda x: vkey(x["version"]), reverse=True)


def fw_get(type_name: str, version: str, log=print) -> Path:
    """The official firmware package (cached in data/firmware/<type>/), downloaded from the Docs link + MD5 checked."""
    entry = next((e for e in fw_catalog(type_name) if e["version"] == version), None)
    if entry is None:
        avail = ", ".join(e["version"] for e in fw_catalog(type_name)) or "none"
        raise OtaError(f"No firmware {version} for {type_name} in the bundled Docs (available: {avail}). A newer "
                       "firmware: Download the zip from the CANedge Docs and pass it with --fw")
    if not entry["url"].startswith(FW_HOST):
        raise OtaError(f"Unexpected firmware URL {entry['url']}")
    path = FW_DIR / type_name.replace(" ", "_") / f"firmware-{version}.zip"
    if path.is_file() and hashlib.md5(path.read_bytes()).hexdigest() == entry["md5"]:
        return path
    log(f"Downloading {type_name} firmware {version} ...")
    with urllib.request.urlopen(entry["url"], timeout=120) as r:   # noqa: S310 (fixed https host)
        data = r.read()
    if hashlib.md5(data).hexdigest() != entry["md5"]:
        raise OtaError(f"MD5 mismatch for {entry['url']} - not used")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def fw_load(path: str | Path) -> dict:
    """A firmware zip or .bin: Type + version from the header, the embedded schema and default config."""
    path = Path(path)
    data = path.read_bytes()
    if zipfile.is_zipfile(path):
        with zipfile.ZipFile(path) as z:
            bins = [n for n in z.namelist() if n.lower().endswith(".bin")]
            if len(bins) != 1:
                raise OtaError(f"{path.name}: Expected one .bin in the zip, found {len(bins)}")
            data = z.read(bins[0])
        # the .bin apply uploads: In the shared cache (never next to a user's zip)
        bin_path = FW_DIR / "bin" / f"{path.stem}-{hashlib.sha256(data).hexdigest()[:12]}.bin"
        if not bin_path.is_file():
            bin_path.parent.mkdir(parents=True, exist_ok=True)
            bin_path.write_bytes(data)
    else:
        bin_path = path
    try:
        p = CANedge._CANedge__parse_fw_bin(data)   # vendored header parser (staticmethod)
    except AssertionError as e:
        raise OtaError(f"{path.name} is not a CANedge firmware file ({e})") from None
    code = p["type"].value
    return {"path": bin_path.as_posix(), "source": path.as_posix(), "type": code, "type_name": TYPE_NAMES.get(code),
            "version": p["fw_ver"], "rev": rev_of(p["fw_ver"]), "bytes": len(data),
            "sha256": hashlib.sha256(data).hexdigest(), "schema": json.loads(p["sch"]),
            "default_config": json.loads(p["cfg"], object_pairs_hook=OrderedDict)}


def fw_path_check(type_name: str, cur: str, new: str) -> str | None:
    """Why updating cur -> new is not allowed (mandatory step skipped, no-downgrade device), else None."""
    if not re.fullmatch(r"\d{2}\.\d{2}\.\d{2}", cur or ""):
        return f"The device reports no valid firmware version ({cur!r})"
    if type_name in NO_DOWNGRADE and vkey(new) < vkey(cur):
        return f"{type_name} firmware cannot be downgraded ({cur} -> {new})"
    for step in MANDATORY_FW_STEPS.get(type_name, []):
        if vkey(cur) < vkey(step) < vkey(new):
            return (f"Mandatory update step: Update this {type_name} to {step} first ({cur} -> {step}), then plan "
                    f"the next step after verify shows it applied")
    return None


_DEFAULTS: dict = {}


def default_config(type_name: str, rev: str, fw: dict | None = None, log=print):
    """The default config of firmware MAJOR.MINOR rev (the migration functions' config_new): From the target firmware
    when it matches, else the newest official package of that rev (downloaded)."""
    if fw and fw["rev"] == rev and fw["type_name"] == type_name:
        return copy.deepcopy(fw["default_config"])
    if (type_name, rev) not in _DEFAULTS:
        cands = [e for e in fw_catalog(type_name) if rev_of(e["version"]) == rev]
        if not cands:
            raise OtaError(f"No {type_name} firmware {rev}.XX in the Docs to take the default config from")
        _DEFAULTS[(type_name, rev)] = fw_load(fw_get(type_name, cands[0]["version"], log))["default_config"]
    return copy.deepcopy(_DEFAULTS[(type_name, rev)])


# ---------------------------------------------------------------- migration
class LazyTools:
    """canedge_manager's CANedgeTools, created on first use (a device without kpub still migrates)."""

    def __init__(self, kpub: str):
        self._kpub, self._sec = kpub, None

    @property
    def security(self) -> CANEdgeSecurity:
        if self._sec is None:
            if not self._kpub:
                raise OtaError("The device.json has no kpub (public key): Cannot encrypt")
            self._sec = CANEdgeSecurity(self._kpub)
        return self._sec


def load_func(path: Path, name: str = "config_func"):
    spec = importlib.util.spec_from_file_location(f"ota_func_{path.stem}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    if not hasattr(mod, name):
        raise OtaError(f"{path} has no function {name}()")
    return getattr(mod, name)


def migration_hops(cur: str, target: str) -> list[tuple[str, str]]:
    hops, rev = [], cur
    while rev != target:
        nxt = next((b for (a, b) in MIGRATIONS if a == rev), None)
        if nxt is None or vkey(nxt) > vkey(target):
            raise OtaError(f"No configuration migration from {rev} towards {target} (vendored: "
                           f"{', '.join(f'{a}->{b}' for a, b in MIGRATIONS)}). A new hop needs a migration function "
                           "in vendor/canedge_manager/config (see references/change-inputs.md)")
        hops.append((rev, nxt))
        rev = nxt
    return hops


def migrate_chain(cfg, type_code: str, dev: str, kpub: str, cur: str, target: str, index: int = 0,
                  fw: dict | None = None, log=print):
    """Run the vendored config_func_* hops cur -> target. Returns (config, [hop names])."""
    if vkey(target) < vkey(cur):
        raise OtaError(f"Config migration back from {cur} to {target} is not supported")
    names = []
    for a, b in migration_hops(cur, target):
        fn = load_func(VENDOR / "config" / MIGRATIONS[(a, b)])
        new_default = default_config(TYPE_NAMES[type_code], b, fw, log)
        try:
            out = fn(LazyTools(kpub), index, CANedgeType(type_code), dev, copy.deepcopy(cfg), new_default)
        except OtaError:
            raise
        except Exception as ex:  # noqa: BLE001 - a vendored function that does not handle this config
            raise OtaError(f"Migration {a} -> {b} ({MIGRATIONS[(a, b)]}) failed on this config: "
                           f"{ex.__class__.__name__}: {ex}") from None
        if not isinstance(out, dict):
            raise OtaError(f"Migration {a} -> {b} returned no config")
        cfg = out
        names.append(f"{a}->{b}")
    return cfg, names


# ---------------------------------------------------------------- change inputs
def load_table(path: str | Path) -> dict[str, dict[str, str]]:
    """CSV: First column = device ID, other columns = JSON paths (e.g. connect.s3.server.bucket). Empty cell = no
    change. Several tables (e.g. devices.csv + secrets.csv) merge per device."""
    with open(path, newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    if not rows:
        return {}
    head = [h.strip() for h in rows[0]]
    for h in head[1:]:
        p = parse_path(h)
        if p[-1] == "kpub":
            raise OtaError(f"{path}: kpub is device-specific and set by the encryption step, not by a table")
    out = {}
    for r in rows[1:]:
        if not r or not r[0].strip():
            continue
        dev = r[0].strip().upper()
        if not DEVICE_RE.match(dev):
            raise OtaError(f"{path}: '{r[0]}' is not a device ID (8 hex characters)")
        out.setdefault(dev, {}).update({h: v for h, v in zip(head[1:], r[1:]) if v != ""})
    return out


class Changes:
    """What to change, applied per device in this order: prepared config (+ keep-list) -> partial -> --set ->
    table row -> change function."""

    def __init__(self, sets: list[str] | None = None, partial: str | None = None, tables: list[str] | None = None,
                 config: str | None = None, keep: list[str] | None = None, change_func: str | None = None,
                 encrypt: bool = False):
        self.sets = []
        for s in sets or []:
            if "=" not in s:
                raise OtaError(f"--set {s}: Use path=value")
            k, v = s.split("=", 1)
            p = parse_path(k)
            if p[-1] == "kpub":
                raise OtaError("kpub is device-specific and set by the encryption step")
            self.sets.append((p, v))
        self.partial = None
        if partial:
            self.partial = json.loads(Path(partial).read_text(encoding="utf-8-sig"), object_pairs_hook=OrderedDict)
            if find_kpub(self.partial):
                raise OtaError(f"{partial} contains a kpub ({', '.join(find_kpub(self.partial))}): Encrypted values "
                               "are device-specific - give plain values and use --encrypt / keyformat 1")
        self.table = {}
        for t in tables or []:
            for dev, row in load_table(t).items():
                self.table.setdefault(dev, {}).update(row)
        self.config = self.config_name = None
        if config:
            self.config = json.loads(Path(config).read_text(encoding="utf-8-sig"), object_pairs_hook=OrderedDict)
            self.config_name = Path(config).name
        self.keep = [parse_path(k) for k in (keep if keep is not None else DEFAULT_KEEP)]
        self.func_path = change_func
        self.func = load_func(Path(change_func)) if change_func else None
        self.encrypt = encrypt
        self.sources = {"set": sets or [], "partial": partial, "tables": tables or [], "config": config,
                        "keep": [path_str(k) for k in self.keep] if config else None, "change_func": change_func,
                        "encrypt": encrypt}

    def empty(self) -> bool:
        return not (self.sets or self.partial or self.table or self.config or self.func or self.encrypt)


def apply_changes(ch: Changes, cfg, dev: str, type_code: str, kpub: str, rev: str, index: int, fw: dict | None,
                  log=print):
    """(new config, notes, func_set_kpub). cfg = the device's config after migration (not modified)."""
    notes, out = [], copy.deepcopy(cfg)
    if ch.config is not None:
        from cfg_common import detect_device_type
        if detect_device_type(ch.config) != TYPE_NAMES[type_code]:
            raise OtaError(f"--config {ch.config_name} is a {detect_device_type(ch.config)} config, the device a "
                           f"{TYPE_NAMES[type_code]}")
        crev = rev_of(ch.config_name)
        if crev and crev != rev:
            raise OtaError(f"--config {ch.config_name} is for {crev}, the device's target config is {rev}")
        base = copy.deepcopy(ch.config)
        for p in ch.keep:
            if has_path(cfg, p):
                set_path(base, p, copy.deepcopy(get_path(cfg, p)))
        out = base
    if ch.partial is not None:
        out = deep_merge(out, ch.partial)
    for p, v in ch.sets:
        set_path(out, p, coerce(v, get_path(out, p)))
    row = ch.table.get(dev)
    for k, v in (row or {}).items():
        p = parse_path(k)
        set_path(out, p, coerce(v, get_path(out, p)))
    func_kpub = False
    if ch.func is not None:
        kw = {"row": dict(row or {})} if "row" in inspect.signature(ch.func).parameters else {}
        before_kpub = get_path(out, ("general", "security", "kpub"))
        # config_new: The target firmware's default config (as in canedge_manager), else a copy of the config
        new_default = copy.deepcopy(fw["default_config"]) if fw and fw["rev"] == rev else copy.deepcopy(out)
        try:
            res = ch.func(LazyTools(kpub), index, CANedgeType(type_code), dev, copy.deepcopy(out), new_default, **kw)
        except OtaError:
            raise
        except Exception as ex:  # noqa: BLE001
            raise OtaError(f"Change function failed: {ex.__class__.__name__}: {ex}") from None
        if res is None:
            notes.append("Change function returned None: No change for this device")
        else:
            out = res
            func_kpub = get_path(out, ("general", "security", "kpub")) != before_kpub
    return out, notes, func_kpub


# ---------------------------------------------------------------- credentials
def cred_sections(cfg) -> list[dict]:
    """The credential sections (same map as the config editor's encryption tool / CANcloud, verified 01.07-01.09):
    One keyformat flag + the password field(s) it governs. Only sections in use."""
    out = []
    log_enc = get_path(cfg, ("log", "encryption")) or {}
    if isinstance(log_enc, dict) and log_enc.get("state") == 1:
        out.append({"id": "log", "flag": ("log", "encryption", "keyformat"), "enc": 1, "plain": 0,
                    "values": [("log", "encryption", "pwd")]})
    aps = get_path(cfg, ("connect", "wifi", "accesspoint"))
    if isinstance(aps, list) and aps:
        out.append({"id": "wifi", "flag": ("connect", "wifi", "keyformat"), "enc": 1, "plain": 0,
                    "values": [("connect", "wifi", "accesspoint", i, "pwd") for i in range(len(aps))]})
    pin = get_path(cfg, ("connect", "cellular", "pin"))
    if isinstance(pin, str) and pin:           # a blank PIN cannot be encrypted (schema length): Out of scope
        out.append({"id": "cellular", "flag": ("connect", "cellular", "keyformat"), "enc": 1, "plain": 0,
                    "values": [("connect", "cellular", "pin")]})
    if isinstance(get_path(cfg, ("connect", "s3", "server")), dict):
        out.append({"id": "s3", "flag": ("connect", "s3", "server", "keyformat"), "enc": 1, "plain": 0,
                    "values": [("connect", "s3", "server", "secretkey")]})
    if get_path(cfg, ("connect", "webserver", "security")) in (1, 2):
        out.append({"id": "webserver", "flag": ("connect", "webserver", "security"), "enc": 2, "plain": 1,
                    "values": [("connect", "webserver", "password")]})
    for s in out:
        s["encrypted"] = get_path(cfg, s["flag"]) == s["enc"]
    return out


def credentials_step(before, after, kpub: str, encrypt: bool = False, func_owns_kpub: bool = False):
    """Make the credentials of `after` consistent. before = the device's config after migration (its ciphertext is
    still valid). Values that differ from `before` are plaintext supplied by the change inputs.

    - Nothing credential-related changed (and no --encrypt): Ciphertext + kpub stay byte for byte
    - Otherwise, if any section ends up encrypted: A new key pair, so EVERY encrypted section is re-encrypted from
      plaintext. A field that is encrypted on the device and has no plaintext in the change inputs blocks the device
    Returns (config, blocks, notes)."""
    if func_owns_kpub:
        return after, [], ["The change function set a new kpub: It handled the encryption itself (not re-checked)"]
    blocks, notes = [], []
    prev = {s["id"]: s for s in cred_sections(before)}
    secs = cred_sections(after)
    plain_vals = {}
    for s in secs:
        p = prev.get(s["id"])
        s["was_enc"] = bool(p and p["encrypted"])
        s["target_enc"] = True if encrypt else s["encrypted"]
        s["changed"] = False
        for vp in s["values"]:
            va, vb = get_path(after, vp), get_path(before, vp, _MISSING)
            if vb is _MISSING or va != vb:          # supplied by the change inputs = plaintext
                s["changed"] = True
                plain_vals[vp] = va
            elif not s["was_enc"]:
                plain_vals[vp] = va
    out = copy.deepcopy(after)
    for s in secs:                                 # sections that end up plain must hold plaintext
        if not s["target_enc"]:
            for vp in s["values"]:
                if vp not in plain_vals:
                    blocks.append(f"{path_str(vp)} is set to plain but holds the device's encrypted value: Give "
                                  "the plaintext in the secrets table")
    enc_secs = [s for s in secs if s["target_enc"]]
    need_key = any(s["changed"] or not s["was_enc"] for s in enc_secs)
    if not need_key:                               # encrypted sections untouched: ciphertext + kpub stay as they are
        if get_path(after, ("general", "security", "kpub")) != get_path(before, ("general", "security", "kpub")):
            blocks.append("general.security.kpub changed without re-encrypting the credentials")
        return out, blocks, notes
    missing = [vp for s in enc_secs for vp in s["values"] if vp not in plain_vals]
    if missing:
        blocks.append("Re-encryption needs the plaintext of every encrypted field (one key for all), missing: "
                      + ", ".join(path_str(vp) for vp in missing) + " - add them as columns in the secrets table")
        return after, blocks, notes
    if not kpub or len(kpub) != 88:
        blocks.append("The device.json has no valid kpub (device public key): Cannot encrypt")
        return after, blocks, notes
    sec = CANEdgeSecurity(kpub)
    for s in enc_secs:
        for vp in s["values"]:
            val = plain_vals[vp]
            val = "" if val is None else str(val)
            enc = sec.encrypt_encode(val)
            if len(enc) > ENCRYPTED_MAX[s["id"]]:
                blocks.append(f"{path_str(vp)}: Too long to encrypt ({len(val)} characters)")
            set_path(out, vp, enc)
        set_path(out, s["flag"], s["enc"])
    set_path(out, ("general", "security", "kpub"), sec.user_public_key_base64)
    notes.append("Encrypted with a new key: " + ", ".join(s["id"] for s in enc_secs))
    return out, blocks, notes


# ---------------------------------------------------------------- validation
def validate(path: Path, schema: dict | None, schema_source: str) -> dict:
    """canedge-config check_config (bundled schema + reject/warning checks) + the target schema (firmware-embedded or
    the device's own schema file)."""
    res, cfg = check_config.validate(path)
    errors = [f"schema ({res.get('schema_file') or 'bundled'}): {e}" for e in res["schema_errors"]]
    errors += [e["message"] for e in res["errors"]]
    if not res["json_valid"]:
        errors += res["notes"]
    if schema is not None and cfg is not None:
        errors += [f"schema ({schema_source}): {e}" for e in check_config.validate_schema(cfg, schema)]
    elif not res["schema_checked"]:
        errors.append("No schema to validate against (" + "; ".join(res["notes"]) + ")")
    seen, uniq = set(), []
    for e in errors:                       # the same error from both schemas once
        k = e.split("): ", 1)[-1]
        if k not in seen:
            seen.add(k)
            uniq.append(e)
    return {"errors": uniq, "warnings": [w["message"] for w in res["warnings"]],
            "schemas": [s for s in (res.get("schema_file"), schema_source if schema is not None else None) if s]}


def masked_diff(old, new, a: str, b: str, max_lines: int = 120) -> list[str]:
    x = json.dumps(mask_secrets(old), indent=2).splitlines() if old is not None else []
    y = json.dumps(mask_secrets(new), indent=2).splitlines()
    lines = list(difflib.unified_diff(x, y, a, b, lineterm="", n=1))
    return lines[:max_lines] + ([f"... ({len(lines) - max_lines} more diff lines)"] if len(lines) > max_lines else [])


S3_FIELDS = ("endpoint", "port", "bucket", "region", "accesskey")


def s3_target(cfg) -> dict | None:
    srv = get_path(cfg, ("connect", "s3", "server"))
    return {k: srv.get(k) for k in S3_FIELDS} if isinstance(srv, dict) else None


def check_new_bucket(srv: dict, secret: str | None, dev: str, cfg_name: str) -> tuple[list[str], list[str]]:
    """The device's new S3 server + keys: GetObject <dev>/<cfg_name> with exactly those keys (nothing is written).
    NoSuchKey = keys + bucket OK, folder clean. An existing config = leftover the device would apply -> block."""
    if not secret:
        return [], ["New S3 server not checked: The plaintext secret key is not in the change inputs"]
    import boto3
    from botocore.config import Config
    ep = str(srv.get("endpoint") or "")
    port = srv.get("port")
    if port and not re.search(r":\d+$", ep) and str(port) not in ("80", "443"):
        ep = f"{ep}:{port}"
    try:
        s3 = boto3.client("s3", aws_access_key_id=srv.get("accesskey"), aws_secret_access_key=secret,
                          region_name=srv.get("region") or None, endpoint_url=ep or None,
                          config=Config(connect_timeout=10, read_timeout=20, retries={"max_attempts": 1},
                                        s3={"addressing_style": "path"}))
        s3.get_object(Bucket=srv.get("bucket"), Key=f"{dev}/{cfg_name}")
    except Exception as e:  # noqa: BLE001
        code = str(getattr(e, "response", {}).get("Error", {}).get("Code", "")) or e.__class__.__name__
        if code in ("NoSuchKey", "404"):
            return [], []
        if code in ("AccessDenied", "403"):
            return [], ["New S3 server: Access denied on a test read (the keys may lack s3:ListBucket/GetObject, "
                        "which hides whether the folder is empty) - check the keys work before a canary"]
        return [f"New S3 server check failed ({code}): The device would lose its connection - fix the endpoint, "
                "bucket or keys"], []
    return [f"New bucket already holds {dev}/{cfg_name}: The device would apply that config after the move - "
            "remove it first (the user) or re-plan"], []


# ---------------------------------------------------------------- plan
def plan_device(b: Bucket, dev: str, ch: Changes, fws: dict, index: int, out_dir: Path,
                check_targets: bool = True, log=print) -> dict:
    st = device_state(b, dev)
    dj = st["device_json"] or {}
    e = {"device": dev, "type": TYPE_NAMES.get(dj.get("type")), "fw_ver": dj.get("fw_ver"),
         "cfg_name": st["cfg_name"], "meta": dj.get("log_meta"), "heartbeat": st["heartbeat"].isoformat()
         if hasattr(st["heartbeat"], "isoformat") else st["heartbeat"], "status": "write", "blocks": [],
         "warnings": [], "notes": [], "writes": [], "expect": {}, "diff": [], "hops": [], "firmware": None,
         "moves_to": None}
    blocks, warns = gate_device(st)
    e["blocks"] += blocks
    e["warnings"] += warns
    if blocks:
        e["status"] = "blocked"
        return e
    type_code, type_name, cur_rev = dj["type"], TYPE_NAMES[dj["type"]], rev_of(st["cfg_name"])
    released = [x["version"] for x in fw_catalog(type_name)]
    if released and dj.get("fw_ver") not in released:
        e["warnings"].append(f"Firmware {dj.get('fw_ver')} is not a released {type_name} version (internal/beta "
                             "build?): Its config may not follow the official schema")
    e["expect"] = {st["cfg_name"]: crc32_hex(st["cfg_raw"])}
    fw = fws.get(type_name)
    if fws and fw is None:
        e["status"] = "skip"
        e["notes"].append(f"No firmware given for {type_name}")
        return e
    target_rev = fw["rev"] if fw else cur_rev
    fw_write = False
    if fw:
        if fw["version"] == dj.get("fw_ver"):
            e["notes"].append(f"Already on firmware {fw['version']}")
        else:
            why = fw_path_check(type_name, dj.get("fw_ver"), fw["version"])
            if why:
                e["blocks"].append(why)
            if vkey(fw["version"]) < vkey(dj.get("fw_ver")):
                e["warnings"].append(f"Firmware downgrade {dj.get('fw_ver')} -> {fw['version']}")
            if vkey(target_rev) < vkey(cur_rev):
                e["blocks"].append(f"Downgrade to {target_rev} needs a {target_rev} config (not migrated back)")
            if st["firmware_pending"]:
                e["blocks"].append("A firmware.bin is already pending in the device folder (an update in progress)")
            if (type_name, fw["version"]) in FW_NOTES:
                e["warnings"].append(FW_NOTES[(type_name, fw["version"])])
            fw_write = True
    elif st["firmware_pending"]:
        e["warnings"].append("A firmware.bin is pending in the device folder (an update in progress)")
    if e["blocks"]:
        e["status"] = "blocked"
        return e
    kpub = dj.get("kpub") or ""
    try:
        orig = load_json(st["cfg_raw"])
    except ValueError as ex:
        e["blocks"].append(f"{st['cfg_name']} in S3 is not valid JSON ({ex})")
        e["status"] = "blocked"
        return e
    try:
        migrated, e["hops"] = migrate_chain(orig, type_code, dev, kpub, cur_rev, target_rev, index, fw, log) \
            if target_rev != cur_rev else (copy.deepcopy(orig), [])
        new, notes, func_kpub = apply_changes(ch, migrated, dev, type_code, kpub, target_rev, index, fw, log)
        e["notes"] += notes
        new, cblocks, cnotes = credentials_step(migrated, new, kpub, ch.encrypt, func_kpub)
    except OtaError as ex:
        e["blocks"].append(str(ex))
        e["status"] = "blocked"
        return e
    e["blocks"] += cblocks
    e["notes"] += cnotes
    if cblocks:                     # the config is incomplete: Validating it would only add follow-on errors
        e["status"] = "blocked"
        return e
    new_name = f"config-{target_rev}.json"
    cfg_write = target_rev != cur_rev or new != orig
    ddir = out_dir / dev
    ddir.mkdir(parents=True, exist_ok=True)
    (ddir / f"before-{st['cfg_name']}").write_bytes(st["cfg_raw"])
    if cfg_write:
        data = dump_config(new)
        p = ddir / new_name
        p.write_bytes(data)
        if new_name != st["cfg_name"]:
            old_target = b.get(f"{dev}/{new_name}")[0]
            e["expect"][new_name] = crc32_hex(old_target) if old_target is not None else None
            if old_target is not None:
                e["warnings"].append(f"{new_name} already exists in the device folder (an earlier attempt?): "
                                     "It gets replaced")
        schema, src = (fw["schema"], f"firmware {fw['version']}") if fw else (None, "")
        if schema is None:
            sraw = b.get(f"{dev}/schema-{target_rev}.json")[0]
            if sraw is not None:
                schema, src = json.loads(sraw.decode("utf-8-sig")), f"device {dev}/schema-{target_rev}.json"
        v = validate(p, schema, src)
        e["blocks"] += v["errors"]
        before_res, _ = check_config.validate(ddir / f"before-{st['cfg_name']}")
        existing = {w["message"] for w in before_res["warnings"]}
        e["warnings"] += [w for w in v["warnings"] if w not in existing]   # only what this change introduces
        e["existing_warnings"] = [w for w in v["warnings"] if w in existing]
        if e["existing_warnings"]:
            e["notes"].append(f"{len(e['existing_warnings'])} config warning(s) already in the current config "
                              "(unchanged, in plan.json existing_warnings)")
        e["schemas"] = v["schemas"]
        e["writes"].append({"key": f"{dev}/{new_name}", "file": p.as_posix(), "bytes": len(data),
                            "crc32": crc32_hex(data)})
        e["diff"] = masked_diff(orig, new, f"s3/{dev}/{st['cfg_name']}", f"new/{dev}/{new_name}")
        if (get_path(orig, ("general", "device", "meta")) != get_path(new, ("general", "device", "meta"))):
            e["notes"].append(f"Meta {get_path(orig, ('general', 'device', 'meta'))!r} -> "
                              f"{get_path(new, ('general', 'device', 'meta'))!r} (log file headers + device.json "
                              "log_meta from the next session)")
        t0, t1 = s3_target(orig), s3_target(new)
        if t1 and t0 != t1:
            e["moves_to"] = {k: t1[k] for k in ("endpoint", "port", "bucket", "region")}
            old = f"{t0.get('endpoint')}/{t0.get('bucket')}" if t0 else "none"
            e["warnings"].append(f"S3 server change {old} -> {t1['endpoint']}/{t1['bucket']}: A wrong value "
                                 "disconnects the device (only the SD card recovers it) - canary first")
            if check_targets:
                secret = (ch.table.get(dev) or {}).get("connect.s3.server.secretkey")
                if secret is None and get_path(new, ("connect", "s3", "server", "keyformat")) == 0:
                    secret = get_path(new, ("connect", "s3", "server", "secretkey"))
                bl, wa = check_new_bucket(t1, secret, dev, new_name)
                e["blocks"] += bl
                e["warnings"] += wa
    if fw_write:
        e["firmware"] = {"version": fw["version"], "path": fw["path"], "sha256": fw["sha256"], "bytes": fw["bytes"]}
        e["writes"].append({"key": f"{dev}/firmware.bin", "file": fw["path"], "bytes": fw["bytes"],
                            "sha256": fw["sha256"]})
        e["expect"]["firmware.bin"] = None
    if e["blocks"]:
        e["status"] = "blocked"
    elif not e["writes"]:
        e["status"] = "no change"
    return e


def make_plan(b: Bucket, devices: list[str], ch: Changes, fws: dict | None, out_root: Path, label: str = "",
              check_targets: bool = True, log=print) -> Path:
    """Plan an update for the devices (nothing is written to S3). fws = {type name: fw_load(...)}. Writes
    <out_root>/<utc>-<label>/plan.json + plan.md + per device the before/after configs. Returns the plan folder."""
    fws = fws or {}
    pid = stamp() + (f"-{re.sub(r'[^a-z0-9]+', '-', label.lower()).strip('-')}" if label else "")
    out = Path(out_root) / pid
    out.mkdir(parents=True, exist_ok=False)
    entries = []
    for i, dev in enumerate(devices):
        log(f"  planning {dev} ({i + 1}/{len(devices)})")
        entries.append(plan_device(b, dev, ch, fws, i, out, check_targets, log))
    plan = {"plan": pid, "created": utc_now().isoformat(timespec="seconds"), "bucket": b.name, "env": b.env,
            "changes": ch.sources,
            "firmware": {k: {x: v[x] for x in ("version", "type_name", "path", "sha256", "bytes")}
                         for k, v in fws.items()},
            "summary": {s: sum(e["status"] == s for e in entries) for s in ("write", "no change", "blocked", "skip")},
            "devices": entries}
    (out / "plan.json").write_text(json.dumps(plan, indent=2, default=str), encoding="utf-8")
    (out / "plan.md").write_text(plan_markdown(plan), encoding="utf-8")
    return out


def plan_markdown(plan: dict) -> str:
    s = plan["summary"]
    fw = ", ".join(f"{k} {v['version']}" for k, v in plan["firmware"].items()) or "none"
    lines = [f"# OTA plan {plan['plan']}", "", f"Bucket: `{plan['bucket']}` | Firmware: {fw} | "
             f"Write: {s['write']}, no change: {s['no change']}, blocked: {s['blocked']}, skipped: {s['skip']}", "",
             "| Device | Type | FW | Meta | Status | Writes | Reason / warnings |", "|---|---|---|---|---|---|---|"]
    for e in plan["devices"]:
        why = "; ".join(e["blocks"] or e["warnings"])[:300]
        lines.append(f"| {e['device']} | {e['type'] or ''} | {e['fw_ver'] or ''} | {e['meta'] or ''} | {e['status']}"
                     f" | {', '.join(w['key'].split('/', 1)[1] for w in e['writes'])} | {why} |")
    for e in plan["devices"]:
        if e["diff"] or e["blocks"] or e["warnings"] or e["notes"]:
            lines += ["", f"## {e['device']} ({e['status']})"]
            lines += [f"- BLOCKED: {x}" for x in e["blocks"]] + [f"- Warning: {x}" for x in e["warnings"]]
            lines += [f"- Note: {x}" for x in e["notes"]] + ([f"- Migration: {', '.join(e['hops'])}"] if e["hops"]
                                                              else [])
            if e["diff"]:
                lines += ["", "```diff", *e["diff"], "```"]
    return "\n".join(lines) + "\n"


def load_plan(plan_dir: Path) -> dict:
    p = Path(plan_dir) / "plan.json"
    if not p.is_file():
        raise OtaError(f"No plan.json in {plan_dir}")
    return json.loads(p.read_text(encoding="utf-8"))


def applied_log(plan_dir: Path) -> dict[str, dict]:
    p = Path(plan_dir) / "apply-log.jsonl"
    out = {}
    if p.is_file():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("result") == "applied":
                    out[r["device"]] = r
    return out


# ---------------------------------------------------------------- apply
def apply_device(b: Bucket, e: dict) -> dict:
    """Re-check, then PUT config before firmware. Never deletes."""
    dev = e["device"]
    r = {"device": dev, "utc": utc_now().isoformat(timespec="seconds"), "written": []}
    st = device_state(b, dev)
    dj = st["device_json"] or {}
    if dj.get("fw_ver") != e["fw_ver"] or st["cfg_name"] != e["cfg_name"]:
        return {**r, "result": "skipped", "reason": f"device changed since the plan (now fw {dj.get('fw_ver')}, "
                                                    f"{st['cfg_name']}): Re-plan"}
    for name, crc in e["expect"].items():
        if name == "firmware.bin":
            if st["firmware_pending"]:
                return {**r, "result": "skipped", "reason": "a firmware.bin is pending in the device folder"}
            continue
        raw = b.get(f"{dev}/{name}")[0]
        now = crc32_hex(raw) if raw is not None else None
        if now != crc:
            return {**r, "result": "skipped", "reason": f"{name} changed in S3 since the plan (CRC {crc} -> {now}): "
                                                        "Re-plan"}
    for w in e["writes"]:
        data = Path(w["file"]).read_bytes()
        if "sha256" in w and hashlib.sha256(data).hexdigest() != w["sha256"]:
            return {**r, "result": "failed", "reason": f"{w['file']} differs from the planned firmware"}
        if "crc32" in w and crc32_hex(data) != w["crc32"]:
            return {**r, "result": "failed", "reason": f"{w['file']} was edited after the plan: Re-plan"}
        size = b.put(w["key"], data)
        r["written"].append(w["key"])
        if size is not None and size != len(data):
            return {**r, "result": "failed", "reason": f"{w['key']}: S3 reports {size} bytes, uploaded {len(data)}"}
    return {**r, "result": "applied"}


def apply_plan(plan_dir: Path, b: Bucket, devices: list[str] | None = None, limit: int | None = None,
               log=print) -> list[dict]:
    plan = load_plan(plan_dir)
    if b.name != plan["bucket"]:
        raise OtaError(f"The plan is for bucket {plan['bucket']}, the profile points to {b.name}")
    done = applied_log(plan_dir)
    todo = [e for e in plan["devices"] if e["status"] == "write" and e["device"] not in done
            and (not devices or e["device"] in devices)]
    if limit is not None:
        todo = todo[:limit]
    results = []
    with open(Path(plan_dir) / "apply-log.jsonl", "a", encoding="utf-8") as f:
        for e in todo:
            try:
                r = apply_device(b, e)
            except Exception as ex:  # noqa: BLE001
                r = {"device": e["device"], "utc": utc_now().isoformat(timespec="seconds"), "result": "failed",
                     "reason": f"{ex.__class__.__name__}: {ex}"}
            f.write(json.dumps(r) + "\n")
            f.flush()
            log(f"  {r['device']}: {r['result']}" + (f" ({r['reason']})" if r.get("reason") else ""))
            results.append(r)
    return results


# ---------------------------------------------------------------- verify / status
def verify_plan(plan_dir: Path, b: Bucket, target: Bucket | None = None) -> list[dict]:
    """Per applied device: applied (device.json shows the new config CRC / firmware), pending or failed."""
    plan = load_plan(plan_dir)
    done = applied_log(plan_dir)
    out = []
    for e in plan["devices"]:
        if e["device"] not in done:
            if e["status"] == "write":
                out.append({"device": e["device"], "state": "not applied", "detail": ""})
            continue
        t_apply = dt.datetime.fromisoformat(done[e["device"]]["utc"])
        src = target if (e["moves_to"] and target) else b
        if e["moves_to"] and target is None:
            st0 = device_state(b, e["device"])
            hb = st0["heartbeat"]
            moved = hb is not None and hb < t_apply
            out.append({"device": e["device"], "state": "check new bucket",
                        "detail": f"moves to {e['moves_to']['bucket']}: Pass --target-env <profile of that bucket>"
                                  + ("" if moved else " (still sends heartbeats to the old bucket)")})
            continue
        st = device_state(src, e["device"])
        dj, hb = st["device_json"] or {}, st["heartbeat"]
        cfg_w = next((w for w in e["writes"] if w["key"].endswith(".json")), None)
        checks = []
        if cfg_w:
            checks.append(("config", (dj.get("cfg_crc32") or "").upper() == cfg_w["crc32"]))
        if e["firmware"]:
            checks.append(("firmware", dj.get("fw_ver") == e["firmware"]["version"]))
        fresh = hb is not None and hb > t_apply
        if all(ok for _, ok in checks) and dj:
            state, detail = "applied", f"heartbeat {hb.isoformat(timespec='seconds') if hb else '?'}"
        elif not fresh:
            state = "pending"
            detail = "no heartbeat since apply" + (" (firmware.bin still waiting)" if st["firmware_pending"] else "")
        elif e["firmware"] and not st["firmware_pending"] and dj.get("fw_ver") != e["firmware"]["version"]:
            state, detail = "failed?", (f"firmware.bin was downloaded but the device still runs {dj.get('fw_ver')} "
                                        "(update aborted? the device deletes firmware.bin also then) - wait for the "
                                        "next heartbeat, else check the config and upload firmware again")
        else:
            state = "pending"
            detail = ", ".join(f"{n} not yet" for n, ok in checks if not ok) + (
                " (firmware.bin still waiting)" if st["firmware_pending"] else "")
        out.append({"device": e["device"], "state": state, "detail": detail, "fw_ver": dj.get("fw_ver"),
                    "cfg_crc32": dj.get("cfg_crc32")})
    return out


def fleet_status(b: Bucket, devices: list[str] | None = None, workers: int = 8) -> list[dict]:
    devices = devices or b.devices()

    def one(dev):
        st = device_state(b, dev)
        dj = st["device_json"] or {}
        sync = None
        if st["cfg_raw"] is not None and dj.get("cfg_crc32"):
            sync = crc32_hex(st["cfg_raw"]) == dj["cfg_crc32"].upper()
        age = heartbeat_age_days(st["heartbeat"])
        blocks, _ = gate_device(st)
        others = sorted(n for n in st["files"] if re.match(r"config-\d{2}\.\d{2}\.json$", n) and n != st["cfg_name"])
        enc = None
        if st["cfg_raw"] is not None:
            try:
                enc = sorted(s["id"] for s in cred_sections(load_json(st["cfg_raw"])) if s["encrypted"])
            except ValueError:
                pass
        return {"device": dev, "type": TYPE_NAMES.get(dj.get("type"), dj.get("type")), "fw_ver": dj.get("fw_ver"),
                "cfg_name": st["cfg_name"], "meta": dj.get("log_meta"), "cfg_synced": sync,
                "heartbeat_days": round(age, 1) if age is not None else None,
                "firmware_pending": st["firmware_pending"], "other_configs": others, "encrypted": enc,
                "blocked": blocks}

    with cf.ThreadPoolExecutor(workers) as ex:
        return list(ex.map(one, devices))


def resolve_firmware(fw_files: list[str] | None, fw_version: str | None, type_names: set[str], log=print) -> dict:
    """{type name: fw_load(...)} from --fw files and/or --fw-version (official package per device type)."""
    out = {}
    for f in fw_files or []:
        fw = fw_load(f)
        if fw["type_name"] in out:
            raise OtaError(f"Two firmware files for {fw['type_name']}")
        out[fw["type_name"]] = fw
    if fw_version:
        for t in sorted(type_names - set(out)):
            out[t] = fw_load(fw_get(t, fw_version, log))
    return out
