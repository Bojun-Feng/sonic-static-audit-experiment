#!/usr/bin/env python3
"""Queue of independent static-correctness discovery cells over prepared static inputs."""

from pathlib import Path
import json
import re
import sys
# select.py is a repository entry point, not the standard library extension.
_import_path = sys.path[:]
sys.path[:] = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
try:
    import subprocess
finally:
    sys.path[:] = _import_path

from common import AttemptError, ReviewError, keys, read_json, require, within
from model_cli import parse_codex
from review_validation import (EXPERIMENT, runner_config, validate_answer,
                               atomic_json, defect_kinds, gate_failures)
from model_cli import discovery_prompt
from diagnostics import verified_worker_prefix
from datetime import datetime, timezone
import argparse
import fcntl
import os
import shutil
import tempfile
import time
import signal
import threading
from concurrent.futures import ThreadPoolExecutor, wait, FIRST_COMPLETED
from collections import deque

ARMS = ('exhaustive', 'targeted')
COUNTS = {'exhaustive': 284, 'targeted': 131}
SOURCE_COMMIT = '2f1f7921551ae9f9c799c8b83b8a15b1d172d395'


def validate_config(value, previous=None):
    keys(value, ['schema_version', 'experiment_version', 'source_commit', 'discovery',
                 'passes', 'concurrency', 'timeout_seconds'], 'incremental config')
    require(type(value['schema_version']) is int and value['schema_version'] == 1, 'unsupported config version')
    require(isinstance(value['experiment_version'], str)
            and re.fullmatch(r'[a-zA-Z0-9][a-zA-Z0-9._-]*', value['experiment_version']), 'invalid experiment version')
    require(value['source_commit'] == SOURCE_COMMIT, 'source changed; explicit NEW experiment version/contract required')
    require(all(type(value[k]) is int and value[k] > 0 for k in ('passes', 'concurrency', 'timeout_seconds')),
            'passes, concurrency and timeout must be positive integers')
    conditions = value['discovery']
    require(isinstance(conditions, list) and conditions, 'discovery conditions must be a nonempty list')
    seen = set()
    for condition in conditions:
        validate_condition(condition)
        identity = (condition['model'], condition['effort'])
        require(identity not in seen, 'duplicate model/effort condition')
        seen.add(identity)
    if previous:
        require(all(value[k] == previous[k] for k in ('schema_version', 'experiment_version', 'source_commit')),
                'contract changed; use an explicit NEW experiment version and output directory')
    return value


def validate_condition(condition):
    keys(condition, ['model', 'effort', 'service_tier'], 'condition')
    require(all(isinstance(v, str) and re.fullmatch(r'[a-z0-9][a-z0-9.-]*', v)
                for v in condition.values()), 'invalid condition identifier')
    catalog = read_json(EXPERIMENT / 'incremental-models.json')
    model = condition['model']
    require(model in catalog, 'unsupported model')
    require(condition['effort'] in catalog[model]['efforts'], 'unsupported model/effort')
    require(condition['service_tier'] == 'default'
            and condition['service_tier'] in catalog[model]['service_tiers'], 'unsupported service tier')


def events_at(directory):
    events = [json.loads(line) for line in (directory / 'trace.jsonl').read_text().splitlines()]
    require(events and all(isinstance(e, dict) for e in events), 'invalid native stream')
    return events


def selection_profile_compatible(prepared, current):
    return ({k: v for k, v in prepared.items() if k != 'guest_cli_versions'}
            == {k: v for k, v in current.items() if k != 'guest_cli_versions'})


class PreparedInputs:
    """Validated prepared static inputs for every queue unit."""
    def __init__(self, source, reference, prepared):
        from validate import validate
        self.source, self.reference, self.prepared = (
            Path(p).resolve() for p in (source, reference, prepared))
        for root in (self.source, self.reference, self.prepared):
            require(root.is_dir(), 'input root must be a directory')
        require({p.name for p in self.prepared.iterdir()} == set(ARMS),
                'prepared root must contain exactly exhaustive and targeted')
        self.contracts = {f'{folder}/{p.name}': p.read_text()
                          for folder in ('prompts', 'schemas')
                          for p in sorted((EXPERIMENT / folder).iterdir()) if p.is_file()}
        self.gates = runner_config()['gates']
        self.units = {}
        manifests = []
        for arm in ARMS:
            directory = self.prepared / arm
            files, groups = validate(directory, self.source, arm)
            manifest = read_json(directory / 'manifest.json')
            index = read_json(directory / 'selected-groups.json')
            require(manifest['source_commit'] == SOURCE_COMMIT, 'prepared source revision mismatch')
            require(selection_profile_compatible(manifest['profile'], read_json(EXPERIMENT / 'config.json')),
                    'prepared selection profile mismatch')
            require(files == COUNTS['exhaustive'] and groups == (COUNTS[arm] if arm == 'targeted' else 0),
                    'incomplete prepared selection: expected 284 files and 131 targeted groups')
            manifests.append(manifest['files'])
            if arm == 'exhaustive':
                self.units[arm] = [dict(mode=arm, unit_id=f'f-{i:04d}', path=entry['path'])
                                   for i, entry in enumerate(manifest['files'])]
            else:
                self.units[arm] = [dict(mode=arm, unit_id=group['id'], path=group['path'])
                                   for group in index['groups']]
        require(manifests[0] == manifests[1], 'prepared arms must select identical source files')


