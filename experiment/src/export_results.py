#!/usr/bin/env python3
"""Export a finished static correctness review run using a fixed file allowlist."""

import argparse
import fcntl
import io
import json
import os
from pathlib import Path
import re
import stat
import sys
import tarfile

from common import ReviewError, exact_children, keys, read_json, require, sha256, within

SUCCESS_FILES = ('trace.jsonl', 'stderr.log', 'prompt.txt', 'result.json', 'metadata.json')
VALIDATION_FIELDS = ('id', 'path', 'start_line', 'end_line', 'defect_kind', 'claims')
SCREENED_FIELDS = ('task_id', 'model', 'path', 'start_line', 'end_line', 'title', 'failed_gates')
REASONS = {
    'missing_cli': ('configuration', 'configuration', False),
    'invalid_configuration': ('configuration', 'configuration', False),
    'isolation_unavailable': ('configuration', 'worker_isolation', False),
    'cli_unavailable': ('configuration', 'invoke', False),
    'cli_usage': ('configuration', 'invoke', False),
    'model_mismatch': ('configuration', 'session', False),
    'cli_exit': ('transport', 'invoke', True),
    'timeout': ('transport', 'invoke', True),
    'invalid_event_stream': ('session', 'event_stream', True),
    'invalid_answer': ('session', 'answer', True),
    'incomplete_session': ('session', 'session', True),
    'invalid_result': ('session', 'result_validation', True),
    'missing_inspection': ('evidence', 'inspection', True),
    'source_unavailable': ('input', 'inspection', False),
}


def regular_bytes(root, relative):
    path = within(root, relative)
    info = path.stat()
    require(stat.S_ISREG(info.st_mode) and info.st_nlink == 1, 'export requires unlinked regular files')
    return path.read_bytes()


