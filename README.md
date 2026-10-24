# Static correctness review experiment

A static correctness review experiment for SONiC SWSS source: a local preparation
step and a model runner with a routing/interface profile. It compares two ways of
using LLM coding sessions to review C++ for one narrow family of correctness
defects: elementary, localized identity-versus-prefix or structured-key matching
mistakes (for example, `Ethernet4` wrongly matching `Ethernet40` through a prefix
comparison) and directly analogous typos or obvious edge cases in routing and
interface code. It runs an **exhaustive** queue of relevant production files and
a **targeted** queue of prefix/partial-match and structured-key candidates.
Preparation generates and validates artifacts. The explicit `run` command
performs model discovery, deterministic hard-gate screening, and blinded
independent candidate validation.

Requirements: Python 3.10+ (standard library only), Bash, and an existing source
checkout. `ast-grep` is optional. Preparation performs no installation. The separate
Lima bootstrap installs the pinned model CLIs only when explicitly run with `--create`.

## Vocabulary

This is a static correctness review experiment; the wording below is used
throughout the repository:

- **review** / **correctness review**: reading SONiC SWSS source for the one
  narrow family of correctness defects described above, under hard gates. A
  **review unit** is one assigned file (exhaustive mode) or one source-card
  group (targeted mode).
- **finding** / **candidate defect**: an issue reported by a discovery model,
  together with its self-assessment against the hard gates. A **screened
  finding** fails a deterministic gate check and is recorded in
  `results/screened.json` without any verifier session. **Admitted** findings
  on the same path and defect kind with overlapping line ranges are combined
  into one **candidate**; each candidate gets one blinded verifier session
  whose **verdict** is confirmed, rejected, or needs-validation.
- **hard gates**: Routing working-group fit; a closed `defect_kind` family
  (identity-versus-prefix, substring, structured-key component, delimiter
  boundary, identity typo, exact-match edge case); demonstrably clear intended
  behavior and reachable impact after tracing; exactly one obvious
  deterministic fix in one file, typically 3-10 and at most
  `max_fix_logic_lines` logic lines; and `min_confidence` (5/5) confidence in
  both the defect and the remedy. Routing working-group fit means ownership by
  the Routing working group (routes, next hops, neighbors, IP interfaces, VRF,
  VXLAN/EVPN L3, IP tunnels); adjacent features such as port mirroring, NAT,
  ACL, CoPP, or STP fail the gate even when they match interface names, and
  uncertain ownership fails it. Both prompts state the three conjunctive gates
  and the validity-first rule once each in capitals and keep everything else in
  plain register: a real defect that fails any gate is omitted by discovery and
  rejected by the verifier. Thresholds live in `runner-config.json`
  under `gates`; the family enum lives in the discovery schema; the wording in
  the prompts. Future repositories change those files, not the runner.
- **validation input**: the only candidate material a verifier session can
  read, `results/validation/ID/validation-input.json`: location, defect kind,
  and one-line claims. It never contains discovery evidence, confidences, fix
  summaries, model names, trace paths, or other verdicts.
- **scope** / **constraints**: what a session may read and run (static, read-only
  inspection only). **Evidence** is the retained read-only commands and output
  that a trace must contain.
- **legacy runtime names**: the existing, already-authenticated VM keeps its
  original names: VM `sonic-static-audit`, guest user `audit` with home
  `/home/audit`, guest paths `/opt/audit-clis` and `/opt/audit-node`, and the
  runner lock directory `/tmp/sonic-static-audit-<uid>`. This repository's name
  and the npm flag `--no-audit` are likewise unchanged.

## Prepare

Run from this repository:

```sh
./experiment/run-experiment.sh prepare exhaustive --repo /path/to/sonic-swss --out ./results/exhaustive
./experiment/run-experiment.sh prepare targeted --repo /path/to/sonic-swss --out ./results/targeted
```

Defaults are the sibling `sonic-swss` checkout, `results/MODE` in this repository,
and `experiment/config.json`. `--config /path/to/profile.json` overrides the
profile. Relative overrides are relative to the caller's working directory.
Output and source must be disjoint directories. Identical reruns are accepted;
changed inputs/configuration or partial output require a fresh `--out` directory.
Existing results are never silently replaced or deleted.

The commands can also be used separately:

