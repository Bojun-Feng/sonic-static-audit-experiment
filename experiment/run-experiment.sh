#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  run-experiment.sh prepare {exhaustive|targeted} [--repo DIR] [--out DIR] [--config FILE] [--identity FILE]
Prepare performs local static selection only.
Benchmark execution: python3 experiment/src/incremental.py (see INCREMENTAL.md).
Defaults: sibling sonic-swss source, results/MODE prepared inputs.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    usage
    exit 0
fi
[[ $# -ge 2 ]] || { usage >&2; exit 2; }
action=$1
[[ $action == prepare ]] || { usage >&2; exit 2; }
mode=$2
[[ $mode == exhaustive || $mode == targeted ]] || fail 'mode must be exhaustive or targeted'
shift 2

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo="$script_dir/../../sonic-swss"
out="$script_dir/../results/$mode"
config="$script_dir/config.json"
identity=''

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo|--out|--config|--identity)
            [[ $# -ge 2 && -n $2 && $2 != --* ]] || fail "$1 requires a value"
            case "$1" in
                --repo) repo=$2 ;;
                --out) out=$2 ;;
                --config) config=$2 ;;
                --identity) identity=$2 ;;
            esac
            shift 2
            ;;
        --help|-h) usage; exit 0 ;;
        *) fail "unknown argument: $1" ;;
    esac
done

command -v python3 >/dev/null 2>&1 || fail 'Python 3.10 or newer is required'
args=(--repo "$repo" --config "$config" --out "$out" --mode "$mode")
[[ -z $identity ]] || args+=(--identity "$identity")
python3 -B "$script_dir/src/select.py" "${args[@]}"
if [[ $mode == targeted ]]; then
    python3 -B "$script_dir/src/extract.py" --repo "$repo" --index "$out/selected-groups.json"
fi
python3 -B "$script_dir/src/validate.py" --repo "$repo" --out "$out" --mode "$mode"
