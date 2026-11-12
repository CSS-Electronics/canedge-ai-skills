"""Check that the CANedge AI skill suite is ready to use. Never prints secret values.

Usage:
  python check_setup.py [--selftest] [--cloud] [--bigquery] [--env NAME] [--grafana [NAME]]

Checks: Python version, required packages, the CSS tools (installed version vs the pinned one), the data lake profiles
(credentials/<name>.env, values never printed; Azure: the Synapse login), the BigQuery key file (only when set or
the profile is Google), folder layout. With --selftest: Runs the tools end to end on the bundled sample-data/ (MF4
header, mdf2csv conversion, DBC decoding into a temp folder that is deleted). With --cloud: The setup window's tests
on the profile (keys, buckets, Athena/BigQuery; Azure: Synapse login, mapped tables, one OPENROWSET read).
With --bigquery: Lists the datasets the BigQuery key can see. With --grafana: The Grafana setup window's tests on the
Grafana profile (credentials/grafana/<name>.env; the only one, or NAME): Reachable, token valid, what it allows, data
sources. The token is never printed.
"""
from __future__ import annotations

import argparse
import importlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from env import (KEY_TYPES, SETUP, bigquery_key, grafana_profile, grafana_profiles, lake, load_env, profile_problem,
                 profiles, synapse_config, synapse_password, use_env)
from mf4_files import find_log_files, read_header
from tools import INSTALL, TOOLS, find, mdf2csv_tables, resolve, version

REPO = Path(__file__).resolve().parents[4]
SAMPLE = REPO / "sample-data"

PACKAGES = ["numpy", "pandas", "pyarrow", "duckdb", "cantools", "openpyxl", "jsonschema", "matplotlib", "boto3",
            "google.cloud.bigquery", "sqlglot", "grafana_client", "pymssql", "Crypto"]


def tool_status(name: str) -> str:
    """Version of an installed CSS tool (or the CANEDGE_MDF2CSV / CANEDGE_MDF2PARQUET override), MISSING if absent."""
    p = find(name)
    if not p.exists():
        return f"MISSING - install it with: {INSTALL}"
    v = version(p)
    if v == "unknown":
        return f"found, but it does not run here (wrong OS build?) - run: {INSTALL} --force"
    pinned = TOOLS[name]["version"]
    return v if v.startswith(pinned) else f"{v} (pinned version: {pinned} - update with: {INSTALL} --force)"


