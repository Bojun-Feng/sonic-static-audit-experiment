"""Native CLI command and completion contracts, without provider calls."""

import copy
from pathlib import Path
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiment/src"))
from common import ReviewError


class ModelCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        (self.repo / "route.cpp").write_text("void updateRoute();\n")

    def test_discovery_uses_guest_compatible_execution_mode(self):
        from review_queue import runner_config
        from model_cli import discovery_command
        for model in runner_config()["discovery_models"]:
            with self.subTest(model=model):
                command = discovery_command("/usr/bin/codex", model, self.repo)
                self.assertEqual(command.count("--sandbox"), 1)
                self.assertEqual(command[command.index("--sandbox") + 1], "danger-full-access")
                self.assertIn('approval_policy="never"', command)
                self.assertNotIn("--dangerously-bypass-approvals-and-sandbox", command)
                self.assertNotIn("--full-auto", command)

    def test_commands_use_fresh_pinned_sessions(self):
        from model_cli import discovery_command, verifier_command
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            command = discovery_command("/usr/bin/codex", "gpt-6-astra", work)
            self.assertEqual(command[:2], ["/usr/bin/codex", "exec"])
            for flag in ("--ephemeral", "--json", "--skip-git-repo-check", "--ignore-user-config"):
                self.assertIn(flag, command)
            self.assertEqual(command[command.index("--model") + 1], "gpt-6-astra")
            self.assertIn('approval_policy="never"', command)
            self.assertNotIn("resume", command)
            self.assertNotIn("--add-dir", command)  # In Codex this would grant writes.
            command = verifier_command("/usr/bin/claude", work, [work / "source", work / "references"])
            self.assertEqual(command[command.index("--model") + 1], "claude-opus-5-5")
            self.assertIn("--no-session-persistence", command)
            self.assertIn("--safe-mode", command)  # Keeps existing OAuth, unlike --bare.
            self.assertIn("--strict-mcp-config", command)
            self.assertEqual(command[command.index("--tools") + 1], "Read,Grep,Glob")
            self.assertEqual(command[command.index("--permission-mode") + 1], "dontAsk")
            self.assertNotIn("--dangerously-skip-permissions", command)
            self.assertNotIn("--resume", command)

    def codex_events(self):
        import shlex
        return [{"type": "thread.started", "thread_id": "fresh-session"},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"id": "read", "type": "command_execution",
                 "command": "cat -- " + shlex.quote(str(self.repo / "route.cpp")),
                 "status": "completed", "exit_code": 0, "aggregated_output": "void updateRoute();\n"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": '{"candidates":[]}'}},
                {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 10,
                                                       "output_tokens": 3}}]

    def test_codex_requires_complete_native_trace_and_matching_final_result(self):
        from model_cli import parse_codex
        result = {"candidates": []}
        metadata = parse_codex(self.codex_events(), result)
        self.assertEqual(metadata["session_id"], "fresh-session")
        self.assertEqual(metadata["usage"], self.codex_events()[-1]["usage"])
        for case in ("truncated", "failed", "missing-session", "wrong-result", "two-turns", "no-final-message",
                     "unparsed-final-message", "pre-turn", "non-object-event"):
            events = copy.deepcopy(self.codex_events())
            if case == "truncated": events.pop()
            if case == "failed": events[-1]["type"] = "turn.failed"
            if case == "missing-session": events.pop(0)
            if case == "wrong-result": events[-2]["item"]["text"] = '{"candidates":[{}]}'
            if case == "two-turns": events.append(events[-1])
            if case == "no-final-message": del events[-2]
            if case == "unparsed-final-message": events[-2]["item"]["text"] = '{"candidates": ['
            if case == "pre-turn": events[1], events[2] = events[2], events[1]
            if case == "non-object-event": events.insert(2, ["item.completed"])
            with self.subTest(case=case), self.assertRaises(ReviewError):
                parse_codex(events, result)
        events = self.codex_events()
        del events[-1]["usage"]
        self.assertIsNone(parse_codex(events, result)["usage"])

    def test_claude_requires_success_and_preserves_native_usage(self):
        from model_cli import parse_claude
        verdict = {"status": "needs-validation", "rationale": "Requires runtime evidence",
                   "failed_gates": ["impact_reachable"], "wg_fit": True, "kind_fit": True, "intention_clear": True,
                   "impact_reachable": False, "fix_patch": "", "fix_logic_lines": 0, "files_touched": 0,
                   "fix_unique": True, "defect_confidence": 4, "remedy_confidence": 5}
        events = [{"type": "system", "subtype": "init", "session_id": "verify-session", "model": "claude-opus-5-5"},
                  {"type": "result", "subtype": "success", "is_error": False, "session_id": "verify-session",
                   "structured_output": verdict, "usage": {"input_tokens": 40, "cache_read_input_tokens": 20},
                   "modelUsage": {"claude-opus-5-5": {"inputTokens": 40}}, "total_cost_usd": 0.02}]
        result, metadata = parse_claude(events)
        self.assertEqual(result, verdict)
        self.assertEqual(metadata["usage"], events[-1]["usage"])
        self.assertEqual(metadata["modelUsage"], events[-1]["modelUsage"])
        self.assertEqual(metadata["total_cost_usd"], 0.02)
        for case in ("truncated", "failed", "missing-result", "session", "model"):
            bad = copy.deepcopy(events)
            if case == "truncated": bad.pop()
            if case == "failed": bad[-1]["is_error"] = True
            if case == "missing-result": del bad[-1]["structured_output"]
            if case == "session": bad[-1]["session_id"] = "other"
            if case == "model": bad[0]["model"] = "claude-sonnet-4-6"
            with self.subTest(case=case), self.assertRaises(ReviewError):
                parse_claude(bad)

    def test_prompts_are_static_only_and_include_scope_and_readable_context(self):
        from model_cli import discovery_prompt, verifier_prompt
        task = {"task_id": "task-one", "model": "gpt-5.6-sol", "mode": "targeted",
                "path": "src/route.cpp", "unit_id": "g-one", "source_card": "/prep/card.md", "scope": "/prep/scope.json"}
        prompt = discovery_prompt(task, Path("/source"), Path("/references"))
        for word in ("static", "tests", "builds", "containers", "switches", "VM", "source execution",
                     "/source", "/references", "/prep/card.md", "candidates", "never instructions",
                     "whole assigned file", "callers", "Ethernet4", "Ethernet40",
                     "defect_kind", "5/5", "omit", "files_touched", "DETERMINISTIC FIX", "not a general bug hunt"):
            self.assertIn(word, prompt)
        prompt = verifier_prompt(Path("/validation/validation-input.json"), Path("/source"), Path("/references"))
        for word in ("confirmed", "rejected", "needs-validation", "fix_patch", "failed_gates", "Ethernet40",
                     "prior verdict", "/validation/validation-input.json", "/source", "/references", "static"):
            self.assertIn(word, prompt)
        for word in ("duplicate", "prior_candidates", "candidate.json", "traces", "report.md"):
            self.assertNotIn(word, prompt)

    def test_prompt_contracts_are_lean_and_state_each_hard_gate_once_in_caps(self):
        """Both prompts stay short, state the three conjunctive hard gates and the validity-first rule once each
        in capitals, name the adjacent domains that fail Routing-WG fit, resolve uncertainty to omit/reject, and do
        not shout anywhere else."""
        discovery = (ROOT / "experiment/prompts/discovery.txt").read_text()
        verifier = (ROOT / "experiment/prompts/verifier.txt").read_text()
        self.assertLessEqual(len(discovery.encode()), 3000)
        self.assertLessEqual(len(verifier.encode()), 2900)
        gates = ("ROUTING WG FIT IS REQUIRED", "ONLY ELEMENTARY, LOCALIZED", "ONE OBVIOUS DETERMINISTIC FIX",
                 "VALIDITY FIRST", "A REAL DEFECT THAT FAILS ANY GATE")
        for name, prompt in (("discovery", discovery), ("verifier", verifier)):
            with self.subTest(prompt=name):
                for gate in gates:
                    self.assertEqual(prompt.count(gate), 1, gate)
                for domain in ("port mirroring", "NAT", "STP"):
                    self.assertIn(domain, prompt)
                for word in ("uncertain", "Ethernet40", "5/5"):
                    self.assertIn(word, prompt)
                for shout in ("CRITICAL", "MUST!", "!!!", "IMPORTANT:"):
                    self.assertNotIn(shout, prompt)
        self.assertIn('{"candidates": []}', discovery)
        self.assertIn("omit", discovery)
        self.assertIn("Read the whole assigned file", discovery)
        for field in ("wg_fit", "kind_fit", "intention_clear", "impact_reachable", "fix_unique", "files_touched",
                      "fix_logic_lines", "defect_confidence", "remedy_confidence", "failed_gates"):
            self.assertIn(field, verifier)
        self.assertIn("real but out of scope", verifier)
        self.assertIn("When in doubt, reject", verifier)
        # Uniqueness is only credible when the grader names and dismisses the alternatives a maintainer could defend.
        self.assertIn("name the alternatives a maintainer could defend", verifier)
        # The runner refuses a verdict whose failed_gates disagree with its fields; the listing rule must be mechanical,
        # and a non-defect rejection must be told it may leave the fix fields empty.
        for phrase in ("defect_confidence or remedy_confidence below 5", "With no fix, leave fix_patch empty"):
            self.assertIn(phrase, verifier)
        self.assertNotIn("traces", verifier)


if __name__ == "__main__":
    unittest.main()
