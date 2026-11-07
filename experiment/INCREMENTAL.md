# Independent discovery queue

Each cell is one static correctness review of one unit by one model, effort, and pass. Prepare both
arms into a fresh directory, then run from the repository root:

```sh
./experiment/run-experiment.sh prepare exhaustive --repo /path/to/source --out /path/to/prepared/exhaustive
./experiment/run-experiment.sh prepare targeted --repo /path/to/source --out /path/to/prepared/targeted
```

For a Git-free frozen snapshot, pass the same immutable `--identity FILE` to both prepare commands.
The identity attests the known revision; preparation validates its source bytes. Keep that identity
available alongside the original input roots for resume. The reference root supplies immutable
reference files. Prepared manifests retain their original source and identity paths.

Start the queue:

```sh
python3 experiment/src/incremental.py --source /path/to/source \
  --reference /path/to/reference --prepared /path/to/prepared \
  --out .runtime/benchmark --config experiment/incremental-config.json --execute
```

The prepared root contains the `exhaustive/` and `targeted/` manifest/index bundles and targeted source
cards: 284 files and 131 groups at revision `2f1f7921551ae9f9c799c8b83b8a15b1d172d395`. An empty output
starts with every configured discovery cell queued. Omit `--execute` to plan offline. Source selection
and cards are validated before scheduling. The frozen source may contain relative symlinks within its own
tree. Exhaustive unit IDs follow the manifest order; targeted unit IDs are the group IDs in the targeted
index.

## Configuration

```json
{
  "schema_version": 1,
  "experiment_version": "sonic-incremental-v1",
  "source_commit": "2f1f7921551ae9f9c799c8b83b8a15b1d172d395",
  "discovery": [
    {"model": "gpt-6-astra", "effort": "low", "service_tier": "default"},
    {"model": "gpt-6-astra", "effort": "medium", "service_tier": "default"}
  ],
  "passes": 5,
  "concurrency": 16,
  "timeout_seconds": 1800
}
```

The two conditions shown illustrate the shape; the committed `incremental-config.json` lists all 40.

The model catalog is `incremental-models.json`. Each condition explicitly names its model, effort, and
service tier. The configured conditions and `passes` are the invocation's worklist: the queue
enumerates exactly those cells, reuses their published results, runs the missing ones, and reports only
them in `status.json`, `candidates.json`, and `screened.json`. Published cells outside the current
configuration stay in place and are reused when a later configuration names them again. Each pass is a
separate discovery sample; its number is in the cell ID and receipts, not the discovery prompt, so
repetitions have identical prompt bytes. Concurrency and timeout may change. Frozen inputs, source
revision, templates, gates, and experiment version bind each output directory. A different contract
needs a fresh output.

The committed `incremental-config.json` lists the full canonical grid: all eight catalog models at all
five efforts (`low`, `medium`, `high`, `xhigh`, `max`) — 40 conditions — over five passes at concurrency
16, enumerating 83,000 discovery cells (415 units × 40 conditions × 5 passes). Running it reproduces the
benchmark grid; for a bounded run, pass `--unit`/`--max-calls` or point `--config` at a smaller file.

## Scheduling and resume

The queue pairs every unit with every discovery condition. Up to `concurrency` independent calls run at
once. Each discovery publishes when it completes, and each admitted finding is recorded as its own
candidate.

Cell IDs use readable arm, unit, model, effort, and pass values. Receipts carry the same task and
condition. Candidate IDs use the discovery cell and the finding index. Changing passes or conditions
preserves earlier IDs and published results. A failed attempt retries within the same pass; it is not a
new pass.

Each missing cell gets one attempt per invocation. A failed or incomplete attempt remains in its
receipt directory; the next invocation starts a fresh attempt. Published successes are validated and
skipped before provider calls. Native usage, timing, prompts, and streams are retained. Missing native
fields remain unknown. Publication uses an atomic directory rename. An interrupted controller may leave
a running receipt; the absence of a published success determines retry.

An output lock prevents simultaneous controllers, and native subprocesses retain the lock until they
exit. Setup copies frozen inputs; interrupted initial copying requires a fresh output directory.
Invalid published successes stop the command and remain available for inspection.

Optional repeatable `--unit ARM:UNIT_ID` restricts native calls to exact frozen units. `--max-calls N`
caps attempted cells. Omitting both visits the full queue. Exit zero means no attempted cell failed;
`status.json` also reports queued cells, including work deferred by a selector or cap. These options use
the same configuration and result directories.

| Path | Contents |
| --- | --- |
| `experiment.json`, `config.json` | Frozen input binding and the current worklist configuration |
| `inputs/` | Source, reference, and source-card copies |
| `results/discovery/` | Published native streams, prompt, answer, metadata, completion |
| `attempts/` | Per-attempt receipts, native streams, usage, timing, and failures |
| `candidates.json`, `screened.json` | Independent findings and hard-gate decisions |
| `status.json` | Queue states and counts |

## Worker boundary

Linux requires bubblewrap with usable user/PID namespaces; macOS requires `sandbox-exec`. A probe checks
isolation before each native invocation. The input roots, output parent, sibling results, and sibling
working directories are hidden. Workers can read their assigned frozen inputs and write their own
scratch directory. Controller-owned pipes retain diagnostics outside worker access. Use a dedicated
output parent such as `.runtime/benchmark`; it must not contain the CLI executable or schemas. Provider
authentication remains host-managed: the native client can authenticate, while model tools are denied
provider credential directories, configured credential roots, macOS keychains, and `/proc`. Codex uses
a read-only native permissions profile with tool networking disabled and a core-only shell environment.
These controls require the pinned standalone CLI version from `config.json`.
