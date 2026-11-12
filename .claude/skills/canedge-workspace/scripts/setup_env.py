r"""Connect a bucket / data lake: Creates a profile credentials/<name>.env. Keys never pass through the chat.

  .venv\Scripts\python.exe .claude/skills/canedge-workspace/scripts/setup_env.py [--cloud aws|google|s3|azure]
  .venv\Scripts\python.exe .claude/skills/canedge-workspace/scripts/setup_env.py --key-file <path.json> --bucket <name>
  .venv\Scripts\python.exe .claude/skills/canedge-workspace/scripts/setup_env.py --grafana [--name NAME]

Window (the agent launches it; it opens on the user's desktop). Fields per --cloud:
  aws (default)  Access key ID + Secret access key + S3 input bucket name. Either the data lake stack's programmatic
                 access keys (S3 input/output bucket + Athena) or the device keys (S3 input bucket only)
  google         The device keys (S3 interoperability keys) + input bucket name: S3 skills, no data lake
  s3             Endpoint URL + access key + secret + bucket name (any S3-compatible server, e.g. MinIO, or the
                 Flexify S3 gateway of an Azure input container: Flexify endpoint + keys + container name)
  azure          Synapse Serverless SQL endpoint + database + SQL user (empty = sqlreaderuser) + password: Azure data
                 lake queries (--synapse). Tests the login, the mapped tables and one OPENROWSET read
--key-file + --bucket: No window (Google data lake). The agent asks the user for the path of the programmatic access
key file <id>-programmatic-access-account.json (in the input bucket root) and the input bucket name - neither is
secret. Validates the key, tests the buckets, BigQuery and the function logs, copies the key into credentials/ and
writes the profile (buckets via gcs_s3.py, no S3 keys needed). The key content is never printed. --key-file without
--bucket (+ --name): Only add a BigQuery key to an existing profile (e.g. an AWS profile that also has BigQuery).

"Test and save" checks the formats, derives the rest (AWS: region from the bucket; key type + lake ID from the keys'
IAM user, e.g. S3LimitedAccessUser-<id>), tests the buckets and Athena/BigQuery, and writes the profile, named after
the input bucket (--name: another name, e.g. the regression tests' "playground"; other lines are kept). The S3 output
bucket (<input>-parquet) entered by mistake is corrected to its input bucket. The result is printed to stdout WITHOUT
secrets for the agent. Exit code 0 = saved. --cli: Terminal prompts instead of the window (systems without Tk).

--grafana: A Grafana profile credentials/grafana/<name>.env instead (no data lake; used by canedge-grafana
grafana_api.py). Window fields: Grafana URL + service account token. Tests: Reachable (/api/health), token valid,
what it allows (GET /api/access-control/user/permissions, Grafana 9+, else probing), whether a data source named
'Amazon Athena' / 'Google BigQuery' / 'Microsoft SQL Server' (Synapse) exists. Profile name from the host
(myorg.grafana.net -> myorg-grafana-net), --name to override. The token is never printed.
"""
from __future__ import annotations

import argparse
import json
import re
import shutil
import sys
from pathlib import Path
from typing import NamedTuple

sys.path.insert(0, str(Path(__file__).resolve().parent))
from env import (CRED_DIR, GOOGLE_ENDPOINT, GRAFANA_DIR, GRAFANA_KEYS, KEY_TYPES, PROFILE_KEYS,  # noqa: E402
                 SYNAPSE_ADMIN_USER, SYNAPSE_KEYS, SYNAPSE_READER_USER, athena_database, athena_workgroup,
                 output_bucket, read_env_file, synapse_connect, synapse_endpoint, synapse_error)

INVISIBLE = "".join(map(chr, (0x200B, 0xFEFF)))   # zero-width space, BOM (picked up by copy/paste)
AWS_KEY_RE = re.compile(r"^(AKIA|ASIA)[A-Z0-9]{16}$")
BUCKET_RE = re.compile(r"^[a-z0-9][a-z0-9.\-_]{1,220}[a-z0-9]$")
NAME_RE = re.compile(r"^[a-z0-9][a-z0-9.\-_]{0,62}$")
OUTPUT_SUFFIX = "-parquet"   # S3 output bucket = <input>-parquet (Athena results: <input>-parquet-query-results)
USERS = {"S3LimitedAccessUser-": "programmatic", "AthenaAccessUser-": "athena"}   # stack IAM user -> key type
DEVICE_USER = "AmazonS3FullAccessBucketUser"
WARN = object()   # check(..., WARN, ...): A warning in the result, does not block saving
GOOGLE_SA_SUFFIXES = {"-programmatic": "google-programmatic", "-bigquery-user": "google-bigquery"}


class Field(NamedTuple):
    """One input in the setup window."""
    name: str                 # key in the values dict (key_id, secret, bucket, endpoint)
    label: str                # shown to the user
    secret: bool = False      # masked input


