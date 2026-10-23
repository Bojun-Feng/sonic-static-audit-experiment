#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage: run-experiment.sh prepare {exhaustive|targeted} [--repo DIR] [--out DIR] [--config FILE]
Only local static preparation is supported. No discovery agents are invoked.
Defaults: sibling sonic-swss checkout, results/MODE in this repository, bundled config.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    usage
    exit 0
fi
[[ $# -ge 2 ]] || { usage >&2; exit 2; }
[[ $1 == prepare ]] || fail 'only the prepare command is supported'
mode=$2
[[ $mode == exhaustive || $mode == targeted ]] || fail 'mode must be exhaustive or targeted'
shift 2

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo="$script_dir/../../sonic-swss"
out="$script_dir/../results/$mode"
config="$script_dir/config.json"

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo|--out|--config)
            [[ $# -ge 2 && -n $2 && $2 != --* ]] || fail "$1 requires a value"
            case "$1" in
                --repo) repo=$2 ;;
                --out) out=$2 ;;
                --config) config=$2 ;;
            esac
            shift 2
            ;;
        --help|-h) usage; exit 0 ;;
        *) fail "unknown argument: $1" ;;
    esac
done

command -v python3 >/dev/null 2>&1 || fail 'Python 3.10 or newer is required'
python3 -B "$script_dir/src/select.py" --repo "$repo" --config "$config" --out "$out" --mode "$mode"
if [[ $mode == targeted ]]; then
    python3 -B "$script_dir/src/extract.py" --repo "$repo" --index "$out/selected-groups.json"
fi
python3 -B "$script_dir/src/validate.py" --repo "$repo" --out "$out" --mode "$mode"
