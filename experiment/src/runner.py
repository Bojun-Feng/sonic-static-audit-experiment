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

from audit_queue import EXPERIMENT, build_tasks, runner_config
from common import (AuditError, exact_children, json_bytes, keys, read_json, require, sha256,
                    source_and_output, source_lines, within)
from model_cli import (discovery_command, discovery_prompt, parse_codex, parse_claude,
                       verifier_command, verifier_prompt)
from scheduler import SlotPool, global_root, open_lock
from validate import load_selection


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    require(not path.is_symlink() and not temporary.is_symlink(), 'unsafe output file')
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


def invoke(command, prompt, work, slot, timeout, output_fd):
    with (work / 'trace.jsonl').open('wb') as stdout, (work / 'stderr.log').open('wb') as stderr:
        process = subprocess.Popen(command, cwd=work, stdin=subprocess.PIPE, stdout=stdout,
                                   stderr=stderr, start_new_session=True, pass_fds=(slot.fd, output_fd))
        try:
            process.communicate(prompt.encode(), timeout=timeout)
        except BaseException:
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                pass
            process.wait()
            raise
        require(process.returncode == 0, f'CLI exit status {process.returncode}')
    try:
        events = [json.loads(line) for line in (work / 'trace.jsonl').read_text().splitlines()]
    except (ValueError, UnicodeError) as exc:
        raise AuditError('invalid CLI event stream') from exc
    require(events and all(isinstance(e, dict) for e in events), 'empty or invalid CLI events')
    return events


def run(prepared, mode, out, reference, queue=None, lock_root=None):
    prepared, reference = Path(prepared), Path(reference)
    tasks = build_tasks(prepared, mode, queue)
    manifest, _, _ = load_selection(prepared)
    repo, out = source_and_output(Path(manifest['source_root']), Path(out))
    require(reference.is_dir(), 'reference directory is missing')
    reference = reference.resolve()
    for protected in (prepared.resolve(), reference):
        require(not out.is_relative_to(protected) and not protected.is_relative_to(out),
                'run output must be disjoint from prepared inputs and references')
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
            raise AuditError('another runner owns this output') from exc
        return run_locked(tasks, binding, repo, out, reference, config, lock_root or global_root(), fd)
    finally:
        os.close(fd)


def run_locked(tasks, binding, repo, out, reference, config, lock_root, output_fd):
    binding_path = out / 'run.json'
    if binding_path.exists():
        require(read_json(binding_path) == binding, 'run inputs changed; use a new output directory')
    else:
        require(set(p.name for p in out.iterdir()) == {'runner.lock'}, 'unrecognized run output')
        atomic_json(binding_path, binding)
    for name in ('work', 'results', 'results/discovery', 'results/candidates', 'results/verifier'):
        within(out, name).mkdir(exist_ok=True)
    # The output lock excludes another runner. Child processes inherit it too.
    for child in (out / 'work').iterdir():
        require(not child.is_symlink() and child.is_dir(), 'unsafe work directory')
        shutil.rmtree(child)
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
            require(not destination.is_symlink(), 'unsafe completed result')
            completion = read_json(destination / 'completion.json')
            keys(completion, ['task', 'artifacts'], 'completion')
            keys(completion['artifacts'], ['trace.jsonl', 'stderr.log', 'prompt.txt',
                                           'result.json', 'metadata.json'], 'completion artifacts')
            exact_children(destination, [*completion['artifacts'], 'completion.json'])
            require(completion['task'] == task, 'completed task identity changed')
            for name, digest in completion['artifacts'].items():
                require(sha256(within(destination, name).read_bytes()) == digest, 'completed artifact changed')
            answer = read_json(destination / 'result.json')
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
        error = 'CLI executable is missing'
        for attempt in range(1, config['max_attempts'] + 1):
            work = Path(tempfile.mkdtemp(prefix=task_id + '-', dir=out / 'work'))
            try:
                require(binary is not None, error)
                executable = Path(binary).resolve()
                require(not any(executable.is_relative_to(p) for p in (repo, reference)), 'CLI is inside source/reference input')
                with SlotPool(Path(lock_root), lane).acquire() as slot:
                    with mutex:
                        states[lane][task_id] = 'running'
                        status()
                    command = (discovery_command(str(executable), task['model'], work) if lane == 'discovery'
                               else verifier_command(str(executable), work, [repo, reference, out / 'results']))
                    (work / 'prompt.txt').write_text(prompt)
                    started = time.monotonic()
                    # Preserve the output lock until all child work has exited.
                    events = invoke(command, prompt, work, slot, config['timeout_seconds'], output_fd)
                    if lane == 'discovery':
                        answer = read_json(work / 'answer.json')
                        metadata = parse_codex(events, answer)
                        (work / 'answer.json').unlink()
                    else:
                        answer, metadata = parse_claude(events)
                    validate_answer(answer, repo, lane, prior)
                    metadata.update({'model': task['model'], 'task_id': task_id, 'attempt': attempt,
                                     'slot': slot.index, 'wait_seconds': slot.wait_seconds,
                                     'elapsed_seconds': time.monotonic() - started})
                    atomic_json(work / 'result.json', answer)
                    atomic_json(work / 'metadata.json', metadata)
                    atomic_json(work / 'completion.json', {'task': task, 'artifacts': {
                        p.name: sha256(p.read_bytes()) for p in work.iterdir()}})
                    work.rename(destination)
                with mutex:
                    states[lane][task_id] = 'succeeded'
                    status()
                return answer
            except (AuditError, OSError, UnicodeError, ValueError, subprocess.TimeoutExpired) as exc:
                # Do not persist arbitrary CLI output or environment in failure metadata.
                error = type(exc).__name__
            finally:
                if work.exists():
                    shutil.rmtree(work)
        with mutex:
            states[lane][task_id] = 'failed'
            notice = {'lane': lane, 'task_id': task_id, 'attempts': config['max_attempts'], 'error': error}
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
    args = parser.parse_args()
    try:
        return run(args.prepared, args.mode, args.out, args.reference, args.queue)
    except (AuditError, OSError, ValueError) as exc:
        print(f'FAIL runner: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
