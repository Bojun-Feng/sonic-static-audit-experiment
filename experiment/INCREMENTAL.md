# Independent review queue

Prepare both arms into a fresh directory, then run from the repository root:

```sh
./experiment/run-experiment.sh prepare exhaustive --repo /path/to/source --out /path/to/prepared/exhaustive
./experiment/run-experiment.sh prepare targeted --repo /path/to/source --out /path/to/prepared/targeted
```

For a Git-free frozen snapshot, pass the same immutable `--identity FILE` to
both prepare commands. The identity attests the known revision; preparation
validates its source bytes. Keep that identity available alongside the original
input roots for resume. The reference root supplies immutable reference files.
Prepared manifests retain their original source and identity paths.

Start the queue:

```sh
python3 experiment/src/incremental.py --source /path/to/source \
  --reference /path/to/reference --prepared /path/to/prepared \
  --out .runtime/benchmark --config experiment/incremental-config.json --execute
```

The prepared root contains newly generated `exhaustive/` and `targeted/`
manifest/index bundles and targeted source cards: 284 files and 131 groups at
revision `2f1f7921551ae9f9c799c8b83b8a15b1d172d395`. Every configured discovery
cell begins missing in an empty output. Omit `--execute` to plan offline.
Source selection and cards are validated before scheduling. The frozen source may
contain relative symlinks within its own tree. Exhaustive unit IDs use the frozen
manifest order; targeted IDs come directly from the newly generated index.

## Configuration

```json
{
  "schema_version": 1,
  "experiment_version": "sonic-incremental-v1",
  "source_commit": "2f1f7921551ae9f9c799c8b83b8a15b1d172d395",
  "discovery": [
    {"model": "gpt-6-astra", "effort": "max", "service_tier": "default"},
    {"model": "gpt-5.6-sol", "effort": "max", "service_tier": "default"}
  ],
  "verifier": [
    {"model": "claude-fable-5-1", "effort": "max", "service_tier": "default"}
  ],
  "passes": 1,
  "concurrency": 2,
  "timeout_seconds": 1800
}
```

The model catalog is `incremental-models.json`. Each condition explicitly names
its model, effort, and service tier. `passes` starts at 1 and can only increase.
Each pass is a separate discovery sample; its number is in the cell ID and
receipts, not the discovery prompt, so repetitions have identical prompt bytes.
Raising `passes` from 1 to 2 queues only pass 2 when pass 1 is complete.
Every finding from every pass gets every configured reviewer condition.
Add conditions to the same configuration and rerun the same command. Existing
conditions must remain; concurrency and timeout may change. Frozen inputs,
source revision, templates, gates, and experiment version bind each output
directory. A different contract needs a fresh output.

## Scheduling and resume

The queue pairs every unit with every discovery condition. Up to `concurrency`
independent calls run at once across discovery and review. A completed discovery
publishes immediately; each admitted finding queues every reviewer condition
without waiting for the remaining discoveries. There is no candidate merging.

Cell IDs use readable arm, unit, model, effort, and pass values. Receipts carry
the same task and condition. Candidate IDs use discovery cell and finding index.
Adding passes or reordering conditions preserves earlier IDs and accepted
results. A failed attempt retries within the same pass; it is not a new pass.
Reviewer input contains only candidate ID, location, defect kind, and title/root
cause claims. Discovery evidence and other verdicts stay private.

Each missing cell gets one attempt per invocation. A failed or incomplete attempt
remains in its receipt directory; the next invocation starts a fresh attempt.
Published successes are validated and skipped before provider calls. Native
usage, timing, prompts, and streams are retained. Missing native fields remain
unknown. Publication uses an atomic directory rename. An interrupted controller
may leave a running receipt; the absence of a published success determines retry.

An output lock prevents simultaneous controllers, and native subprocesses retain
the lock until they exit. Setup copies frozen inputs; interrupted initial copying
requires a fresh output directory. Invalid published successes stop the command
and remain available for inspection.

Optional repeatable `--unit ARM:UNIT_ID` restricts native calls to exact frozen
units and their reviews. `--max-calls N` caps attempted cells across both lanes.
Omitting both visits the full queue. Exit zero means no attempted cell failed;
`status.json` also reports queued cells, including work deferred by a selector or
cap. These options use the same configuration and result directories.

| Path | Contents |
| --- | --- |
| `experiment.json`, `config.json` | Frozen input binding and configured conditions |
| `inputs/` | Source, reference, and source-card copies |
| `results/discovery/`, `results/verifier/` | Published native streams, prompt, answer, metadata, completion |
| `attempts/` | Per-attempt receipts, native streams, usage, timing, and failures |
| `candidates.json`, `screened.json` | Independent findings and hard-gate decisions |
| `validation/` | Blinded reviewer inputs |
| `status.json` | Queue states and counts |

## Worker boundary

Linux requires bubblewrap with usable user/PID namespaces; macOS requires
`sandbox-exec`. A probe checks isolation before each native invocation. The
input roots, output parent, sibling results, and sibling working directories are
hidden. Workers can read their assigned frozen inputs and write their own scratch
directory. Controller-owned pipes retain diagnostics outside worker access.
Use a dedicated output parent such as `.runtime/benchmark`; it must not contain
the CLI executable or schemas. Provider authentication remains host-managed:
the native client can authenticate, while model tools are denied provider
credential directories, configured credential roots, macOS keychains, and
`/proc`. Codex uses a read-only native permissions profile with tool networking
disabled and a core-only shell environment. Claude exposes only Read/Grep/Glob
with credential-path deny rules. These controls require the pinned standalone
CLI versions from `config.json`.