```sh
python3 -B experiment/src/select.py --repo /path/to/sonic-swss --config experiment/config.json --out ./results/manual
python3 -B experiment/src/extract.py --repo /path/to/sonic-swss --index ./results/manual/selected-groups.json
python3 -B experiment/src/validate.py --out ./results/manual
```

Selection defaults to targeted mode. `--mode exhaustive` writes the exhaustive
manifest and an explicit empty, `not-run` targeted index; it does not invoke a
matcher or require cards. The runner passes the expected mode to validation.

## Run a prepared queue

With the configured standalone `codex` and `claude` CLIs already installed and
authenticated, run either prepared mode:

```sh
./experiment/run-experiment.sh run exhaustive --reference /path/to/references
./experiment/run-experiment.sh run targeted --reference /path/to/references
```

`--prepared DIR` defaults to `results/MODE`; `--out DIR` defaults to
`results/MODE-run`. These directories, the source, and reference inputs must be
disjoint. Run invokes provider CLIs; preparation and the local tests do not.
Source and reference inputs should be mounted read-only. Discovery uses
`--sandbox danger-full-access` with `approval_policy="never"`: the Linux guest
runtime failed to execute inspection commands in `read-only` mode, while a direct
probe with this mode retained a completed native command event and source output.
In this mode Codex itself does not restrict file access; the source and reference
inputs stay read-only because the VM mounts them read-only. Verifier tools remain
limited to Read/Grep/Glob. Prompts still exclude source execution, tests, builds,
network tools, and source edits. Retained command output is checked against actual
source bytes.

Each review unit gets fresh sessions for both models in `runner-config.json`.
Kernel file locks enforce sixteen discovery sessions and one verifier session
across runner processes for the Unix user, independent of mode or output path.
Retryable transport, session, result, or inspection-evidence failures get up to
**three total attempts** (the default), each with a fresh ephemeral session and
working directory. Missing CLIs, unavailable isolation, invalid local
configuration (including CLI usage exit status 2), verifier model mismatch, and
local storage errors stop immediately. Only a validated
success is atomically published to `results/discovery/TASK` or
`results/verifier/CANDIDATE`, with its original successful trace, result, prompt,
native usage metadata, and completion record. Failed attempts never become
successful evidence.

Pass `--diagnostics-root /absolute/private/review-diagnostics` to choose a
persistent runner-host-local diagnostics directory. The default is
`/var/tmp/sonic-static-review-diagnostics-<uid>`. It must be owned by the runner,
mode 0700, and outside/disjoint from this repository, the results tree, source,
prepared inputs, and references. Do not mount it into another VM or share it
with discovery workers. All runners sharing the global slots bind to the same
root; a different root is refused. Keep it stable across runs. `TMPDIR` must
also be outside the repository, results, and input directories.

Raw stdout/stderr go directly into private attempt directories, including during
execution. Failed attempts retain their prompt, native trace, stderr, workspace,
and `failure.json` with exact stage, reason, exception detail, and traceback.
`attempt.json` records the latest stage even if an attempt is interrupted.
Successful artifacts are copied through a fixed allowlist, then their private
scratch is removed. No environment dump is collected. The public notification
contains only lane, task ID, actual attempt count, category, stage, reason, and
retryability; no raw provider text or private diagnostic path is copied into it.

The runner enforces the diagnostic boundary for both model lanes. Linux requires
**bubblewrap (`bwrap`) and usable user/PID namespaces on the runner host**;
these are operator prerequisites, separate from the pinned model CLI bootstrap.
It masks diagnostics with an empty read-only mount and hides parent process
filesystem handles with a private PID namespace. The Linux worker also gets
bubblewrap's minimal private `/dev` (null, zero, full, random, urandom, tty,
ptmx with a fresh devpts, shm, and fd links) rather than the host device tree:
a plain bind of `/` carries `nodev`, which left every device node unusable, so
the Codex TTY-backed shell tool could not start a single inspection command.
macOS uses `sandbox-exec` to deny diagnostic reads/writes and access to other
processes. A local canary probe runs before any model invocation; it checks that
the private root is unreadable and that `/dev/null` and PTY allocation work
inside the boundary, so unavailable or unusable isolation fails without calling
a provider. Permissions or prompt instructions alone are not the worker boundary.
The source and reference mounts remain read-only as before.

