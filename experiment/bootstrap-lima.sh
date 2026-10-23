#!/bin/bash
set -euo pipefail
# Forward --name as well as creation/dry-run flags to the host-local planner.

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
command -v python3 >/dev/null 2>&1 || {
    printf 'ERROR: Python 3.10 or newer is required\n' >&2
    exit 1
}
exec python3 -B "$script_dir/src/bootstrap_lima.py" "$@"
