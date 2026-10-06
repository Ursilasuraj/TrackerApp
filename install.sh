#!/bin/sh
# Installs TodoTracker for the current user on Linux or macOS:
#   sh install.sh                   app menu entry, start at login, start now
#   sh install.sh --open-at-login   open the window at login too
#   sh install.sh --no-autostart    app menu entry only
# Details: tools/desktop_install.py. On Windows use install.ps1.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for name in "${TODOTRACKER_PYTHON:-}" python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9; do
    [ -n "$name" ] || continue
    py=$(command -v "$name" 2>/dev/null) || continue
    if "$py" -c 'import sys, sqlite3; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        exec "$py" "$here/tools/desktop_install.py" install --python "$py" "$@"
    fi
done
echo 'TodoTracker needs Python 3.9 or newer.' >&2
echo '  macOS: install it from https://www.python.org/downloads/ (or: brew install python)' >&2
echo '  Linux: install the python3 package (for example: sudo apt install python3)' >&2
exit 1
