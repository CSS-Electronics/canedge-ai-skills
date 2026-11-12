"""The CSS Electronics MF4 tools for all canedge-* scripts: Find mdf2csv / mdf2mdf / mdf2parquet_decode, read their
version, and convert a log file to raw frames with mdf2csv (temporary CSVs, deleted at once; CANmod.router data
demuxed first with mdf2mdf when a MUX-TP mapping is given, see muxtp.py), and the MF4 storage weight per frame.

Tool path: --tool <path> > CANEDGE_MDF2CSV / CANEDGE_MDF2MDF / CANEDGE_MDF2PARQUET > the installed build in
canedge-mdf2csv/bin, canedge-mdf2mdf/bin or canedge-mdf2parquet/bin. install.bat / install.sh download the pinned builds for Windows x64, Linux x64 and Linux
ARM64 (install_tools.py, manifest TOOLS below). No macOS build: Use a Linux VM/container or pass --tool.

  sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "canedge-workspace" / "scripts"))
  from tools import default_workers, mdf2csv_tables, resolve, version
  tool = resolve("mdf2csv", args.tool)
  for bus, table in mdf2csv_tables(log_path, tool, passwords=None):   # bus = "CAN" | "LIN", pyarrow Table
      ...
  # CANmod.router: Demux first (mapping from the project's router DBC, muxtp.py), channels CAN11-14, ...
  mux = muxtp.for_project(project)                                    # [] = no router
  mdf2csv_tables(log_path, tool, muxtp=mux, mdf2mdf=resolve("mdf2mdf") if mux else None)
"""
from __future__ import annotations

import os
import platform
import re
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

SKILLS = Path(__file__).resolve().parents[2]

# Storage weight per frame by data length, from MF4 size measurements (the same anchors as the CANedge config
# editor's filter builder): <= 8 data bytes 1.0 (fixed-size record), 12 B 1.75, 16 B 2.12, 64 B 4.16, linear in
# between. Scaled so a classic CAN frame (<= 8 B) = 17 bytes. Used for the storage shares between IDs (raw
# overview, config simulation).
EST_ANCHORS = ((8, 1.0), (12, 1.75), (16, 2.12), (64, 4.16))
EST_BYTES_CLASSIC = 17


def frame_weight(n: float) -> float:
    """Relative MF4 storage of one frame with n data bytes (classic CAN frame = 1.0)."""
    if n <= EST_ANCHORS[0][0]:
        return EST_ANCHORS[0][1]
    for (a, wa), (b, wb) in zip(EST_ANCHORS, EST_ANCHORS[1:]):
        if n <= b:
            return wa + (n - a) * (wb - wa) / (b - a)
    return EST_ANCHORS[-1][1]


# Pinned tool builds, downloaded by install_tools.py (zip with the single binary; MD5 of the zip as on the docs page).
# The URLs are fixed per version: Sphinx names a download _downloads/<md5 of the zip's path in the docs source>/<file>,
# so a docs rebuild keeps them; moving/renaming the zip in the docs source changes them. Bump a tool: New version + the
# three URLs/MD5s from its docs page, then run install_tools.py.
_DOCS = "https://canlogger.csselectronics.com/tools-docs"
TOOLS = {
    "mdf2csv": {
        "skill": "canedge-mdf2csv", "env": "CANEDGE_MDF2CSV", "version": "24.12.19",
        "docs": f"{_DOCS}/converters_mf4/converters/csv/index.html",
        "builds": {
            "win-x64": (f"{_DOCS}/converters_mf4/_downloads/0e12bb83b69f2b331edba7f20567a24b/24.12.19.zip",
                        "b1e6f49a5480b29c125fa5d9ba007477"),
            "linux-x64": (f"{_DOCS}/converters_mf4/_downloads/0d130708b7c08d7c37df57d4b79e71be/24.12.19.zip",
                          "4daf0d79ca1352d700f7e985cf6eacde"),
            "linux-arm64": (f"{_DOCS}/converters_mf4/_downloads/5b1ff0a035210f42d119a26612ef5f1f/24.12.19.zip",
                            "e40564b1b4295647249f330a105e50fa"),
        },
    },
    "mdf2mdf": {
        "skill": "canedge-mdf2mdf", "env": "CANEDGE_MDF2MDF", "version": "24.12.19",
        "docs": f"{_DOCS}/converters_mf4/converters/mdf/index.html",
        "builds": {
            "win-x64": (f"{_DOCS}/converters_mf4/_downloads/3a7742b6576a9c775a5d139f1efe7721/24.12.19.zip",
                        "4050fe5e68a440c613ab1362bbefb1f7"),
            "linux-x64": (f"{_DOCS}/converters_mf4/_downloads/03226859ddfba25b3e1763be917b7cd7/24.12.19.zip",
                          "f50f30690da91e97f2fe0fefba1f81a5"),
            "linux-arm64": (f"{_DOCS}/converters_mf4/_downloads/343a56a917c3d9538a42d046c2e734ce/24.12.19.zip",
                            "d8a95b31e5eaaf6ecaaef2d501a4371f"),
        },
    },
    "mdf2parquet_decode": {
        "skill": "canedge-mdf2parquet", "env": "CANEDGE_MDF2PARQUET", "version": "25.12.15",
        "docs": f"{_DOCS}/decoders_mf4/decoders/parquet/index.html",
        "builds": {
            "win-x64": (f"{_DOCS}/decoders_mf4/_downloads/d76103f280ff5c4e6de865e3e7c6d21f/25.12.15.zip",
                        "d9d4960a9e6af38062ccb826f86c760f"),
            "linux-x64": (f"{_DOCS}/decoders_mf4/_downloads/f9840eb535b26a0c9a41608e54aae5ff/25.12.15.zip",
                          "abf5d41645c887386a087b05b2ecf41c"),
            "linux-arm64": (f"{_DOCS}/decoders_mf4/_downloads/87982ac63c545928967d9d28cdf69482/25.12.15.zip",
                            "22020f98b01145d735f69a52297562fb"),
        },
    },
}
INSTALL = ".venv/bin/python" if os.name != "nt" else r".venv\Scripts\python.exe"
INSTALL += " .claude/skills/canedge-workspace/scripts/install_tools.py"


