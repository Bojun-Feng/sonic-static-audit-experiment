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


FIXTURE = '// route\nauto x = key.find("Vrf");\nif (x == 0) {\n    return key.substr(3);\n}\nreturn key;\n'


def finding(path, title="Prefix accepts wrong interface", cause="Unbounded prefix matches another key",
            start=2, end=2, kind="prefix-vs-exact", **gates):
    """A discovery finding that passes every hard gate unless `gates` overrides a field."""
    value = {"title": title, "path": path, "start_line": start, "end_line": end,
             "root_cause": cause, "evidence": f"{path}:2 accepts a prefix without a delimiter guard",
             "impact": "Wrong route selected for a conflicting interface name",
             "defect_kind": kind, "wg_fit": True, "intended_behavior": "Only the exact interface key may match",
             "intention_clear": True, "impact_reachable": True,
             "fix_summary": "Compare the full key instead of its prefix", "fix_logic_lines": 3,
             "files_touched": 1, "fix_unique": True, "defect_confidence": 5, "remedy_confidence": 5}
    value.update(gates)
    return value


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.base = Path(self.temp.name)
        self.repo = self.base / "source"
        (self.repo / "src").mkdir(parents=True)
        for i in range(6):
            (self.repo / f"src/route{i}.cpp").write_text(FIXTURE)
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
        # Fake CLIs live outside the run parent: workers may not execute from a hidden tree.
        self.bin_temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.bin_temp.cleanup)
        self.bin = Path(self.bin_temp.name) / "bin"
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
        self.prefix = mock.patch('runner.isolation_prefix', return_value=[])
        self.prefix.start()
        self.addCleanup(self.prefix.stop)
        launcher = self.bin / 'python3'
        launcher.write_text(f'#!{sys.executable} -B\n' + '''import os, sys
assert sys.argv[1] == '-B'
sys.argv = sys.argv[2:]
sys.path.insert(0, os.path.dirname(sys.argv[0]))
import runner
runner.verified_worker_prefix = lambda *args, **kwargs: []
runner.isolation_prefix = lambda *args, **kwargs: []
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
                    "runner.verified_worker_prefix = lambda *args, **kwargs: []; "
                    "runner.isolation_prefix = lambda *args, **kwargs: []; "
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

    def test_complete_sessions_publish_on_first_attempt_with_their_native_traces(self):
        for mode in ("exhaustive", "targeted"):
            self.scenario({"codex": {"src/route1.cpp": {"candidates": [finding("src/route1.cpp")]}}})
            self.assertEqual(self.run_review(mode), 0)
            out = self.base / (mode + "-run")
            self.assertEqual(read_json(out / "status.json")["discovery"],
                             {"queued": 0, "running": 0, "succeeded": 12, "failed": 0})
            self.assertFalse((out / "notifications.jsonl").exists())
            for completion in (out / "results/discovery").glob("*/completion.json"):
                self.assertEqual(read_json(completion.parent / "metadata.json")["attempt"], 1)
                records = [json.loads(line) for line in (completion.parent / "trace.jsonl").read_text().splitlines()]
                self.assertEqual([records[0]["type"], records[-1]["type"]], ["thread.started", "turn.completed"])
            self.assertEqual(len(list((out / "results/candidates").iterdir())), 1)
            self.assertEqual(read_json(out / "status.json")["verifier"]["succeeded"], 1)
        self.assertFalse(list((self.base / "diagnostics").rglob("failure.json")))

    def test_resume_revalidates_the_native_session_even_with_matching_hashes(self):
        from common import sha256
        self.assertEqual(self.run_review(), 0)
        trace = next((self.base / "exhaustive-run/results/discovery").glob("*/trace.jsonl"))
        records = [json.loads(line) for line in trace.read_text().splitlines()]
        trace.write_text("".join(json.dumps(e) + "\n" for e in records if e.get("type") != "turn.completed"))
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
                                 'src/route1.cpp': {'always': 'exit'}}})
        self.assertEqual(self.run_review(), 1)
        out = self.base / 'exhaustive-run'
        diagnostic_failures = list((self.base / 'diagnostics').rglob('failure.json'))
        self.assertEqual(len(diagnostic_failures), 8)
        records = [read_json(path) for path in diagnostic_failures]
        self.assertEqual({r['category'] for r in records}, {'session', 'transport'})
        self.assertEqual({r['stage'] for r in records}, {'answer', 'invoke'})
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
        self.assertTrue(all(n['attempts'] == 3 and n['reason'] == 'cli_exit' for n in notices))

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

    def test_third_fresh_attempt_can_succeed(self):
        self.scenario({'codex': {'src/route0.cpp': {'failures': ['partial', 'exit']}}})
        self.assertEqual(self.run_review(), 0)
        out = self.base / 'exhaustive-run'
        for completion in (out / 'results/discovery').glob('*/completion.json'):
            if read_json(completion)['task']['path'] == 'src/route0.cpp':
                metadata = read_json(completion.parent / 'metadata.json')
                self.assertEqual(metadata['attempt'], 3)
                self.assertTrue(metadata['session_id'].endswith('-3'))
        self.assertEqual(len(list((self.base / 'diagnostics').rglob('failure.json'))), 4)
        self.assertFalse((out / 'notifications.jsonl').exists())

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

    def test_candidates_from_both_models_are_merged_and_verified_serially(self):
        a, b = finding("src/route0.cpp"), finding("src/route1.cpp", cause="Different parsing error")
        self.scenario({"codex": {"src/route0.cpp": {"candidates": [a]},
                                  "gpt-5.6-sol:src/route1.cpp": {"candidates": [b]}},
                       "claude": {"src/route1.cpp": {"status": "needs-validation"}}})
        self.assertEqual(self.run_review(), 0)
        out = self.base / "exhaustive-run"
        candidates = [read_json(p) for p in (out / "results/candidates").glob("*/candidate.json")]
        self.assertEqual(len(candidates), 2)
        self.assertEqual(sorted(len(c["occurrences"]) for c in candidates), [1, 2])
        self.assertEqual(sorted(len(c["models"]) for c in candidates), [1, 2])
        self.assertEqual(len(list((out / "results/candidates").glob("*/report.md"))), 2)
        validations = [read_json(p) for p in (out / "results/validation").glob("*/validation-input.json")]
        # Identical one-line claims from both models collapse into one claim.
        self.assertEqual(sorted(len(v["claims"]) for v in validations), [1, 1])
        verdicts = [read_json(p) for p in (out / "results/verifier").glob("*/result.json")]
        self.assertEqual({v["status"] for v in verdicts}, {"confirmed", "needs-validation"})
        confirmed = next(v for v in verdicts if v["status"] == "confirmed")
        self.assertEqual(confirmed["failed_gates"], [])
        self.assertIn("+++ ", confirmed["fix_patch"])
        events = [e for e in self.events() if e["kind"] == "claude"]
        self.assertEqual([e["event"] for e in events], ["start", "end", "start", "end"])

    def test_screened_findings_never_reach_the_verifier(self):
        self.scenario({"codex": {"src/route0.cpp": {"candidates": [finding("src/route0.cpp", defect_confidence=4)]},
                                 "src/route1.cpp": {"candidates": [finding("src/route1.cpp", fix_logic_lines=60)]},
                                 "src/route2.cpp": {"candidates": [finding("src/route2.cpp", files_touched=2)]},
                                 "src/route3.cpp": {"candidates": [finding("src/route3.cpp", wg_fit=False, fix_unique=False)]},
                                 "src/route4.cpp": {"candidates": [finding("src/route4.cpp")]}}})
        self.assertEqual(self.run_review(), 0)
        out = self.base / "exhaustive-run"
        screened = read_json(out / "results/screened.json")
        self.assertEqual(len(screened), 8)
        self.assertEqual({(s["path"], tuple(s["failed_gates"])) for s in screened},
                         {("src/route0.cpp", ("defect_confidence",)), ("src/route1.cpp", ("fix_logic_lines",)),
                          ("src/route2.cpp", ("files_touched",)), ("src/route3.cpp", ("wg_fit", "fix_unique"))})
        self.assertEqual({s["model"] for s in screened}, {"gpt-6-astra", "gpt-5.6-sol"})
        self.assertTrue(all(set(s) == {"task_id", "model", "path", "start_line", "end_line", "title", "failed_gates"}
                            for s in screened))
        candidates = [read_json(p) for p in (out / "results/candidates").glob("*/candidate.json")]
        self.assertEqual([c["path"] for c in candidates], ["src/route4.cpp"])
        self.assertEqual(len(candidates[0]["occurrences"]), 2)
        self.assertEqual(read_json(out / "status.json")["verifier"],
                         {"queued": 0, "running": 0, "succeeded": 1, "failed": 0})
        self.assertEqual([e["event"] for e in self.events() if e["kind"] == "claude"], ["start", "end"])

    def test_same_defect_merges_only_on_path_kind_and_overlapping_lines(self):
        self.scenario({"codex": {
            "gpt-6-astra:src/route0.cpp": {"candidates": [finding("src/route0.cpp", start=2, end=3)]},
            "gpt-5.6-sol:src/route0.cpp": {"candidates": [finding("src/route0.cpp", title="Different title",
                                                                   cause="Different sentence", start=3, end=4)]},
            "gpt-6-astra:src/route1.cpp": {"candidates": [finding("src/route1.cpp")]},
            "gpt-5.6-sol:src/route1.cpp": {"candidates": [finding("src/route1.cpp", kind="identity-typo")]},
            "gpt-6-astra:src/route2.cpp": {"candidates": [finding("src/route2.cpp", start=2, end=2)]},
            "gpt-5.6-sol:src/route2.cpp": {"candidates": [finding("src/route2.cpp", start=4, end=5)]}}})
        self.assertEqual(self.run_review(), 0)
        out = self.base / "exhaustive-run"
        candidates = {f"{c['path']}:{c['start_line']}-{c['end_line']}:{c['defect_kind']}": c
                      for c in (read_json(p) for p in (out / "results/candidates").glob("*/candidate.json"))}
        self.assertEqual(sorted(candidates), ["src/route0.cpp:2-4:prefix-vs-exact", "src/route1.cpp:2-2:identity-typo",
                                              "src/route1.cpp:2-2:prefix-vs-exact", "src/route2.cpp:2-2:prefix-vs-exact",
                                              "src/route2.cpp:4-5:prefix-vs-exact"])
        merged = candidates["src/route0.cpp:2-4:prefix-vs-exact"]
        self.assertEqual(merged["models"], ["gpt-5.6-sol", "gpt-6-astra"])
        self.assertEqual(len(merged["occurrences"]), 2)
        self.assertEqual({o["finding"]["title"] for o in merged["occurrences"]},
                         {"Prefix accepts wrong interface", "Different title"})
        for candidate in candidates.values():
            path = out / "results/validation" / candidate["id"] / "validation-input.json"
            validation = read_json(path)
            self.assertEqual(set(validation), {"id", "path", "start_line", "end_line", "defect_kind", "claims"})
            self.assertEqual(validation["id"], candidate["id"])
            self.assertEqual(len(validation["claims"]), len(candidate["occurrences"]))
            self.assertTrue(all(set(claim) == {"title", "root_cause"} for claim in validation["claims"]))
            raw = path.read_text()
            for private in ("delimiter guard", "Wrong route selected", "Compare the full key", "gpt-",
                            "trace.jsonl", "results/discovery", "confidence", "fix_"):
                self.assertNotIn(private, raw)
        self.assertEqual(read_json(out / "status.json")["verifier"]["succeeded"], 5)

    def test_verifier_sessions_are_blind_to_siblings_and_discovery_context(self):
        paths = [f"src/route{i}.cpp" for i in range(4)]
        self.scenario({"codex": {p: {"candidates": [finding(p)]} for p in paths}})
        self.assertEqual(self.run_review(), 0)
        out = (self.base / "exhaustive-run").resolve()
        prompts = [p.read_text() for p in sorted((out / "results/verifier").glob("*/prompt.txt"))]
        self.assertEqual(len(prompts), 4)
        for prompt in prompts:
            for leak in ("prior_candidates", "candidate.json", "report.md", "trace.jsonl", "results/discovery",
                         "results/verifier", "results/candidates", "delimiter guard", "Wrong route selected", "gpt-"):
                self.assertNotIn(leak, prompt)
            self.assertEqual(prompt.count("validation-input.json"), 1)
        sizes = [len(p.encode()) for p in prompts]
        self.assertLess(max(sizes) - min(sizes), 64)
        starts = [e for e in self.events() if e["kind"] == "claude" and e["event"] == "start"]
        self.assertEqual(len(starts), 4)
        for start in starts:
            argv = start["argv"]
            added = [argv[i + 1] for i, a in enumerate(argv) if a == "--add-dir"]
            self.assertEqual(len(added), 3)
            self.assertIn(str(self.repo.resolve()), added)
            self.assertIn(str(self.reference.resolve()), added)
            self.assertEqual([Path(a).parent for a in added if Path(a).parent == out / "results/validation"],
                             [out / "results/validation"])
            for forbidden in (out, out / "results", out / "results/verifier", out / "results/candidates",
                              out / "results/discovery"):
                self.assertNotIn(str(forbidden), added)
        self.assertEqual([e["event"] for e in self.events() if e["kind"] == "claude"], ["start", "end"] * 4)

    def test_inconsistent_verdicts_are_invalid_results_and_retried(self):
        paths = [f"src/route{i}.cpp" for i in range(3)]
        self.scenario({"codex": {p: {"candidates": [finding(p)]} for p in paths},
                       "claude": {paths[0]: {"inconsistent": True},
                                  paths[1]: {"status": "rejected", "failed_gates": ["defect_kind", "fix_unique"]},
                                  paths[2]: {"status": "needs-validation"}}})
        self.assertEqual(self.run_review(), 1)
        out = self.base / "exhaustive-run"
        self.assertEqual(read_json(out / "status.json")["verifier"],
                         {"queued": 0, "running": 0, "succeeded": 2, "failed": 1})
        notices = [json.loads(line) for line in (out / "notifications.jsonl").read_text().splitlines()]
        self.assertEqual([(n["lane"], n["attempts"], n["category"], n["stage"], n["reason"]) for n in notices],
                         [("verifier", 3, "session", "result_validation", "invalid_result")])
        verdicts = {read_json(p.parent / "completion.json")["task"]["path"]: read_json(p)
                    for p in (out / "results/verifier").glob("*/result.json")}
        self.assertEqual(verdicts[paths[1]]["status"], "rejected")
        self.assertEqual(verdicts[paths[1]]["failed_gates"], ["defect_kind", "fix_unique"])
        self.assertFalse(verdicts[paths[1]]["kind_fit"])
        self.assertEqual(verdicts[paths[2]]["status"], "needs-validation")
        self.assertEqual(verdicts[paths[2]]["failed_gates"], ["impact_reachable"])
        self.assertEqual(len([e for e in self.events() if e["kind"] == "claude" and e["event"] == "start"]), 5)
        before = self.events()
        self.assertEqual(self.run_review(), 1)
        self.assertEqual(self.events(), before)

    def test_verifier_cap_stops_before_any_verifier_session_and_resumes(self):
        from runner import run
        paths = [f"src/route{i}.cpp" for i in range(3)]
        self.scenario({"codex": {p: {"candidates": [finding(p)]} for p in paths}})
        out = self.base / "exhaustive-run"
        with self.assertRaises(ReviewError):
            run(self.base / "exhaustive", "exhaustive", out, self.reference, lock_root=self.base / "locks",
                diagnostics_root=self.base / "diagnostics", max_verifier_tasks=2)
        self.assertFalse([e for e in self.events() if e["kind"] == "claude"])
        self.assertEqual(len(list((out / "results/candidates").iterdir())), 3)
        self.assertEqual(len(list((out / "results/validation").iterdir())), 3)
        self.assertEqual(read_json(out / "results/screened.json"), [])
        self.assertEqual(read_json(out / "status.json")["state"], "running")
        codex_before = [e for e in self.events() if e["kind"] == "codex"]
        self.assertEqual(run(self.base / "exhaustive", "exhaustive", out, self.reference, lock_root=self.base / "locks",
                             diagnostics_root=self.base / "diagnostics", max_verifier_tasks=3), 0)
        self.assertEqual([e for e in self.events() if e["kind"] == "codex"], codex_before)
        self.assertEqual(read_json(out / "status.json")["verifier"]["succeeded"], 3)

    def test_worker_boundary_hides_sibling_outputs_and_exposes_only_inputs(self):
        import runner
        out = (self.base / 'exhaustive-run').resolve()
        out.mkdir()
        extra = Path(self.bin_temp.name) / 'shared-results'
        (extra / 'nested').mkdir(parents=True)
        hidden, exposed = runner.worker_boundary(out, self.repo.resolve(), self.reference.resolve(),
                                                 (self.base / 'exhaustive').resolve(), [extra, extra / 'nested', out])
        # Default: the run's parent directory. Nested hidden directories collapse into their parent.
        self.assertEqual(hidden, sorted({out.parent, extra.resolve()}))
        (self.base / 'inside').mkdir()
        self.assertEqual(runner.worker_boundary(out, self.repo.resolve(), self.reference.resolve(),
                                                (self.base / 'exhaustive').resolve(), [self.base / 'inside'])[0],
                         [out.parent])
        self.assertEqual(exposed, [self.repo.resolve(), self.reference.resolve(), (self.base / 'exhaustive').resolve()])
        with self.assertRaises(ReviewError):
            runner.worker_boundary(out, self.repo.resolve(), self.reference.resolve(),
                                   (self.base / 'exhaustive').resolve(), [self.base / 'does-not-exist'])
        with self.assertRaises(ReviewError):
            runner.worker_boundary(out, self.repo.resolve(), self.reference.resolve(),
                                   (self.base / 'exhaustive').resolve(), ['relative/path'])

    def test_hidden_scratch_or_cli_is_refused_before_any_model_call(self):
        from runner import run
        with self.assertRaises(ReviewError):
            run(self.base / 'exhaustive', 'exhaustive', self.base / 'exhaustive-run', self.reference,
                lock_root=self.base / 'locks', diagnostics_root=self.base / 'diagnostics',
                hide=[Path(tempfile.gettempdir())])
        self.assertFalse((self.state / 'events.jsonl').exists())
        self.assertFalse((self.base / 'exhaustive-run').exists())

    def test_each_worker_gets_its_own_boundary_with_only_its_inputs_exposed(self):
        import runner
        calls = []

        def recorder(root, hidden=(), exposed=()):
            calls.append((Path(root), list(hidden), list(exposed)))
            return []

        self.scenario({'codex': {'src/route0.cpp': {'candidates': [finding('src/route0.cpp')]}}})
        with mock.patch('runner.isolation_prefix', side_effect=recorder):
            self.assertEqual(self.run_review(), 0)
        out = (self.base / 'exhaustive-run').resolve()
        repo, reference, prepared = self.repo.resolve(), self.reference.resolve(), (self.base / 'exhaustive').resolve()
        discovery = [c for c in calls if c[2][-1] == prepared]
        verifier = [c for c in calls if c[2][-1] != prepared]
        self.assertEqual(len(discovery), 12)
        self.assertEqual(len(verifier), 1)
        self.assertTrue(all(hidden == [out.parent] and exposed[:2] == [repo, reference] for _, hidden, exposed in calls))
        candidate = next((out / 'results/candidates').iterdir()).name
        self.assertEqual(verifier[0][2], [repo, reference, out / 'results/validation' / candidate])
        self.assertTrue(all(root == (self.base / 'diagnostics').resolve() for root, _, _ in calls))

    def test_schemas_match_the_runner_gate_contract(self):
        import runner
        discovery = read_json(ROOT / "experiment/schemas/discovery.json")["properties"]["candidates"]["items"]
        self.assertTrue(set(runner.GATE_FIELDS) <= set(discovery["required"]))
        self.assertEqual(discovery["properties"]["defect_kind"]["enum"], runner.defect_kinds())
        verifier = read_json(ROOT / "experiment/schemas/verifier.json")
        self.assertEqual(verifier["properties"]["failed_gates"]["items"]["enum"], list(runner.GATES))
        self.assertEqual(verifier["properties"]["status"]["enum"], ["confirmed", "rejected", "needs-validation"])
        self.assertEqual(set(verifier["required"]), set(runner.VERDICT_FIELDS))
        # The verifier schema is sent to a structured-output API that supports only
        # types, enum, required and additionalProperties:false.
        for name, spec in verifier["properties"].items():
            self.assertTrue(set(spec) <= {"type", "enum", "items"}, name)

    def test_shell_run_supports_both_modes_and_explicit_queue(self):
        from review_queue import make_pilot_queue
        for mode in ('exhaustive', 'targeted'):
            queue = self.base / (mode + '-queue.json')
            make_pilot_queue(self.base / mode, mode, queue)
            command = ['/bin/bash', str(ROOT / 'experiment/run-experiment.sh'), 'run', mode,
                       '--prepared', str(self.base / mode), '--out', str(self.base / (mode + '-shell')),
                       '--reference', str(self.reference), '--diagnostics-root', str(self.base / 'diagnostics'),
                       '--max-verifier-tasks', '20', '--queue', str(queue)]
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

    def test_verifier_retry_failure_and_rejection_contract(self):
        paths = [f'src/route{i}.cpp' for i in range(3)]
        self.scenario({'codex': {p: {'candidates': [finding(p)]} for p in paths},
                       'claude': {paths[0]: {'first': 'partial'},
                                  paths[1]: {'status': 'rejected'},
                                  paths[2]: {'always': 'exit'}}})
        self.assertEqual(self.run_review(), 1)
        out = self.base / 'exhaustive-run'
        status = read_json(out / 'status.json')
        self.assertEqual(status['discovery']['succeeded'], 12)
        self.assertEqual(status['verifier']['succeeded'], 2)
        self.assertEqual(status['verifier']['failed'], 1)
        verdicts = [read_json(p) for p in (out / 'results/verifier').glob('*/result.json')]
        self.assertEqual({v['status'] for v in verdicts}, {'confirmed', 'rejected'})
        rejected = next(v for v in verdicts if v['status'] == 'rejected')
        self.assertEqual(rejected['failed_gates'], ['fix_unique'])
        self.assertEqual(rejected['fix_patch'], '')
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



class VerdictShapeTests(unittest.TestCase):
    """The consistency rule must accept a non-defect rejection that offers no fix, keep refusing unlisted
    failing gates, and still demand the patch for confirmed."""

    def verdict(self, **overrides):
        value = {"status": "rejected", "rationale": "Not a defect: no producer emits such a key.",
                 "failed_gates": ["impact_reachable", "defect_confidence", "remedy_confidence"],
                 "wg_fit": True, "kind_fit": True, "intention_clear": True, "impact_reachable": False,
                 "fix_patch": "", "fix_logic_lines": 0, "files_touched": 0, "fix_unique": True,
                 "defect_confidence": 1, "remedy_confidence": 2}
        value.update(overrides)
        return value

    def test_rejection_without_a_fix_is_consistent_but_confirmation_still_needs_a_patch(self):
        from review_queue import runner_config
        from runner import validate_answer
        config, task = runner_config(), {"path": "src/route0.cpp"}
        validate_answer(self.verdict(), Path("."), "verifier", task, config)
        validate_answer(self.verdict(files_touched=1), Path("."), "verifier", task, config)
        patch = "--- a/src/route0.cpp\n+++ b/src/route0.cpp\n@@ -2 +2 @@\n-auto x = key.find(\"Vrf\");\n+auto x = key == \"Vrf\";\n"
        for case, bad in (("unlisted confidence", self.verdict(defect_confidence=4, failed_gates=["impact_reachable", "remedy_confidence"])),
                          ("confirmed without patch", self.verdict(status="confirmed", failed_gates=[], impact_reachable=True,
                                                                   defect_confidence=5, remedy_confidence=5)),
                          ("patch offered but lines uncounted", self.verdict(fix_patch=patch, files_touched=1, fix_logic_lines=0)),
                          ("no fix listed as a failed gate", self.verdict(failed_gates=["impact_reachable", "defect_confidence",
                                                                                           "remedy_confidence", "fix_logic_lines"]))):
            with self.subTest(case=case), self.assertRaises(ReviewError):
                validate_answer(bad, Path("."), "verifier", task, config)
        good = self.verdict(status="confirmed", failed_gates=[], impact_reachable=True, defect_confidence=5,
                            remedy_confidence=5, fix_patch=patch, files_touched=1, fix_logic_lines=2)
        validate_answer(good, Path("."), "verifier", task, config)


class FindingShapeTests(unittest.TestCase):
    """A discovery answer must be the schema's shape and name real lines of the frozen source."""

    def test_wrong_candidate_fields_are_invalid_results(self):
        from review_queue import runner_config
        from runner import validate_answer
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            (repo / "src").mkdir()
            (repo / "src/route0.cpp").write_text(FIXTURE)
            config, task = runner_config(), {"path": "src/route0.cpp"}
            validate_answer({"candidates": []}, repo, "discovery", task, config)
            validate_answer({"candidates": [finding("src/route0.cpp")]}, repo, "discovery", task, config)
            good = finding("src/route0.cpp")
            cases = {"extra field": {**good, "trace": "x"},
                     "missing field": {k: v for k, v in good.items() if k != "impact"},
                     "empty text": finding("src/route0.cpp", root_cause=" "),
                     "unknown kind": finding("src/route0.cpp", kind="style"),
                     "text flag": finding("src/route0.cpp", wg_fit="true"),
                     "no file touched": finding("src/route0.cpp", files_touched=0),
                     "confidence above 5": finding("src/route0.cpp", defect_confidence=6),
                     "missing source": finding("src/missing.cpp"),
                     "outside source": finding("../route0.cpp"),
                     "lines past end": finding("src/route0.cpp", start=3, end=99),
                     "reversed lines": finding("src/route0.cpp", start=3, end=2)}
            for case, bad in cases.items():
                with self.subTest(case=case), self.assertRaises(ReviewError):
                    validate_answer({"candidates": [bad]}, repo, "discovery", task, config)
            for answer in ({"candidates": [], "notes": ""}, {"candidates": {}}, {}):
                with self.subTest(answer=answer), self.assertRaises(ReviewError):
                    validate_answer(answer, repo, "discovery", task, config)


if __name__ == "__main__":
    unittest.main()
