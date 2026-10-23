# Static source audit experiment

A static audit preparation and model runner with a SONiC SWSS routing/interface profile. It
compares an **exhaustive** queue of relevant production files with a **targeted**
queue of prefix/partial-match and structured-key candidates. Preparation generates and validates artifacts. The explicit `run` command performs
model discovery and independent candidate verification.

Requirements: Python 3.10+ (standard library only), Bash, and an existing source
checkout. `ast-grep` is optional. Preparation performs no installation. The separate
Lima bootstrap installs pinned audit CLIs only when explicitly run with `--create`.

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
Filesystem protection comes from the VM's read-only source/reference mounts;
Codex does not enforce a read-only sandbox in this mode. Verifier tools remain
limited to Read/Grep/Glob. Prompts still prohibit source execution, tests, builds,
network tools, and source edits, and the required command-trace validator is
unchanged.

Each review unit gets fresh sessions for both models in `runner-config.json`.
Kernel file locks enforce sixteen discovery sessions and one verifier session
across runner processes for the Unix user, independent of mode or output path.
A failed or incomplete session retries in a fresh working directory. Only a
validated final success is atomically moved to `results/discovery/TASK` or
`results/verifier/CANDIDATE`, with trace, result, prompt, native usage metadata,
and a completion record. Temporary failed traces are removed.

Discovery acceptance requires source inspection in the native Codex JSONL trace,
even for an empty candidate list. Before the final answer, the session must retain
two distinct successful `item.completed` / `command_execution` records (nonempty
IDs, `status: completed`, integer `exit_code: 0`, and source output):

- `cat -- ABSOLUTE_ASSIGNED_FILE`: output must equal the entire assigned file.
- `rg -n -F -- IDENTIFIER ABSOLUTE_SOURCE_ROOT`: a literal context search for an
  identifier from the assigned file (at least three characters, excluding common
  language keywords). Each retained match must name an existing file inside the
  source root, with its actual line number and matching text. At least one match
  must be C/C++ source or a header; documentation matches alone do not qualify.

These exact command forms are the executable evidence contract; paths and the
identifier may be shell-quoted, and the native CLI may wrap a command in
`sh`, `bash`, or `zsh` with `-c` or `-lc`. Pipelines, redirections, combined
commands, directory listings, source cards, prose claims, failed reads, and
truncated output do not substitute for these records. Additional read-only
inspection is encouraged. Choose a meaningful symbol to explore callers,
definitions, or guards; when no external references exist, search hits in the
assigned file and its full read supply local context. This gate proves recorded
source access and a source-backed context search, not semantic review quality or
complete caller coverage. The runner never executes commands from saved traces.

Resume checks the native trace again after verifying artifact hashes; a valid
completion record alone cannot authorize passive results. Historical traces
containing only thread/turn events and final answers are incomplete review
evidence, including the reported 262-session targeted run. They must not be
counted as reviewed units or clean findings. Use a new output directory for a
fresh run with the revised prompt; existing run bindings reject prompt changes.

Actual findings produce `results/candidates/ID/candidate.json` and `report.md`;
clean units produce no Markdown report. Findings with the same source range and
normalized root cause are combined, retaining every discovery occurrence.
The serial verifier receives all occurrence traces and prior verdicts for
semantic duplicate checks. Verdicts are confirmed, rejected, duplicate, or
needs-validation. A verdict is not runtime reproduction evidence.

`status.json` records queued/running/succeeded/failed counts for both lanes.
Terminal failures append to `notifications.jsonl`; healthy tasks continue and
the run exits nonzero. Rerunning the same output verifies and skips completed
artifacts and preserves terminal failures without duplicate notifications.
Use a new output directory to retry terminal failures or change run inputs.
Only one runner may own an output directory at a time.

For a six-unit local pilot queue using the identical production runner:

```sh
python3 -B experiment/src/audit_queue.py --prepared results/exhaustive --mode exhaustive --out results/exhaustive-queue.json
./experiment/run-experiment.sh run exhaustive --reference /path/to/references --queue results/exhaustive-queue.json
```

Repeat with `targeted` for six groups. Each queue schedules twelve discovery
sessions. Run the exhaustive and targeted arms simultaneously (24 sessions total)
to exercise the shared global 16-slot discovery limit; a single twelve-session
arm cannot saturate it. Verifier concurrency remains one across both arms.
Supply six `--path` arguments to choose explicit paths instead of the
first six available units. No VM operations are performed by the runner.

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
no Git commands, configuration, hooks, credentials, or history are read.
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
configuration, or inherit provider credentials. Installed-tool errors fail the
run. Its version is recorded; use the same version when comparing preparations.
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