def invoke(command, prompt, work, timeout, output_fd, capture):
    """Stream worker pipes into private files opened only by the controller.

    Inherited regular-file stdout/stderr descriptors point into the hidden tree:
    macOS denies fstat on them, and the CLI runtime can then silently disable its output streams.
    Pipes preserve native output without granting access to diagnostic files.
    """
    with (capture / 'trace.jsonl').open('xb', buffering=0) as stdout, \
            (capture / 'stderr.log').open('xb', buffering=0) as stderr:
        try:
            process = subprocess.Popen(command, cwd=work, stdin=subprocess.PIPE,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                       start_new_session=True, pass_fds=(output_fd,))
        except OSError as exc:
            raise AttemptError('configuration', 'invoke', 'cli_unavailable', False, str(exc)) from exc
        errors = []

        def drain(stream, destination):
            try:
                with stream:
                    while True:
                        data = os.read(stream.fileno(), 65536)
                        if not data:
                            break
                        # FileIO is unbuffered; handle short writes without dropping bytes.
                        pending = memoryview(data)
                        while pending:
                            written = destination.write(pending)
                            if not written:
                                raise OSError('native capture write made no progress')
                            pending = pending[written:]
            except OSError as exc:
                errors.append(exc)

        def send_prompt():
            try:
                with process.stdin:
                    process.stdin.write(prompt.encode())
            except BrokenPipeError:
                # A failed CLI may exit before consuming its prompt; its exit
                # status and captured stderr remain the authoritative error.
                pass
            except OSError as exc:
                errors.append(exc)

        threads = [threading.Thread(target=drain, args=(process.stdout, stdout)),
                   threading.Thread(target=drain, args=(process.stderr, stderr)),
                   threading.Thread(target=send_prompt)]
        try:
            for thread in threads:
                thread.start()
            process.wait(timeout=timeout)
        finally:
            # Also close pipes inherited by tool subprocesses after the CLI exits.
            # Timeout/interrupt retains everything already written by the readers.
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            for thread in threads:
                if thread.ident is not None:
                    thread.join()
        if errors:
            raise errors[0]
        if process.returncode == 2:
            raise AttemptError('configuration', 'invoke', 'cli_usage', False, 'CLI usage/configuration exit status 2')
        if process.returncode:
            raise AttemptError('transport', 'invoke', 'cli_exit', True, f'CLI exit status {process.returncode}')
    try:
        return events_at(capture)
    except (ReviewError, ValueError, UnicodeError) as exc:
        raise AttemptError('session', 'event_stream', 'invalid_event_stream', True, str(exc)) from exc


def credential_paths():
    """Host authentication is available to the CLI, never its model tools."""
    home = Path.home()
    paths = [home / '.codex', home / '.claude', home / '.claude.json',
             home / 'Library/Keychains', Path('/proc')]
    paths += [Path(os.environ[name]).expanduser().resolve()
              for name in ('CODEX_HOME', 'CLAUDE_CONFIG_DIR') if os.environ.get(name)]
    return sorted(set(map(str, paths)))


def command(binary, condition, work):
    validate_condition(condition)
    model, effort = condition['model'], condition['effort']
    return [binary, 'exec', '--model', model, '--ephemeral', '--json', '--ignore-user-config',
            '--ignore-rules', '--skip-git-repo-check', '--strict-config',
            '-c', 'default_permissions="review"',
            '-c', 'permissions.review.filesystem={' + ','.join(
                json.dumps(path) + '=' + json.dumps(mode)
                for path, mode in [('/', 'read'), *[(p, 'deny') for p in credential_paths()]]) + '}',
            '-c', 'permissions.review.network.enabled=false',
            '-c', 'shell_environment_policy.inherit="core"',
            '--color', 'never', '--cd', str(work), '-c', f'model_reasoning_effort="{effort}"',
            '-c', 'approval_policy="never"', '-c', 'web_search="disabled"',
            '-c', 'features.multi_agent=false', '-c', 'features.apps=false',
            '-c', 'project_doc_max_bytes=0', '-c', 'check_for_update_on_startup=false',
            '--output-schema', str(EXPERIMENT / 'schemas/discovery.json'),
            '--output-last-message', str(work / 'answer.json'), '-']


