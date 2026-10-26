# SONiC static correctness benchmark

A configuration-driven queue for static SONiC SWSS review. The frozen inventory
contains 284 exhaustive files and 131 targeted source-card groups at revision
`2f1f7921551ae9f9c799c8b83b8a15b1d172d395`.

Each unit pairs with every configured discovery model/effort condition. Completed
discoveries publish independently, pass through hard-gate screening, and enqueue
each admitted finding for every reviewer condition. Calls share a small concurrency
limit. Findings remain independent even when their claims or locations match.

## Run

With standalone Codex and Claude CLIs available:

```sh
python3 experiment/src/incremental.py --source /path/to/source \
  --reference /path/to/reference --prepared /path/to/prepared \
  --out .runtime/benchmark --config experiment/incremental-config.json --execute
```

Omit `--execute` for offline planning. Rerun the identical command to
skip completed cells and retry missing or failed cells. Extend the configuration
to add model/effort conditions or increase `passes` for independent repetitions.
Optional `--unit ARM:UNIT_ID` and `--max-calls N` bound a run. See
[the queue guide](experiment/INCREMENTAL.md) for configuration, receipts, and
isolation requirements.

Hard gates require Routing working-group ownership, a supported defect kind,
clear intended behavior and reachable impact, one obvious deterministic fix in
one file, and 5/5 confidence in defect and remedy. Prompts, schemas, and
`experiment/runner-config.json` define the static review contract.

## Prepare inputs

Preparation uses Python, Bash, and a source checkout matching
`experiment/config.json`. `ast-grep` is optional.

```sh
./experiment/run-experiment.sh prepare exhaustive --repo /path/to/source --out /path/to/prepared/exhaustive
./experiment/run-experiment.sh prepare targeted --repo /path/to/source --out /path/to/prepared/targeted
```

Use `targeted` for source-card groups. `--config FILE` selects a profile;
`--identity FILE` supplies a snapshot identity attestation. Source and output
must be disjoint. Changed inputs or incomplete preparation need a fresh output.
Selection records source provenance and excludes nonproduction files. Extraction
creates numbered source cards; validation checks source bytes and inventories.

## Bootstrap

```sh
./experiment/bootstrap-lima.sh --dry-run
```

Explicit `--create` provisions a fresh Lima VM using pinned downloads. Supported
hosts are Linux/x86_64 with Lima 2.0.3/QEMU and macOS/arm64 with Lima 2.2.1/VZ.
Supply absolute, disjoint `--source`, `--reference`, and empty `--results`
directories. Source and references mount read-only; existing VMs are refused.
Authentication is a separate manual step. Bootstrap never launches reviews.