## Plan or create the clean audit VM

VMs must be created on host **`u`** unless explicitly overridden. The bootstrap
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
./experiment/bootstrap-lima.sh --create \
  --source /absolute/path/to/sonic-swss \
  --reference /absolute/path/to/reference-materials \
  --results /absolute/path/to/audit-results
```

Run the bootstrap locally on the intended Linux host. The command validates the detected architecture and version pins before creating the VM.
Apple Silicon creation requires this flag. Dry-run works on the supported Mac
without an override and prints its ARM64 profile, the host policy, and override
status. There is no option to select a foreign host profile.

All three paths must be existing, absolute, disjoint directories; results must
be empty. Home directories, the repository parent, hidden directories, and
symlink mount roots are refused. `--dry-run` also accepts these three paths to
validate and print the exact planned YAML, host commands, and guest script. With
no paths it uses labeled placeholders. Dry-run never invokes Lima, downloads,
creates scratch files, or changes VM state. No-argument invocation is an error.

The [create command](https://lima-vm.io/docs/reference/limactl_create/) uses a
literal config for VM `sonic-static-audit`, with the profile's explicit resources
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
`/mnt/references`; the separate `/mnt/results` mount is writable. Host home,
repository parent, host credentials, SSH keys/agent, and proxy credentials are
never shared. Containerd and automatic port forwarding are disabled. Inputs
must be dedicated source/reference trees, not directories containing credentials.
The guest rejects a mismatched architecture before setup, then downloads a
checksummed
[Node 22.14.0 binary](https://nodejs.org/download/release/v22.14.0/SHASUMS256.txt)
(Linux x64 for the AMD64 guest, Linux arm64 for the ARM64 guest) and installs the
exact npm versions in `experiment/config.json`: Codex **0.157.1** and Claude Code
**2.1.302**. Codex npm lifecycle scripts are disabled; Claude Code's official
postinstall remains enabled to fetch its pinned platform binary. No package
source compilation is allowed. CLI wrappers disable automatic updates.

Guest setup runs only version probes, verifies them against the pins, and writes
`/mnt/results/setup-receipt.txt`, including the guest architecture and Node archive
checksum. Host validation requires that final receipt to be the only result and
match the selected profile and CLI pins. A failure during creation, start, setup,
receipt validation, or a caught interrupt destroys the new VM, removes an incomplete receipt, and
cleans temporary config. **A retry starts from scratch.** It never patches or
repairs an existing VM. If Lima itself cannot delete the VM, the script fails
with the exact manual delete command; do not reuse that partial VM. Unexpected
user files in results are reported, never silently deleted.

After successful setup, the script prints these manual commands:

```sh
limactl shell --workdir=/home/audit sonic-static-audit codex login --device-auth
limactl shell --workdir=/home/audit sonic-static-audit claude auth login
```

[Codex device auth](https://developers.openai.com/codex/cli/reference/) and
[Claude auth](https://code.claude.com/docs/en/cli-reference) are never run by
bootstrap. This VM is for isolated **static-only** CLI audit sessions later.
Bootstrap does not execute source, run source builds/tests, install Docker,
start virtual switches, invoke model agents, or mutate GitHub. Preparation
remains local-only; explicit VM creation downloads its image and pinned tooling.

## Local verification

```sh
mkdir -p results/local-test-tmp
TMPDIR="$PWD/results/local-test-tmp" python3 -B -m unittest discover -s tests -v
bash -n experiment/bootstrap-lima.sh
bash -n experiment/guest-setup.sh
bash -n experiment/run-experiment.sh
./experiment/bootstrap-lima.sh --dry-run
rmdir results/local-test-tmp
```

Tests use disposable OS temporary directories (or the caller's `TMPDIR`) for all
fixtures and outputs, with output outside each source fixture. The commands above
keep every test write inside this repository; `TMPDIR` must be an absolute path
with no hidden directory components because mount validation rejects those.
Tests never use `results/exhaustive` or `results/targeted`; generated baseline results are not
retained in this repository. The suite exercises both preparation commands,
deterministic filtering/extraction/validation, mocked ast-grep, and mocked Lima
creation, refusal, rendering, receipt validation, and failure cleanup under both
Linux/x86_64 and macOS/arm64 identities. It also checks the `u` host restriction
and explicit override, unsupported hosts, QEMU prerequisites, exact profile pins,
and mutation-free dry-runs. Both profiles are tested locally with mocked kernel
identity; verification never connects to `u`. C/C++ fixtures are text only, never
compiled or executed. No VM or real provider CLI
is used in verification. Tests need only the Python standard library and Bash.