def selftest() -> bool:
    """The tools end to end on sample-data/: MF4 header, mdf2csv frames, decoded message tables (temp lake)."""
    files = find_log_files([SAMPLE])
    if not files:
        print("Self-test: sample-data/ not found - skipped")
        return True
    try:
        h = read_header(files[0])
        frames = sum(t.num_rows for _, t in mdf2csv_tables(files[0], resolve("mdf2csv")))
        dbcs = [f"--dbc-{d.name.split('-')[0]}={d}" for d in sorted((SAMPLE / "dbc").glob("*.dbc"))]
        with tempfile.TemporaryDirectory(prefix="canedge-selftest-") as tmp:
            r = subprocess.run([sys.executable, str(REPO / ".claude/skills/canedge-mdf2parquet/scripts/build_lake.py"),
                                "--input", str(SAMPLE), *dbcs, "--out", tmp, "--json"], capture_output=True, text=True,
                               timeout=600)
            out = json.loads(r.stdout[r.stdout.index("{"):]) if "{" in r.stdout else {}
        dev = next(iter(out.get("devices", {}).values()), {})
        ok = h["header"] == "mf4" and frames > 0 and r.returncode == 0 and (dev.get("message_folders") or 0) > 0
        print(f"Self-test: {'OK' if ok else 'FAILED'} ({len(files)} sample files, header {h['serial']} FW {h['firmware']}, "
              f"{frames:,} frames in file 1, {dev.get('files_decoded', 0)} files decoded into "
              f"{dev.get('message_folders', 0)} message tables)")
        if r.returncode:
            print("  " + (r.stderr or r.stdout).strip()[-300:])
        return ok
    except Exception as e:  # noqa: BLE001 - report any tool problem plainly
        print(f"Self-test: FAILED ({e.__class__.__name__}: {e})")
        return False


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--selftest", action="store_true", help="Also run the tools end to end on sample-data/")
    ap.add_argument("--cloud", action="store_true", help="Also test the profile (keys, buckets, Athena/BigQuery)")
    ap.add_argument("--bigquery", action="store_true", help="Also test the BigQuery key (list datasets)")
    ap.add_argument("--env", metavar="NAME", help="Data lake profile credentials/NAME.env (default: the only profile)")
    ap.add_argument("--grafana", nargs="?", const="", metavar="NAME", help="Also test the Grafana profile "
                                                                          "credentials/grafana/NAME.env (default: the only one)")
    a = ap.parse_args()
    use_env(a.env)
    ok = True

    print(f"Python {sys.version.split()[0]} ({sys.executable})")
    if ".venv" not in sys.executable:
        print("  WARNING: Not running from the project .venv - use .venv\\Scripts\\python.exe")
    if sys.version_info < (3, 10):
        print("  ERROR: Python 3.10+ required"); ok = False

    missing = []
    for p in PACKAGES:
        try:
            importlib.import_module(p)
        except ImportError:
            missing.append(p)
    print("Packages: " + ("OK" if not missing else "MISSING " + ", ".join(missing) + " - run install.bat"))
    ok &= not missing

    for name in TOOLS:
        v = tool_status(name)
        print(f"Tool {name}: {v}")
        ok &= not v.startswith("MISSING")

    load_env()
    print("Data lake profiles: " + (", ".join(profiles()) or f"none yet (open the setup window: {SETUP})"))
    problem = profile_problem()
    cloud = None
    if problem and profiles():
        print(f"  {problem}")
    elif not problem:
        lk = lake()
        cloud = lk["cloud"]
        syn = synapse_config() if cloud == "azure" else None
        if syn:
            print(f"  Using {lk['profile']} (azure): Synapse {syn['endpoint'] or '-'}, database "
                  f"{syn['database'] or '-'}, user {syn['user']}"
                  + (f" - MISSING {', '.join(syn['missing'])}" if syn["missing"] else ""))
        else:
            print(f"  Using {lk['profile']} ({lk['cloud']}): input bucket {lk['input_bucket'] or '-'}, region "
                  f"{lk['region'] or '-'}, lake ID {lk['lake_id'] or '-'}" +
                  (f", S3 output bucket {lk['output_bucket']}, Athena {lk['athena_database']} / "
                   f"{lk['athena_workgroup']}" if lk['lake_id'] else ""))
        kind = os.environ.get("CANEDGE_KEY_TYPE")
        if kind in KEY_TYPES:
            print(f"  Keys: {KEY_TYPES[kind]}")
    key = bigquery_key()
    if key or cloud == "google":  # AWS/S3 users never need BigQuery: no hint
        print("  BigQuery: " + (f"key file {key}" + ("" if key.exists() else " NOT FOUND") if key else
                                 "no key (Google data lake: setup_env.py --key-file <key file> --bucket <input bucket>)"))

    if grafana_profiles():   # optional: Only listed once one exists
        print("Grafana profiles: " + ", ".join(grafana_profiles()))

    if Path.cwd().resolve() != REPO:
        print(f"Working directory: {Path.cwd()} is not the repo root - run the scripts from {REPO}")
        ok = False
    else:
        for d in ("data", "projects"):
            Path(d).mkdir(exist_ok=True)
        print("Folders: data/ and projects/ ready")

    if a.cloud:
        if problem:
            print(f"Cloud check: {problem}")
            ok = False
        else:
            from setup_env import inspect_credentials
            lk = lake()
            v = {"key_id": os.environ.get("CANEDGE_ACCESS_KEY_ID"), "secret": os.environ.get("CANEDGE_SECRET_ACCESS_KEY"),
                 "bucket": lk["input_bucket"], "endpoint": lk["endpoint"],
                 "key_file": os.environ.get("CANEDGE_GOOGLE_KEY_FILE") if lk["cloud"] == "google" else None}
            if lk["cloud"] == "azure":
                syn = synapse_config()
                v = {"endpoint": syn["endpoint"], "database": syn["database"], "user": syn["user"],
                     "password": synapse_password()}
            try:
                res = inspect_credentials(lk["cloud"], v)
            except Exception as e:  # noqa: BLE001 - report any connectivity problem plainly
                res = {"checks": [["Cloud check", False, f"{e.__class__.__name__}: {e}"]]}
            for name, good, detail in res["checks"]:
                tag = "WARN" if good is None else "OK  " if good else "FAIL"
                print(f"Cloud {tag} {name}" + (f": {detail}" if detail else ""))
            ok &= bool(res.get("ok"))
            if res.get("region") and res["region"] != lk["region"]:
                print(f"  Region changed ({res['region']}) - re-run the setup window"); ok = False

    if a.bigquery:
        try:
            if not (key and key.exists()):
                raise RuntimeError("no BigQuery key file")
            from google.cloud import bigquery
            from google.oauth2 import service_account
            cr = service_account.Credentials.from_service_account_file(str(key))
            ds = [d.dataset_id for d in bigquery.Client(credentials=cr, project=cr.project_id).list_datasets()]
            print(f"BigQuery: OK (project {cr.project_id}, datasets: {', '.join(ds) or '-'})")
        except Exception as e:  # noqa: BLE001 - report any connectivity problem plainly (no key content)
            print(f"BigQuery check failed: {e.__class__.__name__}: {str(e).splitlines()[0] if str(e) else ''}")
            ok = False

    if a.grafana is not None:
        try:
            from setup_env import grafana_summary, inspect_grafana
            prof = grafana_profile(a.grafana or None, exc=RuntimeError)
            s = grafana_summary(inspect_grafana({"url": prof["GRAFANA_URL"], "token": prof["GRAFANA_TOKEN"]}), None)
            print(f"Grafana ({prof['path']}): {s.get('url')}, role {s.get('role', '-')}")
            for c in s["checks"]:
                print(f"  {c}")
            ok &= not any(c.startswith("FAIL") for c in s["checks"])
        except Exception as e:  # noqa: BLE001 - no profile, connection problems (the token is never in the message)
            print(f"Grafana check: {str(e).replace('--grafana-env', '--grafana')}")
            ok = False

    if a.selftest:
        ok &= selftest()

    print("READY" if ok else "NOT READY - fix the items above")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
