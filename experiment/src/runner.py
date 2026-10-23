#!/usr/bin/env python3
"""Run validated static review queues using fresh standalone model sessions."""

import sys
from pathlib import Path

# select.py is a repository entry point, not the stdlib select extension.
_import_path = sys.path[:]
sys.path[:] = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
try:
    import subprocess
finally:
    sys.path[:] = _import_path

import argparse
from concurrent.futures import ThreadPoolExecutor
import fcntl
import json
import os
import shutil
import signal
import tempfile
import threading
import time
import traceback

from review_queue import EXPERIMENT, build_tasks, runner_config
from common import (AttemptError, ReviewError, exact_children, json_bytes, keys, read_json, require, sha256,
                    source_and_output, source_lines, within)
from diagnostics import default_root, private_root, verified_worker_prefix
from model_cli import (discovery_command, discovery_prompt, parse_codex, parse_claude,
                       verifier_command, verifier_prompt)
from scheduler import SlotPool, bind_diagnostics_root, global_root, open_lock
from validate import load_selection


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    require(not path.is_symlink() and not temporary.is_symlink(), 'output file must not be a symlink')
    temporary.write_bytes(json_bytes(value))
    temporary.replace(path)


def validate_answer(answer, repo, lane, prior):
    if lane == 'discovery':
        keys(answer, ['candidates'], 'discovery result')
        require(isinstance(answer['candidates'], list), 'candidates must be an array')
        for finding in answer['candidates']:
            keys(finding, ['title', 'path', 'start_line', 'end_line', 'root_cause', 'evidence', 'impact'], 'finding')
            require(all(isinstance(finding[k], str) and finding[k].strip()
                        for k in ('title', 'path', 'root_cause', 'evidence', 'impact')), 'empty finding field')
            source = within(repo, finding['path'])
            require(source.is_file(), 'finding source is missing')
            require(type(finding['start_line']) is int and type(finding['end_line']) is int
                    and 1 <= finding['start_line'] <= finding['end_line'] <= len(source_lines(source.read_bytes())),
                    'invalid finding line range')
    else:
        keys(answer, ['status', 'rationale', 'duplicate_of'], 'verifier result')
        require(answer['status'] in ('confirmed', 'rejected', 'duplicate', 'needs-validation')
                and isinstance(answer['rationale'], str) and answer['rationale'].strip(), 'invalid verdict')
        require((answer['status'] == 'duplicate' and answer['duplicate_of'] in [p['id'] for p in prior])
                or (answer['status'] != 'duplicate' and answer['duplicate_of'] is None), 'invalid duplicate reference')


