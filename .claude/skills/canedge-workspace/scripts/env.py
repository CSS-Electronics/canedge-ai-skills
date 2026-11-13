"""Credentials for all canedge-* scripts: One profile per data lake, credentials/<name>.env (written by setup_env.py).

A profile is one CANedge data lake (or one bucket): The S3 input bucket, its S3 output bucket (<input>-parquet) and
the lake's query service. The variables are cloud-neutral:

  CANEDGE_CLOUD                 aws | google | s3 (any S3-compatible server, e.g. MinIO or the Flexify gateway of an
                                Azure input container) | azure (the Synapse SQL login of an Azure data lake only)
  CANEDGE_ACCESS_KEY_ID         S3 access key: AWS = the data lake stack's programmatic access keys (S3 buckets,
  CANEDGE_SECRET_ACCESS_KEY     Athena, Glue jobs, logs) or the device keys; Google/s3 = the device keys (HMAC)
  CANEDGE_INPUT_BUCKET          the S3 input bucket (profile name = this bucket)
  CANEDGE_REGION                derived (AWS: the bucket's region)
  CANEDGE_LAKE_ID               derived (the deployment's unique ID -> Athena database/workgroup, log view)
  CANEDGE_S3_ENDPOINT           Google device keys: https://storage.googleapis.com; s3: the server URL; AWS: empty
  CANEDGE_GOOGLE_KEY_FILE       Google: a service account JSON key (copied into credentials/), used for the buckets
                                (gcs_s3.py, instead of S3 keys) and BigQuery - normally the data lake's programmatic
                                access key <id>-programmatic-access-account.json
  CANEDGE_KEY_TYPE              derived (KEY_TYPES: programmatic, athena, device, google-bigquery, ...) -> what the
                                profile allows (ACCESS); empty in old profiles = not checked
  optional: CANEDGE_BIGQUERY_DATASET, CANEDGE_BIGQUERY_PROJECT
  Azure (CANEDGE_CLOUD=azure): CANEDGE_SYNAPSE_ENDPOINT (<workspace>-ondemand.sql.azuresynapse.net),
  CANEDGE_SYNAPSE_DATABASE, CANEDGE_SYNAPSE_USER (default sqlreaderuser, the read-only login),
  CANEDGE_SYNAPSE_PASSWORD. The input
  container is a separate s3 profile (its Flexify endpoint + keys)

Everything else is derived by lake() (the naming rules of the CANedge deployments). There is no root .env: All
settings live in the profiles.

Profile selection: --env NAME (use_env) = credentials/NAME.env; without it the only profile is used. Values are
loaded as environment variables (existing ones win) and never printed. Scripts in other skills import this module:

  sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
  from env import aws_client, lake, require_s3, use_env
  use_env(args.env)          # --env NAME -> credentials/NAME.env
  require_s3()               # clear error if the profile is missing/incomplete (athena=True: also needs an AWS lake)
  s3 = aws_client("s3")
  lake()["output_bucket"]    # <input>-parquet

Grafana profiles are no data lake: credentials/grafana/<name>.env (GRAFANA_URL, GRAFANA_TOKEN + detected info),
written by setup_env.py --grafana, loaded with grafana_profile(name) (the only one, or --grafana-env NAME).
"""
from __future__ import annotations

import json
import os
from pathlib import Path

CRED_DIR = Path("credentials")   # profiles (*.env) + BigQuery service account keys (*.json), gitignored
KEY_DIR = CRED_DIR
SETUP = ".claude/skills/canedge-workspace/scripts/setup_env.py"   # run with the project .venv python
KEYS = ["CANEDGE_ACCESS_KEY_ID", "CANEDGE_SECRET_ACCESS_KEY"]
PROFILE_KEYS = ["CANEDGE_CLOUD", *KEYS, "CANEDGE_INPUT_BUCKET", "CANEDGE_REGION", "CANEDGE_LAKE_ID",
                "CANEDGE_S3_ENDPOINT", "CANEDGE_GOOGLE_KEY_FILE", "CANEDGE_KEY_TYPE"]
