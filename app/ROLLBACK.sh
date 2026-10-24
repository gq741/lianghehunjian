#!/usr/bin/env bash
# Keep this controller outside the source manifest so restoration stays available.
set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd -P)"
if [[ -n "${PYTHON:-}" ]]; then
    INTERPRETER="$PYTHON"
elif [[ -x "$HERE/.venv/Scripts/python.exe" ]]; then
    INTERPRETER="$HERE/.venv/Scripts/python.exe"
elif command -v python >/dev/null 2>&1; then
    INTERPRETER="$(command -v python)"
else
    INTERPRETER="python3"
fi
exec "$INTERPRETER" -B -X utf8 "$HERE/tools/rollback.py" "$@"
