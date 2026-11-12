"""Local backend: DuckDB views on a folder with the data lake layout <device>/<CANx_Message>/<yyyy>/<mm>/<dd>/*.parquet."""
from __future__ import annotations

import datetime as dt
import difflib
import re
import sys
from pathlib import Path

import pandas as pd

from _backends import Lake, LakeError, _iso, check_sql, classify, parse_utc, strip_sql, table_name, utc_frame

DATE_DIR_RE = re.compile(r"^\d{4}$")

# Trino functions DuckDB lacks (checked on DuckDB 1.5): Same SQL on both backends
DUCKDB_MACROS = [
    "CREATE OR REPLACE MACRO approx_percentile(x, p) AS approx_quantile(x, p)",
    "CREATE OR REPLACE MACRO approx_distinct(x) AS approx_count_distinct(x)",
    "CREATE OR REPLACE MACRO from_unixtime(x) AS make_timestamp(CAST(round(x * 1000000) AS BIGINT))",
    "CREATE OR REPLACE MACRO to_unixtime(x) AS epoch(x)",
    "CREATE OR REPLACE MACRO array_join(a, s) AS array_to_string(a, s)",
    "CREATE OR REPLACE MACRO is_nan(x) AS isnan(x)",
]

# ---------------------------------------------------------------- local: DuckDB on a Parquet folder
class LocalLake(Lake):
    """A local folder with the data lake layout <device>/<CANx_Message>/<yyyy>/<mm>/<dd>/*.parquet."""
    kind = "local"

    def __init__(self, root: str | Path):
        import duckdb
        self.root = Path(root)
        if not self.root.is_dir():
            raise LakeError(f"Lake folder not found: {self.root} (decode log files first with canedge-mdf2parquet)")
        self.con = duckdb.connect()
        self.con.execute("SET TimeZone = 'UTC'")
        for m in DUCKDB_MACROS:
            self.con.execute(m)
        self._tables: list[dict] | None = None
        self._dirs: dict[str, tuple[Path, Path]] = {}     # table name -> (device dir, message dir)
        self._bad: set[Path] = set()                      # unreadable Parquet files (skipped with a warning)

    def describe_source(self) -> str:
        return f"local lake {self.root.as_posix()}"

    def tables(self) -> list[dict]:
        """Like the Glue mapping: One table per device + message folder, lower-case names and columns."""
        if self._tables is not None:
            return self._tables
        import pyarrow.parquet as pq
        out = []
        for dev in sorted(p for p in self.root.iterdir() if p.is_dir() and not p.name.startswith(("_", "."))):
            msgs = []
            for msg in sorted(p for p in dev.iterdir() if p.is_dir()):
                schema = None
                for f in self._files(msg):   # the first readable file
                    try:
                        schema = pq.read_schema(f)
                        break
                    except Exception:  # noqa: BLE001 - corrupt file: skipped in _register with a warning
                        self._bad.add(f)
                if schema is None:
                    continue
                cols = [[x.name.lower(), "timestamp" if str(x.type).startswith("timestamp") else str(x.type)]
                        for x in schema]
                name = table_name(dev.name, msg.name)
                out.append({"name": name, "columns": cols + [["date_created", "string"]],
                            "partition_keys": [["date_created", "string"]], "location": msg.as_posix(),
                            **classify(name)})
                self._dirs[name] = (dev, msg)
                msgs.append(msg.name)
            if msgs and classify(f"tbl_{dev.name}_x")["device"]:   # device folders only (not aggregations/)
                name = f"tbl_{dev.name.lower()}_messages"
                out.append({"name": name, "columns": [["messagename", "string"]], "partition_keys": [],
                            "location": None, **classify(name)})
                self._dirs[name] = (dev, dev)
        self._tables = out
        return out

    @staticmethod
    def _files(msg_dir: Path, start=None, end=None) -> list[Path]:
        """Parquet files of one message folder, pruned by the yyyy/mm/dd folders to [start, end] (UTC)."""
        t0, t1 = parse_utc(start), parse_utc(end)
        d0, d1 = (t0.date() if t0 else None), (t1.date() if t1 else None)
        out = []
        for y in sorted(p for p in msg_dir.iterdir() if p.is_dir() and DATE_DIR_RE.match(p.name)):
            for m in sorted(p for p in y.iterdir() if p.is_dir()):
                for d in sorted(p for p in m.iterdir() if p.is_dir()):
                    try:
                        day = dt.date(int(y.name), int(m.name), int(d.name))
                    except ValueError:
                        continue
                    if (d0 and day < d0) or (d1 and day > d1):
                        continue
                    out += sorted(d.glob("*.parquet"))
        return out

    def _register(self, name: str, start=None, end=None) -> None:
        """Create the view for one table name. Columns are lower-cased (as Glue); date_created from the folder."""
        self.tables()
        if name not in self._dirs:
            close = difflib.get_close_matches(name, list(self._dirs), n=5, cutoff=0.5)
            raise LakeError(f"Unknown table {name} ({self.describe_source()}). Close: {', '.join(close) or '-'}")
        dev, msg = self._dirs[name]
        if name.endswith("_messages") and dev == msg:
            vals = ", ".join("('" + p.name.replace("'", "''") + "')" for p in sorted(dev.iterdir()) if p.is_dir())
            self.con.execute(f"CREATE OR REPLACE VIEW {name} AS SELECT * FROM (VALUES {vals or '(NULL)'}) v(messagename)")
            return
        for attempt in (1, 2):
            fs = [p for p in self._files(msg, start, end) if p not in self._bad]
            empty = not fs
            if empty:   # no files in the window: an empty view with the right columns
                fs = [p for p in self._files(msg) if p not in self._bad][:1]
            lst = "[" + ", ".join("'" + p.as_posix().replace("'", "''") + "'" for p in fs) + "]"
            src = f"read_parquet({lst}, filename=true, union_by_name=true)"
            try:
                names = [r[0] for r in self.con.execute(f"DESCRIBE SELECT * FROM {src}").fetchall() if r[0] != "filename"]
                break
            except Exception:  # noqa: BLE001 - find unreadable files (only on error: no cost on the normal path)
                import pyarrow.parquet as pq
                bad = []
                for p in fs:
                    try:
                        pq.read_metadata(p)
                    except Exception:  # noqa: BLE001
                        bad.append(p)
                if not bad or attempt == 2:
                    raise
                self._bad |= set(bad)
                print(f"WARNING: Skipped {len(bad)} unreadable Parquet file(s) in {name} (first: {bad[0].as_posix()}). "
                      f"Likely a variable-length ID: canedge-mdf2parquet decoder-technical.md section 4", file=sys.stderr)
        seen, cols = set(), []
        for c in names:
            low = c.lower()
            if low in seen:
                continue
            seen.add(low)
            cols.append(f'"{c}" AS "{low}"' if c != low else f'"{c}"')
        self.con.execute(
            f"CREATE OR REPLACE VIEW {name} AS SELECT {', '.join(cols)}, "
            f"regexp_extract(replace(filename, '\\', '/'), '(\\d{{4}}/\\d{{2}}/\\d{{2}})/[^/]+$', 1) AS date_created "
            f"FROM {src}" + (" WHERE false" if empty else ""))

    def sql(self, query: str, start=None, end=None, **_) -> pd.DataFrame:
        """DuckDB SQL. Views for every tbl_* name in the query are created first; start/end limit the files per
        view by date folder (still filter date_created/t in the SQL for exact bounds)."""
        chk = check_sql(query, "local")
        if chk["errors"]:
            raise LakeError("REFUSED: " + "; ".join(chk["errors"]))
        names = sorted({n.lower() for n in re.findall(r"\btbl_[A-Za-z0-9_]+", strip_sql(query))})
        for n in names:
            self._register(n, start, end)
        try:
            return utc_frame(self.con.execute(query).df())
        except Exception as e:  # noqa: BLE001 - duckdb errors: one clean line
            raise LakeError(f"DuckDB: {str(e).splitlines()[0]}") from None

    def span(self, table: str) -> tuple[str | None, str | None]:
        r = self.sql(f"SELECT min(t) AS a, max(t) AS b FROM {self.table(table)['name']}").iloc[0]
        return _iso(r["a"]), _iso(r["b"])
