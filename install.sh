#!/usr/bin/env sh
# ===========================================================================
#  One-time setup for the CANedge AI data analysis skills (Linux / macOS).
#  Creates a local virtual environment in .venv, installs requirements and
#  downloads the CSS Electronics MF4 converter + decoder (Linux x64 / ARM64).
#  Safe to re-run; it will reuse / update the existing .venv.
# ===========================================================================
set -e
cd "$(dirname "$0")"

python3 -c "import sys; sys.exit(sys.version_info < (3, 10))" 2>/dev/null || {
  echo "ERROR: Python 3.10+ not found (python3 --version). Install it, e.g. Debian/Ubuntu:"
  echo "       sudo apt install python3 python3-venv   - macOS: brew install python   - then re-run install.sh"
  exit 1
}

echo "[1/4] Creating virtual environment in .venv ..."
python3 -m venv .venv || {
  echo "ERROR: could not create the virtual environment."
  echo "       Debian/Ubuntu: sudo apt install python3-venv, then re-run install.sh"
  exit 1
}

echo "[2/4] Upgrading pip ..."
.venv/bin/python -m pip install --upgrade pip

echo "[3/4] Installing dependencies from requirements.txt ..."
.venv/bin/python -m pip install -r requirements.txt

echo "[4/4] Downloading the CSS Electronics MF4 converter + decoder ..."
if [ "$(uname -s)" = "Darwin" ]; then
  echo "Skipped: There is no macOS build of the MF4 tools. Data lake / S3 / DBC / config skills work; for MF4 log"
  echo "         files use a Linux VM/container or set CANEDGE_MDF2CSV / CANEDGE_MDF2PARQUET to a build elsewhere."
else
  .venv/bin/python .claude/skills/canedge-workspace/scripts/install_tools.py
fi

echo
echo "Done. The environment is ready in .venv"
echo "Open your AI agent in this folder and ask it to analyze your CANedge data."