SYNAPSE_KEYS = ["CANEDGE_SYNAPSE_ENDPOINT", "CANEDGE_SYNAPSE_DATABASE", "CANEDGE_SYNAPSE_USER",
                "CANEDGE_SYNAPSE_PASSWORD"]
SYNAPSE_ADMIN_USER = "sqladminuser"   # the SQL admin login the Azure deployment creates
SYNAPSE_READER_USER = "sqlreaderuser"   # the read-only login (deployments from 2026-11, created by the mapping job)
# Key types (detected by setup_env.py) and what they allow: s3 = the buckets (S3 skills), lake = Athena/BigQuery
# queries, admin = function/job logs + Glue job runs (canedge-lake-admin). Types not in ACCESS are not checked
KEY_TYPES = {"programmatic": "the data lake stack's programmatic access keys: S3 input/output bucket + Athena "
                             "(+ function/job logs and Glue job runs on stacks with ProgrammaticAccessPolicy)",
             "athena": "the Athena user's keys: Athena queries only (no S3 skills)",
             "device": "the device keys: S3 skills on the input bucket (Athena queries need the data lake "
                       "stack's programmatic access keys)",
             "other": "keys of another IAM user: S3 skills as far as its permissions allow",
             "google": "the device keys: S3 skills on the input bucket (BigQuery queries need the data lake's "
                       "programmatic access key file)",
             "s3": "S3-compatible server keys: S3 skills on the bucket",
             "google-programmatic": "the data lake's programmatic access key: input/output bucket, BigQuery, "
                                    "function logs",
             "google-bigquery": "the BigQuery user's key: BigQuery queries + reading the buckets",
             "google-other": "a service account key: buckets and BigQuery as far as its permissions allow",
             "azure": "the Synapse SQL login: Azure data lake queries only (the input container: a separate "
                      "S3-compatible profile with the Flexify endpoint + keys)"}
ACCESS = {"programmatic": {"s3", "lake", "admin"}, "google-programmatic": {"s3", "lake", "admin"},
          "athena": {"lake"}, "google-bigquery": {"s3", "lake"},
          "device": {"s3"}, "google": {"s3"}, "s3": {"s3"}, "azure": {"lake"}}
NEEDS = {"s3": "bucket access (the device keys or the data lake's programmatic access keys)",
         "lake": "data lake queries (the programmatic access keys, or the Athena / BigQuery user's keys, or the "
                 "Synapse SQL login)",
         "admin": "data lake admin access (the data lake's programmatic access keys)"}
GRAFANA_DIR = CRED_DIR / "grafana"   # Grafana profiles (not data lakes, so not in profiles())
GRAFANA_SETUP = f"{SETUP} --grafana"
GRAFANA_KEYS = ["GRAFANA_URL", "GRAFANA_TOKEN", "GRAFANA_ORG", "GRAFANA_VERSION", "GRAFANA_ROLE", "GRAFANA_ACCESS"]
GOOGLE_ENDPOINT = "https://storage.googleapis.com"
# Storage request prices (rough cost estimates; AWS S3 Standard, similar for GCS)
GET_USD = 0.0004 / 1000          # read request (S3 GET/HEAD, GCS class B)
LIST_USD = 0.005 / 1000          # list request (S3 LIST, GCS class A)


# ---------------------------------------------------------------- naming rules (CANedge deployments)
def output_bucket(input_bucket: str) -> str:
    return f"{input_bucket}-parquet"


def athena_database(lake_id: str) -> str:
    return f"dbparquet{lake_id}"


def athena_workgroup(lake_id: str) -> str:
    return f"athenaworkgroup-{lake_id}"


# ---------------------------------------------------------------- profiles
def profiles() -> list[str]:
    """Names of the data lake profiles (credentials/<name>.env)."""
    return sorted(p.name[:-4] for p in CRED_DIR.glob("*.env")) if CRED_DIR.is_dir() else []


def profile_path() -> Path | None:
    """credentials/<CANEDGE_ENV>.env, else the only profile, else None (no profile, or several and none chosen)."""
    name = os.environ.get("CANEDGE_ENV")
    if name:
        return CRED_DIR / f"{name}.env"
    names = profiles()
    return CRED_DIR / f"{names[0]}.env" if len(names) == 1 else None


