"""Private attempt retention and a small OS boundary for model subprocesses."""

import json
import os
from pathlib import Path
import shutil
import stat
import subprocess
import sys
import tempfile

from common import AttemptError, require

ROOT = Path(__file__).resolve().parents[2]


def default_root():
    return Path('/var/tmp') / f'sonic-static-review-diagnostics-{os.getuid()}'


def private_root(value, protected):
    path = Path(value)
    require(path.is_absolute() and not path.is_symlink(), 'diagnostics root must be absolute and not a symlink')
    path = path.resolve()
    for other in (ROOT, Path('/mnt/results'), *protected):
        other = Path(other).resolve()
        require(not path.is_relative_to(other) and not other.is_relative_to(path),
                'diagnostics must be outside and disjoint from repository, results, inputs, and worker scratch')
    path.mkdir(parents=True, mode=0o700, exist_ok=True)
    info = path.stat()
    require(path.is_dir() and info.st_uid == os.getuid() and stat.S_IMODE(info.st_mode) == 0o700,
            'diagnostics root must be an owned mode-0700 directory')
    return path


def isolation_prefix(root):
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
        return [binary, '--die-with-parent', '--unshare-user', '--unshare-pid', '--unshare-ipc',
                '--bind', '/', '/', '--dev', '/dev', '--tmpfs', str(root), '--remount-ro', str(root),
                '--proc', '/proc', '--cap-drop', 'ALL', '--']
    if sys.platform == 'darwin':
        profile = ('(version 1) (allow default) '
                   f'(deny file-read* file-write* (subpath {json.dumps(str(root))})) '
                   '(deny process-info* (target others)) (deny mach-priv-task-port)')
        return ['/usr/bin/sandbox-exec', '-p', profile]
    raise AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False,
                       'worker isolation requires Linux bubblewrap or macOS sandbox-exec')


def verified_worker_prefix(root, protected=()):
    """Fail before any model call if the OS cannot enforce the private root."""
    prefix = isolation_prefix(root)
    if any(Path(prefix[0]).resolve().is_relative_to(Path(path).resolve()) for path in protected):
        raise AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False,
                           'isolation executable is inside source/reference/prepared/output directories')
    with tempfile.TemporaryDirectory(prefix='isolation-probe-', dir=root) as directory:
        sentinel = Path(directory) / 'private.txt'
        sentinel.write_text('private diagnostic isolation probe')
        # Prove both halves of the boundary before any provider call: the
        # private root is unreadable, and the worker still has the ordinary
        # device nodes and PTY allocation that the CLI shell tool needs.
        probe = '''import os, sys
problems = []
try:
    fd = os.open(sys.argv[1], os.O_RDONLY)
except (PermissionError, FileNotFoundError):
    pass
else:
    os.close(fd)
    problems.append('private diagnostics are readable inside the worker boundary')
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
    sys.exit(9 if problems[0].startswith('private') else 7)
'''
        try:
            result = subprocess.run([*prefix, sys.executable, '-I', '-c', probe, str(sentinel)],
                                    capture_output=True, timeout=10, check=False)
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False, str(exc)) from exc
        if result.returncode:
            raise AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False,
                               f'isolation probe exit {result.returncode}: {result.stderr.decode(errors="replace")}')
    return prefix