def parse_discovery(events, answer, condition):
    metadata = parse_codex(events, answer)
    for event in events:
        if event.get('type') in ('thread.started', 'turn.started'):
            for field in ('model', 'effort'):
                if field in event:
                    require(event[field] == condition[field], f'discovery {field} mismatch')
                    metadata['observed_' + field] = event[field]
    return metadata


def tree_files(root):
    return {str(p.relative_to(root)): p for p in root.rglob('*') if p.is_file() or p.is_symlink()}


def same_tree(left, right):
    a, b = tree_files(left), tree_files(right)
    return a.keys() == b.keys() and all(
        (a[n].is_symlink() == b[n].is_symlink()) and
        (os.readlink(a[n]) == os.readlink(b[n]) if a[n].is_symlink() else a[n].read_bytes() == b[n].read_bytes())
        for n in a)


def cell_id(task, condition, pass_no):
    return '--'.join((task['mode'], task['unit_id'], condition['model'], condition['effort'], f'pass-{pass_no}'))


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def publish(directory, task, condition, pass_no, answer, metadata, source, origin):
    directory.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(prefix='publish-', dir=directory.parent) as temporary:
        staged = Path(temporary) / 'complete'
        staged.mkdir()
        for name in ('trace.jsonl', 'stderr.log', 'prompt.txt'):
            shutil.copyfile(source / name, staged / name)
        atomic_json(staged / 'result.json', answer)
        atomic_json(staged / 'metadata.json', metadata)
        atomic_json(staged / 'completion.json', {'task': task, 'condition': condition, 'pass': pass_no,
                                                'origin': origin, 'session_id': metadata['session_id']})
        staged.rename(directory)


def read_success(directory, task, condition, pass_no, repo, prompt):
    completion = read_json(directory / 'completion.json')
    require(completion['task'] == task and completion['condition'] == condition
            and completion['pass'] == pass_no, 'completed cell identity changed')
    require((directory / 'prompt.txt').read_text() == prompt, 'completed prompt changed')
    require((directory / 'stderr.log').is_file(), 'completed stderr missing')
    origin = completion['origin']
    keys(origin, ['kind', 'attempt'], 'completion origin')
    require(origin['kind'] == 'native', 'completion must come from a queue attempt')
    attempt = within(directory.parents[2], origin['attempt'])
    require(attempt.parent == directory.parents[2] / 'attempts' / 'discovery' / directory.name
            and (attempt / 'attempt.json').is_file(), 'completion attempt missing or outside its cell')
    answer = read_json(directory / 'result.json')
    events = events_at(directory)
    native = parse_discovery(events, answer, condition)
    metadata = read_json(directory / 'metadata.json')
    require(completion['session_id'] == native['session_id']
            and all(metadata.get(k) == v for k, v in native.items()), 'completion metadata mismatch')
    validate_answer(answer, repo)
    return answer


