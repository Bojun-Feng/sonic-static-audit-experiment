"""Production queue and explicit pilot selection contracts."""
import importlib.util
import io
import json
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiment/src"))
from common import ReviewError, json_bytes, read_json


class QueueTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "source"
        (self.repo / "src").mkdir(parents=True)
        for i in range(8):
            (self.repo / f"src/route{i}.cpp").write_text('// route\nauto x = key.find("Vrf");\n')
        self.config = read_json(ROOT / "experiment/config.json")
        self.config["intended_source_commit"] = "UNPINNED"
        self.config["source_roots"] = ["src"]
        path = self.base / "config.json"
        path.write_bytes(json_bytes(self.config))
        spec = importlib.util.spec_from_file_location("selection_queue", ROOT / "experiment/src/select.py")
        selection = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selection)
        from extract import extract
        for mode in ("exhaustive", "targeted"):
            out = self.base / mode
            with mock.patch.object(selection, "find_ast_grep", return_value=(None, None)), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                selection.select(self.repo, path, out, mode)
                if mode == "targeted":
                    extract(self.repo, out / "selected-groups.json")

    def test_production_and_pilot_use_same_units_and_models(self):
        from review_queue import build_tasks, make_pilot_queue
        for mode in ("exhaustive", "targeted"):
            prepared = self.base / mode
            full = build_tasks(prepared, mode)
            self.assertEqual(len(full), 16)
            queue = self.base / (mode + "-pilot.json")
            make_pilot_queue(prepared, mode, queue)
            explicit = read_json(queue)
            self.assertEqual(len(explicit["units"]), 6)
            tasks = build_tasks(prepared, mode, queue)
            self.assertEqual(len(tasks), 12)
            self.assertEqual({t["model"] for t in tasks}, {"gpt-6-astra", "gpt-5.6-sol"})
            self.assertEqual(len({t["task_id"] for t in tasks}), 12)
            self.assertTrue(all(t in full for t in tasks))
            self.assertEqual({t["unit_id"] for t in tasks}, set(explicit["units"]))
            if mode == "targeted":
                self.assertTrue(all(Path(t["source_card"]).is_file() for t in tasks))

    def test_queue_rejects_wrong_mode_unknown_duplicate_and_stale_units(self):
        from review_queue import build_tasks, make_pilot_queue
        prepared = self.base / "exhaustive"
        queue = self.base / "pilot.json"
        make_pilot_queue(prepared, "exhaustive", queue)
        original = read_json(queue)
        for case in ("mode", "unknown", "duplicate", "hash", "empty"):
            value = read_json(queue)
            value = {**original, "units": original["units"][:]}
            if case == "mode": value["mode"] = "targeted"
            if case == "unknown": value["units"][0] = "../bad"
            if case == "duplicate": value["units"][1] = value["units"][0]
            if case == "hash": value["manifest_sha256"] = "0" * 64
            if case == "empty": value["units"] = []
            queue.write_bytes(json_bytes(value))
            with self.subTest(case=case), self.assertRaises(ReviewError):
                build_tasks(prepared, "exhaustive", queue)
        with self.assertRaises(ReviewError):
            build_tasks(prepared, "targeted")

    def test_recovery_queue_uses_only_failed_task_ids_and_new_preparation(self):
        from review_queue import build_tasks, make_recovery_queue
        for mode in ('exhaustive', 'targeted'):
            prepared = self.base / mode
            full = build_tasks(prepared, mode)
            chosen = [task['task_id'] for task in full[2:14:2]]
            notices = self.base / (mode + '-notifications.jsonl')
            notices.write_text(''.join(json.dumps({'lane': 'discovery', 'task_id': task,
                                                   'attempts': 2, 'error': 'ReviewError'}) + '\n'
                                       for task in chosen))
            queue = self.base / (mode + '-recovery.json')
            make_recovery_queue(prepared, mode, queue, notices, chosen)
            tasks = build_tasks(prepared, mode, queue)
            self.assertEqual(len(tasks), 12)
            self.assertEqual({t['unit_id'] for t in tasks}, {t['unit_id'] for t in full[2:14]})
            self.assertTrue(all(t in full for t in tasks))
            for bad in (chosen[:-1], chosen[:5] + [chosen[0]], chosen[:5] + [full[-1]['task_id']],
                        chosen[:5] + [chosen[0].replace('gpt-6-astra', 'unknown-model')]):
                with self.assertRaises(ReviewError):
                    make_recovery_queue(prepared, mode, self.base / 'bad.json', notices, bad)
            notices.write_text(json.dumps({'lane': 'verifier', 'task_id': chosen[0]}) + '\n')
            with self.assertRaises(ReviewError):
                make_recovery_queue(prepared, mode, self.base / 'bad.json', notices, chosen)


if __name__ == "__main__":
    unittest.main()