The same boundary hides run outputs from model sessions. The run's parent
directory is always masked (an empty read-only mount on Linux, a denied subpath
on macOS), so sibling arms and earlier runs next to it are invisible; pass
`--hide DIR` (repeatable) to mask a shared results tree such as the results
mount. Inside the mask only the worker's inputs are bound back read-only: the
prepared inputs for discovery sessions, and for each verifier session its own
`results/validation/ID` directory. This matters because the verifier's
`--add-dir` list confines the Read tool but not Grep or Glob: a search rooted
above the results tree can otherwise return lines from other candidates'
reports, results, and traces. The pre-flight probe also checks that a sentinel
inside a hidden tree is unreadable and that every exposed input is readable.
Scratch (`TMPDIR`) and the CLI executables must be outside the hidden trees.

Discovery acceptance requires successful native `command_execution` events before
the final answer, including for an empty candidate list. The retained output must
cover the assigned file and show related source context. Whole reads, numbered
reads, or multiple complete ranges can establish coverage. Accepted forms include
`cat` (with or without `--`, one or several files), `sed -n` ranges (including
`A,Bp;C,Dp` lists), `nl -ba`, `cat -n`, and `head`/`tail` ranges, alone or piped
through those same range and numbering filters (for example
`nl -ba FILE | sed -n '241,480p'`). Numbered `rg` and `grep` searches can use
different flag spellings, literal or regular-expression patterns, context lines
(`-A`/`-B`/`-C`), a `head` bound, and source directories or specific files; search
roots may also name reference inputs, whose hits earn nothing. Reads of related
C/C++ source containing an assigned-file identifier also establish context. Local
search hits suffice when there are no external references.

One retained command may chain several simple commands with `&&`, `;`, `||`, or
newlines. The validator reconstructs the exact expected bytes of every read (and
of `wc -l`) from the source and attributes the aggregated output to the chain in
order. Commands whose output cannot be reconstructed (reads of source cards or
references, listings, echoes, `find`) are skipped without credit, and a search's
output is verified only when its boundaries in the chain are known. A chain's exit
status reflects only its last command, so byte-exact reads and verified searches
in a chain keep their credit, while a single failed command never counts. Paths
can be absolute or source-relative, including after `cd SOURCE && ...`;
`sh`/`bash`/`zsh` wrappers and a discarded stderr (`2>/dev/null`) are recognized.
Every counted line is checked against source bytes and line numbers. Reads must
retain their complete requested ranges, with all assigned lines covered across
reads. At least two distinct credited commands are required; chained commands
count individually. No-tool sessions, listings, prose claims, source cards,
failed reads, unrelated content, and fabricated/truncated evidence do not
establish coverage. The validator never executes saved commands. This proves
recorded source access and related context, not semantic review quality or
complete caller coverage.

Resume checks the native trace again after verifying artifact hashes; a valid
completion record alone cannot authorize passive results. Historical traces
containing only thread/turn events and final answers are incomplete review
evidence, including the reported 262-session targeted run. They must not be
counted as reviewed units or clean findings. Use a new output directory for a
fresh run with the revised prompt; existing run bindings reject prompt changes.

Actual findings produce `results/candidates/ID/candidate.json` and `report.md`;
clean units produce no Markdown report. Every discovery finding carries the
gate fields (`defect_kind`, `wg_fit`, `intended_behavior`, `intention_clear`,
`impact_reachable`, `fix_summary`, `fix_logic_lines`, `files_touched`,
`fix_unique`, `defect_confidence`, `remedy_confidence`). Findings that fail a
deterministic gate check are listed in `results/screened.json` with their
failed gates and never reach the verifier. Admitted findings with the same
path and defect kind and overlapping inclusive line ranges are combined into
one candidate, retaining every discovery occurrence and the set of reporting
models; agreement is aligned by location and kind, never by wording. The
serial verifier gets a fresh session per candidate with `--add-dir` limited to
the source, the references, and that candidate's own `results/validation/ID`
directory; it receives no prior verdicts, sibling candidates, discovery
evidence, or traces, so its prompt size is constant. It re-scores the gates
blind, writes the single fix as a unified diff in `fix_patch`, and returns
confirmed (every gate holds at 5/5 and the patch changes exactly the candidate
file within the logic-line bound), rejected (with `failed_gates`), or
needs-validation. A verdict whose status disagrees with its own gate fields is
an invalid result and is retried like any other session failure. A rejection
that offers no fix leaves `fix_patch` empty with `files_touched` and
`fix_logic_lines` at 0; those two gates are then not scored, while a confirmed
verdict always needs its patch. A verdict is
not runtime reproduction evidence.