# What the user sees per cloud: Window title, a few short hint lines and the fields
CLOUDS = {
    "aws": {
        "title": "Amazon S3",
        "hint": ["Paste your keys and S3 input bucket name from your stack's 'Outputs' tab in AWS CloudFormation:",
                 "- Data lake deployed: The 'programmatic access' keys",
                 "- Only an S3 bucket: The 'device' keys (also in your CANedge Configuration File)",
                 "- Query access only (e.g. a team member): The 'Athena' keys, also used by Grafana"],
        "fields": [Field("key_id", "Access key"),
                   Field("secret", "Secret key", secret=True),
                   Field("bucket", "S3 input bucket")],
    },
    "google": {
        "title": "Google Cloud",
        "hint": ["Paste the device keys from your CANedge Configuration File and your input bucket name.",
                 "Data lake deployed: Give the agent your programmatic access key file instead",
                 "(or, for query access only, the BigQuery user's key file, also used by Grafana)."],
        "fields": [Field("key_id", "Access key"),
                   Field("secret", "Secret key", secret=True),
                   Field("bucket", "Input bucket")],
    },
    "s3": {
        "title": "S3 server (e.g. MinIO, Flexify)",
        "hint": ["Paste your S3 server's address, your keys and the bucket name.",
                 "The keys can be the device keys from your CANedge Configuration File.",
                 "Azure input container: The Flexify endpoint URL + keys and the container name."],
        "fields": [Field("endpoint", "Server URL"),
                   Field("key_id", "Access key"),
                   Field("secret", "Secret key", secret=True),
                   Field("bucket", "Bucket")],
    },
    "azure": {
        "title": "Azure Synapse data lake",
        "hint": ["Paste the Synapse connection details (shown at the end of the Synapse deployment, as for Grafana):",
                 "- Serverless SQL endpoint: Synapse workspace > Overview (synapse-<id>-ondemand.sql.azuresynapse.net)",
                 "- Database + password: Also in the Container App Job 'synapse-map-tables-<id>' (Containers >",
                 "  Environment variables: SYNAPSE_DATABASE; Settings > Secrets: synapse-reader-password)",
                 "- SQL user: Leave empty for the read-only login sqlreaderuser (recommended). Older deployments",
                 "  without it: sqladminuser + the secret synapse-password (or deploy Synapse again to add it)",
                 "Your input container (Flexify S3 gateway) is a separate profile: --cloud s3"],
        "fields": [Field("endpoint", "Serverless SQL endpoint"),
                   Field("database", "Database"),
                   Field("user", "SQL user"),
                   Field("password", "Password", secret=True)],
    },
}


GRAFANA = {
    "title": "Grafana",
    "hint": ["Paste your Grafana URL and a service account token. To create one in Grafana: Administration >",
             "Users and access > Service accounts > Add service account > role > Add service account token.",
             "- Role Editor (recommended): Import/edit dashboards + run their queries to check the panels",
             "  (the agent never creates data sources: Add them as the CANedge Intro describes)"],
    "fields": [Field("url", "Grafana URL"), Field("token", "Token", secret=True)],
}
# Browser URLs pasted instead of the base URL: Cut at the first Grafana UI path
GRAFANA_UI_PATH = re.compile(r"/(d|d-solo|dashboards?|explore|login|a|connections|admin|org|alerting|plugins|"
                             r"datasources|profile|goto|playlists|library-panels)(/.*)?$")
GRAFANA_DS = {"grafana-athena-datasource": "Amazon Athena", "grafana-bigquery-datasource": "Google BigQuery",
              "mssql": "Microsoft SQL Server"}   # Microsoft SQL Server = Azure Synapse (the Grafana-Synapse guide)
# What the token allows: RBAC actions (GET /api/access-control/user/permissions, Grafana 9+)
GRAFANA_ACTIONS = {"dashboards_write": ("dashboards:create", "dashboards:write"),
                   "folders_write": ("folders:create", "folders:write"),
                   "datasources_read": ("datasources:read",),
                   "datasources_query": ("datasources:query",),
                   "datasources_write": ("datasources:create", "datasources:write")}
GRAFANA_ROLES = {
    "Admin": "Admin: Import/edit dashboards and folders, run their queries, create/edit data sources",
    "Editor": "Editor: Import/edit dashboards and folders, run their queries (data source changes need Admin)",
    "Viewer": "Viewer: Read dashboards and run their queries (importing needs the Editor role)",
    "none": "No dashboard or query access: Give the service account the Editor role (the token stays valid)",
    "unknown": "Not detected (no permission list, Grafana < 9): An import shows whether it may write",
}


def clean(v: str) -> str:
    """Strip spaces, quotes and invisible characters picked up by copy/paste."""
    return re.sub(rf"[\s{INVISIBLE}]", "", v or "").strip("'\"")


def clean_bucket(v: str) -> str:
    """A bucket name also when pasted as s3://name/, gs://name or an ARN."""
    v = re.sub(r"^(s3://|gs://|arn:aws:s3:::)", "", clean(v), flags=re.I)
    return v.split("/")[0].lower() if v else v


def clean_path(v: str) -> str:
    return (v or "").strip().strip("'\"").strip()


def err(e) -> str:
    from botocore.exceptions import ClientError
    return e.response.get("Error", {}).get("Code", "error") if isinstance(e, ClientError) else e.__class__.__name__


def bucket_found(s3, name: str) -> bool:
    """True if the bucket exists (also when these keys get 403 on it)."""
    from botocore.exceptions import BotoCoreError, ClientError
    try:
        s3.head_bucket(Bucket=name)
        return True
    except ClientError as e:
        return e.response.get("Error", {}).get("Code") not in ("404", "NoSuchBucket")
    except BotoCoreError:
        return False


def new_result(cloud: str) -> dict:
    return {"ok": False, "cloud": cloud, "kind": None, "lake_id": None, "region": None, "bucket": None,
            "endpoint": None, "key_file": None, "bq_project": None, "bq_datasets": None, "synapse": None,
            "checks": []}


def inspect_credentials(cloud: str, v: dict) -> dict:
    """Validate + derive + test. v: key_id, secret, bucket (+ endpoint for s3), or for google key_file (+ bucket).
    Returns new_result() fields (no secrets). ok = the profile is usable and can be saved."""
    res = new_result(cloud)

    def check(name, ok, detail=""):   # ok WARN = a warning (not blocking)
        res["checks"].append([name, None if ok is WARN else bool(ok), detail])
        return ok is WARN or bool(ok)

    if cloud == "google" and clean_path(v.get("key_file")):
        ok = inspect_google_key(Path(clean_path(v["key_file"])), clean_bucket(v.get("bucket")), res, check)
    elif cloud == "azure":
        ok = inspect_azure(v, res, check)
    else:
        ok = inspect_s3(cloud, v, res, check)
    res["ok"] = res.get("athena_ok", False) if res["kind"] == "athena" else ok   # Athena keys: queries only
    return res