def run(source, reference, prepared, out, config, *, execute=False, codex='codex',
        units=None, max_calls=None):
    """Plan missing cells offline; execute only when explicitly requested."""
    validate_config(config)
    prepared_inputs = PreparedInputs(source, reference, prepared)
    available = {f"{arm}:{unit['unit_id']}" for arm in ARMS for unit in prepared_inputs.units[arm]}
    require(units is None or (isinstance(units, list) and units
            and all(isinstance(unit, str) and unit in available for unit in units)
            and len(set(units)) == len(units)), 'selected units must be distinct exact frozen arm:unit IDs')
    require(max_calls is None or (type(max_calls) is int and max_calls > 0), 'max-calls must be a positive integer')
    out = Path(out).resolve()
    require(all(not out.is_relative_to(root) and not root.is_relative_to(out)
                for root in (prepared_inputs.source, prepared_inputs.reference, prepared_inputs.prepared)),
            'output must be disjoint from input roots')
    require(not (EXPERIMENT / 'schemas/discovery.json').is_relative_to(out.parent),
            'output parent would hide the CLI schema; use a separate results directory')
    out.mkdir(parents=True, exist_ok=True)
    with (out / 'runner.lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReviewError('another runner owns this output') from exc
        return run_locked(prepared_inputs, out, config, execute, codex, lock.fileno(), units, max_calls)


def run_locked(prepared_inputs, out, config, execute, codex, output_fd, units, max_calls):
    binding_path = out / 'experiment.json'
    inputs = out / 'inputs'
    sources = {'source': prepared_inputs.source, 'reference': prepared_inputs.reference, 'prepared': prepared_inputs.prepared}
    binding = {'format': 'fresh-input-queue-v1', 'experiment_version': config['experiment_version'], 'source_commit': config['source_commit'],
               'input_roots': {k: str(v) for k, v in sources.items()}, 'units': prepared_inputs.units, 'gates': prepared_inputs.gates}
    if binding_path.exists():
        saved = read_json(binding_path)
        templates = saved.pop('templates')
        require(saved == binding and all(templates.get(k) == v for k, v in prepared_inputs.contracts.items()),
                'inputs/templates changed; explicit NEW experiment version required')
        validate_config(config, read_json(out / 'config.json'))
        require(all(same_tree(source, inputs / name) for name, source in sources.items()),
                'source/reference/card/contract changed; explicit NEW experiment version required')
    else:
        require(set(p.name for p in out.iterdir()) == {'runner.lock'}, 'unrecognized output; choose a fresh directory')
        for name, source in sources.items():
            shutil.copytree(source, inputs / name, symlinks=True)
        atomic_json(binding_path, {**binding, 'templates': prepared_inputs.contracts})
    atomic_json(out / 'config.json', config)
    for name in ('attempts', 'results/discovery', 'work'):
        (out / name).mkdir(parents=True, exist_ok=True)
    repo, reference, prepared = (inputs / name for name in ('source', 'reference', 'prepared'))
    hidden = [out.parent, *sources.values()]
    # Collapse nested masks; workers read only the frozen copies.
    hidden = [p for p in hidden if not any(p != q and p.is_relative_to(q) for q in hidden)]
    states = {'discovery': {}}
    selected = set(units) if units else {
        f"{arm}:{unit['unit_id']}" for arm in ARMS for unit in prepared_inputs.units[arm]
    }
    calls_started = 0
    candidates = []
    screened = []
    pending = deque()

    def status():
        cells = states['discovery']
        value = {'discovery': {state: list(cells.values()).count(state)
                               for state in ('queued', 'running', 'succeeded', 'failed')}}
        value.update(passes=config['passes'], cells=states, calls_started=calls_started,
                     counts={'returned': len(candidates) + len(screened),
                             'admitted': len(candidates), 'screened': len(screened)})
        atomic_json(out / 'status.json', value)
        atomic_json(out / 'candidates.json', candidates)
        atomic_json(out / 'screened.json', screened)
        return value

    def accept(job, answer):
        key, task, condition, prompt, readable, allowed, pass_no = job
        states['discovery'][key] = 'succeeded'
        for index, finding in enumerate(answer['candidates']):
            failed = gate_failures(finding, prepared_inputs.gates, defect_kinds())
            if failed:
                screened.append({'cell': key, 'index': index, 'finding': finding, 'failed_gates': failed})
                continue
            # Literal origin IDs are independent of completion order and config expansion.
            candidate_id = f'c--{key}--{index}'
            candidate = {'id': candidate_id, 'pass': pass_no,
                         'origin': {'cell': key, 'index': index},
                         'arm': task['mode'], 'unit': task['unit_id'],
                         'condition': condition, 'finding': finding}
            candidates.append(candidate)

    def enqueue(job):
        key, task, condition, prompt, readable, allowed, pass_no = job
        destination = out / 'results' / 'discovery' / key
        states['discovery'][key] = 'queued'
        if destination.exists():
            accept(job, read_success(destination, task, condition, pass_no, repo, prompt))
        elif execute and allowed:
            pending.append(job)

    def execute_cell(job):
        key, task, condition, prompt, readable, allowed, pass_no = job
        attempts = out / 'attempts' / 'discovery' / key
        attempts.mkdir(parents=True, exist_ok=True)
        capture = Path(tempfile.mkdtemp(prefix='attempt-', dir=attempts))
        work = Path(tempfile.mkdtemp(prefix='session-', dir=out / 'work'))
        started = time.monotonic()
        receipt = {'state': 'running', 'pass': pass_no, 'started_at': timestamp(), 'ended_at': None,
                   'duration_seconds': None, 'condition': condition, 'task': task,
                   'usage': None}
        atomic_json(capture / 'attempt.json', receipt)
        (capture / 'prompt.txt').write_text(prompt)
        try:
            binary = shutil.which(codex)
            require(binary is not None, 'CLI executable missing')
            require(not any(Path(binary).resolve().is_relative_to(p) for p in hidden),
                    'CLI is inside hidden inputs/results')
            prefix = verified_worker_prefix(out / 'attempts', hidden=hidden, exposed=readable,
                                            writable=[work], probe_dir=out / 'work')
            argv = command(binary, condition, work)
            receipt['argv'] = argv
            receipt['invoked_at'] = timestamp()
            atomic_json(capture / 'attempt.json', receipt)
            events = invoke([*prefix, *argv], prompt, work, config['timeout_seconds'], output_fd, capture)
            receipt['returncode'] = 0
            answer = read_json(work / 'answer.json')
            metadata = parse_discovery(events, answer, condition)
            receipt.update(metadata)
            validate_answer(answer, repo)
            receipt.update(state='succeeded', ended_at=timestamp(), duration_seconds=time.monotonic() - started)
            metadata['pass'] = pass_no
            metadata.update(condition=condition, started_at=receipt['started_at'],
                            ended_at=receipt['ended_at'], duration_seconds=receipt['duration_seconds'])
            atomic_json(capture / 'result.json', answer)
            atomic_json(capture / 'metadata.json', metadata)
            atomic_json(capture / 'attempt.json', receipt)
            publish(out / 'results' / 'discovery' / key, task, condition, pass_no, answer, metadata, capture,
                    {'kind': 'native', 'attempt': str(capture.relative_to(out))})
            return answer
        except (ReviewError, OSError, ValueError, KeyError, TypeError, subprocess.TimeoutExpired) as exc:
            receipt.update(state='failed', error=str(exc), exception_type=type(exc).__name__,
                           ended_at=timestamp(), duration_seconds=time.monotonic() - started)
            if (capture / 'trace.jsonl').exists():
                try:
                    receipt['native_accounting'] = [
                        {k: e[k] for k in ('type', 'usage') if k in e}
                        for e in events_at(capture) if 'usage' in e]
                except (ReviewError, ValueError, OSError):
                    pass
            return None
        finally:
            atomic_json(capture / 'attempt.json', receipt)
            shutil.rmtree(work)

    for pass_no in range(1, config['passes'] + 1):
        for arm in ARMS:
            for unit in prepared_inputs.units[arm]:
                for condition in config['discovery']:
                    task = {**unit, 'model': condition['model'],
                            'task_id': f"{arm}--{condition['model']}--{unit['unit_id']}"}
                    if arm == 'targeted':
                        group = prepared / arm / 'groups' / unit['unit_id']
                        task.update(source_card=str(group / 'source-card.md'), scope=str(group / 'scope.json'))
                    enqueue((cell_id(task, condition, pass_no), task, condition,
                             discovery_prompt(task, repo, reference), [repo, reference, prepared],
                             f"{arm}:{unit['unit_id']}" in selected, pass_no))
    status()
    with ThreadPoolExecutor(max_workers=config['concurrency']) as pool:
        running = {}
        while pending or running:
            while pending and len(running) < config['concurrency'] and (
                    max_calls is None or calls_started < max_calls):
                job = pending.popleft()
                states['discovery'][job[0]] = 'running'
                calls_started += 1
                running[pool.submit(execute_cell, job)] = job
            status()
            if not running:
                break
            finished, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in finished:
                job = running.pop(future)
                answer = future.result()
                if answer is None:
                    states['discovery'][job[0]] = 'failed'
                else:
                    accept(job, answer)
            status()
    return status()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    for name in ('source', 'reference', 'prepared'):
        parser.add_argument('--' + name, required=True, type=Path)
    parser.add_argument('--out', required=True, type=Path)
    parser.add_argument('--config', type=Path, default=EXPERIMENT / 'incremental-config.json')
    parser.add_argument('--execute', action='store_true', help='invoke the provider for missing cells (default: offline plan)')
    parser.add_argument('--unit', action='append', dest='units', help='exact frozen ARM:UNIT_ID (repeatable)')
    parser.add_argument('--max-calls', type=int, help='maximum cells attempted this invocation')
    args = parser.parse_args()
    try:
        report = run(args.source, args.reference, args.prepared, args.out, read_json(args.config), execute=args.execute, units=args.units,
                     max_calls=args.max_calls)
        print(json.dumps({k: v for k, v in report.items() if k != 'cells'}, indent=2))
        return int(report['discovery']['failed'] > 0)
    except (ReviewError, OSError, ValueError) as exc:
        print(f'FAIL incremental: {exc}', file=__import__('sys').stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