Pass `--max-verifier-tasks N` to refuse starting the serial verifier when more
than N candidates were admitted. The run stops after writing the candidates,
validation inputs, and `results/screened.json`, reports the count, and can be
rerun with the same output directory and a higher cap; discovery results are
resumed, not repeated.

`status.json` records queued/running/succeeded/failed counts for both lanes.
Terminal failures append to `notifications.jsonl`; healthy tasks continue and
the run exits nonzero. Rerunning the same output verifies and skips completed
artifacts and preserves terminal failures without duplicate notifications.
Use a new output directory to retry terminal failures or change run inputs.
Only one runner may own an output directory at a time.

For a six-unit local pilot queue using the identical production runner:

```sh
python3 -B experiment/src/review_queue.py --prepared results/exhaustive --mode exhaustive --out results/exhaustive-queue.json
./experiment/run-experiment.sh run exhaustive --reference /path/to/references --queue results/exhaustive-queue.json
```

Repeat with `targeted` for six groups. Each queue schedules twelve discovery
sessions. Run the exhaustive and targeted arms simultaneously (24 sessions total)
to exercise the shared global 16-slot discovery limit; a single twelve-session
arm cannot saturate it. Verifier concurrency remains one across both arms.
Supply six `--path` arguments to choose explicit paths instead of the
first six available units. No VM operations are performed by the runner.

Before any full production run, evaluate the finished pilot arms with the
acceptance gate:

```sh
python3 -B experiment/src/pilot_gate.py --run results/exhaustive-run --run results/targeted-run \
  --positive orchagent/known-positive.cpp --negative orchagent/known-negative.cpp \
  --max-candidates 12 --max-discovery-failures 0 --max-cost-usd 10 --out results/pilot-gate.json
```

It re-validates each run through the export inventory (hashes, exact children)
and refuses an active run, then checks: runner integrity (empty `work/`, every
candidate verified with no verifier failure, every review unit covered by at
least one published discovery session, and terminal discovery failures within
the explicit `--max-discovery-failures` allowance, default 0); gate fields on
every published finding and `results/screened.json` present; the admitted
candidate count per run against `--max-candidates` (and, only when
`--max-admitted-ratio` is given, admitted over raw findings, which depends on
how positive-heavy the queue is); the fix shape of every confirmed verdict;
verifier independence (no prior verdicts, sibling candidates, discovery
evidence, or trace paths in any verifier prompt; every absolute path in a
verifier tool input inside the source, the references, or that session's own
validation directory; no other results path in any tool result; validation
inputs limited to their allowed fields; prompt sizes within 2 KB of each
other); the positive/negative control paths; and the verifier cost. It prints
PASS or FAIL per check, writes the JSON report, and exits nonzero on any
failure. A failed gate means tune the prompts or controls, use a new output
directory, and re-pilot; do not start production.

To select six **previously failed** review units, use only the historical full
run's `notifications.jsonl` and the new preparation; old failed traces are not
needed or opened:

```sh
python3 -B experiment/src/review_queue.py \
  --prepared /path/to/new-prepared --mode targeted --out /path/to/recovery-queue.json \
  --notifications /path/to/historical-full-run/notifications.jsonl \
  --task-id "$FAILED_TASK_1" --task-id "$FAILED_TASK_2" --task-id "$FAILED_TASK_3" \
  --task-id "$FAILED_TASK_4" --task-id "$FAILED_TASK_5" --task-id "$FAILED_TASK_6"
./experiment/run-experiment.sh run targeted \
  --prepared /path/to/new-prepared --queue /path/to/recovery-queue.json \
  --reference /path/to/references --out /path/to/new-pilot-run \
  --diagnostics-root /absolute/private/review-diagnostics
```