def inspect_s3(cloud: str, v: dict, res: dict, check) -> bool:
    """The S3 part: Formats, key type (AWS), output bucket entered by mistake, region, buckets, Athena (AWS lake)."""
    import boto3
    from botocore.exceptions import BotoCoreError, ClientError

    key_id, secret, bucket = clean(v.get("key_id")), clean(v.get("secret")), clean_bucket(v.get("bucket"))
    endpoint = GOOGLE_ENDPOINT if cloud == "google" else clean(v.get("endpoint")).rstrip("/") if cloud == "s3" else None
    res.update(bucket=bucket, endpoint=endpoint, kind=None if cloud == "aws" else cloud)

    # 1. Formats
    if cloud == "aws":
        fmt = [check("Access key ID format", AWS_KEY_RE.match(key_id), "20 characters, starts with AKIA"),
               check("Secret access key format", len(secret) == 40, "40 characters")]
    elif cloud == "google":
        fmt = [check("Access key format", key_id.startswith("GOOG"), "starts with GOOG"),
               check("Secret format", len(secret) == 40, "40 characters")]
    else:
        fmt = [check("Endpoint URL", re.match(r"^https?://[^/\s]+", endpoint or ""), "e.g. http://192.168.1.10:9000"),
               check("Keys entered", key_id and secret)]
    fmt.append(check("Bucket name format", BUCKET_RE.match(bucket or ""), "lowercase letters, numbers, - and ."))
    if not all(fmt):
        return False
    session = boto3.Session(aws_access_key_id=key_id, aws_secret_access_key=secret)
    probe = session.client("s3", region_name="us-east-1", endpoint_url=endpoint)

    # 2. AWS: Who are these keys? (sts:GetCallerIdentity needs no permission) -> key type + lake ID
    if cloud == "aws":
        try:
            user = session.client("sts", region_name="us-east-1").get_caller_identity()["Arn"].split("/")[-1]
        except (ClientError, BotoCoreError) as e:
            return check("Keys valid", False, f"{err(e)} - copy the whole key values")
        kind = next((k for p, k in USERS.items() if user.startswith(p)), "device" if DEVICE_USER in user else "other")
        res["kind"] = kind
        res["lake_id"] = user.split("-", 1)[1] if kind in ("programmatic", "athena") else None
        check("Keys valid", True, KEY_TYPES[kind])

    # 3. The output bucket entered by mistake: Use its input bucket if that exists
    for suffix in (OUTPUT_SUFFIX + "-query-results", OUTPUT_SUFFIX):
        if bucket.endswith(suffix) and bucket_found(probe, bucket[:-len(suffix)]):
            check("Input bucket", True, f"{bucket} is the output bucket - using the input bucket {bucket[:-len(suffix)]}")
            bucket = res["bucket"] = bucket[:-len(suffix)]
            break

    # 4. AWS: Region from the bucket (also answered for 403/301 responses)
    region = None
    if cloud == "aws":
        try:
            r = probe.head_bucket(Bucket=bucket)
            region = r["ResponseMetadata"]["HTTPHeaders"].get("x-amz-bucket-region")
        except ClientError as e:
            region = e.response.get("ResponseMetadata", {}).get("HTTPHeaders", {}).get("x-amz-bucket-region")
            if e.response.get("Error", {}).get("Code") in ("404", "NoSuchBucket"):
                return check("Input bucket exists", False, f"{bucket} not found - check the name")
        except BotoCoreError as e:
            return check("Input bucket reachable", False, err(e))
        if not check("Region", region, region or "could not be read from the bucket"):
            return False
        res["region"] = region

    # 5. Buckets
    s3 = session.client("s3", region_name=region, endpoint_url=endpoint)
    ok = True
    if res["kind"] != "athena":
        try:
            n = s3.list_objects_v2(Bucket=bucket, MaxKeys=1).get("KeyCount", 0)
            ok = check("Input bucket", True, f"{bucket} ({'not empty' if n else 'empty'})")
        except (ClientError, BotoCoreError) as e:
            ok = check("Input bucket", False, f"{bucket}: {err(e)} - check the keys and the bucket name")
    if res["kind"] == "programmatic":
        try:
            n = s3.list_objects_v2(Bucket=output_bucket(bucket), MaxKeys=1).get("KeyCount", 0)
            check("Output bucket", True, f"{output_bucket(bucket)} ({'not empty' if n else 'empty'})")
        except (ClientError, BotoCoreError) as e:
            ok &= check("Output bucket", False, f"{output_bucket(bucket)}: {err(e)} - is {bucket} your S3 input bucket?")

    # 6. AWS data lake: Athena
    if res["lake_id"]:
        lid = res["lake_id"]
        ath = session.client("athena", region_name=region)
        a_ok = True
        try:
            names = [w["Name"] for w in ath.list_work_groups()["WorkGroups"]]
            a_ok &= check("Athena workgroup", athena_workgroup(lid) in names, athena_workgroup(lid))
        except (ClientError, BotoCoreError) as e:
            a_ok &= check("Athena workgroup", False, f"{athena_workgroup(lid)}: {err(e)}")
        try:
            ath.get_database(CatalogName="AwsDataCatalog", DatabaseName=athena_database(lid))
            a_ok &= check("Athena database", True, athena_database(lid))
        except (ClientError, BotoCoreError) as e:
            a_ok &= check("Athena database", False, f"{athena_database(lid)}: {err(e)}")
        res["athena_ok"] = a_ok
        ok &= a_ok
    return ok


