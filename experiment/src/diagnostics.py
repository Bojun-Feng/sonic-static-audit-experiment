"""An OS isolation boundary for model subprocesses, verified by a probe before use."""

import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile

from common import AttemptError

def isolation_prefix(root, hidden=(), exposed=(), writable=()):
    """Worker command prefix: the private diagnostics root and every hidden tree
    are masked; only the listed inputs are re-exposed, read-only, inside them."""
    hidden, exposed = [str(Path(p)) for p in hidden], [str(Path(p)) for p in exposed]
    if sys.platform == 'linux':
        binary = shutil.which('bwrap')
        if binary is None:
            raise AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False,
                               'bubblewrap is required on the runner host')
        # A private PID namespace also closes /proc/<parent>/root and fd routes
        # to hidden diagnostics. No capabilities survive in the worker.
        # A plain bind of / carries nodev, which leaves every device node
        # unusable: the CLI's TTY-backed shell tool then cannot open /dev/null
        # or allocate a PTY and no inspection command ever starts. bubblewrap's
        # minimal private /dev (null, zero, full, random, urandom, tty, ptmx
        # with a fresh devpts, shm, fd links) restores that without exposing
        # the host's device tree; the masked diagnostics root is unaffected.
        # Hidden trees (sibling run outputs) are masked the same way, the
        # worker's inputs are bound back read-only inside the mask, and the
        # mask is sealed read-only afterwards, in that order.
        masks = [arg for path in hidden for arg in ('--tmpfs', path)]
        binds = [arg for path in exposed for arg in ('--ro-bind', path, path)]
        binds += [arg for path in writable for arg in ('--bind', str(path), str(path))]
        seals = [arg for path in reversed(hidden) for arg in ('--remount-ro', path)]
        return [binary, '--die-with-parent', '--unshare-user', '--unshare-pid', '--unshare-ipc',
                '--bind', '/', '/', '--dev', '/dev', '--tmpfs', str(root), '--remount-ro', str(root),
                *masks, *binds, *seals, '--proc', '/proc', '--cap-drop', 'ALL', '--']
    if sys.platform == 'darwin':
        # Later rules win: deny the masks, then allow reads of the exposed inputs.
        rules = ' '.join([f'(deny file-read* file-write* (subpath {json.dumps(path)}))' for path in (str(root), *hidden)]
                         + [f'(allow file-read* (subpath {json.dumps(path)}))' for path in exposed]
                         + [f'(allow file-read* file-write* (subpath {json.dumps(str(path))}))' for path in writable])
        profile = f'(version 1) (allow default) {rules} (deny process-info* (target others)) (deny mach-priv-task-port)'
        return ['/usr/bin/sandbox-exec', '-p', profile]
    raise AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False,
                       'worker isolation requires Linux bubblewrap or macOS sandbox-exec')


def verified_worker_prefix(root, protected=(), hidden=(), exposed=(), writable=(), probe_dir=None):
    """Fail before any model call unless the OS enforces the private root, the
    hidden trees, and the read-only exposures. Returns the base worker prefix."""
    prefix = isolation_prefix(root, hidden, exposed, writable)
    if any(Path(prefix[0]).resolve().is_relative_to(Path(path).resolve()) for path in (*protected, *hidden)):
        raise AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False,
                           'isolation executable is inside source/reference/prepared/output/hidden directories')
    if hidden and not (probe_dir is not None and any(Path(probe_dir).resolve().is_relative_to(Path(path).resolve())
                                                     for path in hidden)):
        raise AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False,
                           'the hidden-tree probe needs a scratch directory inside a hidden tree')
    with tempfile.TemporaryDirectory(prefix='isolation-probe-', dir=root) as directory:
        sentinel = Path(directory) / 'private.txt'
        sentinel.write_text('private diagnostic isolation probe')
        spec = {'hidden': None, 'exposed': [str(Path(path)) for path in exposed]}
        hidden_scratch = tempfile.TemporaryDirectory(prefix='isolation-probe-', dir=probe_dir) if hidden else None
        try:
            if hidden_scratch is not None:
                hidden_sentinel = Path(hidden_scratch.name) / 'sibling.txt'
                hidden_sentinel.write_text('hidden sibling output probe')
                spec['hidden'] = str(hidden_sentinel)
            # Prove every half of the boundary before any provider call: the
            # private root and hidden trees are unreadable, the exposed inputs
            # are readable, and the worker still has the ordinary device nodes
            # and PTY allocation that the CLI shell tool needs.
            probe = '''import json, os, sys
spec, sentinel = json.loads(sys.argv[1]), sys.argv[2]
problems = []
def readable(path):
    try:
        if os.path.isdir(path):
            os.listdir(path)
        else:
            os.close(os.open(path, os.O_RDONLY))
        return True
    except (PermissionError, FileNotFoundError):
        return False
if readable(sentinel):
    problems.append('private diagnostics are readable inside the worker boundary')
if spec['hidden'] and readable(spec['hidden']):
    problems.append('hidden sibling outputs are readable inside the worker boundary')
for path in spec['exposed']:
    if not readable(path):
        problems.append(f'exposed input is unreadable inside the worker boundary: {path}')
try:
    fd = os.open('/dev/null', os.O_RDWR)
    os.write(fd, b'probe')
    os.close(fd)
except OSError as exc:
    problems.append(f'/dev/null is unusable inside the worker boundary: {exc}')
try:
    master, worker = os.openpty()
    os.close(master)
    os.close(worker)
except OSError as exc:
    problems.append(f'pty allocation failed inside the worker boundary: {exc}')
if problems:
    print('; '.join(problems), file=sys.stderr)
    sys.exit(9 if problems[0].startswith(('private', 'hidden')) else 7)
'''
            try:
                result = subprocess.run([*prefix, sys.executable, '-I', '-c', probe, json.dumps(spec), str(sentinel)],
                                        capture_output=True, timeout=10, check=False)
            except (OSError, subprocess.TimeoutExpired) as exc:
                raise AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False, str(exc)) from exc
        finally:
            if hidden_scratch is not None:
                hidden_scratch.cleanup()
        if result.returncode:
            raise AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False,
                               f'isolation probe exit {result.returncode}: {result.stderr.decode(errors="replace")}')
    return prefix