The six IDs must be historical discovery failures mapping to six distinct current
units. Unavailable/stale IDs, wrong modes/models, and duplicate units are refused.
Both configured models are queued for every selected unit, including when only
one model previously failed. The pilot uses the production runner unchanged.

Export only validated publishable outputs with the allowlist helper:

```sh
python3 -B experiment/src/export_results.py --run /path/to/new-pilot-run --out /path/to/review-results.tar.gz
```

It requires a finished, unlocked run and checks completion inventories and
artifact hashes. The archive includes run/status metadata, bounded notifications,
validated success artifacts, candidate JSON/Markdown, screened findings, and
validation inputs. It never walks arbitrary
output subtrees or follows symlinks/hardlinks. Scratch, private diagnostics,
locks, and unknown files are excluded. Broad historical failure notices cannot
be exported with this helper. Do not archive a workspace with a recursive tar or
copy command; use this allowlist. Export is local and does not publish to GitHub.

For an archive without Git metadata, `source_identity.py --repo DIR --commit SHA --out FILE` creates an
immutable identity file attesting the snapshot bytes and the supplied known
source commit. Pass that file as `prepare ... --identity FILE`; subsequent
validation checks both the identity and snapshot bytes. The identity is an
operator attestation, not independent proof of an archive's origin.

## Selection and provenance

Edit the small profile to change roots, literal domain anchors, patterns, or
context. `intended_source_commit` is pinned to the exact proposed-fix commit;
selection rejects any other source revision. The manifest also records the actual
HEAD independently.
Only `.git/HEAD`, its loose ref, or `packed-refs` are read for commit metadata;
no Git commands, configuration, hooks, or history are used.
Standalone checkouts and plain snapshots are supported. Snapshots record a null
commit. Linked worktrees/external Git metadata are rejected instead of traversed.

Selection walks the configured roots in sorted order and includes `.c`, `.cc`,
`.cpp`, `.cxx`, `.h`, `.hh`, `.hpp`, and `.hxx` files. It skips symlinks, hidden
entries, test/mock directories and common test filenames, build outputs,
packaging/container/testbed directories, and virtual environments. Missing roots,
invalid UTF-8 source, and an empty relevant manifest fail explicitly.

Domain inclusion is a case-insensitive literal substring match in the relative
path **or** file contents. Every matching anchor is recorded as an inclusion
reason. Broad anchors such as `port` intentionally favor recall. The manifest
records the canonical source root, commit, complete profile, file SHA-256 hashes,
and LF-based line counts. It covers current working-tree bytes, including local
edits/untracked production files; the commit alone does not assert a clean tree.

