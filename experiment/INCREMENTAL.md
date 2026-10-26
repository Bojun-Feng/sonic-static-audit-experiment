# Incremental benchmark

`src/incremental.py` runs configuration → queue → discovery → screening →
independent review → resume. Its frozen inventory contains 284 exhaustive files
and 131 targeted units at source commit
`2f1f7921551ae9f9c799c8b83b8a15b1d172d395`.

## Execute or plan

From the repository root:

```sh
python3 experiment/src/incremental.py --archive /path/to/frozen-input \
  --out .runtime/benchmark --config experiment/incremental-config.json --execute
```

Omit `--execute` to import and plan offline. The importer reads completed
max-production discovery observations, task definitions, prepared inputs, and
source/reference trees. It validates task identity, prompt context, structured
answers, native sessions, and metadata. The archived launch record attests
max effort; effective model settings are known when emitted natively.

Every admitted finding gets its own candidate ID, including findings with the
same location and claim. Every enabled reviewer condition gets a blinded input
containing the candidate ID, location, defect kind, and title/root-cause claim.
Discovery evidence, model identity, and other verdicts stay private.

## Configure

`incremental-config.json` enables Astra and GPT-5.6 Sol discovery at max and
Fable 5.1 review at max. Each condition explicitly names `model`, `effort`, and
`service_tier`. The capability catalog is `incremental-models.json`; add a model
there and enable its condition in the run configuration. Supported service tier
is `default`. Missing native effective-setting fields remain unknown.

A fresh output uses the supplied conditions. Reusing an output allows condition
expansion and changes to retry count or timeout. Recorded conditions retain their
identity. Reordering conditions preserves cell identity. The explicit experiment
version, source revision, templates, gates, and input bytes bind the output.
Use a new version and output directory for an intentional contract change.

`prompts/`, `schemas/`, and `runner-config.json` define the contract;
`review_validation.py` enforces the gates. Template contents are recorded in
`experiment.json`. Comparison uses literal contents and explicit identities.

## Resume and results

Execution is serial under an output lock. An identical command validates and
skips completed cells, then attempts missing or failed cells. Each invocation
supplies a fresh per-cell attempt budget. Interrupted attempts keep their receipt
and captured stream, with an additional interruption record. Unobserved end times
and usage remain unknown. Success publication is atomic after native-session,
answer, and gate validation.

An invalid published success stops preflight, preserves the result and candidate
ledger, and writes `state: invalid` to `status.json`. Resolve the damaged input
or choose a fresh output directory. Comparisons retained under invalid status
require validation before use.

Initial input copying is not transactional. An interruption before both
`experiment.json` and `config.json` exist requires a fresh output directory.
Resume applies to model attempts after setup completes.

| Path | Contents |
| --- | --- |
| `experiment.json`, `config.json` | Input binding and enabled conditions |
| `inputs/` | Frozen source, references, and prepared cards |
| `results/discovery/`, `results/verifier/` | Successful native streams, prompt, answer, metadata, completion |
| `attempts/` | Attempt receipts, stdout/stderr, usage, timing, failures |
| `candidates.json`, `screened.json` | Independent findings and gate decisions |
| `validation/` | Blinded inputs |
| `status.json` | Cell states and counts |
| `import-diagnostics.json` | Invalid archive observations eligible for discovery |

Native accounting is retained when emitted, including on failed attempts.
Imported timing contains the recorded elapsed time; unavailable timestamps stay
null. Controller-owned pipes capture stdout/stderr while worker diagnostics stay
hidden. Source/reference/card copies are exposed read-only inside the OS boundary.
The archive, output parent, and sibling results stay hidden. Outputs whose parent
would hide the CLI schema are rejected before setup; `.runtime/benchmark` uses a
dedicated results parent. The isolation probe must pass before model invocation.

## Pilots and repeats

Use repeatable `--unit ARM:UNIT_ID` to select exact frozen units. Reviews follow
the candidate's origin unit. Use positive `--max-calls N` to bound native launches
across discovery, repeats, reviews, and retries. Unselected or budget-deferred
cells stay queued. Exit zero means the invocation observed no failed executions;
inspect status for queued work. Omitting selectors and the cap visits the full
configured queue.

`incremental-pilot-config.json` enables low, medium, high, xhigh, and max for Astra
and production Sol, with Fable max and one attempt per invocation. For example,
`--unit exhaustive:f-41d17f83ff64d407110c` selects `cfgmgr/intfmgr.cpp` and
`--unit exhaustive:f-e228024a409a3ef6d0d0` selects `orchagent/return_code.h`.
These two units have 16 non-max discovery cells and one admitted imported finding.
Actual review counts depend on admitted discoveries and the call cap.

To repeat an imported discovery, select its unit and supply
`--repeat-discovery ARM--UNIT_ID--MODEL--max --repeat-id NAME`. The name binds to
that cell; reusing it resumes/skips the repeat, and a new name requests another
observation. The imported result stays intact. `repeats/NAME/historical/` holds
its snapshot; `fresh/` holds the repeat success, `attempts/` its traces, and
`comparison.json` the comparison.

A repeat is a separate reproducibility observation with its own findings and
comparison. The main candidate/review ledger is independent. Comparisons include
parsed-result equality, schema validity, screening counts, sessions, usage,
cost, duration, and prompt contexts. Relocated input paths are recorded explicitly.
Model output equality is measured per repeat.

## Verify offline

```sh
python3 -m unittest discover -s tests -p 'test_incremental*.py'
python3 -m unittest discover -s tests
```

Fake-CLI tests cover full-queue execution, zero-call resume, retries, expansion,
blinding, pilots, and repeats. Read-only archive tests verify 830 valid discovery
observations and 49 independent admitted findings. macOS tests exercise the real
sandbox with fake CLIs. Tests report skips for unavailable archive or sandbox
prerequisites; Linux isolation and bootstrap contracts use local mocks.
