"""Athena backend: The CANedge S3 data lake via Amazon Athena + Glue catalog, keys + names from the data lake profile."""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

import pandas as pd

from _backends import MAX_REQUEST_USD, Lake, LakeError, check_sql, classify, load_env, use_env
from env import aws_client, lake, s3_problem

PRICE_PER_TB = 5.0
MIN_BILLED_BYTES = 10 * 1024 * 1024
SCHEMA_CACHE_DIR = Path("data/athena-schema")
SCHEMA_MAX_AGE_H = 24


# ---------------------------------------------------------------- credentials (data lake profile credentials/<name>.env)
def athena_config() -> dict:
    """Configuration without secrets (database/workgroup derived from the lake ID, see canedge-workspace env.py)."""
    lk = lake()
    return {"region": lk["region"], "database": lk["athena_database"], "workgroup": lk["athena_workgroup"],
            "profile": lk["profile"], "problem": s3_problem(athena=True)}


# ---------------------------------------------------------------- S3 data lake via Athena
class AthenaLake(Lake):
    """The CANedge S3 data lake via Amazon Athena (workgroup + database derived from the data lake profile)."""
    kind = "athena"
    needs_window = True
    meta_where = " WHERE date_created = '2024/01/01'"   # devicemeta's single partition
    workers = 4            # a few concurrent queries, well below the account quota (shared with Grafana)

    def __init__(self, env: str | None = None, max_scan_gb: float = 10.0, refresh_schema: bool = False,
                 offline_schema: bool = False, timeout: float = 600, reuse_minutes: int = 1440,
                 max_request_usd: float = MAX_REQUEST_USD):
        use_env(env)
        load_env()
        self.cfg = athena_config()
        self.max_scan_bytes = max_scan_gb * 1e9
        self.max_request_usd = max_request_usd
        self.scanned_bytes = 0
        self.queries = 0
        self.reused = 0
        self.timeout = timeout
        self.reuse_minutes = reuse_minutes   # Athena result reuse: The same SQL within this age costs nothing
        self.refresh_schema, self.offline_schema = refresh_schema, offline_schema
        self._tables: list[dict] | None = None
        self._names: dict[str, str | None] | None = None
        self._client = None
        self.schema_source = None

    def describe_source(self) -> str:
        return f"Athena database {self.cfg['database']}"

    def _require(self):
        if self.cfg["problem"]:
            raise LakeError(self.cfg["problem"])

    def client(self, service: str = "athena"):
        self._require()
        c = aws_client(service)
        if service == "athena":
            self._client = self._client or c
            return self._client
        return c

    # -------------------------------------------------------- schema (Glue catalog, cached 24 h)
    def tables(self) -> list[dict]:
        if self._tables is not None:
            return self._tables
        db = self.cfg["database"]
        cache = SCHEMA_CACHE_DIR / f"{db}.json" if db else None
        self._schema_cache = cache   # devices() keeps the meta names in it too
        if cache and cache.exists() and not self.refresh_schema:
            c = json.loads(cache.read_text(encoding="utf-8"))
            age_h = (dt.datetime.now(dt.timezone.utc) - dt.datetime.fromisoformat(c["updated_utc"])).total_seconds() / 3600
            if self.offline_schema or age_h < SCHEMA_MAX_AGE_H:
                self.schema_source = f"cache {cache.as_posix()} ({age_h:.1f} h old)"
                self._tables = [{**t, **classify(t["name"])} for t in c["tables"]]
                return self._tables
        if self.offline_schema:
            raise LakeError(f"No schema cache{' at ' + str(cache) if cache else ''} - run without --offline first")
        self._require()
        try:
            glue = self.client("glue")
            tables = []
            for page in glue.get_paginator("get_tables").paginate(DatabaseName=db):
                for t in page["TableList"]:
                    sd = t.get("StorageDescriptor", {})
                    tables.append({"name": t["Name"],
                                   "columns": [[c["Name"], c.get("Type", "?")] for c in sd.get("Columns", [])],
                                   "partition_keys": [[c["Name"], c.get("Type", "?")] for c in t.get("PartitionKeys", [])],
                                   "location": sd.get("Location")})
        except LakeError:
            raise
        except Exception as e:  # noqa: BLE001 - botocore errors: clean message, no secrets
            raise LakeError(f"Glue request failed: {e.__class__.__name__}: {e}") from None
        SCHEMA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"schema": "canedge-athena-schema/1", "database": db,
                                     "region": self.cfg["region"],
                                     "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "tables": tables}),
                         encoding="utf-8")
        self.schema_source = f"Glue database {db} ({len(tables)} tables, cached to {cache.as_posix()})"
        self._tables = [{**t, **classify(t["name"])} for t in tables]
        return self._tables

    def list_files(self, table: str, after: str = "") -> tuple[list[str], bool]:
        """Up to 1000 Parquet keys of a table from its S3 folder (Glue location), after <folder>/<after>."""
        m = re.fullmatch(r"s3://([^/]+)/(.*)", self.table(table).get("location") or "")
        if not m:
            raise LakeError(f"No S3 location for {table}")
        prefix = m.group(2).rstrip("/") + "/"
        kw = {"StartAfter": prefix + after} if after else {}
        r = self.client("s3").list_objects_v2(Bucket=m.group(1), Prefix=prefix, MaxKeys=1000, **kw)
        return [o["Key"] for o in r.get("Contents", [])], bool(r.get("IsTruncated"))

    # -------------------------------------------------------- queries
    def run(self, sql: str, max_rows: int = 1000) -> dict:
        """Run one query: Rows (up to max_rows), scanned MB, cost estimate, times. Enforces the scan budget."""
        if self.scanned_bytes >= self.max_scan_bytes:
            raise LakeError(f"Scan budget reached: {self.scanned_bytes / 1e9:.2f} GB scanned in {self.queries} "
                            f"queries (max_scan_gb {self.max_scan_bytes / 1e9:g}). Ask the user, then re-run with a "
                            f"higher budget (--max-scan-gb / connect(..., max_scan_gb=...))")
        ath = self.client()
        kw = {"ResultReuseConfiguration": {"ResultReuseByAgeConfiguration": {
            "Enabled": True, "MaxAgeInMinutes": self.reuse_minutes}}} if self.reuse_minutes else {}
        try:
            qid = ath.start_query_execution(
                QueryString=sql, WorkGroup=self.cfg["workgroup"],
                QueryExecutionContext={"Database": self.cfg["database"], "Catalog": "AwsDataCatalog"},
                **kw)["QueryExecutionId"]
            t0, delay = time.time(), 0.5
            try:
                while True:
                    ex = ath.get_query_execution(QueryExecutionId=qid)["QueryExecution"]
                    state = ex["Status"]["State"]
                    if state in ("SUCCEEDED", "FAILED", "CANCELLED"):
                        break
                    if time.time() - t0 > self.timeout:
                        ath.stop_query_execution(QueryExecutionId=qid)
                        raise LakeError(f"Query exceeded the timeout ({self.timeout:.0f} s) and was stopped ({qid})")
                    time.sleep(delay)
                    delay = min(delay * 1.5, 5)
            except KeyboardInterrupt:
                ath.stop_query_execution(QueryExecutionId=qid)
                raise
        except LakeError:
            raise
        except Exception as e:  # noqa: BLE001 - botocore errors: clean message, no secrets
            raise LakeError(f"Athena request failed: {e.__class__.__name__}: {e}") from None
        st = ex.get("Statistics", {})
        scanned = st.get("DataScannedInBytes", 0)
        reused = bool(st.get("ResultReuseInformation", {}).get("ReusedPreviousResult"))
        self.scanned_bytes += scanned
        self.queries += 1
        self.reused += reused
        out = {"query_execution_id": qid, "state": state, "reused": reused,
               "error": ex["Status"].get("StateChangeReason") if state != "SUCCEEDED" else None,
               "data_scanned_mb": round(scanned / 1e6, 3),
               "cost_usd_est": (0 if reused else round(max(scanned, MIN_BILLED_BYTES) / 1e12 * PRICE_PER_TB, 6))
               if state == "SUCCEEDED" else 0,
               "execution_s": round(st.get("TotalExecutionTimeInMillis", 0) / 1000, 2),
               "engine_s": round(st.get("EngineExecutionTimeInMillis", 0) / 1000, 2),
               "columns": [], "column_types": [], "rows": [], "truncated": False}
        if state != "SUCCEEDED":
            return out
        cols, rows, first = None, [], True
        for page in ath.get_paginator("get_query_results").paginate(QueryExecutionId=qid):
            if cols is None:
                cols = [(c["Name"], c["Type"].lower()) for c in page["ResultSet"]["ResultSetMetadata"]["ColumnInfo"]]
            for r in page["ResultSet"]["Rows"]:
                vals = [d.get("VarCharValue") for d in r["Data"]]
                if first and vals == [c[0] for c in cols]:  # SELECT results start with a header row
                    first = False
                    continue
                first = False
                if len(rows) >= max_rows:
                    out["truncated"] = True
                    break
                rows.append([_convert(v, cols[i][1]) for i, v in enumerate(vals)])
            if out["truncated"]:
                break
        out["columns"] = [c[0] for c in cols or []]
        out["column_types"] = [c[1] for c in cols or []]
        out["rows"] = rows
        return out

    def sql(self, query: str, max_rows: int = 100_000, **_) -> pd.DataFrame:
        """Athena SQL -> DataFrame. Stops when the result has more than max_rows rows: Aggregate in SQL instead of
        paging raw rows out of S3 (resample on the fly, see resample())."""
        chk = check_sql(query, "athena")
        if chk["errors"]:
            raise LakeError("REFUSED: " + "; ".join(chk["errors"]))
        for w in chk["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        res = self.run(query, max_rows)
        if res["state"] != "SUCCEEDED":
            raise LakeError(f"{res['state']}: {res['error']} (query {res['query_execution_id']})")
        if res["truncated"]:
            raise LakeError(f"Result has more than {max_rows:,} rows - aggregate in SQL (resample to a coarser "
                            f"interval, GROUP BY day/device) instead of downloading raw rows")
        df = self.frame(res)
        df.attrs.update(data_scanned_mb=res["data_scanned_mb"], cost_usd_est=res["cost_usd_est"], reused=res["reused"],
                        execution_s=res["execution_s"], query_execution_id=res["query_execution_id"])
        return df

    @staticmethod
    def frame(res: dict) -> pd.DataFrame:
        """run() result -> DataFrame; timestamp/date columns tz-aware UTC (Athena returns t with ms precision)."""
        df = pd.DataFrame(res["rows"], columns=res["columns"])
        for c, ty in zip(res["columns"], res["column_types"]):
            if ty.startswith("timestamp") or ty == "date":
                df[c] = pd.to_datetime(df[c].astype("string").str.replace(r" UTC$", "", regex=True), utc=True,
                                       errors="coerce")
        return df

    def budget(self) -> str:
        billed = self.queries - self.reused
        return (f"{self.queries} Athena queries ({self.reused} reused, free), {self.scanned_bytes / 1e6:.1f} MB "
                f"scanned, est. scan cost ${max(self.scanned_bytes, MIN_BILLED_BYTES * billed) / 1e12 * PRICE_PER_TB:.4f} "
                "(rough)" + self.request_text("S3"))


def _convert(v: str | None, typ: str):
    if v is None:
        return None
    try:
        if typ in ("tinyint", "smallint", "integer", "bigint", "int"):
            return int(v)
        if typ in ("double", "float", "real", "decimal") or typ.startswith("decimal"):
            return float(v)
    except ValueError:
        pass
    return v
