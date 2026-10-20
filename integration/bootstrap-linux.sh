#!/usr/bin/env bash
# Downloads only to the chosen directory; no system installation or service start.
set -euo pipefail
exec python3 "$(dirname "$0")/bootstrap-release-linux.py" "$@"