def profile_problem() -> str | None:
    """Why no profile can be used (None = fine)."""
    p = profile_path()
    if p and p.exists():
        return None
    names = profiles()
    if p:
        return (f"{p.as_posix()} not found" + (f" (profiles: {', '.join(names)})" if names else "") +
                f" - create it with the setup window: {SETUP}")
    if names:
        return f"Several data lake profiles ({', '.join(names)}): Pass --env <name>"
    return f"No data lake credentials yet - open the setup window: {SETUP}"


def use_env(name: str | None, exc: type[BaseException] = SystemExit) -> None:
    """--env NAME: Use credentials/NAME.env (also in subprocesses). Raises exc if that profile does not exist."""
    if name:
        if os.environ.get("CANEDGE_ENV") != name:   # switching profiles: Drop what load_env set from the old one
            for k, v in list(_LOADED.items()):
                if os.environ.get(k) == v:
                    del os.environ[k]
            _LOADED.clear()
        os.environ["CANEDGE_ENV"] = name
        if not profile_path().exists():
            msg = profile_problem()
            raise exc(f"ERROR: {msg}" if exc is SystemExit else msg)


def grafana_profiles() -> list[str]:
    """Names of the Grafana profiles (credentials/grafana/<name>.env)."""
    return sorted(p.name[:-4] for p in GRAFANA_DIR.glob("*.env")) if GRAFANA_DIR.is_dir() else []


def grafana_profile(name: str | None = None, exc: type[BaseException] = SystemExit) -> dict:
    """The Grafana profile NAME, else the only one: Its values + 'name' + 'path'. Raises exc with what to do when
    none or several exist, or GRAFANA_URL / GRAFANA_TOKEN are missing. The token is never printed."""
    names = grafana_profiles()
    if not name and len(names) != 1:
        msg = (f"Several Grafana profiles ({', '.join(names)}): Pass --grafana-env <name>" if names else
               f"No Grafana profile yet - open the setup window: {GRAFANA_SETUP}")
        raise exc(f"ERROR: {msg}" if exc is SystemExit else msg)
    name = name or names[0]
    path = GRAFANA_DIR / f"{name}.env"
    v = read_env_file(path)
    missing = [k for k in ("GRAFANA_URL", "GRAFANA_TOKEN") if not v.get(k)]
    if missing:
        msg = (f"{path.as_posix()} not found" + (f" (Grafana profiles: {', '.join(names)})" if names else "")
               if not path.exists() else f"Missing in {path.as_posix()}: {', '.join(missing)}") + \
              f" - open the setup window: {GRAFANA_SETUP}"
        raise exc(f"ERROR: {msg}" if exc is SystemExit else msg)
    return {**v, "name": name, "path": path.as_posix()}


def read_env_file(path: Path) -> dict:
    """KEY=VALUE lines (comments and blank lines skipped; surrounding spaces and quotes stripped)."""
    out = {}
    if path and path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                out[k.strip()] = v.strip().strip('"').strip("'").strip()
    return out


_LOADED: dict[str, str] = {}   # variables load_env set from a profile (use_env clears them on a profile switch)


def load_env() -> None:
    """Load the selected profile into the environment, without overriding existing env vars."""
    for k, v in read_env_file(profile_path()).items():
        if k not in os.environ:
            os.environ[k] = _LOADED[k] = v


def lake() -> dict:
    """The data lake of the profile (no secrets): Cloud, buckets, region, lake ID, Athena database/workgroup."""
    load_env()
    p = profile_path()
    inp = os.environ.get("CANEDGE_INPUT_BUCKET") or None
    lid = os.environ.get("CANEDGE_LAKE_ID") or None
    return {"profile": p.as_posix() if p else None, "cloud": os.environ.get("CANEDGE_CLOUD") or "aws",
            "input_bucket": inp, "output_bucket": output_bucket(inp) if inp else None,
            "region": os.environ.get("CANEDGE_REGION") or None, "lake_id": lid,
            "athena_database": athena_database(lid) if lid else None,
            "athena_workgroup": athena_workgroup(lid) if lid else None,
            "endpoint": os.environ.get("CANEDGE_S3_ENDPOINT") or None}


