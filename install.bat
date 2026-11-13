@echo off
REM ===========================================================================
REM  One-time setup for the CANedge AI data analysis skills (Windows).
REM  Creates a local virtual environment in .venv and installs requirements.
REM  Safe to re-run; it will reuse / update the existing .venv.
REM ===========================================================================
setlocal
cd /d "%~dp0"

REM Find Python 3.10+: "python" on PATH, else the py launcher, else a per-user install (e.g. fresh from winget,
REM before PATH is refreshed in this terminal)
set "PY="
python -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=python"
if not defined PY py -3 -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set "PY=py -3"
if not defined PY for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do if exist "%%D\python.exe" "%%D\python.exe" -c "import sys; sys.exit(sys.version_info < (3, 10))" >nul 2>&1 && set PY="%%D\python.exe"
if not defined PY (
  echo.
  echo ERROR: Python 3.10+ not found. Install it, e.g.: winget install -e --id Python.Python.3.12
  echo        or from https://www.python.org/downloads/ ^(tick "Add python.exe to PATH"^), then re-run install.bat
  exit /b 1
)

echo.
echo [1/4] Creating virtual environment in .venv ...
%PY% -m venv .venv
if errorlevel 1 (
  echo.
  echo ERROR: could not create the virtual environment with %PY%.
  exit /b 1
)

echo.
echo [2/4] Upgrading pip ...
".venv\Scripts\python.exe" -m pip install --upgrade pip

echo.
echo [3/4] Installing dependencies from requirements.txt ...
".venv\Scripts\python.exe" -m pip install -r requirements.txt
if errorlevel 1 (
  echo.
  echo ERROR: dependency installation failed. See the messages above.
  exit /b 1
)

echo.
REM MF4 converters mdf2csv + mdf2mdf and decoder mdf2parquet_decode -> the skills' bin/ folders (pinned versions,
REM MD5-checked; manifest TOOLS in canedge-workspace/scripts/tools.py). A build elsewhere: Set CANEDGE_MDF2CSV /
REM CANEDGE_MDF2MDF / CANEDGE_MDF2PARQUET. Reinstall only the tools: install_tools.py (--force re-downloads)
echo [4/4] Downloading the CSS Electronics MF4 converter + decoder ...
".venv\Scripts\python.exe" .claude\skills\canedge-workspace\scripts\install_tools.py
if errorlevel 1 (
  echo.
  echo ERROR: the MF4 tools could not be installed. See the messages above.
  exit /b 1
)

echo.
echo ===========================================================================
echo  Done. The environment is ready in .venv
echo  Open your AI agent in this folder and ask it to analyze your CANedge data.
echo ===========================================================================
endlocal