def inventory(root):
    """Select known outputs explicitly. Never recurse over an output directory."""
    binding = read_json(within(root, 'run.json'))
    keys(binding, ['schema_version', 'tasks', 'manifest_sha256', 'reference', 'config', 'contracts'], 'run binding')
    status = read_json(within(root, 'status.json'))
    keys(status, ['state', 'discovery', 'verifier'], 'run status')
    require(status['state'] in ('complete', 'failed'), 'only finished runs can be exported')
    files = {'run.json': regular_bytes(root, 'run.json'), 'status.json': regular_bytes(root, 'status.json')}
    task_ids = {task['task_id'] for task in binding['tasks']}
    for lane in ('discovery', 'verifier'):
        directory = within(root, 'results/' + lane)
        for result in sorted(directory.iterdir()):
            require(not result.is_symlink() and result.is_dir(), 'invalid completed result directory')
            if lane == 'discovery':
                require(result.name in task_ids, 'unrecognized discovery result')
            else:
                require(re.fullmatch(r'c-[0-9a-f]{24}', result.name), 'invalid candidate ID')
            relative = result.relative_to(root).as_posix()
            completion = read_json(within(root, relative + '/completion.json'))
            keys(completion, ['task', 'artifacts'], 'completion')
            keys(completion['artifacts'], SUCCESS_FILES, 'completion artifacts')
            require(completion['task']['task_id'] == result.name, 'completed task mismatch')
            exact_children(result, [*SUCCESS_FILES, 'completion.json'])
            for name in SUCCESS_FILES:
                data = regular_bytes(root, relative + '/' + name)
                require(sha256(data) == completion['artifacts'][name], 'completed artifact changed')
                files[relative + '/' + name] = data
            files[relative + '/completion.json'] = regular_bytes(root, relative + '/completion.json')
    candidate_ids = set()
    for directory in sorted(within(root, 'results/candidates').iterdir()):
        require(not directory.is_symlink() and directory.is_dir()
                and re.fullmatch(r'c-[0-9a-f]{24}', directory.name), 'invalid candidate directory')
        candidate_ids.add(directory.name)
        exact_children(directory, ['candidate.json', 'report.md'])
        for name in ('candidate.json', 'report.md'):
            relative = (directory / name).relative_to(root).as_posix()
            files[relative] = regular_bytes(root, relative)
    validation_root = within(root, 'results/validation')
    if validation_root.exists():
        validation_ids = set()
        for directory in sorted(validation_root.iterdir()):
            require(not directory.is_symlink() and directory.is_dir()
                    and re.fullmatch(r'c-[0-9a-f]{24}', directory.name), 'invalid validation directory')
            exact_children(directory, ['validation-input.json'])
            relative = (directory / 'validation-input.json').relative_to(root).as_posix()
            data = regular_bytes(root, relative)
            value = json.loads(data)
            keys(value, VALIDATION_FIELDS, 'validation input')
            require(value['id'] == directory.name, 'validation input identity mismatch')
            files[relative] = data
            validation_ids.add(directory.name)
        require(validation_ids == candidate_ids, 'validation inputs do not match candidates')
    screened = within(root, 'results/screened.json')
    if screened.exists():
        data = regular_bytes(root, 'results/screened.json')
        value = json.loads(data)
        require(isinstance(value, list), 'screened findings must be a list')
        for entry in value:
            keys(entry, SCREENED_FIELDS, 'screened finding')
            require(entry['task_id'] in task_ids and isinstance(entry['failed_gates'], list) and entry['failed_gates'],
                    'invalid screened finding')
        files['results/screened.json'] = data
    notices = within(root, 'notifications.jsonl')
    if notices.exists():
        data = regular_bytes(root, 'notifications.jsonl')
        for line in data.decode().splitlines():
            notice = json.loads(line)
            keys(notice, ['lane', 'task_id', 'attempts', 'category', 'stage', 'reason', 'retryable'], 'notification')
            require(notice['lane'] in ('discovery', 'verifier') and type(notice['attempts']) is int
                    and 1 <= notice['attempts'] <= binding['config']['max_attempts']
                    and type(notice['retryable']) is bool, 'invalid failure summary')
            require(notice['task_id'] in (task_ids if notice['lane'] == 'discovery' else candidate_ids),
                    'unknown failed task')
            if notice['reason'] == 'local_io':
                require(notice['category'] == 'storage' and notice['retryable'] is False
                        and notice['stage'] in ('scheduler', 'invoke', 'answer', 'session', 'result_validation', 'publish'),
                        'invalid storage summary')
            else:
                require(REASONS.get(notice['reason']) == (notice['category'], notice['stage'], notice['retryable']),
                        'unrecognized failure reason; raw diagnostics cannot be exported')
        files['notifications.jsonl'] = data
    return files


def export_run(root, destination):
    root, destination = Path(root), Path(destination)
    require(not root.is_symlink() and root.is_dir(), 'run must be a regular directory')
    root = root.resolve()
    require(not destination.resolve().is_relative_to(root), 'archive must be outside the run')
    lock = within(root, 'runner.lock')
    fd = os.open(lock, os.O_RDONLY | os.O_NOFOLLOW)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            raise ReviewError('cannot export an active run') from exc
        files = inventory(root)  # Validate everything before creating an archive.
        with destination.open('xb') as stream:
            try:
                with tarfile.open(fileobj=stream, mode='w:gz') as archive:
                    for name, data in sorted(files.items()):
                        member = tarfile.TarInfo(name)
                        member.size, member.mode = len(data), 0o644
                        archive.addfile(member, io.BytesIO(data))
            except BaseException:
                destination.unlink()
                raise
    finally:
        os.close(fd)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--run', type=Path, required=True)
    parser.add_argument('--out', type=Path, required=True)
    args = parser.parse_args()
    try:
        export_run(args.run, args.out)
    except (ReviewError, OSError, ValueError, KeyError, TypeError) as exc:
        print(f'FAIL export: {exc}', file=sys.stderr)
        return 1
    print(f'Exported validated review outputs: {args.out}')
    return 0


if __name__ == '__main__':
    sys.exit(main())