def google_key_mode() -> bool:
    """A Google profile with a service account key file: Buckets via gcs_s3.py (no S3 keys needed)."""
    return (os.environ.get("CANEDGE_CLOUD") == "google") and bool(os.environ.get("CANEDGE_GOOGLE_KEY_FILE"))


def access_problem(athena: bool = False, admin: bool = False) -> str | None:
    """The profile's key type lacks what the task needs (None = fine or key type unknown)."""
    kind = os.environ.get("CANEDGE_KEY_TYPE") or ""
    if kind not in ACCESS:
        return None
    for need in ["lake" if athena else "s3"] + (["admin"] if admin else []):
        if need not in ACCESS[kind]:
            return (f"This profile holds {KEY_TYPES[kind].split(':')[0]}. This task needs {NEEDS[need]}: Ask the "
                    f"data lake owner for them and add them as a second profile with the setup window: {SETUP} "
                    "(then --env <name>)")
    return None


def s3_problem(athena: bool = False, admin: bool = False) -> str | None:
    """Why the profile's S3 access (athena=True: its Athena data lake; admin=True: logs + Glue job runs) cannot be
    used (None = fine)."""
    load_env()
    if not profile_problem() and (msg := access_problem(athena, admin)):
        return msg
    need = ["CANEDGE_INPUT_BUCKET"] + ([] if google_key_mode() else KEYS)
    msg = profile_problem()
    if msg and not all(os.environ.get(k) for k in need):   # keys may also come from the environment
        return msg
    if (os.environ.get("CANEDGE_CLOUD") or "aws") == "aws" and not os.environ.get("CANEDGE_S3_ENDPOINT"):
        need.append("CANEDGE_REGION")
    missing = [k for k in need if not os.environ.get(k)]
    if missing:
        return f"Missing in {(profile_path() or CRED_DIR).as_posix()}: {', '.join(missing)} - re-run the setup window: {SETUP}"
    if athena:
        if (os.environ.get("CANEDGE_CLOUD") or "aws") != "aws":
            return ("This profile is not an AWS data lake - use --bigquery for a Google data lake, --synapse for an "
                    "Azure data lake")
        if not os.environ.get("CANEDGE_LAKE_ID"):
            return ("This profile has no data lake (CANEDGE_LAKE_ID is empty: The keys are not the data lake stack's "
                    f"programmatic access keys) - re-run the setup window with those keys: {SETUP}")
    return None


def require_s3(athena: bool = False, exc: type[BaseException] = SystemExit, admin: bool = False) -> None:
    msg = s3_problem(athena, admin)
    if msg:
        raise exc(f"ERROR: {msg}" if exc is SystemExit else msg)


def aws_client(service: str, **config):
    """boto3 client with the profile's keys and region (+ CANEDGE_S3_ENDPOINT for s3: Google, MinIO, ...). config =
    botocore Config options. Google key file profiles: The gcs_s3.py client (same calls). Call require_s3() first for
    a clear message when the profile is incomplete."""
    import boto3
    from botocore.config import Config
    load_env()
    if service == "s3" and google_key_mode():
        from gcs_s3 import GcsS3
        return GcsS3(os.environ["CANEDGE_GOOGLE_KEY_FILE"], os.environ.get("CANEDGE_BIGQUERY_PROJECT") or None)
    endpoint = os.environ.get("CANEDGE_S3_ENDPOINT") if service == "s3" else None
    return boto3.client(service, aws_access_key_id=os.environ["CANEDGE_ACCESS_KEY_ID"],
                        aws_secret_access_key=os.environ["CANEDGE_SECRET_ACCESS_KEY"],
                        region_name=os.environ.get("CANEDGE_REGION") or None, endpoint_url=endpoint or None,
                        config=Config(**config) if config else None)


def synapse_endpoint(value: str) -> str:
    """The Serverless SQL endpoint host from what the user entered: The endpoint, a URL/host with a port, or just the
    workspace name (synapse-<id> -> synapse-<id>-ondemand.sql.azuresynapse.net)."""
    v = (value or "").strip().removeprefix("tcp:").split("://")[-1].split("/")[0].split(",")[0].split(":")[0]
    if v and "." not in v:
        v = v.removesuffix("-ondemand") + "-ondemand.sql.azuresynapse.net"
    return v.lower()