When available on PATH, `ast-grep` (or an `sg` binary reporting itself as
`ast-grep`) searches the configured structural patterns. Source is passed on
stdin, parsed as C++ (including C/header inputs), with an explicit empty config
inside output scratch space. It does not traverse the target, load target
configuration, or inherit provider environment variables. Installed-tool errors
fail the run. Its version is recorded; use the same version when comparing
preparations.
The command format follows the [ast-grep CLI reference](https://ast-grep.github.io/reference/cli/run.html).

If neither executable is available, selection announces and records the
deterministic `python-regex` fallback. Its version `1` refers to this baseline
algorithm. Configured ASCII regexes scan UTF-8 bytes with Python's `re.MULTILINE`.
They can match comments/strings, miss complex syntax, and stop at the first
closing parenthesis. Both engines intentionally produce broad candidates (for
example, `find` may be a full lookup rather than a prefix check); their results
are not equivalent and are not findings. No fallback match claims an AST-derived
function. All baseline cards use explicitly labeled line windows.

## Artifacts and validation

```text
OUT/
  manifest.json
  selected-groups.json
  groups/                    # targeted mode only, even for zero groups
    g-<20 hex digits>/
      source-card.md
      scope.json
```

There is one targeted group per matched file. Matches are deduplicated and sorted
by start byte, end byte, and pattern ID. A group ID hashes the relative path,
source hash, exact matches, and context size. The index binds to the manifest's
SHA-256. No timestamps, random IDs, or output paths enter the persisted artifacts.
For the same source location/bytes, profile, engine, and engine version, repeated
preparations produce byte-identical artifacts.

Each match includes a zero-based, half-open byte range and one-based inclusive
line range. Extraction adds the configured context before/after the full match,
clips at file boundaries, and merges overlapping windows. Disjoint windows stay
separate within the same card. Scope metadata contains exact ranges and source
hashes; numbered excerpts are verified against the source bytes.

Validation checks exact directory inventories (including rejection of extra
partial files), JSON structure, source commit, the complete current manifest,
hashes, groups, locations, IDs, and byte-for-byte rendered scopes/cards. It also
recomputes regex candidates, detecting omissions. AST artifacts can be validated
without installing ast-grep: their source bytes, ranges, and cards are verified,
but structural-match completeness is not independently re-proved. An interrupted
write remains invalid and must be regenerated in a fresh output directory.
Validation prints `PASS` or `FAIL` and returns nonzero for invalid state.

## Plan or create the review VM

The new VM `sonic-static-review-v2` must be created locally on host **`u`**. The bootstrap
reads the kernel fields for `uname -s` and `uname -m` (via Python's `os.uname`)
and selects exactly one supported profile; it never guesses from installed tools:

| Host | Required Lima | VM / Ubuntu 24.04 image | CPUs | Memory | Disk | Mounts |
| --- | --- | --- | --- | --- | --- | --- |
| Linux / x86_64 (`u`) | **2.0.3** | QEMU / AMD64 | **8** | **32 GiB** | **200 GiB** | 9p |
| macOS / arm64 (explicitly supported) | **2.2.1** | VZ / ARM64 | 4 | 8 GiB | 40 GiB | virtiofs |

Other OS/architecture pairs, including Intel macOS and ARM Linux, are rejected
even in dry-run and even with a host override. Linux creation also requires
`qemu-system-x86_64` and `qemu-img` on PATH, outside all mounts. Bootstrap checks
their availability without executing them; Lima uses them during explicit
creation. Host tools are never installed by bootstrap.

Preview the current host's exact configuration without Lima or QEMU installed:

```sh
./experiment/bootstrap-lima.sh --dry-run
```

For a later, explicitly requested creation, run on **`u`** with Lima **2.0.3**
and QEMU already installed:

```sh
./experiment/bootstrap-lima.sh --create --name sonic-static-review-v2 \
  --source /absolute/path/to/sonic-swss \
  --reference /absolute/path/to/reference-materials \
  --results /absolute/path/to/review-results
```

Run the bootstrap locally on the intended Linux host. The command validates the
detected architecture and version pins before creating the VM.
The optional name defaults to the legacy `sonic-static-audit`; its existing
behavior and profiles are preserved. The new name requires Linux/x86_64 on `u`
(or its fully qualified hostname), even with `--allow-other-host`. Dry-run on a
supported Mac can preview either name without creating a VM. There is no option
to select a foreign host profile. The wrapper contains no remote-access command.

All three paths must be existing, absolute, disjoint directories; results must
be empty. Home directories, the repository parent, hidden directories, and
symlink mount roots are refused. `--dry-run` also accepts these three paths to
validate and print the exact planned YAML, host commands, and guest script. With
no paths it uses labeled placeholders. Dry-run never invokes Lima, downloads,
creates scratch files, or changes VM state. No-argument invocation is an error.

The [create command](https://lima-vm.io/docs/reference/limactl_create/) uses a
literal config for the selected VM name, with the profile's explicit resources
and Ubuntu 24.04 image dated 2026-10-17, verified against its architecture-specific
[SHA-256](https://cloud-images.ubuntu.com/releases/noble/release-20261017/SHA256SUMS).
Linux explicitly selects [QEMU](https://lima-vm.io/docs/config/vmtype/qemu/) and
9p, as supported by the [Lima 2.0.3 configuration](https://github.com/lima-vm/lima/blob/v2.0.3/templates/default.yaml).
It refuses an existing VM, including stopped or partial state. Inherited Lima
`_config/default.yaml` or `override.yaml` is refused; use a clean, absolute
`LIMA_HOME` if needed, and keep that same environment for later manual commands.
Lima maintains its own VM state there (normally `~/.lima`). Bootstrap's input
YAML lives only in a private temporary directory, removed on success or failure.
No permanent host YAML is written separately from Lima's managed VM state.

Only source and references are mounted read-only, at `/mnt/source` and
`/mnt/references`; the separate `/mnt/results` mount is writable. The host home
directory, the repository parent, SSH keys/agent, and proxy settings are not
shared with the guest. Containerd and automatic port forwarding are disabled.
Inputs must be dedicated source and reference trees, not general-purpose
directories.
The guest rejects a mismatched architecture before setup, then downloads a
checksummed
[Node 22.14.0 binary](https://nodejs.org/download/release/v22.14.0/SHASUMS256.txt)
(Linux x64 for the AMD64 guest, Linux arm64 for the ARM64 guest) and installs the
exact npm versions in `experiment/config.json`: Codex **0.157.1** and Claude Code
**2.1.302**. Codex npm lifecycle scripts are disabled; Claude Code's official
postinstall remains enabled to fetch its pinned platform binary. No package
source compilation is allowed. CLI wrappers disable automatic updates.

For `sonic-static-review-v2`, setup also installs Ubuntu's `bubblewrap` package
and persists `kernel.apparmor_restrict_unprivileged_userns=0` in
`/etc/sysctl.d/99-sonic-static-review-userns.conf` inside the new guest. It applies
and reads back that setting, then runs the runner's exact `verified_worker_prefix()`
sentinel probe as the unprivileged guest user. The planner sends its local probe
modules over stdin; no code is imported from the reviewed source mount. Package,
sysctl, or isolation failures abort setup before the ready receipt. The default
legacy setup skips these steps; existing VMs are still refused.

Guest setup verifies CLI versions against the pins and writes
`/mnt/results/setup-receipt.txt`, including the selected VM name, guest
architecture, and Node archive checksum. Host validation requires that final receipt to be the only result and
match the selected profile and CLI pins. A failure during creation, start, setup,
receipt validation, or a caught interrupt destroys the new VM, removes an
incomplete receipt, and cleans temporary config. **A retry starts from scratch.** It never patches or
repairs an existing VM. If Lima itself cannot delete the VM, the script fails
with the exact manual delete command; do not reuse that partial VM. Unexpected
user files in results are reported, never silently deleted.

After successful setup, the script prints these manual commands:

```sh
limactl shell --workdir=/home/audit sonic-static-review-v2 codex login --device-auth
limactl shell --workdir=/home/audit sonic-static-review-v2 claude auth login
```

[Codex device auth](https://developers.openai.com/codex/cli/reference/) and
[Claude auth](https://code.claude.com/docs/en/cli-reference) are never run by
bootstrap. This VM hosts the later **static-only** CLI review sessions.
Bootstrap does not execute source, run source builds/tests, install Docker,
start virtual switches, invoke model agents, or mutate GitHub. Preparation
remains local-only; explicit VM creation downloads its image and pinned tooling.

## Local verification

```sh
python3 -B -m unittest discover -s tests -v
bash -n experiment/bootstrap-lima.sh
bash -n experiment/guest-setup.sh
bash -n experiment/run-experiment.sh
./experiment/bootstrap-lima.sh --dry-run
./experiment/bootstrap-lima.sh --dry-run --name sonic-static-review-v2
git diff --check
```

Tests use disposable OS temporary directories (or the caller's `TMPDIR`) for all
fixtures and outputs, with output outside each source fixture. Keep `TMPDIR`
outside this repository and results so the diagnostic boundary is exercised. It must be an absolute path with no hidden directory components because
mount validation rejects those.
Tests never use `results/exhaustive` or `results/targeted`; generated baseline results are not
retained in this repository. The suite exercises both preparation commands,
deterministic filtering/extraction/validation, mocked ast-grep, and mocked Lima
creation, refusal, rendering, receipt validation, and failure cleanup under both
Linux/x86_64 and macOS/arm64 identities. It also checks the new VM name and `u`
restriction, legacy behavior, unsupported hosts, QEMU prerequisites, exact
profile pins, and mutation-free dry-runs. Both profiles are tested locally with mocked kernel
identity; verification never connects to `u`. C/C++ fixtures are text only, never
compiled or executed. No VM or real provider CLI is used in verification. Tests
need only the Python standard library and Bash. Fake model CLIs bypass
OS isolation through test-only mocks; separate tests verify the isolation command,
probe, and refusal contracts. They do not prove a host's kernel namespace support.
The production preflight must succeed on the eventual runner host. Nested macOS
sandbox execution may be unavailable inside a managed development environment.
