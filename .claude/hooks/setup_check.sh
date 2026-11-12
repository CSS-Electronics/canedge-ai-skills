#!/usr/bin/env sh
# SessionStart hook (.claude/settings.json): Tells the agent when the setup is missing, so it can offer to run the
# installer for this OS (canedge-workspace conventions section 1 "Setup"). Prints nothing when all is in place.
# Plain sh: Runs in Git Bash on Windows, sh on Linux/macOS. Never fails the session (always exit 0).
cd "${CLAUDE_PROJECT_DIR:-.}" 2>/dev/null || exit 0

case "$(uname -s)" in
  MINGW*|MSYS*|CYGWIN*) os="Windows"; venv=".venv/Scripts/python.exe"; ext=".exe"
                        cmd="install.bat (Bash tool: cmd //c '.\install.bat'; PowerShell: .\\install.bat)" ;;
  Darwin)               os="macOS"; venv=".venv/bin/python"; ext=""; cmd="sh install.sh" ;;
  *)                    os="Linux $(uname -m)"; venv=".venv/bin/python"; ext=""; cmd="sh install.sh" ;;
esac

missing=""
add() { missing="${missing:+$missing, }$1"; }
[ -f "$venv" ] || add ".venv (Python packages)"
if [ "$os" != "macOS" ]; then  # no macOS build of the MF4 tools
  [ -f ".claude/skills/canedge-mdf2csv/bin/mdf2csv$ext" ] || add "MF4 converter mdf2csv"
  [ -f ".claude/skills/canedge-mdf2mdf/bin/mdf2mdf$ext" ] || add "MF4 converter mdf2mdf"
  [ -f ".claude/skills/canedge-mdf2parquet/bin/mdf2parquet_decode$ext" ] || add "MF4 decoder mdf2parquet_decode"
fi
[ -z "$missing" ] && exit 0

python="not checked"
if [ ! -f "$venv" ]; then
  python="not found"
  for p in python3 python "py -3" "${LOCALAPPDATA:-/nonexistent}"/Programs/Python/Python3*/python.exe; do
    if $p -c "import sys; sys.exit(sys.version_info < (3, 10))" >/dev/null 2>&1; then
      python="$($p --version 2>&1) found"; break
    fi
  done
fi

echo "CANEDGE SETUP NEEDED: OS $os; missing: $missing; Python 3.10+: $python. In your first reply, offer to set" \
     "everything up now with $cmd as one yes/no question, then run it on yes (canedge-workspace conventions" \
     "section 1 \"Setup\"). Only the MF4 tools missing: Offer install_tools.py instead."
exit 0
