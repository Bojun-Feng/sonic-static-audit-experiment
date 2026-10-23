"""Kernel-backed slots shared by all runner processes for this Unix user.

Never unlink a lock file: doing so can create two independently locked inodes.
File descriptors are passed to CLI children so an orphaned session retains its
slot if the runner is killed. A stale filename alone is never evidence of life.
"""

from contextlib import contextmanager
from dataclasses import dataclass
import fcntl
import os
from pathlib import Path
import stat
import time

from review_queue import runner_config
from common import require


def global_root():
    # Deliberately independent of output, mode, model, HOME and TMPDIR. The
    # legacy directory name is shared with already-running runners; keep it.
    return Path("/tmp") / f"sonic-static-audit-{os.getuid()}"


def open_lock(path):
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    info = os.fstat(fd)
    try:
        require(stat.S_ISREG(info.st_mode) and info.st_uid == os.getuid()
                and info.st_nlink == 1, "invalid lock file")
    except BaseException:
        os.close(fd)
        raise
    return fd


def bind_diagnostics_root(root, diagnostics):
    """All runners sharing slots must hide the same persistent private root."""
    SlotPool(root, 'discovery')  # Apply the same owner/symlink checks as slots.
    fd = open_lock(root / 'diagnostics-root.lock')
    expected = (str(diagnostics) + '\n').encode()
    try:
        fcntl.flock(fd, fcntl.LOCK_EX)
        recorded = os.read(fd, 16384)
        require(not recorded or recorded == expected,
                'global scheduler is bound to another diagnostics root; use that same root')
        if not recorded:
            os.write(fd, expected)
            os.fsync(fd)
    finally:
        os.close(fd)


@dataclass
class Slot:
    index: int
    fd: int
    wait_seconds: float


class SlotPool:
    def __init__(self, root, lane):
        require(lane in ("discovery", "verifier"), "unknown scheduler lane")
        self.root = root
        self.lane = lane
        self.limit = runner_config()[f"{lane}_slots"]
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
        require(not root.is_symlink() and root.is_dir() and root.stat().st_uid == os.getuid(),
                "invalid global semaphore directory")

    @contextmanager
    def acquire(self):
        started = time.monotonic()
        chosen = None
        while chosen is None:
            for index in range(self.limit):
                fd = open_lock(self.root / f"{self.lane}-{index}.lock")
                try:
                    fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                except BlockingIOError:
                    os.close(fd)
                    continue
                chosen = Slot(index, fd, time.monotonic() - started)
                break
            if chosen is None:
                time.sleep(0.025)
        try:
            yield chosen
        finally:
            # close, not LOCK_UN: inherited copies must continue holding a slot.
            os.close(chosen.fd)
