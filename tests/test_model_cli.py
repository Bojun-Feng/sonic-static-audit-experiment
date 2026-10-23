"""Native CLI command and completion contracts, without provider calls."""

import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "experiment/src"))
from common import AuditError, json_bytes


class ModelCLITests(unittest.TestCase):
    def test_commands_use_fresh_pinned_read_only_sessions(self):
        from model_cli import discovery_command, verifier_command
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            command = discovery_command("/usr/bin/codex", "gpt-6-astra", work)
            self.assertEqual(command[:2], ["/usr/bin/codex", "exec"])
            for flag in ("--ephemeral", "--json", "--skip-git-repo-check", "--ignore-user-config"):
                self.assertIn(flag, command)
            self.assertEqual(command[command.index("--model") + 1], "gpt-6-astra")
            self.assertEqual(command[command.index("--sandbox") + 1], "read-only")
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
        return [{"type": "thread.started", "thread_id": "fresh-session"},
                {"type": "turn.started"},
                {"type": "item.completed", "item": {"type": "agent_message", "text": '{"candidates":[]}'}},
                {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 10,
                                                       "output_tokens": 3}}]

    def test_codex_requires_complete_native_trace_and_matching_final_result(self):
        from model_cli import parse_codex
        result = {"candidates": []}
        metadata = parse_codex(self.codex_events(), result)
        self.assertEqual(metadata["session_id"], "fresh-session")
        self.assertEqual(metadata["usage"], self.codex_events()[-1]["usage"])
        for case in ("truncated", "failed", "missing-session", "wrong-result", "two-turns"):
            events = copy.deepcopy(self.codex_events())
            if case == "truncated": events.pop()
            if case == "failed": events[-1]["type"] = "turn.failed"
            if case == "missing-session": events.pop(0)
            if case == "wrong-result": events[-2]["item"]["text"] = '{"candidates":[{}]}'
            if case == "two-turns": events.append(events[-1])
            with self.subTest(case=case), self.assertRaises(AuditError):
                parse_codex(events, result)
        events = self.codex_events()
        del events[-1]["usage"]
        self.assertIsNone(parse_codex(events, result)["usage"])

    def test_claude_requires_success_and_preserves_native_usage(self):
        from model_cli import parse_claude
        verdict = {"status": "needs-validation", "rationale": "Requires runtime evidence", "duplicate_of": None}
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
            with self.subTest(case=case), self.assertRaises(AuditError):
                parse_claude(bad)

    def test_prompts_are_static_only_and_include_scope_and_readable_context(self):
        from model_cli import discovery_prompt, verifier_prompt
        task = {"task_id": "task-one", "model": "gpt-5.6-sol", "mode": "targeted",
                "path": "src/route.cpp", "unit_id": "g-one", "source_card": "/prep/card.md", "scope": "/prep/scope.json"}
        prompt = discovery_prompt(task, Path("/source"), Path("/references"))
        for word in ("static", "tests", "builds", "containers", "switches", "VM", "source execution",
                     "/source", "/references", "/prep/card.md", "candidates", "untrusted"):
            self.assertIn(word, prompt)
        prompt = verifier_prompt(Path("/candidate/candidate.json"), Path("/source"), Path("/references"), [])
        for word in ("confirmed", "rejected", "duplicate", "needs-validation", "traces", "/candidate/candidate.json"):
            self.assertIn(word, prompt)


if __name__ == "__main__":
    unittest.main()