def inspect_azure(v: dict, res: dict, check) -> bool:
    """The Synapse SQL login (serverless SQL pool): Formats, login, the mapped tables and one OPENROWSET read through
    the data source ParquetDataLake (TOP 1: billed as the 10 MB minimum). The password is never printed."""
    endpoint = synapse_endpoint(clean(v.get("endpoint")))
    host = endpoint.split(".")[0]
    if endpoint.endswith(".sql.azuresynapse.net") and not host.endswith("-ondemand"):   # the dedicated SQL endpoint
        check("Serverless SQL endpoint", True, f"{endpoint} is the dedicated SQL endpoint - using the serverless one")
        endpoint = f"{host}-ondemand.sql.azuresynapse.net"
    database, user, password = clean(v.get("database")), clean(v.get("user")) or SYNAPSE_READER_USER, \
        (v.get("password") or "").strip()
    res.update(kind="azure", endpoint=endpoint, synapse={"endpoint": endpoint, "database": database, "user": user})
    fmt = [check("Serverless SQL endpoint", re.match(r"^[a-z0-9][a-z0-9\-]*\.[a-z0-9.\-]+$", endpoint),
                 endpoint or "e.g. synapse-abc123-ondemand.sql.azuresynapse.net"),
           check("Database", database and database.lower() != "master",
                 database or "the data lake database, e.g. canedge or database1 (not master)"),
           check("Password entered", bool(password))]
    if not all(fmt):
        return False
    try:
        con = synapse_connect(endpoint, database, user, password, timeout=120)
    except RuntimeError as e:
        hint = ((" - check the password (secret synapse-reader-password). Deployed before 2026-11 or the mapping "
                 "job not run yet: The read-only login does not exist - use sqladminuser, or deploy Synapse again "
                 "and run the job") if "Login failed" in str(e) and user.lower() == SYNAPSE_READER_USER else
                " - check the user and password" if "Login failed" in str(e) else
                " - the serverless SQL pool is starting: Click 'Test and save' again in a minute"
                if "warming up" in str(e).lower() else
                " - check the database name" if "database" in str(e).lower() else
                " - check the endpoint" if "Unable to connect" in str(e) else "")
        return check("Login", False, f"{e}{hint}")
    ok = check("Login", True, f"{user} on {endpoint}, database {database}")
    try:
        cur = con.cursor()
        cur.execute("SELECT name, location FROM sys.external_tables")
        tables = cur.fetchall()
        msgs = [(n, loc) for n, loc in tables if re.match(r"^tbl_[0-9A-Fa-f]{8}_", n) and not n.endswith("_messages")]
        devices = {n[4:12].upper() for n, _ in msgs}
        res["synapse"].update(tables=len(msgs), devices=len(devices))
        has_meta = any(n.lower() == "tbl_aggregations_devicemeta" for n, _ in tables)
        check("Mapped tables", WARN if not msgs else True,
              f"{len(msgs)} message tables, {len(devices)} devices" + ("" if has_meta else ", no device meta table")
              if msgs else "none yet: Run the Container App Job 'synapse-map-tables-<id>' (Run now), then test again")
        # The data source ParquetDataLake: A read-only login can use it but not see it in sys.external_data_sources,
        # so the read through it decides
        if msgs:   # the folder as the skills read it: <DEVICE>/<Message>/yyyy/mm/dd/<file> (location: '/DEV/MSG/*')
            loc = msgs[0][1].strip().strip("/").removesuffix("/**").removesuffix("/*").strip("/") + "/*/*/*/*"
            try:
                cur.execute(f"SELECT TOP 1 * FROM OPENROWSET(BULK '{loc}', DATA_SOURCE = 'ParquetDataLake', "
                            "FORMAT = 'PARQUET') AS r")
                cur.fetchall()
                check("Read Parquet", True, f"{loc} via the data source ParquetDataLake")
            except Exception as e:  # noqa: BLE001 - saved anyway (login works): the agent diagnoses it
                check("Read Parquet", WARN, f"{loc}: {synapse_error(e)} - is this the data lake database, and may "
                                            "this login read it (the read-only login is created by the mapping job)?")
    except Exception as e:  # noqa: BLE001 - pymssql errors: one clean line (no password in them)
        ok &= check("Schema", False, synapse_error(e))
    finally:
        con.close()
    if user.lower() == SYNAPSE_ADMIN_USER:
        check("SQL login", WARN, "the admin login: The skills only run read-only queries, but the read-only login "
                                 "sqlreaderuser is safer (Synapse deployments from 2026-11; older: deploy Synapse "
                                 "again and run the mapping job)")
    return ok


