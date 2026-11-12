"""BigQuery backend: The CANedge data lake on Google Cloud (external tables over the Parquet bucket), read-only.

Credentials: The service account JSON key of the data lake's BigQuery user. The user copies the key file into the
workspace folder credentials/ (gitignored); CANEDGE_GOOGLE_KEY_FILE in a data lake profile credentials/<name>.env points to another
path. Optional: CANEDGE_BIGQUERY_DATASET (default: the project's only dataset), CANEDGE_BIGQUERY_PROJECT (default:
the key's project).

SQL: The same Athena (Trino) dialect as on the other backends, translated with sqlglot, plus:
  - date_created (the Athena partition column; BigQuery tables have none) -> FORMAT_TIMESTAMP('%Y/%m/%d', t)
  - from_unixtime / to_unixtime -> TIMESTAMP_MICROS / UNIX_MICROS (sub-second buckets stay exact)
  - TIMESTAMP '...' literals stay TIMESTAMP (UTC); table names get their real case and the dataset
Costs: On-demand $6.25 per TiB billed, min 10 MB per table and query (the printed estimate; Cloud Storage reads are
billed separately: fleet()/summary() estimate them with the request guard, see _backends.py). The scan budget is
passed to BigQuery as maximum_bytes_billed: A query that would exceed it fails without cost.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd

from _backends import MAX_REQUEST_USD, Lake, LakeError, check_sql, classify, load_env, use_env, utc_frame
from env import KEY_DIR, bigquery_key, profile_path

PRICE_PER_TIB = 6.25
MIN_BILLED_BYTES = 10 * 1024 * 1024
SCHEMA_CACHE_DIR = Path("data/bigquery-schema")
SCHEMA_MAX_AGE_H = 24


# ---------------------------------------------------------------- credentials
def key_file() -> Path | None:
    """CANEDGE_GOOGLE_KEY_FILE, else the only service account key (*.json) in credentials/."""
    return bigquery_key()


def bigquery_config() -> dict:
    """Configuration without secrets."""
    k = key_file()
    project = os.environ.get("CANEDGE_BIGQUERY_PROJECT")
    if k and k.exists() and not project:
        try:
            project = json.loads(k.read_text(encoding="utf-8")).get("project_id")
        except (OSError, ValueError):
            pass
    return {"key_file": str(k) if k else None, "key_found": bool(k and k.exists()), "project": project,
            "dataset": os.environ.get("CANEDGE_BIGQUERY_DATASET") or None, "profile": profile_label(),
            "missing": [] if k and k.exists() else ["CANEDGE_GOOGLE_KEY_FILE"]}


def profile_label() -> str:
    p = profile_path()
    return p.as_posix() if p else f"{KEY_DIR}/<name>.env"


def creds_error() -> str:
    return (f"BigQuery is not configured: No service account key found. Ask the user to copy the JSON key of the data "
            f"lake's BigQuery user into {KEY_DIR}/ (gitignored; one key there is picked up automatically), or set "
            f"CANEDGE_GOOGLE_KEY_FILE in {profile_label()} (setup_env.py --key-file). Never paste the key into the chat.")


# ---------------------------------------------------------------- SQL translation
def to_bigquery(sql: str, real_names: dict[str, str], dataset: str) -> str:
    """Athena (Trino) SQL -> BigQuery SQL (see the module docstring)."""
    import logging

    import sqlglot
    from sqlglot import exp
    logging.getLogger("sqlglot").setLevel(logging.ERROR)   # e.g. 'NULLS LAST not supported' notes (no effect on results)
    tree = sqlglot.parse_one(sql, read="trino")

    def unix(n):       # to_unixtime(t) -> UNIX_MICROS(t) / 1e6 (sqlglot would write TIME_TO_UNIX, not BigQuery)
        if isinstance(n, exp.TimeToUnix):
            return exp.Div(this=exp.Anonymous(this="UNIX_MICROS", expressions=[n.this]),
                           expression=exp.Literal.number("1e6"))
        return n

    def from_unix(n):  # from_unixtime(x) -> TIMESTAMP_MICROS(CAST(ROUND(x * 1e6) AS INT64)) (x may be fractional)
        if isinstance(n, exp.UnixToTime):
            micros = exp.Round(this=exp.Mul(this=n.this, expression=exp.Literal.number("1e6")))
            return exp.Anonymous(this="TIMESTAMP_MICROS", expressions=[exp.Cast(this=micros,
                                                                                to=exp.DataType.build("BIGINT"))])
        return n

    def rest(n):
        if isinstance(n, exp.Cast) and n.to.this == exp.DataType.Type.TIMESTAMP and isinstance(n.this, exp.Literal):
            return exp.Anonymous(this="TIMESTAMP", expressions=[n.this])     # not DATETIME: t is a TIMESTAMP
        if isinstance(n, exp.Column) and n.name.lower() == "date_created":
            return sqlglot.parse_one("FORMAT_TIMESTAMP('%Y/%m/%d', t)", read="bigquery")
        if isinstance(n, exp.Table) and n.name.lower() in real_names and not n.args.get("db"):
            t = exp.to_table(f"{dataset}.{real_names[n.name.lower()]}", dialect="bigquery")
            if n.alias:
                t.set("alias", n.args["alias"])
            return t
        return n

    for f in (unix, from_unix, rest):
        tree = tree.transform(f)
    return tree.sql(dialect="bigquery")


# ---------------------------------------------------------------- the data lake in BigQuery
class BigQueryLake(Lake):
    """The CANedge data lake in Google BigQuery (external tables over the Parquet bucket, read-only key)."""
    kind = "bigquery"
    workers = 4

    def __init__(self, env: str | None = None, max_scan_gb: float = 10.0, refresh_schema: bool = False,
                 offline_schema: bool = False, timeout: float = 600, max_request_usd: float = MAX_REQUEST_USD, **_):
        use_env(env)
        load_env()
        self.cfg = bigquery_config()
        self.max_scan_bytes = max_scan_gb * 1e9
        self.max_request_usd = max_request_usd
        self.scanned_bytes = self.billed_bytes = 0
        self.queries = 0
        self.timeout = timeout
        self.refresh_schema, self.offline_schema = refresh_schema, offline_schema
        self._tables: list[dict] | None = None
        self._names: dict[str, str | None] | None = None
        self._client = self._storage = None
        self.schema_source = None

    def describe_source(self) -> str:
        return f"BigQuery dataset {self.cfg['project']}.{self.cfg['dataset'] or '?'}"

    def client(self):
        if self._client is None:
            if self.cfg["missing"]:
                raise LakeError(creds_error())
            from google.cloud import bigquery
            from google.oauth2 import service_account
            try:
                cr = service_account.Credentials.from_service_account_file(self.cfg["key_file"])
            except Exception as e:  # noqa: BLE001 - never echo the key content
                raise LakeError(f"Cannot read the BigQuery key file {self.cfg['key_file']}: {e.__class__.__name__}") from None
            self._client = bigquery.Client(credentials=cr, project=self.cfg["project"])
            self._creds = cr
        return self._client

    def dataset(self) -> str:
        if not self.cfg["dataset"]:
            ds = [d.dataset_id for d in self.client().list_datasets()]
            if len(ds) != 1:
                raise LakeError(f"Project {self.cfg['project']} has {len(ds)} datasets ({', '.join(ds) or '-'}): Set "
                                f"CANEDGE_BIGQUERY_DATASET in {profile_label()} (the dataset of the data lake)")
            self.cfg["dataset"] = ds[0]
        return self.cfg["dataset"]

    # -------------------------------------------------------- schema (INFORMATION_SCHEMA, cached 24 h)
    def tables(self) -> list[dict]:
        if self._tables is not None:
            return self._tables
        cache = SCHEMA_CACHE_DIR / f"{self.cfg['project']}.{self.cfg['dataset'] or '_default'}.json"
        self._schema_cache = cache   # devices() keeps the meta names in it too
        if cache.exists() and not self.refresh_schema:
            c = json.loads(cache.read_text(encoding="utf-8"))
            age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(c["updated_utc"])).total_seconds() / 3600
            if self.offline_schema or age_h < SCHEMA_MAX_AGE_H:
                self.cfg["dataset"] = self.cfg["dataset"] or c["dataset"]
                self.schema_source = f"cache {cache.as_posix()} ({age_h:.1f} h old)"
                self._tables = [{**t, **classify(t["name"])} for t in c["tables"]]
                return self._tables
        if self.offline_schema:
            raise LakeError(f"No schema cache at {cache} - run without --offline first")
        ds = self.dataset()
        res = self.run(f"SELECT table_name, column_name, data_type FROM `{self.cfg['project']}.{ds}`."
                       f"INFORMATION_SCHEMA.COLUMNS ORDER BY table_name, ordinal_position", max_rows=10_000_000,
                       translate=False)
        if res["state"] != "SUCCEEDED":
            raise LakeError(f"Schema query failed: {res['error']}")
        by_table: dict[str, list] = {}
        for tname, col, ty in res["rows"]:
            by_table.setdefault(tname, []).append([col.lower(), ty.lower()])
        tables = []
        for tname, cols in by_table.items():
            name = tname.lower()
            if classify(name)["kind"] == "message":   # date_created is emulated from t (see to_bigquery)
                cols = cols + [["date_created", "string"]]
            tables.append({"name": name, "bq_name": tname, "columns": cols, "partition_keys": [],
                           "location": f"{self.cfg['project']}.{ds}.{tname}"})
        SCHEMA_CACHE_DIR.mkdir(parents=True, exist_ok=True)   # same file name as read above (auto dataset: _default)
        cache.write_text(json.dumps({"schema": "canedge-bigquery-schema/1", "project": self.cfg["project"],
                                     "dataset": ds, "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat(),
                                     "tables": tables}), encoding="utf-8")
        self.schema_source = f"INFORMATION_SCHEMA of {self.cfg['project']}.{ds} ({len(tables)} tables, cached)"
        self._tables = [{**t, **classify(t["name"])} for t in tables]
        return self._tables

    def list_files(self, table: str, after: str = "") -> tuple[list[str], bool]:
        """Up to 1000 Parquet object names of a table from its Cloud Storage folder (the external table's source
        URI gs://<bucket>/<DEVICE>/<Message>/*), from <folder>/<after> on (JSON API, same key)."""
        t = self.table(table)
        if "source_uri" not in t:   # cached on the table entry for this connection
            ext = self.client().get_table(t["location"]).external_data_configuration
            t["source_uri"] = (ext.source_uris or [None])[0] if ext else None
        m = re.fullmatch(r"gs://([^/]+)/(.*?)\*?", t["source_uri"] or "")
        if not m:
            raise LakeError(f"No Cloud Storage source for {table}")
        if self._storage is None:
            from google.auth.transport.requests import AuthorizedSession
            self.client()
            self._storage = AuthorizedSession(
                self._creds.with_scopes(["https://www.googleapis.com/auth/devstorage.read_only"]))
        params = {"prefix": m.group(2), "maxResults": 1000, "fields": "items(name),nextPageToken"}
        if after:
            params["startOffset"] = m.group(2) + after
        r = self._storage.get(f"https://storage.googleapis.com/storage/v1/b/{m.group(1)}/o", params=params, timeout=30)
        r.raise_for_status()
        j = r.json()
        return [i["name"] for i in j.get("items", [])], bool(j.get("nextPageToken"))

    # -------------------------------------------------------- queries
    def translate(self, sql: str) -> str:
        real = {t["name"]: t["bq_name"] for t in self.tables()}
        try:
            return to_bigquery(sql, real, self.dataset())
        except Exception as e:  # noqa: BLE001 - sqlglot parse errors: one clean line
            raise LakeError(f"Cannot translate the SQL to BigQuery: {str(e).splitlines()[0]}") from None

    def run(self, sql: str, max_rows: int = 1000, translate: bool = True) -> dict:
        """Run one query (Athena-dialect SQL, translated): Rows (up to max_rows), billed MB, cost, times."""
        left = self.max_scan_bytes - self.billed_bytes
        if left <= 0:
            raise LakeError(f"Scan budget reached: {self.billed_bytes / 1e9:.2f} GB billed in {self.queries} queries "
                            f"(max_scan_gb {self.max_scan_bytes / 1e9:g}). Ask the user, then re-run with a higher "
                            f"budget (connect(..., max_scan_gb=...))")
        from google.cloud import bigquery
        bq_sql = self.translate(sql) if translate else sql
        cfg = bigquery.QueryJobConfig(maximum_bytes_billed=max(int(left), MIN_BILLED_BYTES), use_query_cache=True)
        t0 = time.time()
        try:
            job = self.client().query(bq_sql, job_config=cfg)
            it = job.result(timeout=self.timeout, max_results=max_rows + 1)
            rows = [list(r.values()) for r in it]
            cols = [f.name for f in it.schema]
        except LakeError:
            raise
        except Exception as e:  # noqa: BLE001 - google errors: one clean line (no credentials in them)
            msg = str(getattr(e, "message", "") or e).splitlines()[0]
            return {"query_execution_id": getattr(locals().get("job"), "job_id", None), "state": "FAILED",
                    "error": f"{e.__class__.__name__}: {msg}", "sql_bigquery": bq_sql, "data_scanned_mb": 0,
                    "cost_usd_est": 0, "execution_s": round(time.time() - t0, 2), "engine_s": 0, "reused": False,
                    "columns": [], "column_types": [], "rows": [], "truncated": False}
        billed = job.total_bytes_billed or 0
        self.billed_bytes += billed
        self.scanned_bytes += job.total_bytes_processed or 0
        self.queries += 1
        truncated = len(rows) > max_rows
        return {"query_execution_id": job.job_id, "state": "SUCCEEDED", "error": None, "sql_bigquery": bq_sql,
                "reused": bool(job.cache_hit), "data_scanned_mb": round((job.total_bytes_processed or 0) / 1e6, 3),
                "billed_mb": round(billed / 1e6, 3), "cost_usd_est": round(billed / 2 ** 40 * PRICE_PER_TIB, 6),
                "execution_s": round(time.time() - t0, 2),
                "engine_s": round(((job.ended or job.started) - job.started).total_seconds(), 2) if job.started else 0,
                "columns": cols, "column_types": [f.field_type.lower() for f in it.schema],
                "rows": rows[:max_rows], "truncated": truncated}

    def sql(self, query: str, max_rows: int = 100_000, **_) -> pd.DataFrame:
        """Athena-dialect SQL -> translated -> DataFrame. Stops when the result has more than max_rows rows."""
        chk = check_sql(query, "bigquery")
        if chk["errors"]:
            raise LakeError("REFUSED: " + "; ".join(chk["errors"]))
        for w in chk["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        res = self.run(query, max_rows)
        if res["state"] != "SUCCEEDED":
            raise LakeError(f"{res['state']}: {res['error']}")
        if res["truncated"]:
            raise LakeError(f"Result has more than {max_rows:,} rows - aggregate in SQL (resample to a coarser "
                            f"interval, GROUP BY day/device) instead of downloading raw rows")
        df = self.frame(res)
        df.attrs.update(data_scanned_mb=res["data_scanned_mb"], billed_mb=res["billed_mb"],
                        cost_usd_est=res["cost_usd_est"], query_execution_id=res["query_execution_id"])
        return df

    @staticmethod
    def frame(res: dict) -> pd.DataFrame:
        df = pd.DataFrame(res["rows"], columns=res["columns"])
        for c, ty in zip(res["columns"], res["column_types"]):
            if ty in ("timestamp", "datetime", "date"):
                df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
        return utc_frame(df)

    def budget(self) -> str:
        return (f"{self.queries} BigQuery queries, {self.billed_bytes / 1e6:.1f} MB billed, est. scan cost "
                f"${self.billed_bytes / 2 ** 40 * PRICE_PER_TIB:.4f} (rough)" + self.request_text("Cloud Storage"))