def platform_key() -> str | None:
    """win-x64 | linux-x64 | linux-arm64 for this machine, None if there is no build (e.g. macOS). Windows on ARM
    runs the x64 build (emulation)."""
    system, machine = platform.system(), platform.machine().lower()
    if system == "Windows":
        return "win-x64"
    if system == "Linux":
        return {"x86_64": "linux-x64", "amd64": "linux-x64", "aarch64": "linux-arm64", "arm64": "linux-arm64"}.get(machine)
    return None


def installed_path(name: str) -> Path:
    """Where install_tools.py puts the tool: <skill>/bin/<name>[.exe]."""
    return SKILLS / TOOLS[name]["skill"] / "bin" / (name + (".exe" if os.name == "nt" else ""))


def find(name: str, cli: str | Path | None = None) -> Path:
    """The tool's path (it may not exist): --tool > env var > installed build."""
    return Path(cli or os.environ.get(TOOLS[name]["env"]) or installed_path(name)).resolve()


def resolve(name: str, cli: str | Path | None = None) -> Path:
    """The tool's path; exits with a hint if it is missing."""
    p = find(name, cli)
    if not p.exists():
        raise SystemExit(f"ERROR: {name} not found at {p} - install it with: {INSTALL} (or pass --tool <path> / set "
                         f"{TOOLS[name]['env']})")
    return p


