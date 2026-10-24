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
import functools
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

# Hard gates, in reporting order. Discovery self-assesses them, the runner screens
# deterministically, and the blinded verifier re-scores them before `confirmed`.
GATES = ('wg_fit', 'defect_kind', 'intention_clear', 'impact_reachable', 'files_touched',
         'fix_logic_lines', 'fix_unique', 'defect_confidence', 'remedy_confidence')
GATE_FIELDS = ('defect_kind', 'wg_fit', 'intended_behavior', 'intention_clear', 'impact_reachable', 'fix_summary',
               'fix_logic_lines', 'files_touched', 'fix_unique', 'defect_confidence', 'remedy_confidence')
FINDING_FIELDS = ('title', 'path', 'start_line', 'end_line', 'root_cause', 'evidence', 'impact', *GATE_FIELDS)
VERDICT_FIELDS = ('status', 'rationale', 'failed_gates', 'wg_fit', 'kind_fit', 'intention_clear', 'impact_reachable',
                  'fix_patch', 'fix_logic_lines', 'files_touched', 'fix_unique', 'defect_confidence', 'remedy_confidence')
# The only candidate material a verifier session may read.
VALIDATION_FIELDS = ('id', 'path', 'start_line', 'end_line', 'defect_kind', 'claims')


def atomic_json(path, value):
    temporary = path.with_name(path.name + '.tmp')
    require(not path.is_symlink() and not temporary.is_symlink(), 'output file must not be a symlink')
    temporary.write_bytes(json_bytes(value))
    temporary.replace(path)


@functools.lru_cache(maxsize=1)
def defect_kinds():
    """The closed defect family, read from the discovery schema so there is one source of truth."""
    schema = read_json(EXPERIMENT / 'schemas/discovery.json')
    kinds = schema['properties']['candidates']['items']['properties']['defect_kind']['enum']
    require(isinstance(kinds, list) and kinds and all(isinstance(k, str) for k in kinds), 'invalid defect kinds')
    return kinds


def gate_failures(assessment, gates, kinds=None):
    """Names of the hard gates an assessment fails; empty means it passes them all.

    A discovery finding carries `defect_kind` (checked against `kinds`); a verifier
    verdict carries `kind_fit` instead.
    """
    kind = assessment['defect_kind'] in kinds if kinds is not None else assessment['kind_fit']
    holds = {'wg_fit': assessment['wg_fit'] is True,
             'defect_kind': kind is True,
             'intention_clear': assessment['intention_clear'] is True,
             'impact_reachable': assessment['impact_reachable'] is True,
             'files_touched': 1 <= assessment['files_touched'] <= gates['max_files_touched'],
             'fix_logic_lines': 1 <= assessment['fix_logic_lines'] <= gates['max_fix_logic_lines'],
             'fix_unique': assessment['fix_unique'] is True,
             'defect_confidence': assessment['defect_confidence'] >= gates['min_confidence'],
             'remedy_confidence': assessment['remedy_confidence'] >= gates['min_confidence']}
    return [name for name in GATES if not holds[name]]


def patch_files(patch):
    """Files named by a unified diff's `+++` headers, without `a/`/`b/` prefixes."""
    files = []
    for line in patch.splitlines():
        if line.startswith('+++ '):
            name = line[4:].split('\t')[0].strip()
            files.append(name[2:] if name.startswith(('a/', 'b/')) else name)
    return files


def patch_logic_lines(patch):
    """Added or removed non-blank, non-comment lines of a unified diff."""
    count = 0
    for line in patch.splitlines():
        if line.startswith(('+++', '---')) or len(line) < 2 or line[0] not in '+-':
            continue
        body = line[1:].strip()
        if body and not body.startswith(('//', '/*', '*')):
            count += 1
    return count


