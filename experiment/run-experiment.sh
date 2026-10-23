#!/bin/bash
set -euo pipefail

usage() {
    cat <<'EOF'
Usage:
  run-experiment.sh prepare {exhaustive|targeted} [--repo DIR] [--out DIR] [--config FILE] [--identity FILE]
  run-experiment.sh run {exhaustive|targeted} --reference DIR [--prepared DIR] [--out DIR] [--queue FILE] [--diagnostics-root DIR]
Prepare performs local static selection only. Run invokes standalone model CLIs.
Defaults: sibling sonic-swss source, results/MODE prepared inputs, results/MODE-run output.
EOF
}

fail() { printf 'ERROR: %s\n' "$*" >&2; exit 2; }

if [[ ${1:-} == --help || ${1:-} == -h ]]; then
    usage
    exit 0
fi
[[ $# -ge 2 ]] || { usage >&2; exit 2; }
action=$1
[[ $action == prepare || $action == run ]] || fail 'command must be prepare or run'
mode=$2
[[ $mode == exhaustive || $mode == targeted ]] || fail 'mode must be exhaustive or targeted'
shift 2

script_dir=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
repo="$script_dir/../../sonic-swss"
prepared="$script_dir/../results/$mode"
out="$prepared"
[[ $action != run ]] || out="$script_dir/../results/$mode-run"
config="$script_dir/config.json"
reference=''
identity=''
queue=''
diagnostics=''

while [[ $# -gt 0 ]]; do
    case "$1" in
        --repo|--out|--config|--identity|--prepared|--reference|--queue|--diagnostics-root)
            [[ $# -ge 2 && -n $2 && $2 != --* ]] || fail "$1 requires a value"
            case "$1" in
                --repo|--config|--identity) [[ $action == prepare ]] || fail "$1 is only valid for prepare" ;;
                --prepared|--reference|--queue|--diagnostics-root) [[ $action == run ]] || fail "$1 is only valid for run" ;;
            esac
            case "$1" in
                --repo) repo=$2 ;;
                --out) out=$2 ;;
                --config) config=$2 ;;
                --identity) identity=$2 ;;
                --prepared) prepared=$2 ;;
                --reference) reference=$2 ;;
                --queue) queue=$2 ;;
                --diagnostics-root) diagnostics=$2 ;;
            esac
            shift 2
            ;;
        --help|-h) usage; exit 0 ;;
        *) fail "unknown argument: $1" ;;
    esac
done

command -v python3 >/dev/null 2>&1 || fail 'Python 3.10 or newer is required'
if [[ $action == run ]]; then
    [[ -n $reference ]] || fail 'run requires --reference DIR'
    args=(--prepared "$prepared" --mode "$mode" --out "$out" --reference "$reference")
    [[ -z $queue ]] || args+=(--queue "$queue")
    [[ -z $diagnostics ]] || args+=(--diagnostics-root "$diagnostics")
    exec python3 -B "$script_dir/src/runner.py" "${args[@]}"
fi
args=(--repo "$repo" --config "$config" --out "$out" --mode "$mode")
[[ -z $identity ]] || args+=(--identity "$identity")
python3 -B "$script_dir/src/select.py" "${args[@]}"
if [[ $mode == targeted ]]; then
    python3 -B "$script_dir/src/extract.py" --repo "$repo" --index "$out/selected-groups.json"
fi
python3 -B "$script_dir/src/validate.py" --repo "$repo" --out "$out" --mode "$mode"