def version(tool: Path) -> str:
    """Tool version (e.g. 24.12.19.c8f9fd28), 'unknown' if it cannot be read."""
    try:
        r = subprocess.run([str(tool), "--version"], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return "unknown"
    m = re.search(r"Version:\s*(\S+)", r.stdout + r.stderr)
    return m.group(1) if m else "unknown"


def default_workers(n_jobs: int) -> int:
    """Parallel converter calls (one process each): At most 4 (decoder RAM up to ~1 GB per call, disk throughput)
    and half the logical CPUs."""
    return max(1, min(n_jobs, 4, (os.cpu_count() or 2) // 2))


def read_mdf2csv(path: Path, columns: list[str] | None = None):
    """One mdf2csv CSV (';'-separated) as a pyarrow Table; columns = only these (missing ones as nulls)."""
    import pyarrow as pa
    import pyarrow.csv as pacsv
    types = {"TimestampEpoch": pa.float64(), "ID": pa.string(), "DataBytes": pa.string()}
    opts = pacsv.ConvertOptions(column_types=types, include_columns=columns, include_missing_columns=bool(columns))
    return pacsv.read_csv(path, parse_options=pacsv.ParseOptions(delimiter=";"), convert_options=opts)


def mdf2csv_tables(log: Path | str, tool: Path, passwords: str | Path | None = None, tmp_root: str | None = None,
                   columns: list[str] | None = None, muxtp: list | None = None,
                   mdf2mdf: Path | None = None) -> list[tuple[str, object]]:
    """Convert one log file (MF4/MFC/MFE/MFM) with mdf2csv into a temp folder and return [(bus, Table)], bus = CAN or
    LIN (one table per mdf2csv CSV). Each CSV is deleted as soon as it is read (CSV = 3-10x the MF4 size), the temp
    folder always. Raises RuntimeError if mdf2csv produced no CSV. Never passes -d: The log file stays untouched.

    muxtp = MUX-TP mapping (muxtp.py, e.g. [("CAN2", "010", [11, 12, 13, 14])]) and mdf2mdf = its path: The file is
    first demuxed with mdf2mdf into the temp folder (CANmod.router channels S1-S4 become CAN11-14, ...), then that
    temporary MF4 is converted. The tunnel frames (CAN2 0x10) are removed by the demux."""
    tmp = Path(tempfile.mkdtemp(prefix="mdf2csv-", dir=tmp_root))
    try:
        src = Path(log)
        if muxtp:
            src = demux(src, mdf2mdf, muxtp, tmp / "demux", passwords)
        cmd = [str(tool), "--non-interactive", "--verbosity=1", "-O", str((tmp / "csv").resolve())]
        (tmp / "csv").mkdir()
        if passwords and not muxtp:
            cmd.append(f"--password-file={Path(passwords).resolve()}")
        # cwd = the temp folder: The tools fall back to a passwords.json in the working directory, never pick up a
        # stray one (only the password file passed here counts)
        p = subprocess.run(cmd + [str(Path(src).resolve())], capture_output=True, text=True, timeout=3600, cwd=tmp)
        csvs = sorted((tmp / "csv").rglob("*.csv"))
        if not csvs:
            raise RuntimeError(f"mdf2csv produced no CSV (exit {p.returncode}): {(p.stderr or p.stdout).strip()[-300:]}")
        if muxtp:
            remove_temp(src)
        out = []
        for c in csvs:
            out.append(("LIN" if c.stem.endswith("_LIN") else "CAN", read_mdf2csv(c, columns)))
            remove_temp(c)
        return out
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def demux(log: Path | str, mdf2mdf: Path | None, muxtp: list, out_dir: Path | str,
          passwords: str | Path | None = None) -> Path:
    """Demux one log file (MF4/MFC/MFE/MFM) with mdf2mdf --muxtp-canN=... into out_dir and return the demuxed MF4
    (<stem>_out.MF4: decompressed, decrypted, finalized; the MF4 header is kept). muxtp = [] only finalizes. Never
    passes -d. Raises RuntimeError if no MF4 is written."""
    import muxtp as _mx
    if mdf2mdf is None:
        mdf2mdf = resolve("mdf2mdf")
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)  # mdf2mdf fails (exit 129) if -O does not exist
    cmd = [str(mdf2mdf), "--non-interactive", "--verbosity=1", "-O", str(out_dir.resolve())] + _mx.flags(muxtp)
    if passwords:
        cmd.append(f"--password-file={Path(passwords).resolve()}")
    p = subprocess.run(cmd + [str(Path(log).resolve())], capture_output=True, text=True, timeout=3600, cwd=out_dir)
    mf4s = sorted(out_dir.rglob("*.MF4"))
    if not mf4s:
        msg = (p.stderr or p.stdout).strip()
        err = [ln for ln in msg.splitlines() if " E " in ln]  # mdf2mdf error lines ("... E Could not ...")
        raise RuntimeError(f"mdf2mdf produced no MF4 (exit {p.returncode}): {(' | '.join(err) or msg)[-300:]}")
    return mf4s[0]


def finalized_header(log: Path | str, passwords: str | Path | None = None, mdf2mdf: Path | None = None,
                     tmp_root: str | Path | None = None) -> dict:
    """The MF4 header of an MFC/MFE/MFM file (mf4_files.read_header fields): mdf2mdf writes a finalized copy into a
    temp folder (the whole file: MFC carries a file checksum, a truncated copy fails), the header is read, the copy
    deleted. Cost ~0.6 s per MB of MFC (the copy is 2-4x larger). Raises RuntimeError if mdf2mdf fails.
    Known: mdf2mdf 24.12.19 drops the LIN bit-rates from the header (all other fields are kept)."""
    from mf4_files import read_header
    tmp = Path(tempfile.mkdtemp(prefix="mdf2mdf-hdr-", dir=tmp_root))
    try:
        return read_header(demux(log, mdf2mdf, [], tmp / "out", passwords))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


ENCRYPTED_EXTS = (".mfe", ".mfm")


def password_file(explicit: str | Path | None, project: str | Path | None = None) -> Path | None:
    """The password file for encrypted log files: --passwords if given (exits if missing), else
    <project>/passwords.json if present, else None. The file is passed to the tools, never read or printed."""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise SystemExit(f"ERROR: Password file not found: {p}")
        return p
    if project and (Path(project) / "passwords.json").is_file():
        return Path(project) / "passwords.json"
    return None


def passwords_refusal(paths: list, pw: Path | str | None, project: str | Path | None = None) -> str | None:
    """None if fine, else the message to refuse with: Encrypted MFE/MFM files among paths but no password file."""
    enc = [str(p) for p in paths if Path(p).suffix.lower() in ENCRYPTED_EXTS]
    if not enc or pw:
        return None
    where = (f" in the project folder ({Path(project) / 'passwords.json'})" if project
             else ", then pass --passwords <path>")
    return (f"REFUSED: {len(enc)} encrypted log file(s) (MFE/MFM), e.g. {enc[0]}, but no password file. Ask the user "
            f"to save their passwords as passwords.json ({{\"default\": \"...\", \"<DEVICEID>\": \"...\"}}){where} "
            f"- never ask them to paste passwords into the chat")


def remove_temp(path: Path, tries: int = 8) -> bool:
    """Delete a temp file that is already read. Windows: An antivirus scan or the search indexer can hold a freshly
    written file for a moment (WinError 32, "used by another process"), more often with many parallel workers.
    Retry briefly, then leave it to the folder cleanup instead of failing the log file (its data is read)."""
    for i in range(tries):
        try:
            path.unlink(missing_ok=True)
            return True
        except PermissionError:
            time.sleep(0.1 * (i + 1))
    return False
