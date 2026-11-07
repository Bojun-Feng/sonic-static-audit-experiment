# SONiC static correctness benchmark

The SONiC static correctness benchmark is a configuration-driven queue that runs independent static correctness reviews of SONiC SWSS Routing working-group source (routes, next hops, neighbors, IP interfaces, VRF, VXLAN/EVPN L3, and IP tunnels) through standalone Codex CLIs.

Each discovery cell is one model, at one reasoning effort, reviewing one source unit on one pass. The reviewed inventory is frozen at source revision `2f1f7921551ae9f9c799c8b83b8a15b1d172d395`: 284 `exhaustive` files and 131 `targeted` source-card groups (415 units total).

## Quick start

### 1. Prepare inputs

Preparation requires Python 3.10 or newer, Bash, and a source checkout matching `experiment/config.json` (`ast-grep` is optional). Source and output directories must be disjoint.

```sh
./experiment/run-experiment.sh prepare exhaustive --repo /path/to/source --out /path/to/prepared/exhaustive
./experiment/run-experiment.sh prepare targeted --repo /path/to/source --out /path/to/prepared/targeted
```

Use `targeted` to generate source-card groups. Pass `--config FILE` to select a profile and `--identity FILE` to supply a snapshot identity attestation. Selection records source provenance and excludes nonproduction files, extraction creates numbered source cards, and validation checks source bytes and inventories.

### 2. Run the queue

With the standalone Codex CLI (`0.164.0`) available, start the queue from the repository root:

```sh
python3 experiment/src/incremental.py --source /path/to/source \
  --reference /path/to/reference --prepared /path/to/prepared \
  --out .runtime/benchmark --config experiment/incremental-config.json --execute
```

Omit `--execute` to inspect an offline plan. Pass repeatable `--unit ARM:UNIT_ID` and `--max-calls N` to bound a run, or point `--config` at a smaller file. Rerun the identical command to skip completed cells and retry missing or failed cells. Change the grid by editing the conditions or `passes` in the configuration; every condition names a model and effort listed in `experiment/incremental-models.json`.

### 3. Bootstrap an isolated VM (optional)

Preview or provision a fresh Lima VM with pinned downloads:

```sh
./experiment/bootstrap-lima.sh --dry-run
```

Pass explicit `--create` with absolute, disjoint `--source`, `--reference`, and empty `--results` directories to provision the VM. Supported hosts are Linux/x86_64 with Lima 2.0.3/QEMU and macOS/arm64 with Lima 2.2.1/VZ. Source and reference directories mount read-only, and existing VMs are refused. Authentication is a separate manual step; bootstrap never launches a review.

## How a run works

The queue pairs every unit with every configured discovery condition, and each pass is an independent sample. Each discovery publishes when it completes. Each reported finding undergoes deterministic hard-gate screening, and each admitted finding is recorded as its own candidate in `candidates.json`.

Every admitted finding must satisfy all hard gates defined in `experiment/prompts/discovery.txt`, `experiment/schemas/discovery.json`, and `experiment/runner-config.json`:

- Routing working-group ownership.
- A supported `defect_kind` from the discovery schema.
- Clear intended behavior and a reachable impact.
- One obvious deterministic fix in one file (`files_touched` equal to `1`) that changes at most 50 logic lines (`max_fix_logic_lines` equal to `50`, tests excluded).
- 5/5 confidence in both the defect and the remedy.

## Configuration and isolation

- **Model catalog (`experiment/incremental-models.json`):** Eight Codex models, each supporting `low`, `medium`, `high`, `xhigh`, and `max` efforts and the `default` service tier.
- **Canonical grid (`experiment/incremental-config.json`):** All eight models across all five efforts (40 conditions) over 5 passes at concurrency 16, totaling 83,000 discovery cells (415 units × 40 conditions × 5 passes).
- **Worker isolation:** Each native invocation runs behind a worker boundary probed before the call (`bubblewrap` on Linux, `sandbox-exec` on macOS). The boundary hides the output parent (sibling results and working directories) and the original input roots, exposes the frozen input copies read-only, and gives each attempt its own scratch directory. Codex runs with a read-only native permissions profile, tool networking disabled, and provider credential directories, configured credential roots, macOS keychains, and `/proc` denied to model tools.

For full details on configuration, receipts, artifact layout, and worker boundaries, read the [independent discovery queue guide](experiment/INCREMENTAL.md).

## License

Released under the [MIT License](LICENSE).
