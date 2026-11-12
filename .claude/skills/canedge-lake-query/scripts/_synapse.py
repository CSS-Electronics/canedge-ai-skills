"""Synapse backend: The CANedge data lake on Azure via the Synapse serverless SQL pool (SQL login), read-only.

Credentials: An Azure data lake profile credentials/<name>.env (CANEDGE_CLOUD=azure; setup_env.py --cloud azure):
CANEDGE_SYNAPSE_ENDPOINT (<workspace>-ondemand.sql.azuresynapse.net), CANEDGE_SYNAPSE_DATABASE, CANEDGE_SYNAPSE_USER
(default sqladminuser), CANEDGE_SYNAPSE_PASSWORD. Connection via pymssql (no ODBC driver).

Schema: The external tables of the Synapse mapping job (Container App Job synapse-map-tables-<id>):
tbl_<DEVICE>_<Message> in real case over the folders <DEVICE>/<Message> of the output container (data source ParquetDataLake), plus
tbl_aggregations_devicemeta. Trip summaries and events (aggregations/tripsummary, aggregations/events) are not mapped by
the job: They are listed when their folder holds data (columns from the first file). New devices/messages appear
after the mapping job has run again.

SQL: The same Athena (Trino) dialect as on the other backends, translated to T-SQL with sqlglot (to_synapse):
  - message/aggregation tables -> OPENROWSET(BULK '<DEVICE>/<Message>/*/*/*/*', DATA_SOURCE = 'ParquetDataLake',
    FORMAT = 'PARQUET') AS <alias> (the official Synapse query form: The mapped tables hold names only, no data rows)
  - date_created -> CONCAT(<alias>.filepath(1), '/', <alias>.filepath(2), '/', <alias>.filepath(3)): Filters on it
    skip whole yyyy/mm/dd folders (partition pruning, as in the CANedge Grafana-Synapse dashboards)
  - from_unixtime / to_unixtime / date_trunc / date_diff / approx_distinct -> DATEADD / DATEDIFF(_BIG) /
    APPROX_COUNT_DISTINCT; GROUP BY ordinals -> the expressions; ORDER BY in subqueries dropped; nested CTEs inlined
    to the top with unique names
  - Not available (clear error): max_by/min_by (resample first/last), approx_percentile, arrays/lambdas
Costs: Synapse bills ~$5 per TB processed (10 MB minimum per query). Data processed is NOT tracked here: The date window
(needs_window) + filepath pruning keep scans small; an admin can set a hard limit in Synapse
(sp_set_data_processed_limit). max_scan_gb does not apply.
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import threading
import time
from pathlib import Path

import pandas as pd

from _backends import Lake, LakeError, check_sql, classify, load_env, use_env, utc_frame
from env import synapse_config, synapse_connect, synapse_error, synapse_password

DATA_SOURCE = "ParquetDataLake"   # created by the Synapse mapping job (the output container)
SCHEMA_CACHE_DIR = Path("data/synapse-schema")
SCHEMA_MAX_AGE_H = 24
AGGREGATIONS = {"tbl_aggregations_tripsummary": "aggregations/tripsummary",   # not mapped by the job: probed
                "tbl_aggregations_events": "aggregations/events"}
TYPES = {"float": "double", "real": "double", "datetime": "timestamp", "datetime2": "timestamp", "bigint": "bigint",
         "int": "integer", "bit": "boolean", "varchar": "string", "nvarchar": "string", "char": "string",
         "nchar": "string", "varbinary": "binary", "date": "date", "decimal": "decimal", "smallint": "smallint",
         "tinyint": "tinyint"}
TRUNC_UNITS = {"second", "minute", "hour", "day", "month", "year"}


def openrowset(folder: str) -> str:
    """The official Synapse read of one table folder (yyyy/mm/dd below it: filepath(1..3))."""
    return f"OPENROWSET(BULK '{folder}/*/*/*/*', DATA_SOURCE = '{DATA_SOURCE}', FORMAT = 'PARQUET')"


def creds_error(cfg: dict) -> str:
    """Why the profile cannot be used for Synapse: No/several profiles, not an Azure profile, or keys missing."""
    from env import profile_problem
    setup = ("Open the setup window (canedge-workspace setup_env.py --cloud azure) and let the user enter the Synapse "
             "login. Never ask for the password in the chat.")
    if (msg := profile_problem()):
        return f"{msg} (Azure: {setup})"
    cloud = os.environ.get("CANEDGE_CLOUD") or "aws"
    if cloud != "azure":
        return (f"{cfg['profile']} is not an Azure data lake profile (CANEDGE_CLOUD={cloud}): Use --athena / "
                f"--bigquery for it, or --env <azure profile>. No Azure profile yet: {setup}")
    return f"Synapse is not configured ({', '.join(cfg['missing'])} missing in {cfg['profile']}): {setup}"


# ---------------------------------------------------------------- SQL translation
def to_synapse(sql: str, folders: dict[str, str]) -> str:
    """Athena (Trino) SQL -> Synapse serverless T-SQL (see the module docstring). folders: lower-case table name ->
    real-case folder of the tables read with OPENROWSET; other tables stay as they are (e.g. the devicemeta table)."""
    import logging

    import sqlglot
    from sqlglot import exp
    logging.getLogger("sqlglot").setLevel(logging.ERROR)
    tree = sqlglot.parse_one(sql, read="trino")
    if not isinstance(tree, exp.Query):
        raise LakeError("Only SELECT / WITH queries can run on Synapse")

    # 1. Trino functions without a Synapse equivalent
    unsupported = [("ArgMax", "max_by (resample agg 'last')"), ("ArgMin", "min_by (resample agg 'first')"),
                   ("ApproxQuantile", "approx_percentile"), ("Lambda", "lambdas (transform, filter, ...)"),
                   ("Explode", "UNNEST"), ("Unnest", "UNNEST")]
    for cls, what in unsupported:
        node_type = getattr(exp, cls, None)
        if node_type and tree.find(node_type):
            raise LakeError(f"{what} is not available on Synapse - rewrite the query (e.g. avg/min/max per time "
                            "bucket; percentiles: a histogram with hist)")

    # 2. GROUP BY <n> -> the n-th select expression (T-SQL has no ordinals; before the rewrites below, so the copies
    #    get them too)
    for sel in list(tree.find_all(exp.Select)):
        grp = sel.args.get("group")
        if not grp:
            continue
        items = []
        for e in grp.expressions:
            if isinstance(e, exp.Literal) and e.is_int:
                k = int(e.this)
                if not 1 <= k <= len(sel.expressions):
                    raise LakeError(f"GROUP BY {k}: The select list has only {len(sel.expressions)} items")
                items.append(sel.expressions[k - 1].unalias().copy())
            else:
                items.append(e)
        grp.set("expressions", items)

    # 2b. Unnamed select items get Athena's names _col0, _col1, ... (T-SQL returns no name; derived tables need one)
    for sel in list(tree.find_all(exp.Select)):
        for i, e in enumerate(sel.expressions):
            if not isinstance(e, (exp.Alias, exp.Column, exp.Star)):
                e.replace(exp.alias_(e.copy(), f"_col{i}"))

    # 3. ORDER BY inside subqueries / CTEs (not allowed in T-SQL without TOP): Dropped (fleet() wraps queries)
    for sel in list(tree.find_all(exp.Select)):
        if sel is not tree and sel.args.get("order") and not sel.args.get("limit") and not sel.args.get("offset") \
                and not (isinstance(tree, exp.Union) and sel.parent is tree):
            sel.set("order", None)

    # 4. CTEs inside subqueries: Unique names (sqlglot moves them to the top level for T-SQL; UNION ALL batches of
    #    fleet() would otherwise define the same name several times)
    n = 0
    for w in list(tree.find_all(exp.With)):
        if w.parent is tree:
            continue
        owner = w.parent
        for cte in w.expressions:
            n += 1
            old, new = cte.alias, f"{cte.alias}_{n}"
            for t in owner.find_all(exp.Table):
                if t.name == old and not t.args.get("db"):
                    t.set("this", exp.to_identifier(new))
            cte.set("alias", exp.TableAlias(this=exp.to_identifier(new)))

    # 5. Tables -> OPENROWSET (placeholders, replaced after generation); remember the alias of each
    ors: dict[str, str] = {}      # placeholder -> folder
    aliases: dict[str, str] = {}  # placeholder -> alias
    for t in list(tree.find_all(exp.Table)):
        name = t.name.lower()
        if t.args.get("db") or name not in folders:
            continue
        ph = f"ors{len(ors)}__"
        ors[ph], aliases[ph] = folders[name], t.alias or name
        new = exp.Table(this=exp.to_identifier(ph), alias=exp.TableAlias(this=exp.to_identifier(aliases[ph])))
        t.replace(new)

    def sources(sel) -> dict[str, str]:
        """OPENROWSET sources directly in this SELECT: alias (lower) -> alias."""
        f = sel.args.get("from_") or sel.args.get("from")   # sqlglot >= 28: from_
        frm = [f.this] if f else []
        frm += [j.this for j in sel.args.get("joins") or []]
        return {aliases[s.name].lower(): aliases[s.name] for s in frm if isinstance(s, exp.Table) and s.name in aliases}

    fp: list[str] = []   # filepath placeholders: index -> alias
    for col in list(tree.find_all(exp.Column)):
        if col.name.lower() != "date_created":
            continue
        sel = col.find_ancestor(exp.Select)
        src = sources(sel) if sel else {}
        if col.table:
            alias = src.get(col.table.lower())
        elif len(src) > 1:
            raise LakeError("date_created is ambiguous (several tables in one SELECT): Qualify it with the table "
                            "alias, e.g. a.date_created")
        else:
            alias = next(iter(src.values()), None)
        if alias is None:   # a column of a subquery / CTE (already computed there)
            continue
        fp.append(alias)
        i = len(fp) - 1
        parts = [exp.Anonymous(this=f"fp{i}_{k}__") for k in (1, 2, 3)]
        new = exp.Anonymous(this="CONCAT", expressions=[parts[0], exp.Literal.string("/"), parts[1],
                                                         exp.Literal.string("/"), parts[2]])
        if isinstance(col.parent, exp.Select) and col.arg_key == "expressions":   # keep the column name
            new = exp.alias_(new, "date_created")
        col.replace(new)

    # 6. Functions (bottom-up, so nested calls are rewritten first)
    def tmpl(sql_t: str, **parts) -> exp.Expression:
        e = sqlglot.parse_one(sql_t, read="tsql")
        for c in list(e.find_all(exp.Column)):
            if c.name in parts:
                c.replace(parts[c.name].copy())
        return e

    # DATEDIFF_BIG on a column fails in serverless SQL ('not supported in distributed processing mode', error 15816):
    # Milliseconds = whole seconds as BIGINT * 1000 + the millisecond part (exact)
    epoch = "CAST('1970-01-01' AS DATETIME2(3))"
    for node in reversed(list(tree.dfs())):
        new = None
        if isinstance(node, exp.TimeToUnix):        # seconds since 1970 (ms precision, exact decimal)
            new = tmpl(f"(CAST(DATEDIFF(SECOND, {epoch}, x__) AS BIGINT) * 1000 + DATEPART(MILLISECOND, x__)) / 1000.0",
                       x__=node.this)
        elif isinstance(node, exp.DateDiff) and (node.unit.name if isinstance(node.unit, exp.Expression)
                                                 else str(node.unit or "")).lower() in ("millisecond", "ms"):
            # DATEDIFF(MILLISECOND) is INT: overflows after 24.8 days (also inside SUM) -> BIGINT seconds + ms parts
            new = tmpl("(CAST(DATEDIFF(SECOND, a__, b__) AS BIGINT) * 1000 + DATEPART(MILLISECOND, b__) - "
                       "DATEPART(MILLISECOND, a__))", a__=node.expression, b__=node.this)
        elif isinstance(node, exp.UnixToTime):      # DATEADD takes int: whole days + milliseconds of the day
            ms = "CAST(ROUND(x__ * 1000, 0) AS BIGINT)"
            new = tmpl(f"DATEADD(MILLISECOND, CAST({ms} % 86400000 AS INT), DATEADD(DAY, CAST({ms} / 86400000 AS INT), "
                       f"{epoch}))", x__=node.this)
        elif isinstance(node, (exp.TimestampTrunc, exp.DateTrunc)):
            unit = (node.unit.name if isinstance(node.unit, exp.Expression) else str(node.unit or "")).lower()
            if unit not in TRUNC_UNITS:
                raise LakeError(f"date_trunc('{unit}', ...) is not available on Synapse (use "
                                f"{', '.join(sorted(TRUNC_UNITS))})")
            new = tmpl(f"DATEADD({unit.upper()}, DATEDIFF({unit.upper()}, {epoch}, x__), {epoch})", x__=node.this)
        elif isinstance(node, exp.ApproxDistinct):
            new = exp.Anonymous(this="APPROX_COUNT_DISTINCT", expressions=[node.this.copy()])
        elif isinstance(node, exp.IsNan):           # T-SQL FLOAT holds no NaN values
            new = sqlglot.parse_one("1 = 0", read="tsql")
        elif isinstance(node, (exp.NullSafeNEQ, exp.NullSafeEQ)):   # IS [NOT] DISTINCT FROM
            ne = tmpl("(a__ <> b__ OR (a__ IS NULL AND NOT b__ IS NULL) OR (NOT a__ IS NULL AND b__ IS NULL))",
                      a__=node.this, b__=node.expression)
            new = exp.paren(ne) if isinstance(node, exp.NullSafeNEQ) else exp.Not(this=exp.paren(ne))
        if new is not None:
            if node is tree:
                tree = new
            else:
                node.replace(new)

    out = tree.sql(dialect="tsql")
    for ph, folder in ors.items():
        out = re.sub(rf"(?<![\w\[]){re.escape(ph)}(?![\w\]])|\[{re.escape(ph)}\]", openrowset(folder), out)
    for i, alias in enumerate(fp):
        for k in (1, 2, 3):
            out = re.sub(rf"\bfp{i}_{k}__\(\)", f"[{alias}].filepath({k})", out, flags=re.I)
    return out


# ---------------------------------------------------------------- the data lake in Synapse
class SynapseLake(Lake):
    """The CANedge data lake in Azure Synapse (serverless SQL pool, OPENROWSET over the output container)."""
    kind = "synapse"
    needs_window = True
    workers = 4

    def __init__(self, env: str | None = None, refresh_schema: bool = False, offline_schema: bool = False,
                 timeout: float = 600, **_):
        use_env(env)
        load_env()
        self.cfg = synapse_config()
        self.max_request_usd = None   # no storage request guard (Synapse reads the storage itself)
        self.queries = 0
        self.timeout = timeout
        self.refresh_schema, self.offline_schema = refresh_schema, offline_schema
        self._tables: list[dict] | None = None
        self._names: dict[str, str | None] | None = None
        self._local = threading.local()   # one connection per thread (fleet() runs batches concurrently)
        self.schema_source = None

    def describe_source(self) -> str:
        return f"Synapse database {self.cfg['database'] or '?'} on {self.cfg['endpoint'] or '?'}"

    def conn(self):
        c = getattr(self._local, "conn", None)
        if c is None:
            if self.cfg["missing"]:
                raise LakeError(creds_error(self.cfg))
            try:
                c = synapse_connect(self.cfg["endpoint"], self.cfg["database"], self.cfg["user"], synapse_password(),
                                    timeout=self.timeout)
            except RuntimeError as e:
                raise LakeError(f"Cannot connect to Synapse ({self.describe_source()}): {e}") from None
            self._local.conn = c
        return c

    def _drop_conn(self):
        c = getattr(self._local, "conn", None)
        self._local.conn = None
        if c is not None:
            try:
                c.close()
            except Exception:  # noqa: BLE001 - already broken
                pass

    def _query(self, sql: str, max_rows: int) -> tuple[list[str], list, list, bool]:
        """Raw T-SQL -> (columns, type codes, rows, truncated). A truncated result closes the connection."""
        cur = self.conn().cursor()
        cur.execute(sql)
        desc = cur.description or []
        rows = cur.fetchmany(max_rows + 1) if desc else []
        truncated = len(rows) > max_rows
        if truncated:
            self._drop_conn()   # the rest of the result is not read
        return [d[0] for d in desc], [d[1] for d in desc], rows[:max_rows], truncated

    # -------------------------------------------------------- schema (sys.external_tables, cached 24 h)
    def tables(self) -> list[dict]:
        if self._tables is not None:
            return self._tables
        host = (self.cfg["endpoint"] or "unknown").split(".")[0]
        cache = SCHEMA_CACHE_DIR / f"{host}.{self.cfg['database'] or '_'}.json"
        self._schema_cache = cache   # devices() keeps the meta names in it too
        if cache.exists() and not self.refresh_schema:
            c = json.loads(cache.read_text(encoding="utf-8"))
            age_h = (dt.datetime.now(dt.timezone.utc)
                     - dt.datetime.fromisoformat(c["updated_utc"])).total_seconds() / 3600
            if self.offline_schema or age_h < SCHEMA_MAX_AGE_H:
                self.schema_source = f"cache {cache.as_posix()} ({age_h:.1f} h old)"
                self._tables = [{**t, **classify(t["name"])} for t in c["tables"]]
                return self._tables
        if self.offline_schema:
            raise LakeError(f"No schema cache at {cache} - run without --offline first")
        try:
            cols, _, rows, _ = self._query(
                "SELECT t.name, t.location, c.name, ty.name FROM sys.external_tables t "
                "JOIN sys.columns c ON c.object_id = t.object_id "
                "JOIN sys.types ty ON ty.user_type_id = c.user_type_id ORDER BY t.name, c.column_id", 10_000_000)
        except LakeError:
            raise
        except Exception as e:  # noqa: BLE001 - pymssql errors: one clean line
            raise LakeError(f"Schema query failed ({self.describe_source()}): {synapse_error(e)}") from None
        by_table: dict[str, dict] = {}
        for tname, loc, col, ty in rows:
            t = by_table.setdefault(tname, {"location": loc, "columns": []})
            t["columns"].append([col.lower(), TYPES.get(ty.lower(), ty.lower())])
        tables = []
        for tname, t in by_table.items():
            name = re.sub(r"[^a-z0-9_]", "_", tname.lower())
            kind = classify(name)["kind"]
            folder = (t["location"] or "").strip().strip("/").removesuffix("/**").removesuffix("/*").strip("/")
            entry = {"name": name, "synapse_name": tname, "columns": t["columns"], "partition_keys": [],
                     "location": folder}
            if kind == "message":
                entry["folder"] = folder
                entry["columns"] = entry["columns"] + [["date_created", "string"]]
            elif kind == "message_list":   # the job writes <DEVICE>/messages/2024/01/01/messages.parquet
                entry.update(folder=folder, columns=[["messagename", "string"], ["date_created", "string"]])
            tables.append(entry)
        tables += self._aggregation_tables({t["name"] for t in tables})
        SCHEMA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps({"schema": "canedge-synapse-schema/1", "endpoint": self.cfg["endpoint"],
                                     "database": self.cfg["database"],
                                     "updated_utc": dt.datetime.now(dt.timezone.utc).isoformat(), "tables": tables}),
                         encoding="utf-8")
        self.schema_source = (f"sys.external_tables of {self.describe_source()} ({len(tables)} tables, cached to "
                              f"{cache.as_posix()})")
        self._tables = [{**t, **classify(t["name"])} for t in tables]
        return self._tables

    def _aggregation_tables(self, known: set[str]) -> list[dict]:
        """Trip summaries / events (not mapped by the job): Columns of the first file, if the folder holds data."""
        out = []
        for name, folder in AGGREGATIONS.items():
            if name in known:
                continue
            probe = f"SELECT TOP 0 * FROM {openrowset(folder)} AS r".replace("'", "''")
            try:
                cols, _, rows, _ = self._query(f"EXEC sp_describe_first_result_set N'{probe}'", 1000)
            except Exception:  # noqa: BLE001 - no such folder (aggregation / event detection not deployed)
                self._drop_conn()
                continue
            i_name, i_type = cols.index("name"), cols.index("system_type_name")
            columns = [[r[i_name].lower(), TYPES.get(r[i_type].split("(")[0].lower(), r[i_type].lower())] for r in rows]
            if columns:
                out.append({"name": name, "columns": columns + [["date_created", "string"]], "partition_keys": [],
                            "location": folder, "folder": folder})
        return out

    def list_files(self, table: str, after: str = "") -> tuple[list[str], bool]:
        raise LakeError("Synapse: No storage listing (no request guard on Azure)")

    # -------------------------------------------------------- queries
    def translate(self, sql: str) -> str:
        folders = {t["name"]: t["folder"] for t in self.tables() if t.get("folder")}
        try:
            return to_synapse(sql, folders)
        except LakeError:
            raise
        except Exception as e:  # noqa: BLE001 - sqlglot parse errors: one clean line
            raise LakeError(f"Cannot translate the SQL to Synapse T-SQL: {str(e).splitlines()[0]}") from None

    def run(self, sql: str, max_rows: int = 1000, translate: bool = True) -> dict:
        """Run one query (Athena-dialect SQL, translated): Rows (up to max_rows), times. Data processed is not
        tracked (data_scanned_mb / cost_usd_est = None)."""
        q = self.translate(sql) if translate else sql
        chk = check_sql(q, "synapse")   # the generated T-SQL must still be one read-only statement
        if chk["errors"] and translate:
            raise LakeError("REFUSED (translated SQL): " + "; ".join(chk["errors"]))
        t0 = time.time()
        base = {"query_execution_id": None, "sql_synapse": q, "reused": False, "data_scanned_mb": None,
                "cost_usd_est": None, "engine_s": 0}
        try:
            cols, types, rows, truncated = self._query(q, max_rows)
        except LakeError:
            raise
        except Exception as e:  # noqa: BLE001 - pymssql errors: one clean line (no password in them)
            self._drop_conn()
            return {**base, "state": "FAILED", "error": synapse_error(e), "execution_s": round(time.time() - t0, 2),
                    "columns": [], "column_types": [], "rows": [], "truncated": False}
        self.queries += 1
        return {**base, "state": "SUCCEEDED", "error": None, "execution_s": round(time.time() - t0, 2),
                "engine_s": round(time.time() - t0, 2), "columns": cols, "column_types": types,
                "rows": [list(r) for r in rows], "truncated": truncated}

    def sql(self, query: str, max_rows: int = 100_000, **_) -> pd.DataFrame:
        """Athena-dialect SQL -> T-SQL -> DataFrame. Stops when the result has more than max_rows rows."""
        chk = check_sql(query, "synapse")
        if chk["errors"]:
            raise LakeError("REFUSED: " + "; ".join(chk["errors"]))
        for w in chk["warnings"]:
            print(f"WARNING: {w}", file=sys.stderr)
        res = self.run(query, max_rows)
        if res["state"] != "SUCCEEDED":
            raise LakeError(f"{res['state']}: {res['error']}\nSynapse SQL (translated): {res['sql_synapse']}")
        if res["truncated"]:
            raise LakeError(f"Result has more than {max_rows:,} rows - aggregate in SQL (resample to a coarser "
                            f"interval, GROUP BY day/device) instead of downloading raw rows")
        df = self.frame(res)
        df.attrs.update(execution_s=res["execution_s"])
        return df

    @staticmethod
    def frame(res: dict) -> pd.DataFrame:
        """run() result -> DataFrame: Lower-case column names (as Athena), datetimes tz-aware UTC, DECIMAL -> float."""
        import pymssql
        df = pd.DataFrame(res["rows"], columns=[c.lower() for c in res["columns"]])
        for c, ty in zip(df.columns, res["column_types"]):
            if ty == pymssql.DATETIME:
                df[c] = pd.to_datetime(df[c], utc=True, errors="coerce")
            elif ty == pymssql.DECIMAL:
                df[c] = pd.to_numeric(df[c], errors="coerce").astype(float)
        return utc_frame(df)

    def budget(self) -> str:
        return (f"{self.queries} Synapse queries (data processed is not tracked on Azure; Synapse bills ~$5 per TB "
                "processed, scans are kept small by the date window + folder pruning)")