def validate_answer(answer, repo, lane, task, config):
    gates = config['gates']
    if lane == 'discovery':
        keys(answer, ['candidates'], 'discovery result')
        require(isinstance(answer['candidates'], list), 'candidates must be an array')
        for finding in answer['candidates']:
            keys(finding, FINDING_FIELDS, 'finding')
            require(all(isinstance(finding[k], str) and finding[k].strip()
                        for k in ('title', 'path', 'root_cause', 'evidence', 'impact', 'intended_behavior', 'fix_summary')),
                    'empty finding field')
            require(finding['defect_kind'] in defect_kinds(), 'unknown defect kind')
            require(all(type(finding[k]) is bool for k in ('wg_fit', 'intention_clear', 'impact_reachable', 'fix_unique')),
                    'gate flags must be booleans')
            require(all(type(finding[k]) is int and finding[k] >= 1 for k in ('fix_logic_lines', 'files_touched')),
                    'invalid fix size')
            require(all(type(finding[k]) is int and 1 <= finding[k] <= 5
                        for k in ('defect_confidence', 'remedy_confidence')), 'invalid confidence')
            source = within(repo, finding['path'])
            require(source.is_file(), 'finding source is missing')
            require(type(finding['start_line']) is int and type(finding['end_line']) is int
                    and 1 <= finding['start_line'] <= finding['end_line'] <= len(source_lines(source.read_bytes())),
                    'invalid finding line range')
    else:
        keys(answer, VERDICT_FIELDS, 'verifier result')
        require(answer['status'] in ('confirmed', 'rejected', 'needs-validation')
                and isinstance(answer['rationale'], str) and answer['rationale'].strip(), 'invalid verdict')
        require(all(type(answer[k]) is bool for k in ('wg_fit', 'kind_fit', 'intention_clear', 'impact_reachable', 'fix_unique')),
                'gate flags must be booleans')
        require(all(type(answer[k]) is int and answer[k] >= 0 for k in ('fix_logic_lines', 'files_touched')),
                'invalid fix size')
        require(all(type(answer[k]) is int and 1 <= answer[k] <= 5 for k in ('defect_confidence', 'remedy_confidence')),
                'invalid confidence')
        require(isinstance(answer['fix_patch'], str), 'fix_patch must be text')
        failed = answer['failed_gates']
        require(isinstance(failed, list) and all(isinstance(g, str) and g in GATES for g in failed)
                and len(set(failed)) == len(failed), 'invalid failed_gates')
        # The verdict must agree with its own gate fields: confirmed iff every gate holds.
        derived = gate_failures(answer, gates)
        require(sorted(failed) == sorted(derived), 'failed_gates disagree with the gate fields')
        require((answer['status'] == 'confirmed') == (not derived), 'status disagrees with the hard gates')
        if answer['status'] == 'confirmed':
            files = patch_files(answer['fix_patch'])
            require(len(files) == 1 and (files[0] == task['path'] or files[0].endswith('/' + task['path'])),
                    'fix_patch must change exactly the candidate file')
            require(1 <= patch_logic_lines(answer['fix_patch']) <= gates['max_fix_logic_lines'],
                    'fix_patch logic lines are out of bounds')


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


def run(prepared, mode, out, reference, queue=None, lock_root=None, diagnostics_root=None, max_verifier_tasks=None):
    prepared, reference = Path(prepared), Path(reference)
    require(max_verifier_tasks is None or (type(max_verifier_tasks) is int and max_verifier_tasks >= 0),
            'the verifier cap must be a non-negative integer')
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
        return run_locked(tasks, binding, repo, out, reference, config, lock_root, fd, diagnostics, prepared,
                          max_verifier_tasks)
    finally:
        os.close(fd)


def merge_candidates(findings, out):
    """One candidate per distinct defect: same path and defect kind with overlapping inclusive line ranges.

    Agreement is aligned by location and kind, never by free-text wording, and
    every model's own finding is retained as an occurrence.
    """
    groups = {}
    for task, finding in findings:
        groups.setdefault((finding['path'], finding['defect_kind']), []).append((task, finding))
    candidates = {}
    for (path, kind), members in sorted(groups.items()):
        members.sort(key=lambda member: (member[1]['start_line'], member[1]['end_line'], member[0]['task_id']))
        clusters = []
        for task, finding in members:
            if clusters and finding['start_line'] <= clusters[-1]['end_line']:
                clusters[-1]['end_line'] = max(clusters[-1]['end_line'], finding['end_line'])
                clusters[-1]['members'].append((task, finding))
            else:
                clusters.append({'start_line': finding['start_line'], 'end_line': finding['end_line'],
                                 'members': [(task, finding)]})
        for cluster in clusters:
            identity = {'path': path, 'defect_kind': kind,
                        'start_line': cluster['start_line'], 'end_line': cluster['end_line']}
            candidate_id = 'c-' + sha256(json_bytes(identity))[:24]
            occurrences = []
            for task, finding in cluster['members']:
                result_dir = out / 'results/discovery' / task['task_id']
                occurrence = {'task_id': task['task_id'], 'model': task['model'], 'finding': finding,
                              'trace': str(result_dir / 'trace.jsonl'), 'result': str(result_dir / 'result.json')}
                if occurrence not in occurrences:
                    occurrences.append(occurrence)
            candidates[candidate_id] = {'id': candidate_id, **identity, 'finding': cluster['members'][0][1],
                                        'models': sorted({task['model'] for task, _ in cluster['members']}),
                                        'occurrences': occurrences}
    return candidates


