"""Real local process contention, with no provider or target execution."""

import fcntl
import json
import multiprocessing
import select  # Load the stdlib extension before adding the selector script path.
from pathlib import Path
import sys
import tempfile
import time
import unittest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "experiment/src"))


def hold_slot(root, lane, number):
    from scheduler import SlotPool
    root = Path(root)
    (root / f"ready-{number}").touch()
    deadline = time.monotonic() + 20
    while not (root / "gate").exists():
        if time.monotonic() > deadline:
            raise RuntimeError("test gate timeout")
        time.sleep(0.01)
    with SlotPool(Path(root), lane).acquire() as slot:
        def event(name):
            with (root / "events.jsonl").open("a") as handle:
                fcntl.flock(handle, fcntl.LOCK_EX)
                handle.write(json.dumps([name, number, slot.index]) + "\n")

        event("start")
        (root / f"acquired-{number}").touch()
        deadline = time.monotonic() + 20
        while not (root / "release").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("test release timeout")
            time.sleep(0.01)
        event("end")


class SchedulerTests(unittest.TestCase):
    def test_global_limits_span_processes_and_drain_waiters(self):
        from scheduler import SlotPool
        with tempfile.TemporaryDirectory() as directory:
            ctx = multiprocessing.get_context("spawn")
            for lane, limit in (("discovery", 4), ("verifier", 1)):
                root = Path(directory) / lane
                root.mkdir()
                workers = [ctx.Process(target=hold_slot, args=(str(root), lane, i)) for i in range(12)]
                try:
                    for worker in workers:
                        worker.start()
                    deadline = time.monotonic() + 20
                    while len(list(root.glob("ready-*"))) != 12 and time.monotonic() < deadline:
                        time.sleep(0.02)
                    self.assertEqual(len(list(root.glob("ready-*"))), 12)
                    (root / "gate").touch()
                    deadline = time.monotonic() + 20
                    while len(list(root.glob("acquired-*"))) < limit and time.monotonic() < deadline:
                        time.sleep(0.01)
                    self.assertEqual(len(list(root.glob("acquired-*"))), limit)
                    (root / "release").touch()
                    for worker in workers:
                        worker.join(20)
                        self.assertEqual(worker.exitcode, 0)
                    # The locked append order is shared across processes;
                    # per-process monotonic clock origins may be different.
                    events = [json.loads(line) for line in (root / "events.jsonl").read_text().splitlines()]
                    self.assertEqual(len(events), 24)
                    live = {}
                    seen = set()
                    peak = 0
                    for name, number, slot in events:
                        self.assertIn(name, ("start", "end"))
                        self.assertTrue(0 <= slot < limit)
                        if name == "start":
                            self.assertNotIn(number, seen)
                            self.assertNotIn(slot, live.values())
                            seen.add(number)
                            live[number] = slot
                        else:
                            self.assertIn(number, live)
                            self.assertEqual(live.pop(number), slot)
                        self.assertLessEqual(len(live), limit)
                        peak = max(peak, len(live))
                    self.assertEqual(seen, set(range(12)))
                    self.assertEqual(peak, limit)
                    self.assertEqual(live, {})
                finally:
                    for worker in workers:
                        if worker.is_alive():
                            worker.kill()
                            worker.join()

    def test_locks_release_on_exception_and_reject_symlink_lock_files(self):
        from scheduler import SlotPool
        from common import AuditError
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            pool = SlotPool(root, "verifier")
            with self.assertRaises(RuntimeError), pool.acquire():
                raise RuntimeError("task failure")
            with pool.acquire() as slot:
                self.assertEqual(slot.index, 0)
                self.assertGreaterEqual(slot.wait_seconds, 0)
            lock = root / "verifier-0.lock"
            lock.unlink()
            lock.symlink_to(root / "outside")
            with self.assertRaises((AuditError, OSError)), pool.acquire():
                pass


if __name__ == "__main__":
    unittest.main()