def invoke(command, prompt, work, slot, timeout, output_fd, capture):
    with (capture / 'trace.jsonl').open('xb') as stdout, (capture / 'stderr.log').open('xb') as stderr:
        try:
            process = subprocess.Popen(command, cwd=work, stdin=subprocess.PIPE, stdout=stdout,
                                       stderr=stderr, start_new_session=True, pass_fds=(slot.fd, output_fd))
        except OSError as exc:
            raise AttemptError('configuration', 'invoke', 'cli_unavailable', False, str(exc)) from exc
        try:
            process.communicate(prompt.encode(), timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        if process.returncode == 2:
            raise AttemptError('configuration', 'invoke', 'cli_usage', False, 'CLI usage/configuration exit status 2')
        if process.returncode:
            raise AttemptError('transport', 'invoke', 'cli_exit', True, f'CLI exit status {process.returncode}')
    try:
        events = [json.loads(line) for line in (capture / 'trace.jsonl').read_text().splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise AttemptError('session', 'event_stream', 'invalid_event_stream', True, str(exc)) from exc
    if not events or not all(isinstance(e, dict) for e in events):
        raise AttemptError('session', 'event_stream', 'invalid_event_stream', True, 'empty or invalid CLI events')
    return events


def run(prepared, mode, out, reference, queue=None, lock_root=None, diagnostics_root=None):
    prepared, reference = Path(prepared), Path(reference)
    tasks = build_tasks(prepared, mode, queue)
    manifest, _, _ = load_selection(prepared)
    repo, out = source_and_output(Path(manifest['source_root']), Path(out))
    require(reference.is_dir(), 'reference directory is missing')
    reference = reference.resolve()
    for protected in (prepared.resolve(), reference):
        require(not out.is_relative_to(protected) and not protected.is_relative_to(out),
                'run output must be disjoint from prepared inputs and references')
    # Resolve/prove the storage boundary before writing any run output.
    scratch = Path(tempfile.gettempdir()).resolve()
    for protected in (EXPERIMENT.parent, repo, out, prepared.resolve(), reference):
        require(not scratch.is_relative_to(protected), 'worker scratch must be outside repository, results, and inputs')
    diagnostics = private_root(diagnostics_root or default_root(), (repo, out, prepared, reference))
    require(not scratch.is_relative_to(diagnostics), 'worker scratch must be outside diagnostics')
    lock_root = Path(lock_root or global_root())
    bind_diagnostics_root(lock_root, diagnostics)
    config = runner_config()
    binding = {'schema_version': 1, 'tasks': tasks, 'manifest_sha256': sha256((prepared / 'manifest.json').read_bytes()),
               'reference': str(reference), 'config': config,
               'contracts': {str(p.relative_to(EXPERIMENT)): sha256(p.read_bytes())
                             for folder in ('prompts', 'schemas') for p in sorted((EXPERIMENT / folder).iterdir())}}
    out.mkdir(parents=True, exist_ok=True)
    fd = open_lock(out / 'runner.lock')
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReviewError('another runner owns this output') from exc
        return run_locked(tasks, binding, repo, out, reference, config, lock_root, fd, diagnostics, prepared)
    finally:
        os.close(fd)


def run_locked(tasks, binding, repo, out, reference, config, lock_root, output_fd, diagnostics, prepared):
    binding_path = out / 'run.json'
    if binding_path.exists():
        require(read_json(binding_path) == binding, 'run inputs changed; use a new output directory')
    else:
        require(set(p.name for p in out.iterdir()) == {'runner.lock'}, 'unrecognized run output')
        atomic_json(binding_path, binding)
    for name in ('work', 'results', 'results/discovery', 'results/candidates', 'results/verifier'):
        within(out, name).mkdir(exist_ok=True)
    # Existing interrupted output is never silently discarded or exported.
    require(not any((out / 'work').iterdir()), 'unexpected legacy work; use a fresh output directory')
    private_run = within(diagnostics, sha256(str(out).encode())[:24])
    private_run.mkdir(mode=0o700, exist_ok=True)
    isolation_error = None
    try:
        worker_prefix = verified_worker_prefix(diagnostics, (repo, reference, out, prepared))
    except AttemptError as exc:
        worker_prefix, isolation_error = [], exc
    mutex = threading.Lock()
    states = {'discovery': {t['task_id']: 'queued' for t in tasks}, 'verifier': {}}
    failures = {}
    notices_path = within(out, 'notifications.jsonl')
    if notices_path.exists():
        for line in notices_path.read_text().splitlines():
            notice = json.loads(line)
            failures[(notice['lane'], notice['task_id'])] = notice

    def status(final=False):
        counts = {lane: {state: list(values.values()).count(state)
                         for state in ('queued', 'running', 'succeeded', 'failed')}
                  for lane, values in states.items()}
        counts['state'] = ('failed' if any('failed' in v.values() for v in states.values()) else 'complete') if final else 'running'
        atomic_json(out / 'status.json', counts)

    def execute(lane, task, prompt, prior):
        task_id = task['task_id']
        destination = out / 'results' / lane / task_id
        if destination.exists():
            require(not destination.is_symlink(), 'completed result must not be a symlink')
            completion = read_json(destination / 'completion.json')
            keys(completion, ['task', 'artifacts'], 'completion')
            keys(completion['artifacts'], ['trace.jsonl', 'stderr.log', 'prompt.txt',
                                           'result.json', 'metadata.json'], 'completion artifacts')
            exact_children(destination, [*completion['artifacts'], 'completion.json'])
            require(completion['task'] == task, 'completed task identity changed')
            for name, digest in completion['artifacts'].items():
                require(sha256(within(destination, name).read_bytes()) == digest, 'completed artifact changed')
            answer = read_json(destination / 'result.json')
            if lane == 'discovery':
                events = [json.loads(line) for line in (destination / 'trace.jsonl').read_text().splitlines()]
                parse_codex(events, answer, task, repo)
            validate_answer(answer, repo, lane, prior)
            with mutex:
                states[lane][task_id] = 'succeeded'
                status()
            return answer
        if (lane, task_id) in failures:
            with mutex:
                states[lane][task_id] = 'failed'
                status()
            return None
        binary = shutil.which('codex' if lane == 'discovery' else 'claude')
        for attempt in range(1, config['max_attempts'] + 1):
            capture = Path(tempfile.mkdtemp(prefix=task_id + f'-attempt-{attempt}-', dir=private_run))
            work = None
            stage = 'configuration'
            def at_stage(value):
                nonlocal stage
                stage = value
                atomic_json(capture / 'attempt.json', {'task': task, 'lane': lane, 'attempt': attempt,
                                                       'stage': stage, 'run': str(out)})
            try:
                at_stage('configuration')
                (capture / 'prompt.txt').write_text(prompt)
                if binary is None:
                    raise AttemptError('configuration', stage, 'missing_cli', False, 'CLI executable is missing')
                if isolation_error:
                    raise isolation_error
                executable = Path(binary).resolve()
                require(not any(executable.is_relative_to(p.resolve()) for p in (repo, reference, prepared, out)),
                        'CLI is inside source/reference/prepared/output directories')
                work = Path(tempfile.mkdtemp(prefix='review-session-'))
                at_stage('scheduler')
                with SlotPool(Path(lock_root), lane).acquire() as slot:
                    with mutex:
                        states[lane][task_id] = 'running'
                        status()
                    command = (discovery_command(str(executable), task['model'], work) if lane == 'discovery'
                               else verifier_command(str(executable), work, [repo, reference, out / 'results']))
                    at_stage('invoke')
                    started = time.monotonic()
                    # Preserve the output lock until all child work has exited.
                    events = invoke([*worker_prefix, *command], prompt, work, slot, config['timeout_seconds'], output_fd, capture)
                    at_stage('answer')
                    if lane == 'discovery':
                        answer = read_json(work / 'answer.json')
                        at_stage('session')
                        metadata = parse_codex(events, answer, task, repo)
                    else:
                        at_stage('session')
                        answer, metadata = parse_claude(events)
                    at_stage('result_validation')
                    validate_answer(answer, repo, lane, prior)
                    metadata.update({'model': task['model'], 'task_id': task_id, 'attempt': attempt,
                                     'slot': slot.index, 'wait_seconds': slot.wait_seconds,
                                     'elapsed_seconds': time.monotonic() - started})
                    at_stage('publish')
                    atomic_json(capture / 'result.json', answer)
                    atomic_json(capture / 'metadata.json', metadata)
                    # Allowlist success artifacts; never move the attempt directory
                    # itself or arbitrary model-created files into results.
                    with tempfile.TemporaryDirectory(prefix='success-', dir=out / 'work') as staging:
                        staged = Path(staging) / 'result'
                        staged.mkdir()
                        artifacts = ('trace.jsonl', 'stderr.log', 'prompt.txt', 'result.json', 'metadata.json')
                        for name in artifacts:
                            shutil.copyfile(capture / name, staged / name)
                        atomic_json(staged / 'completion.json', {'task': task, 'artifacts': {
                            name: sha256((staged / name).read_bytes()) for name in artifacts}})
                        staged.rename(destination)
                with mutex:
                    states[lane][task_id] = 'succeeded'
                    status()
                shutil.rmtree(capture)
                return answer
            except (ReviewError, OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
                if isinstance(exc, AttemptError):
                    error = exc
                elif isinstance(exc, subprocess.TimeoutExpired):
                    error = AttemptError('transport', 'invoke', 'timeout', True, str(exc))
                elif stage in ('answer', 'session', 'result_validation') and not isinstance(exc, PermissionError):
                    reason = {'answer': 'invalid_answer', 'session': 'incomplete_session',
                              'result_validation': 'invalid_result'}[stage]
                    error = AttemptError('session', stage, reason, True, str(exc))
                else:
                    error = AttemptError('configuration' if stage == 'configuration' else 'storage',
                                         stage, 'invalid_configuration' if stage == 'configuration' else 'local_io',
                                         False, str(exc))
                atomic_json(capture / 'failure.json', {**error.summary(), 'task': task, 'lane': lane,
                            'attempt': attempt, 'detail': str(exc), 'exception_type': type(exc).__name__,
                            'traceback': traceback.format_exc()})
                if not error.retryable:
                    break
            finally:
                if work is not None and work.exists():
                    if capture.exists():
                        # Retain even unexpected model output or interrupted
                        # attempts privately, without following any symlinks.
                        shutil.move(str(work), capture / 'workspace')
                    else:
                        shutil.rmtree(work)
        with mutex:
            states[lane][task_id] = 'failed'
            notice = {'lane': lane, 'task_id': task_id, 'attempts': attempt, **error.summary()}
            with notices_path.open('a') as handle:
                handle.write(json.dumps(notice, sort_keys=True) + '\n')
            failures[(lane, task_id)] = notice
            status()
        return None

    status()
    with ThreadPoolExecutor(max_workers=config['discovery_slots']) as executor:
        answers = list(executor.map(lambda task: execute('discovery', task, discovery_prompt(task, repo, reference), []), tasks))
    candidates = {}
    for task, answer in zip(tasks, answers):
        for finding in answer['candidates'] if answer else []:
            identity = {k: finding[k] for k in ('path', 'start_line', 'end_line', 'root_cause')}
            identity['root_cause'] = ' '.join(identity['root_cause'].casefold().split())
            candidate_id = 'c-' + sha256(json_bytes(identity))[:24]
            candidate = candidates.setdefault(candidate_id, {'id': candidate_id, 'finding': finding, 'occurrences': []})
            result_dir = out / 'results/discovery' / task['task_id']
            occurrence = {'task_id': task['task_id'], 'model': task['model'], 'finding': finding,
                          'trace': str(result_dir / 'trace.jsonl'), 'result': str(result_dir / 'result.json')}
            if occurrence not in candidate['occurrences']:
                candidate['occurrences'].append(occurrence)
    prior = []
    states['verifier'] = {key: 'queued' for key in candidates}
    for candidate_id, candidate in candidates.items():
        directory = within(out, 'results/candidates/' + candidate_id)
        directory.mkdir(exist_ok=True)
        atomic_json(directory / 'candidate.json', candidate)
        finding = candidate['finding']
        report = f"# {finding['title']}\n\n{finding['path']}:{finding['start_line']}-{finding['end_line']}\n\n" + '\n\n'.join(finding[k] for k in ('root_cause', 'evidence', 'impact')) + '\n'
        within(directory, 'report.md').write_text(report)
        task = {'task_id': candidate_id, 'model': config['verifier_model'], 'candidate_sha256': sha256(json_bytes(candidate))}
        answer = execute('verifier', task, verifier_prompt(directory / 'candidate.json', repo, reference, prior), prior)
        if answer:
            prior.append({'id': candidate_id, 'finding': finding, 'verdict': answer})
    status(final=True)
    return int(any('failed' in values.values() for values in states.values()))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--prepared', type=Path, required=True)
    parser.add_argument('--mode', choices=('exhaustive', 'targeted'), required=True)
    parser.add_argument('--out', type=Path, required=True)
    parser.add_argument('--reference', type=Path, required=True)
    parser.add_argument('--queue', type=Path)
    parser.add_argument('--diagnostics-root', type=Path, default=default_root())
    args = parser.parse_args()
    try:
        return run(args.prepared, args.mode, args.out, args.reference, args.queue,
                   diagnostics_root=args.diagnostics_root)
    except (ReviewError, OSError, ValueError) as exc:
        print(f'FAIL runner: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
