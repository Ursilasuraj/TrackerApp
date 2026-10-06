#!/bin/sh
# Removes TodoTracker's app menu entry and login item and stops the app.
# Your data stays in ./data. On Windows use uninstall.ps1.
set -eu
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
for name in "${TODOTRACKER_PYTHON:-}" python3 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9; do
    [ -n "$name" ] || continue
    py=$(command -v "$name" 2>/dev/null) || continue
    if "$py" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
        exec "$py" "$here/tools/desktop_install.py" uninstall "$@"
    fi
done
echo 'Python 3.9 or newer was not found.' >&2
exit 1