def inspect_google_key(path: Path, bucket: str, res: dict, check) -> bool:
    """A service account JSON key (key content never printed): Key type + lake ID from its account name, then the
    buckets (gcs_s3.py), BigQuery and (programmatic access key) the function logs. No bucket: BigQuery only."""
    from botocore.exceptions import ClientError
    try:
        info = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return check("Key file", False, f"{path.name}: not found or not a JSON file")
    if info.get("type") != "service_account" or "@" not in info.get("client_email", ""):
        return check("Key file", False, f"{path.name}: not a service account key file")
    account = info["client_email"].split("@")[0]
    kind, lid = "google-other", None
    for suffix, k in GOOGLE_SA_SUFFIXES.items():
        if account.endswith(suffix):
            kind, lid = k, account[:-len(suffix)]
    res.update(kind=kind, lake_id=lid, key_file=path, bq_project=info.get("project_id"))
    check("Key file", True, KEY_TYPES[kind])
    try:
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_info(info)
        from google.auth.transport.requests import Request
        creds.with_scopes(["https://www.googleapis.com/auth/cloud-platform"]).refresh(Request())
    except Exception as e:  # noqa: BLE001 - never echo the key content
        return check("Key valid", False, f"{e.__class__.__name__} - the key is incomplete, revoked or deleted")
    ok = True

    # Buckets (skipped without a bucket: only a BigQuery key for an existing profile)
    if bucket:
        if not check("Bucket name format", BUCKET_RE.match(bucket), "lowercase letters, numbers, - and ."):
            return False
        from gcs_s3 import GcsS3
        s3 = GcsS3(str(path))
        for suffix in (OUTPUT_SUFFIX + "-query-results", OUTPUT_SUFFIX):
            if bucket.endswith(suffix) and bucket_found(s3, bucket[:-len(suffix)]):
                check("Input bucket", True, f"{bucket} is the output bucket - using the input bucket {bucket[:-len(suffix)]}")
                bucket = bucket[:-len(suffix)]
                break
        res["bucket"] = bucket
        try:
            n = s3.list_objects_v2(Bucket=bucket, MaxKeys=1)["KeyCount"]
            ok &= check("Input bucket", True, f"{bucket} ({'not empty' if n else 'empty'})")
        except ClientError as e:
            ok &= check("Input bucket", False, f"{bucket}: {err(e)} - check the bucket name")
        if kind != "google-other":
            try:
                n = s3.list_objects_v2(Bucket=output_bucket(bucket), MaxKeys=1)["KeyCount"]
                check("Output bucket", True, f"{output_bucket(bucket)} ({'not empty' if n else 'empty'})")
            except ClientError as e:
                ok &= check("Output bucket", False, f"{output_bucket(bucket)}: {err(e)} - is {bucket} your input bucket?")

    # BigQuery
    try:
        from google.cloud import bigquery
        ds = [d.dataset_id for d in bigquery.Client(credentials=creds, project=creds.project_id).list_datasets()]
        res["bq_datasets"] = ds
        ok &= check("BigQuery", bool(ds), f"project {creds.project_id}, datasets: {', '.join(ds) or 'none'}")
    except Exception as e:  # noqa: BLE001 - never echo the key content
        ok &= check("BigQuery", False, e.__class__.__name__)

    # Function logs (programmatic access key: the log view canedge-<id>)
    if kind == "google-programmatic":
        view = f"projects/{creds.project_id}/locations/global/buckets/_Default/views/canedge-{lid}"
        try:
            from google.auth.transport.requests import AuthorizedSession
            r = AuthorizedSession(creds.with_scopes(["https://www.googleapis.com/auth/logging.read"])).post(
                "https://logging.googleapis.com/v2/entries:list", json={"resourceNames": [view], "pageSize": 1},
                timeout=30)
            ok &= check("Function logs", r.ok, f"log view canedge-{lid}" + ("" if r.ok else f": HTTP {r.status_code}"))
        except Exception as e:  # noqa: BLE001
            ok &= check("Function logs", False, e.__class__.__name__)
    return ok


