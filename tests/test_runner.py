"""End-to-end runner tests with real local subprocesses and fake provider CLIs."""

from contextlib import redirect_stdout, redirect_stderr
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiment/src"))
from common import ReviewError, json_bytes, read_json
from source_identity import bind


def finding(path, title="Prefix accepts wrong interface", cause="Unbounded prefix matches another key"):
    return {"title": title, "path": path, "start_line": 2, "end_line": 2,
            "root_cause": cause, "evidence": f"{path}:2 accepts a prefix without a delimiter guard",
            "impact": "Wrong route selected for a conflicting interface name"}


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "source"
        (self.repo / "src").mkdir(parents=True)
        for i in range(6):
            (self.repo / f"src/route{i}.cpp").write_text('// route\nauto x = key.find("Vrf");\n')
        self.reference = self.base / "references"
        self.reference.mkdir()
        (self.reference / "notes.txt").write_text("Reference definitions for routing")
        self.identity = self.base / "source-identity.json"
        self.config = read_json(ROOT / "experiment/config.json")
        self.config["source_roots"] = ["src"]
        self.config_path = self.base / "config.json"
        self.config_path.write_bytes(json_bytes(self.config))
        bind(self.repo, self.config["intended_source_commit"], self.identity)
        spec = importlib.util.spec_from_file_location("selection_runner", ROOT / "experiment/src/select.py")
        selection = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(selection)
        from extract import extract
        for mode in ("exhaustive", "targeted"):
            out = self.base / mode
            with mock.patch.object(selection, "find_ast_grep", return_value=(None, None)), \
                    redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                selection.select(self.repo, self.config_path, out, mode, identity=self.identity)
                if mode == "targeted": extract(self.repo, out / "selected-groups.json")
        self.bin = self.base / "bin"
        self.bin.mkdir()
        for name in ("codex", "claude"):
            script = self.bin / name
            script.write_text(f"#!{sys.executable}\n" + (ROOT / "tests/fake_model_cli.py").read_text())
            script.chmod(0o755)
        self.state = self.bin / "state"
        self.state.mkdir()
        self.scenario({})
        self.env = mock.patch.dict(os.environ, {"PATH": str(self.bin) + os.pathsep + os.environ.get("PATH", ""),
                                               "UNRELATED_VARIABLE": "must-not-be-recorded"})
        self.env.start()
        self.addCleanup(self.env.stop)
        # Provider doubles run on disposable source fixtures. OS isolation has
        # separate command/probe tests; nested sandboxing is unavailable in CI.
        self.isolation = mock.patch('runner.verified_worker_prefix', return_value=[])
        self.isolation.start()
        self.addCleanup(self.isolation.stop)
        launcher = self.bin / 'python3'
        launcher.write_text(f'#!{sys.executable} -B\n' + '''import os, sys
assert sys.argv[1] == '-B'
sys.argv = sys.argv[2:]
sys.path.insert(0, os.path.dirname(sys.argv[0]))
import runner
runner.verified_worker_prefix = lambda *args: []
runner.global_root = lambda: __import__('pathlib').Path(__file__).parent.parent / 'locks'
sys.exit(runner.main())
''')
        launcher.chmod(0o755)

    def scenario(self, value):
        (self.state / "scenario.json").write_bytes(json_bytes(value))

    def run_review(self, mode="exhaustive"):
        from runner import run
        return run(self.base / mode, mode, self.base / (mode + "-run"), self.reference,
                   lock_root=self.base / "locks", diagnostics_root=self.base / 'diagnostics')

    def events(self):
        return [json.loads(line) for line in (self.state / "events.jsonl").read_text().splitlines()]

    def test_simultaneous_pilot_arms_share_sixteen_slots_and_no_clean_reports(self):
        from review_queue import make_pilot_queue
        self.scenario({"start_gate": 16, "shared_gate": True, "process_relative_clock": True})
        source_before = {p.name: p.read_bytes() for p in (self.repo / "src").iterdir()}
        processes = []
        try:
            for mode in ("exhaustive", "targeted"):
                queue = self.base / (mode + "-queue.json")
                make_pilot_queue(self.base / mode, mode, queue)
                processes.append(subprocess.Popen([
                    sys.executable, "-B", "-c",
                    "import sys; from pathlib import Path; "
                    "sys.path.insert(0, sys.argv.pop(1)); "
                    "import runner; "
                    "runner.verified_worker_prefix = lambda *args: []; "
                    "lock_root = Path(sys.argv.pop(1)); "
                    "runner.global_root = lambda: lock_root; "
                    "sys.exit(runner.main())",
                    str(ROOT / "experiment/src"), str(self.base / "locks"),
                    "--mode", mode, "--prepared", str(self.base / mode),
                    "--out", str(self.base / (mode + "-run")),
                    "--reference", str(self.reference), '--diagnostics-root', str(self.base / 'diagnostics'),
                    "--queue", str(queue)],
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
            for process in processes:
                stdout, stderr = process.communicate(timeout=45)
                self.assertEqual(process.returncode, 0, stdout + stderr)
        finally:
            for process in processes:
                if process.poll() is None:
                    process.kill()
                process.communicate()
        events = self.events()
        live = set()
        peak = 0
        for event in events:
            if event["event"] == "start":
                self.assertNotIn(event["task"], live)
                live.add(event["task"])
            else:
                self.assertIn(event["task"], live)
                live.remove(event["task"])
            self.assertLessEqual(len(live), 16)
            peak = max(peak, len(live))
        self.assertEqual(peak, 16)
        self.assertFalse(live)
        self.assertEqual(len(events), 48)
        for mode in ("exhaustive", "targeted"):
            out = self.base / (mode + "-run")
            status = read_json(out / "status.json")
            self.assertEqual(status["state"], "complete")
            self.assertEqual(status["discovery"], {"queued": 0, "running": 0, "succeeded": 12, "failed": 0})
            finals = list((out / "results/discovery").glob("*/completion.json"))
            self.assertEqual(len(finals), 12)
            self.assertFalse(list((out / "results").rglob("*.md")))
            self.assertEqual(source_before, {p.name: p.read_bytes() for p in (self.repo / "src").iterdir()})
            events = [e for e in self.events() if e["task"].startswith(mode + "--")]
            active = peak = 0
            live = set()
            # Appends are serialized by flock in the fake CLI. Monotonic clock
            # origins need not agree across processes (notably macOS Python 3.9).
            for e in events:
                self.assertIn(e["event"], ("start", "end"))
                if e["event"] == "start":
                    self.assertNotIn(e["task"], live)
                    live.add(e["task"])
                else:
                    self.assertIn(e["task"], live)
                    live.remove(e["task"])
                active = len(live)
                self.assertLessEqual(active, 12)
                peak = max(peak, active)
            self.assertGreater(peak, 0)
            self.assertEqual(active, 0)
            starts = [e for e in events if e["event"] == "start"]
            self.assertEqual(len(events), 24)
            self.assertEqual(len(starts), 12)
            self.assertEqual(len({e["cwd"] for e in starts}), 12)
            for final in finals:
                metadata = read_json(final.parent / "metadata.json")
                self.assertEqual(metadata["usage"]["input_tokens"], 100)
                self.assertIn(metadata["model"], ("gpt-6-astra", "gpt-5.6-sol"))
                self.assertTrue(metadata["session_id"])
                self.assertNotIn("must-not-be-recorded", (final.parent / "metadata.json").read_text())

    def test_retry_replaces_partial_trace_and_rerun_skips_final_success(self):
        self.scenario({"codex": {"src/route0.cpp": {"first": "partial"}}})
        self.assertEqual(self.run_review(), 0)
        out = self.base / "exhaustive-run"
        all_data = b"".join(p.read_bytes() for p in (out / "results").rglob("*") if p.is_file())
        self.assertNotIn(b"INCOMPLETE-MARKER", all_data)
        counts = read_json(self.state / "counts.json")
        self.assertEqual(sorted(counts.values()), [1] * 10 + [2, 2])
        before = self.events()
        self.assertEqual(self.run_review(), 0)
        self.assertEqual(before, self.events())
        self.assertEqual(list((out / "work").iterdir()), [])

    def test_passive_trace_is_rejected_even_with_valid_candidates(self):
        for mode in ("exhaustive", "targeted"):
            self.scenario({"codex": {"src/route0.cpp": {
                "always": "passive", "candidates": [finding("src/route0.cpp")]}}})
            self.assertEqual(self.run_review(mode), 1)
            out = self.base / (mode + "-run")
            self.assertEqual(read_json(out / "status.json")["discovery"]["failed"], 2)
            self.assertFalse(list((out / "results/candidates").iterdir()))
            for trace in (out / "results/discovery").glob("*/trace.jsonl"):
                records = [json.loads(line) for line in trace.read_text().splitlines()]
                commands = [e["item"] for e in records if e.get("item", {}).get("type") == "command_execution"]
                self.assertGreaterEqual(len(commands), 2)
                self.assertTrue(all(c["status"] == "completed" and c["aggregated_output"] for c in commands))

    def test_passive_clean_trace_retries_then_accepts_inspected_attempt(self):
        self.scenario({"codex": {"src/route0.cpp": {"first": "passive"},
                                  "src/route1.cpp": {"always": "passive"}}})
        self.assertEqual(self.run_review(), 1)
        out = self.base / "exhaustive-run"
        self.assertEqual(read_json(out / "status.json")["discovery"]["failed"], 2)
        for completion in (out / "results/discovery").glob("*/completion.json"):
            value = read_json(completion)
            if value["task"]["path"] == "src/route0.cpp":
                self.assertEqual(read_json(completion.parent / "metadata.json")["attempt"], 2)
        self.assertEqual(list((out / "work").iterdir()), [])
        before = self.events()
        self.assertEqual(self.run_review(), 1)
        self.assertEqual(before, self.events())

    def test_resume_revalidates_native_inspection_even_with_matching_hashes(self):
        from common import sha256
        self.assertEqual(self.run_review(), 0)
        trace = next((self.base / "exhaustive-run/results/discovery").glob("*/trace.jsonl"))
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        trace.write_text("".join(json.dumps(e) + "\n" for e in records
                                 if e.get("item", {}).get("type") != "command_execution"))
        completion = trace.parent / "completion.json"
        value = read_json(completion)
        value["artifacts"]["trace.jsonl"] = sha256(trace.read_bytes())
        completion.write_bytes(json_bytes(value))
        with self.assertRaises(ReviewError):
            self.run_review()

    def test_terminal_failure_notifies_and_healthy_tasks_finish(self):
        self.scenario({"codex": {"gpt-6-astra:src/route0.cpp": {"always": "exit"}}})
        self.assertEqual(self.run_review(), 1)
        out = self.base / "exhaustive-run"
        status = read_json(out / "status.json")
        self.assertEqual(status["state"], "failed")
        self.assertEqual(status["discovery"]["succeeded"], 11)
        self.assertEqual(status["discovery"]["failed"], 1)
        notices = [json.loads(line) for line in (out / "notifications.jsonl").read_text().splitlines()]
        self.assertEqual(len(notices), 1)
        self.assertEqual(notices[0]["attempts"], 3)
        self.assertEqual(notices[0]['category'], 'transport')
        self.assertEqual(notices[0]['stage'], 'invoke')
        self.assertEqual(notices[0]['reason'], 'cli_exit')
        self.assertEqual(len(list((out / "results/discovery").iterdir())), 11)

    def test_failed_attempts_stay_outside_publishable_results(self):
        self.scenario({'codex': {'src/route0.cpp': {'first': 'partial'},
                                 'src/route1.cpp': {'always': 'passive'}}})
        self.assertEqual(self.run_review(), 1)
        out = self.base / 'exhaustive-run'
        diagnostic_failures = list((self.base / 'diagnostics').rglob('failure.json'))
        self.assertEqual(len(diagnostic_failures), 8)
        records = [read_json(path) for path in diagnostic_failures]
        self.assertEqual({r['category'] for r in records}, {'session', 'evidence'})
        self.assertEqual({r['stage'] for r in records}, {'answer', 'inspection'})
        self.assertTrue(all(r['detail'] and r['retryable'] for r in records))
        raw = b''.join(p.read_bytes() for p in (self.base / 'diagnostics').rglob('*') if p.is_file())
        self.assertIn(b'INCOMPLETE-MARKER', raw)
        public = b''.join(p.read_bytes() for p in out.rglob('*') if p.is_file())
        self.assertNotIn(b'INCOMPLETE-MARKER', public)
        self.assertNotIn(b'failure.json', public)
        self.assertNotIn(str(self.base / 'diagnostics').encode(), public)
        starts = [e for e in self.events() if e['event'] == 'start']
        self.assertEqual(len({e['cwd'] for e in starts}), len(starts))
        self.assertTrue(all(not Path(e['cwd']).is_relative_to(out) for e in starts))
        self.assertTrue(all(not Path(e['cwd']).exists() for e in starts))
        notices = [json.loads(line) for line in (out / 'notifications.jsonl').read_text().splitlines()]
        self.assertTrue(all(n['attempts'] == 3 and n['reason'] == 'missing_inspection' for n in notices))

    def test_missing_cli_is_deterministic_and_not_retried(self):
        (self.bin / 'codex').unlink()
        with mock.patch('runner.shutil.which', return_value=None):
            self.assertEqual(self.run_review(), 1)
        out = self.base / 'exhaustive-run'
        notices = [json.loads(line) for line in (out / 'notifications.jsonl').read_text().splitlines()]
        self.assertEqual(len(notices), 12)
        self.assertTrue(all(n['attempts'] == 1 and n['category'] == 'configuration'
                            and n['reason'] == 'missing_cli' and not n['retryable'] for n in notices))
        self.assertFalse((self.state / 'events.jsonl').exists())

    def test_third_fresh_attempt_can_succeed_with_range_inspection(self):
        self.scenario({'codex': {'src/route0.cpp': {'failures': ['partial', 'passive'],
                                                  'inspection': 'ranges'}}})
        self.assertEqual(self.run_review(), 0)
        out = self.base / 'exhaustive-run'
        for completion in (out / 'results/discovery').glob('*/completion.json'):
            if read_json(completion)['task']['path'] == 'src/route0.cpp':
                metadata = read_json(completion.parent / 'metadata.json')
                self.assertEqual(metadata['attempt'], 3)
                self.assertTrue(metadata['session_id'].endswith('-3'))
        self.assertEqual(len(list((self.base / 'diagnostics').rglob('failure.json'))), 4)
        self.assertFalse((out / 'notifications.jsonl').exists())

    def test_chained_inspection_in_one_shell_line_is_accepted_end_to_end(self):
        self.scenario({'codex': {'src/route0.cpp': {'inspection': 'chained', 'candidates': [finding('src/route0.cpp')]}}})
        self.assertEqual(self.run_review(), 0)
        out = self.base / 'exhaustive-run'
        self.assertFalse((out / 'notifications.jsonl').exists())
        self.assertEqual(len(list((self.base / 'diagnostics').rglob('failure.json'))), 0)
        checked = 0
        for completion in (out / 'results/discovery').glob('*/completion.json'):
            if read_json(completion)['task']['path'] != 'src/route0.cpp':
                continue
            checked += 1
            self.assertEqual(read_json(completion.parent / 'metadata.json')['attempt'], 1)
            records = [json.loads(line) for line in (completion.parent / 'trace.jsonl').read_text().splitlines()]
            commands = [e['item']['command'] for e in records if e.get('item', {}).get('type') == 'command_execution']
            self.assertEqual(len(commands), 1)
            self.assertIn(' && ', commands[0])
            self.assertIn('; grep -rn key ', commands[0])
        self.assertEqual(checked, 2)
        self.assertEqual(len(list((out / 'results/candidates').iterdir())), 1)

    def test_isolation_failure_is_private_deterministic_and_never_starts_a_model(self):
        from common import AttemptError
        error = AttemptError('configuration', 'worker_isolation', 'isolation_unavailable', False,
                             'PRIVATE-ISOLATION-DETAIL')
        with mock.patch('runner.verified_worker_prefix', side_effect=error):
            self.assertEqual(self.run_review(), 1)
        out = self.base / 'exhaustive-run'
        self.assertFalse((self.state / 'events.jsonl').exists())
        notices = (out / 'notifications.jsonl').read_text()
        self.assertNotIn('PRIVATE-ISOLATION-DETAIL', notices)
        self.assertTrue(all(json.loads(line)['attempts'] == 1 for line in notices.splitlines()))
        self.assertTrue(all('PRIVATE-ISOLATION-DETAIL' in p.read_text()
                            for p in (self.base / 'diagnostics').rglob('failure.json')))

    def test_cli_usage_error_stops_after_one_attempt(self):
        self.scenario({'codex': {'src/route0.cpp': {'always': 'usage'}}})
        self.assertEqual(self.run_review(), 1)
        notices = (self.base / 'exhaustive-run/notifications.jsonl').read_text().splitlines()
        self.assertEqual(len(notices), 2)
        self.assertTrue(all(json.loads(line)['category'] == 'configuration' and
                            json.loads(line)['attempts'] == 1 and not json.loads(line)['retryable']
                            for line in notices))

    def test_diagnostics_must_be_disjoint_from_repository_results_and_inputs(self):
        from runner import run
        for root in (ROOT / 'diagnostics', self.base / 'exhaustive-run/diagnostics', self.repo / 'diagnostics',
                     self.reference / 'diagnostics', self.base / 'exhaustive/diagnostics', self.base):
            with self.subTest(root=root), self.assertRaises(ReviewError):
                run(self.base / 'exhaustive', 'exhaustive', self.base / 'exhaustive-run', self.reference,
                    lock_root=self.base / 'locks', diagnostics_root=root)
        self.assertFalse((self.state / 'events.jsonl').exists())

    def test_candidates_from_both_models_are_deduplicated_and_verified_serially(self):
        a, b = finding("src/route0.cpp"), finding("src/route1.cpp", cause="Different parsing error")
        self.scenario({"codex": {"src/route0.cpp": {"candidates": [a]},
                                  "gpt-5.6-sol:src/route1.cpp": {"candidates": [b]}},
                       "claude": {"src/route1.cpp": {"status": "needs-validation"}}})
        self.assertEqual(self.run_review(), 0)
        out = self.base / "exhaustive-run"
        candidates = [read_json(p) for p in (out / "results/candidates").glob("*/candidate.json")]
        self.assertEqual(len(candidates), 2)
        self.assertEqual(sorted(len(c["occurrences"]) for c in candidates), [1, 2])
        self.assertEqual(len(list((out / "results/candidates").glob("*/report.md"))), 2)
        verdicts = [read_json(p) for p in (out / "results/verifier").glob("*/result.json")]
        self.assertEqual({v["status"] for v in verdicts}, {"confirmed", "needs-validation"})
        events = [e for e in self.events() if e["kind"] == "claude"]
        self.assertEqual([e["event"] for e in events], ["start", "end", "start", "end"])

    def test_shell_run_supports_both_modes_and_explicit_queue(self):
        from review_queue import make_pilot_queue
        for mode in ('exhaustive', 'targeted'):
            queue = self.base / (mode + '-queue.json')
            make_pilot_queue(self.base / mode, mode, queue)
            command = ['/bin/bash', str(ROOT / 'experiment/run-experiment.sh'), 'run', mode,
                       '--prepared', str(self.base / mode), '--out', str(self.base / (mode + '-shell')),
                       '--reference', str(self.reference), '--diagnostics-root', str(self.base / 'diagnostics'),
                       '--queue', str(queue)]
            result = subprocess.run(command, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(read_json(self.base / (mode + '-shell/status.json'))['state'], 'complete')
            # Omitting the optional queue also works on macOS Bash 3 with nounset.
            before = self.events()
            result = subprocess.run(command[:-2], capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(self.events(), before)

    def test_invalid_findings_never_enter_results_and_terminal_failures_resume(self):
        bad = finding('../outside.cpp')
        self.scenario({'codex': {'src/route0.cpp': {'candidates': [bad]}}})
        self.assertEqual(self.run_review(), 1)
        out = self.base / 'exhaustive-run'
        self.assertEqual(read_json(out / 'status.json')['discovery']['failed'], 2)
        self.assertFalse(list((out / 'results/candidates').iterdir()))
        before = self.events()
        self.assertEqual(self.run_review(), 1)
        self.assertEqual(self.events(), before)
        self.assertEqual(len((out / 'notifications.jsonl').read_text().splitlines()), 2)

    def test_verifier_retry_failure_and_duplicate_contract(self):
        paths = [f'src/route{i}.cpp' for i in range(3)]
        self.scenario({'codex': {p: {'candidates': [finding(p)]} for p in paths},
                       'claude': {paths[0]: {'first': 'partial'},
                                  paths[1]: {'status': 'duplicate', 'duplicate_of': 'previous'},
                                  paths[2]: {'always': 'exit'}}})
        self.assertEqual(self.run_review(), 1)
        out = self.base / 'exhaustive-run'
        status = read_json(out / 'status.json')
        self.assertEqual(status['discovery']['succeeded'], 12)
        self.assertEqual(status['verifier']['succeeded'], 2)
        self.assertEqual(status['verifier']['failed'], 1)
        verdicts = [read_json(p) for p in (out / 'results/verifier').glob('*/result.json')]
        self.assertEqual({v['status'] for v in verdicts}, {'confirmed', 'duplicate'})
        before = self.events()
        self.assertEqual(self.run_review(), 1)
        self.assertEqual(self.events(), before)

    def test_resume_rejects_changed_completed_artifacts(self):
        self.assertEqual(self.run_review(), 0)
        out = self.base / 'exhaustive-run'
        result = next((out / 'results/discovery').glob('*/result.json'))
        result.write_text('{"candidates": []}')
        before = self.events()
        with self.assertRaises(ReviewError):
            self.run_review()
        self.assertEqual(self.events(), before)

    def test_timeout_kills_cli_cleans_work_and_releases_slots(self):
        from review_queue import runner_config
        from runner import run
        self.scenario({'sleep': 2})
        config = runner_config()
        config['timeout_seconds'] = 0.15
        with mock.patch('runner.runner_config', return_value=config):
            self.assertEqual(self.run_review(), 1)
        out = self.base / 'exhaustive-run'
        self.assertEqual(read_json(out / 'status.json')['discovery']['failed'], 12)
        self.assertEqual(list((out / 'work').iterdir()), [])
        self.assertEqual(list((out / 'results/discovery').iterdir()), [])
        self.scenario({})
        self.assertEqual(run(self.base / 'exhaustive', 'exhaustive', self.base / 'after-timeout',
                             self.reference, lock_root=self.base / 'locks', diagnostics_root=self.base / 'diagnostics'), 0)

    def test_changed_run_binding_and_overlapping_outputs_fail_before_cli(self):
        from runner import run
        for out in (self.repo / 'output', self.reference / 'output', self.base / 'exhaustive/output'):
            with self.assertRaises(ReviewError):
                run(self.base / 'exhaustive', 'exhaustive', out, self.reference)
            self.assertFalse(out.exists())
        self.assertEqual(self.run_review(), 0)
        other = self.base / 'other-reference'
        other.mkdir()
        before = self.events()
        with self.assertRaises(ReviewError):
            run(self.base / 'exhaustive', 'exhaustive', self.base / 'exhaustive-run', other,
                diagnostics_root=self.base / 'diagnostics', lock_root=self.base / 'locks')
        self.assertEqual(self.events(), before)

    def test_incomplete_completion_record_is_not_resumed(self):
        self.assertEqual(self.run_review(), 0)
        path = next((self.base / 'exhaustive-run/results/discovery').glob('*/completion.json'))
        value = read_json(path)
        value['artifacts'] = {}
        path.write_bytes(json_bytes(value))
        with self.assertRaises(ReviewError):
            self.run_review()


if __name__ == "__main__":
    unittest.main()