def synapse_config() -> dict:
    """The Azure Synapse login of the profile, without the password: endpoint, database, user, missing keys."""
    load_env()
    p = profile_path()
    cfg = {"profile": p.as_posix() if p else f"{CRED_DIR}/<name>.env",
           "endpoint": synapse_endpoint(os.environ.get("CANEDGE_SYNAPSE_ENDPOINT", "")) or None,
           "database": os.environ.get("CANEDGE_SYNAPSE_DATABASE") or None,
           "user": os.environ.get("CANEDGE_SYNAPSE_USER") or SYNAPSE_READER_USER}
    cfg["missing"] = [k for k in ("CANEDGE_SYNAPSE_ENDPOINT", "CANEDGE_SYNAPSE_DATABASE", "CANEDGE_SYNAPSE_PASSWORD")
                      if not os.environ.get(k)]
    cfg["admin_login"] = cfg["user"].lower() == SYNAPSE_ADMIN_USER
    return cfg


def synapse_password() -> str:
    """For the SQL connection only - never print it."""
    load_env()
    return os.environ.get("CANEDGE_SYNAPSE_PASSWORD", "")


def synapse_connect(endpoint: str, database: str, user: str, password: str, timeout: float = 600):
    """A pymssql connection to the serverless SQL pool (SQL login, TLS by the server; no ODBC driver). An idle pool
    answers 'warming up' (error 42109) for up to ~1 min: Retried for 2 min. Raises RuntimeError with a short message
    (never the password)."""
    import time

    import pymssql
    t0 = time.time()
    while True:
        try:
            return pymssql.connect(server=endpoint, port="1433", user=user, password=password, database=database,
                                   login_timeout=30, timeout=int(timeout), autocommit=True, charset="UTF-8")
        except pymssql.Error as e:
            msg = synapse_error(e)
            if ("42109" in msg or "warming up" in msg.lower()) and time.time() - t0 < 120:
                time.sleep(10)
                continue
            raise RuntimeError(msg) from None


def synapse_error(e: Exception) -> str:
    """One clean line for a pymssql/Synapse error (no password in it)."""
    args = getattr(e, "args", ())
    raw = args[0] if args else e
    if len(args) == 2 and isinstance(args[0], int):   # server errors: args = (code, b'message ...')
        raw = args
    if isinstance(raw, tuple) and len(raw) > 1:   # (code, b'server message DB-Lib error message N, severity S:\n...')
        code, msg = raw[0], raw[1]
        msg = msg.decode("utf-8", "replace") if isinstance(msg, bytes) else str(msg)
        server = msg.split("DB-Lib error")[0].strip()   # the server's own message (e.g. Login failed ...)
        if not server:   # client-side errors: The line after the first 'DB-Lib error message N, severity S:'
            lines = [x.strip() for x in msg.splitlines() if x.strip() and not x.startswith("DB-Lib error")]
            server = lines[0] if lines else ""
        server = " ".join(server.split()).rstrip(".")[:300]
        return f"{server} (error {code})" if server else f"error {code}"
    s = str(raw).replace("\\n", " ")
    s = s.split("DB-Lib error")[0].strip() or s
    return " ".join(s.split())[:300]


def bigquery_key() -> Path | None:
    """CANEDGE_GOOGLE_KEY_FILE, else the only service account key (*.json) in credentials/ (content never printed)."""
    load_env()
    if os.environ.get("CANEDGE_GOOGLE_KEY_FILE"):
        return Path(os.environ["CANEDGE_GOOGLE_KEY_FILE"])
    keys = []
    for f in sorted(KEY_DIR.glob("*.json")) if KEY_DIR.is_dir() else []:
        try:
            if json.loads(f.read_text(encoding="utf-8")).get("type") == "service_account":
                keys.append(f)
        except (OSError, ValueError):
            continue
    return keys[0] if len(keys) == 1 else None