def save(name: str, v: dict, res: dict) -> Path:
    """Write credentials/<name>.env (values not given now and other lines of an existing profile are kept); copy a
    Google key file into credentials/<name>-google.json."""
    CRED_DIR.mkdir(exist_ok=True)
    path = CRED_DIR / f"{name}.env"
    old = read_env_file(path)
    values = {k: old.get(k, "") for k in PROFILE_KEYS}
    # a key file added to an existing profile without a bucket keeps its cloud (e.g. AWS profile + BigQuery)
    values["CANEDGE_CLOUD"] = res["cloud"] if res["bucket"] or not old.get("CANEDGE_CLOUD") else old["CANEDGE_CLOUD"]
    if res["bucket"]:
        values.update({"CANEDGE_ACCESS_KEY_ID": clean(v.get("key_id")), "CANEDGE_SECRET_ACCESS_KEY": clean(v.get("secret")),
                       "CANEDGE_INPUT_BUCKET": res["bucket"], "CANEDGE_REGION": res["region"] or "",
                       "CANEDGE_LAKE_ID": res["lake_id"] or "", "CANEDGE_S3_ENDPOINT": res["endpoint"] or "",
                       "CANEDGE_KEY_TYPE": res["kind"] or ""})
    if res["key_file"]:
        dst = CRED_DIR / f"{name}-google.json"
        if res["key_file"].resolve() != dst.resolve():
            shutil.copyfile(res["key_file"], dst)
        values["CANEDGE_GOOGLE_KEY_FILE"] = dst.as_posix()
    keys = list(PROFILE_KEYS)
    if res["synapse"] or any(old.get(k) for k in SYNAPSE_KEYS):   # Azure: The Synapse SQL login
        keys += SYNAPSE_KEYS
        values.update({k: old.get(k, "") for k in SYNAPSE_KEYS})
    if res["synapse"]:
        values.update({"CANEDGE_CLOUD": "azure", "CANEDGE_KEY_TYPE": "azure",
                       "CANEDGE_SYNAPSE_ENDPOINT": res["synapse"]["endpoint"],
                       "CANEDGE_SYNAPSE_DATABASE": res["synapse"]["database"],
                       "CANEDGE_SYNAPSE_USER": res["synapse"]["user"],
                       "CANEDGE_SYNAPSE_PASSWORD": (v.get("password") or "").strip()})
    keep = {k: x for k, x in old.items() if k not in keys}
    lines = ["# CANedge profile - written by setup_env.py (re-run it to change it; never share this file)",
             *(f"{k}={values[k]}" for k in keys)]
    if keep:
        lines += ["# Other settings", *(f"{k}={x}" for k, x in keep.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def summary(res: dict, path: Path | None) -> dict:
    """Result for the agent / the window: No secrets."""
    lid, b = res["lake_id"], res["bucket"]
    s = {"saved": path.as_posix() if path else None, "cloud": res["cloud"], "keys": KEY_TYPES.get(res["kind"]),
         "input_bucket": b, "region": res["region"], "lake_id": lid,
         "output_bucket": output_bucket(b) if b and (lid or res["bq_datasets"]) else None,
         "athena_database": athena_database(lid) if lid else None,
         "athena_workgroup": athena_workgroup(lid) if lid else None, "endpoint": res["endpoint"],
         "bigquery_project": res["bq_project"], "bigquery_datasets": res["bq_datasets"],
         "synapse": res["synapse"],
         "checks": [f"{'WARN' if ok is None else 'OK  ' if ok else 'FAIL'} {n}" + (f": {d}" if d else "")
                    for n, ok, d in res["checks"]]}
    return {k: x for k, x in s.items() if x is not None}


def run(cloud: str, v: dict, name: str = "") -> tuple[dict, int]:
    """Profile name = the input bucket (else the BigQuery dataset; Azure: the Synapse workspace name); name: Override,
    e.g. for the regression tests."""
    res = inspect_credentials(cloud, v)
    ds = res["bq_datasets"] or []
    workspace = (res["synapse"] or {}).get("endpoint", "").split(".")[0].removesuffix("-ondemand")
    name = clean(name).lower() or res["bucket"] or workspace or \
        (ds[0].lower() if len(ds) == 1 else (res["bq_project"] or ""))
    if res["ok"] and not NAME_RE.match(name or ""):
        res["checks"].append(["Profile name", False, "lowercase letters, numbers, ., - and _"])
        res["ok"] = False
    path = save(name, v, res) if res["ok"] else None
    return summary(res, path), 0 if path else 1


def key_file_setup(path: str, bucket: str, name: str) -> int:
    """--key-file (+ --bucket): A Google profile from a service account key file, without the window."""
    s, code = run("google", {"key_file": path, "bucket": bucket}, name)
    print(json.dumps(s, indent=1))
    return code


# ---------------------------------------------------------------- Grafana (--grafana; also used by grafana_api.py)
def clean_grafana_url(v: str) -> str:
    """The base URL also when a browser URL was pasted (dashboard, login page, ?orgId=1). No scheme: https."""
    v = clean(v).split("#")[0].split("?")[0].rstrip("/")
    if v and not re.match(r"^https?://", v, re.I):
        v = "https://" + v
    m = re.match(r"^(https?://[^/]+)(/.*)?$", v, re.I)
    if not m:
        return v
    path = GRAFANA_UI_PATH.sub("", m.group(2) or "").rstrip("/")
    return m.group(1).lower() + path


def grafana_client(url: str, token: str, timeout: float = 15.0):
    """grafana-client GrafanaApi with the token (Bearer header, never in the URL or the output)."""
    from grafana_client import GrafanaApi
    return GrafanaApi.from_url(url, credential=token, timeout=timeout)


def grafana_error(e: Exception) -> str:
    """A short message for an API error (no request headers, so no token)."""
    from grafana_client.client import GrafanaException
    if isinstance(e, GrafanaException):
        msg = re.sub(r"^(Client|Server) Error \d+: ", "", str(e.message if e.status_code else e))
        return (f"HTTP {e.status_code}: " if e.status_code else "") + " ".join(msg.split())[:200]
    name = e.__class__.__name__
    if "Connection" in name or "Timeout" in name or "SSL" in name:
        reason = re.search(r"(certificate verify failed|timed out|getaddrinfo failed|Name or service not known|"
                           r"actively refused|Connection refused|Failed to establish a new connection)", str(e), re.I)
        return f"{name}: no connection" + (f" ({reason.group(1)})" if reason else "")
    return f"{name}: {' '.join(str(e).split())[:200]}"


def grafana_access(api) -> tuple[dict, str]:
    """What the token may do: {dashboards_write, folders_write, datasources_read, datasources_query,
    datasources_write} -> True/False (None = unknown), and the source (permissions / probe)."""
    from grafana_client.client import GrafanaException
    try:
        perms = api.client.GET("/access-control/user/permissions")
        if isinstance(perms, dict):
            return {k: any(a in perms for a in acts) for k, acts in GRAFANA_ACTIONS.items()}, "permissions"
    except GrafanaException:
        pass
    access = dict.fromkeys(GRAFANA_ACTIONS)   # Grafana < 9: Probe what can be read without changing anything
    try:
        api.client.GET("/datasources")
        access["datasources_read"] = True
    except GrafanaException as e:
        access["datasources_read"] = False if e.status_code in (401, 403) else None
    return access, "probe"


def grafana_role(access: dict) -> str:
    """The basic role the permissions match (a key of GRAFANA_ROLES)."""
    if access.get("dashboards_write") is None:
        return "unknown"
    if access.get("datasources_write"):
        return "Admin"
    if access.get("dashboards_write"):
        return "Editor"
    return "Viewer" if access.get("datasources_query") else "none"


def grafana_datasources(api) -> list[dict]:
    """Data sources (name, uid, type, default): The data source API (needs datasources:read), else the frontend
    settings (the data sources the token may query)."""
    from grafana_client.client import GrafanaException
    try:
        return [{"name": d.get("name"), "uid": d.get("uid"), "type": d.get("type"), "default": bool(d.get("isDefault"))}
                for d in api.datasource.list_datasources()]
    except GrafanaException:
        fs = api.client.GET("/frontend/settings").get("datasources") or {}
        return [{"name": n, "uid": d.get("uid"), "type": d.get("type"), "default": bool(d.get("isDefault"))}
                for n, d in fs.items() if not (d.get("meta") or {}).get("builtIn")]


def grafana_profile_name(url: str) -> str:
    """myorg.grafana.net -> myorg-grafana-net, localhost:3000 -> localhost-3000."""
    host = re.sub(r"^https?://", "", url or "", flags=re.I).split("/")[0]
    return re.sub(r"[^a-z0-9]+", "-", host.lower()).strip("-")


def inspect_grafana(v: dict) -> dict:
    """v: url, token. Reachable, token valid, what it allows, the expected data sources. ok = usable (saved)."""
    res = {"ok": False, "url": None, "org": None, "version": None, "role": None, "access": None,
           "datasources": None, "checks": []}

    def check(name, ok, detail=""):   # ok None = a warning (not blocking)
        res["checks"].append([name, ok, detail])
        return ok

    url, token = clean_grafana_url(v.get("url")), clean(v.get("token"))
    if "@" in url.split("/")[2 if url.count("/") >= 2 else 0]:
        check("Grafana URL", False, "no user name or password in the URL: Only the address, and the token below")
        return res
    res["url"] = url
    if not check("Grafana URL", re.match(r"^https?://[^/\s]+(/\S*)?$", url) is not None,
                 url or "e.g. https://myorg.grafana.net or http://localhost:3000"):
        return res
    if not token:
        check("Token entered", False, "paste the service account token")
        return res
    if token.startswith("glc_"):
        check("Token type", False, "a Grafana Cloud access policy token: Create a service account token in Grafana "
                                   "(Administration > Users and access > Service accounts)")
        return res
    if not token.startswith("glsa_"):
        check("Token type", None, "not a service account token (glsa_...): Used as an API key")
    try:
        api = grafana_client(url, token)
        h = api.client.GET("/health")
        if not isinstance(h, dict):
            raise ValueError("no Grafana health response")
        check("Reachable", True, f"{url} (database {h.get('database', '?')})")
    except Exception as e:  # noqa: BLE001 - any connection problem, shown plainly (no token in it)
        check("Reachable", False, f"{url}: {grafana_error(e)} - check the URL (is it your Grafana?)")
        return res
    from grafana_client.client import GrafanaException
    try:
        api.client.GET("/search", params={"limit": 1})
    except GrafanaException as e:
        hint = "the token is wrong, expired or revoked" if e.status_code == 401 else "check the token"
        check("Token valid", False, f"{grafana_error(e)} - {hint}")
        return res
    try:
        res["org"] = api.client.GET("/org").get("name")
    except GrafanaException:
        pass
    try:
        res["version"] = api.version
    except Exception:  # noqa: BLE001 - informational only
        pass
    check("Token valid", True, ", ".join(x for x in (f"organization {res['org']}" if res["org"] else "",
                                                     f"Grafana {res['version']}" if res["version"] else "") if x))
    res["ok"] = True

    access, source = grafana_access(api)
    res["access"], res["role"] = access, grafana_role(access)
    check("Token allows", {"Admin": True, "Editor": True, "unknown": None, "Viewer": None}.get(res["role"], False),
          GRAFANA_ROLES[res["role"]] + ("" if source == "permissions" else " (probed)"))
    try:
        dss = grafana_datasources(api)
    except Exception as e:  # noqa: BLE001
        check("Data sources", None, f"not readable: {grafana_error(e)}")
        return res
    res["datasources"] = dss
    found = [n for n in GRAFANA_DS.values() if any(d["name"] == n for d in dss)]
    other = [f"{d['name']} ({d['type']})" for d in dss if d["type"] in GRAFANA_DS and d["name"] not in found]
    if found:
        check("Data source", True, " + ".join(f"'{n}'" for n in found) + " found (the name the dashboards expect)"
              + (f"; also {', '.join(other)}" if other else ""))
    elif other:
        check("Data source", None, f"{', '.join(other)}, but none named 'Amazon Athena' / 'Google BigQuery' / "
                                   "'Microsoft SQL Server': Import with --datasource-uid <uid>, or rename it in "
                                   "Grafana")
    else:
        check("Data source", None, "No Athena, BigQuery or Synapse data source yet: Add it in Grafana (CANedge "
                                   "Intro: Grafana-Athena / Grafana-BigQuery / Grafana-Synapse), named 'Amazon "
                                   "Athena' / 'Google BigQuery' / 'Microsoft SQL Server'")
    return res


def save_grafana(name: str, v: dict, res: dict) -> Path:
    """Write credentials/grafana/<name>.env (other lines of an existing profile are kept)."""
    GRAFANA_DIR.mkdir(parents=True, exist_ok=True)
    path = GRAFANA_DIR / f"{name}.env"
    old = read_env_file(path)
    access = res["access"] or {}
    values = {"GRAFANA_URL": res["url"], "GRAFANA_TOKEN": clean(v.get("token")), "GRAFANA_ORG": res["org"] or "",
              "GRAFANA_VERSION": res["version"] or "", "GRAFANA_ROLE": res["role"] or "",
              "GRAFANA_ACCESS": ",".join(k for k, x in access.items() if x)}
    keep = {k: x for k, x in old.items() if k not in GRAFANA_KEYS}
    lines = ["# Grafana profile - written by setup_env.py --grafana (re-run it to change it; never share this file)",
             *(f"{k}={values[k]}" for k in GRAFANA_KEYS)]
    if keep:
        lines += ["# Other settings", *(f"{k}={x}" for k, x in keep.items())]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return path


def grafana_summary(res: dict, path: Path | None) -> dict:
    """Result for the agent / the window: No token."""
    tag = {True: "OK  ", False: "FAIL", None: "WARN"}
    dss = res["datasources"]
    s = {"saved": path.as_posix() if path else None, "url": res["url"], "organization": res["org"],
         "grafana_version": res["version"], "role": res["role"],
         "token_allows": GRAFANA_ROLES[res["role"]] if res["role"] else None,
         "data_sources": ", ".join(f"{d['name']} ({d['type']})" for d in dss) or "none" if dss is not None else None,
         "checks": [f"{tag[ok]} {n}" + (f": {d}" if d else "") for n, ok, d in res["checks"]]}
    return {k: x for k, x in s.items() if x is not None}


def run_grafana(v: dict, name: str = "") -> tuple[dict, int]:
    """Test + save a Grafana profile; name: Override (default from the host)."""
    res = inspect_grafana(v)
    name = clean(name).lower() or grafana_profile_name(res["url"])
    if res["ok"] and not NAME_RE.match(name or ""):
        res["checks"].append(["Profile name", False, "lowercase letters, numbers, ., - and _"])
        res["ok"] = False
    path = save_grafana(name, v, res) if res["ok"] else None
    return grafana_summary(res, path), 0 if path else 1


def setup_run(cloud: str, v: dict, name: str) -> tuple[dict, int]:
    """The window's / terminal's 'Test and save': A data lake profile, or a Grafana profile (cloud 'grafana')."""
    return run_grafana(v, name) if cloud == "grafana" else run(cloud, v, name)


def window(cloud: str, name: str = "") -> int:
    import tkinter as tk
    from tkinter import ttk

    spec = GRAFANA if cloud == "grafana" else CLOUDS[cloud]
    try:   # sharp text on scaled Windows displays
        import ctypes
        ctypes.windll.shcore.SetProcessDpiAwareness(1)
    except (AttributeError, OSError):
        pass
    root = tk.Tk()
    root.title(f"CANedge setup - {spec['title']}")
    root.resizable(False, False)
    f = ttk.Frame(root, padding=16)
    f.grid()
    ttk.Label(f, text="\n".join(spec["hint"]), justify="left").grid(row=0, column=0, columnspan=3, sticky="w", pady=(0, 10))
    entries = {}
    for i, fld in enumerate(spec["fields"], start=1):
        ttk.Label(f, text=fld.label).grid(row=i, column=0, sticky="w", pady=3)
        e = ttk.Entry(f, width=60, show="*" if fld.secret else "")
        e.grid(row=i, column=1, pady=3)
        entries[fld.name] = e
    row = len(spec["fields"]) + 1
    out = tk.Text(f, width=80, height=12, font=("Consolas", 9), state="disabled", wrap="word")
    out.grid(row=row + 1, column=0, columnspan=3, pady=(10, 0))
    result = {"code": 1, "summary": None}

    def show(text):
        out.configure(state="normal")
        out.delete("1.0", "end")
        out.insert("end", text)
        out.configure(state="disabled")

    def test_and_save():
        show("Testing ...")
        root.update()
        try:
            s, code = setup_run(cloud, {k: e.get() for k, e in entries.items()}, name)
        except Exception as ex:  # noqa: BLE001 - show any problem plainly (no secrets in these messages)
            show(f"Unexpected error: {ex.__class__.__name__}: {ex}")
            return
        result.update(code=code, summary=s)
        head = (f"Saved {s['saved']} - you can close this window and return to the agent.\n\n" if code == 0 else
                "Not saved - fix the items marked FAIL and click 'Test and save' again.\n\n")
        facts = [f"{k}: {x}" for k, x in s.items() if k not in ("saved", "checks")]
        show(head + "\n".join(s["checks"]) + "\n\n" + "\n".join(facts))

    ttk.Button(f, text="Test and save", command=test_and_save).grid(row=row, column=1, sticky="e", pady=(8, 0))
    root.lift()
    root.attributes("-topmost", True)
    root.after(800, lambda: root.attributes("-topmost", False))
    next(iter(entries.values())).focus_set()
    root.mainloop()
    print(json.dumps(result["summary"] or {"saved": None, "note": "window closed without saving"}, indent=1))
    return result["code"]


def cli(cloud: str, name: str = "") -> int:
    import getpass
    spec = GRAFANA if cloud == "grafana" else CLOUDS[cloud]
    print("\n".join(spec["hint"]))
    v = {}
    for fld in spec["fields"]:
        v[fld.name] = getpass.getpass(f"{fld.label} (hidden): ") if fld.secret else input(f"{fld.label}: ")
    s, code = setup_run(cloud, v, name)
    print(json.dumps(s, indent=1))
    return code


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--cloud", choices=list(CLOUDS), default="aws", help="The user's cloud (default: aws)")
    ap.add_argument("--key-file", metavar="PATH", help="Google data lake: The programmatic access key file (no window)")
    ap.add_argument("--bucket", default="", help="With --key-file: The input bucket name")
    ap.add_argument("--cli", action="store_true", help="Terminal prompts instead of the window")
    ap.add_argument("--grafana", action="store_true", help="A Grafana profile (URL + service account token) instead")
    ap.add_argument("--name", default="", help="Profile name instead of the input bucket / the Grafana host "
                                               "(e.g. playground)")
    a = ap.parse_args()
    if a.key_file:
        return key_file_setup(a.key_file, a.bucket, a.name)
    if not a.cli:
        try:
            import tkinter  # noqa: F401
        except ImportError:
            print("No Tk on this system - run this script yourself in a terminal with --cli")
            return 1
    cloud = "grafana" if a.grafana else a.cloud
    return cli(cloud, a.name) if a.cli else window(cloud, a.name)


if __name__ == "__main__":
    sys.exit(main())
