# SONiC static correctness benchmark

A configurable benchmark for localized identity-versus-prefix and structured-key
correctness defects in SONiC SWSS. The workflow is configuration → queue →
discovery → hard-gate screening → independent review → resume.

The benchmark uses 284 exhaustive files and 131 targeted source-card units at
source revision `2f1f7921551ae9f9c799c8b83b8a15b1d172d395`.
Each admitted finding receives an independent candidate ID and a blinded session
for every configured reviewer condition. Review verdicts are `confirmed`,
`rejected`, or `needs-validation`.

## Run

From the repository root, with authenticated standalone Codex and Claude CLIs:

```sh
python3 experiment/src/incremental.py --archive /path/to/frozen-input \
  --out .runtime/benchmark --config experiment/incremental-config.json --execute
```

Omit `--execute` for offline import and planning. Execution visits the configured
queue, skips validated successes, and retries missing, failed, or interrupted
attempts. Rerun the same command to continue. Optional `--unit` and `--max-calls`
flags bound pilots. See [the benchmark guide](experiment/INCREMENTAL.md) for
configuration, repeats, records, and recovery limits.

Reviews are static and read-only: no source execution, builds, tests, network
tools, or source edits. Hard gates require Routing working-group ownership,
a supported defect kind, clear intended behavior and reachable impact, one
obvious deterministic fix in one file, and 5/5 confidence in defect and remedy.
Thresholds are in `experiment/runner-config.json`; prompts and schemas define
the review contract.

Workers receive frozen source, reference material, and their assigned inputs.
An OS boundary hides the archive and sibling results. Linux requires bubblewrap
and usable user/PID namespaces; macOS requires `sandbox-exec`. The isolation
probe must succeed before a provider invocation.

## Prepare source inputs

Preparation performs local static selection using Python, Bash, and a source
checkout matching `experiment/config.json`. `ast-grep` is optional.

```sh
./experiment/run-experiment.sh prepare exhaustive --repo /path/to/sonic-swss --out ./results/exhaustive
```

Use `targeted` as the mode for source-card groups. `--config FILE` selects a
profile and `--identity FILE` supplies a snapshot identity attestation. Source
and output directories must be disjoint. An identical preparation can reuse its
output; changed inputs or partial setup require a fresh output directory.
The benchmark's frozen unit selection is bound to its archive.

Selection scans configured C/C++ roots in sorted order, filters production files
by literal domain anchors, and records source provenance. Targeted selection
uses `ast-grep` when available or the recorded Python-regex fallback. Extraction
creates line-window cards with numbered excerpts and scope metadata. Validation
checks manifests, source bytes, groups, locations, and complete inventories.
Snapshot identity files can be created with `experiment/src/source_identity.py`.

## Bootstrap

Preview a Lima setup locally:

```sh
./experiment/bootstrap-lima.sh --dry-run
```

Explicit `--create` provisions a fresh VM and installs pinned tools from
`experiment/config.json`. Supported hosts are Linux/x86_64 with Lima 2.0.3 and
QEMU, and macOS/arm64 with Lima 2.2.1 and VZ. The default VM name is
`sonic-static-audit`; `sonic-static-review-v2` creation requires Linux host `u`.
The latter profile provisions bubblewrap and verifies worker isolation.

Supply existing absolute, disjoint `--source`, `--reference`, and empty
`--results` directories for creation. Source and references mount read-only.
Existing VMs are refused. Setup validates a completion receipt and cleans up
incomplete creation; authentication is a separate manual step. Bootstrap never
runs source or model review sessions. Read the CLI help for host and path checks.

## Offline verification

```sh
python3 -m unittest discover -s tests
bash -n experiment/run-experiment.sh
bash -n experiment/bootstrap-lima.sh
bash -n experiment/guest-setup.sh
git diff --check
```

Tests use disposable fixtures, fake provider CLIs, and mocked VM commands.
Archive-dependent and macOS sandbox tests identify unavailable prerequisites as
skips. Keep `TMPDIR` outside source/results and use an absolute path with visible
directory components for mount-validation tests.