def validation_input(candidate):
    """Blinded verifier input: location, kind, and one-line claims only."""
    claims = []
    for occurrence in candidate['occurrences']:
        claim = {'title': occurrence['finding']['title'], 'root_cause': occurrence['finding']['root_cause']}
        if claim not in claims:
            claims.append(claim)
    value = {**{k: candidate[k] for k in VALIDATION_FIELDS if k != 'claims'}, 'claims': claims}
    assert set(value) == set(VALIDATION_FIELDS)
    return value


def run_locked(tasks, binding, repo, out, reference, config, lock_root, output_fd, diagnostics, prepared,
               max_verifier_tasks=None):
    binding_path = out / 'run.json'
    if binding_path.exists():
        require(read_json(binding_path) == binding, 'run inputs changed; use a new output directory')
    else:
        require(set(p.name for p in out.iterdir()) == {'runner.lock'}, 'unrecognized run output')
        atomic_json(binding_path, binding)
    for name in ('work', 'results', 'results/discovery', 'results/candidates', 'results/validation', 'results/verifier'):
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

    def execute(lane, task, prompt, readable=None):
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
            validate_answer(answer, repo, lane, task, config)
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
                    # The verifier may read only the source, the references, and its own validation input.
                    command = (discovery_command(str(executable), task['model'], work) if lane == 'discovery'
                               else verifier_command(str(executable), work, readable))
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
                    validate_answer(answer, repo, lane, task, config)
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
        answers = list(executor.map(lambda task: execute('discovery', task, discovery_prompt(task, repo, reference)), tasks))
    # Deterministic hard-gate screening: a finding that does not even claim to pass
    # every gate is recorded and never costs a verifier session.
    admitted, screened = [], []
    for task, answer in zip(tasks, answers):
        for finding in answer['candidates'] if answer else []:
            failed = gate_failures(finding, config['gates'], defect_kinds())
            if failed:
                screened.append({'task_id': task['task_id'], 'model': task['model'], 'path': finding['path'],
                                 'start_line': finding['start_line'], 'end_line': finding['end_line'],
                                 'title': finding['title'], 'failed_gates': failed})
            else:
                admitted.append((task, finding))
    atomic_json(within(out, 'results/screened.json'), sorted(screened, key=json_bytes))
    candidates = merge_candidates(admitted, out)
    states['verifier'] = {key: 'queued' for key in candidates}
    for candidate_id, candidate in candidates.items():
        directory = within(out, 'results/candidates/' + candidate_id)
        directory.mkdir(exist_ok=True)
        atomic_json(directory / 'candidate.json', candidate)
        finding = candidate['finding']
        report = (f"# {finding['title']}\n\n{candidate['path']}:{candidate['start_line']}-{candidate['end_line']}"
                  f" ({candidate['defect_kind']}; reported by {', '.join(candidate['models'])})\n\n"
                  + '\n\n'.join(finding[k] for k in ('root_cause', 'evidence', 'impact', 'intended_behavior', 'fix_summary'))
                  + '\n')
        within(directory, 'report.md').write_text(report)
        validation = within(out, 'results/validation/' + candidate_id)
        validation.mkdir(exist_ok=True)
        atomic_json(validation / 'validation-input.json', validation_input(candidate))
    status()
    require(max_verifier_tasks is None or len(candidates) <= max_verifier_tasks,
            f'{len(candidates)} admitted candidates exceed the verifier cap of {max_verifier_tasks}; '
            'no verifier session was started (rerun this output with a higher --max-verifier-tasks)')
    for candidate_id, candidate in candidates.items():
        validation = within(out, 'results/validation/' + candidate_id)
        task = {'task_id': candidate_id, 'model': config['verifier_model'], 'path': candidate['path'],
                'candidate_sha256': sha256(json_bytes(candidate))}
        execute('verifier', task, verifier_prompt(validation / 'validation-input.json', repo, reference),
                [repo, reference, validation])
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
    parser.add_argument('--max-verifier-tasks', type=int,
                        help='refuse to start the verifier when more candidates were admitted')
    args = parser.parse_args()
    try:
        return run(args.prepared, args.mode, args.out, args.reference, args.queue,
                   diagnostics_root=args.diagnostics_root, max_verifier_tasks=args.max_verifier_tasks)
    except (ReviewError, OSError, ValueError) as exc:
        print(f'FAIL runner: {exc}', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
