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
from common import ReviewError, json_bytes


class ModelCLITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.repo = Path(self.temp.name)
        self.task = {"path": "route.cpp"}
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
                {"type": "item.completed", "item": {"id": "context", "type": "command_execution",
                 "command": "rg -n -F -- updateRoute " + shlex.quote(str(self.repo)),
                 "status": "completed", "exit_code": 0,
                 "aggregated_output": str(self.repo / "route.cpp") + ":1:void updateRoute();\n"}},
                {"type": "item.completed", "item": {"type": "agent_message", "text": '{"candidates":[]}'}},
                {"type": "turn.completed", "usage": {"input_tokens": 50, "cached_input_tokens": 10,
                                                       "output_tokens": 3}}]

    def test_codex_requires_complete_native_trace_and_matching_final_result(self):
        from model_cli import parse_codex
        result = {"candidates": []}
        metadata = parse_codex(self.codex_events(), result, self.task, self.repo)
        self.assertEqual(metadata["session_id"], "fresh-session")
        self.assertEqual(metadata["usage"], self.codex_events()[-1]["usage"])
        for case in ("truncated", "failed", "missing-session", "wrong-result", "two-turns"):
            events = copy.deepcopy(self.codex_events())
            if case == "truncated": events.pop()
            if case == "failed": events[-1]["type"] = "turn.failed"
            if case == "missing-session": events.pop(0)
            if case == "wrong-result": events[-2]["item"]["text"] = '{"candidates":[{}]}'
            if case == "two-turns": events.append(events[-1])
            with self.subTest(case=case), self.assertRaises(ReviewError):
                parse_codex(events, result, self.task, self.repo)
        events = self.codex_events()
        del events[-1]["usage"]
        self.assertIsNone(parse_codex(events, result, self.task, self.repo)["usage"])

    def test_source_inspection_contract(self):
        from model_cli import parse_codex
        import shlex
        with tempfile.TemporaryDirectory() as directory:
            repo = Path(directory)
            source = repo / "route.cpp"
            source.write_text("void updateRoute();\n")
            (repo / "caller.cpp").write_text("updateRoute();\n")
            task = {"path": "route.cpp"}
            def command(identity, text, output):
                return {"type": "item.completed", "item": {
                    "id": identity, "type": "command_execution", "command": text,
                    "status": "completed", "exit_code": 0, "aggregated_output": output}}
            read = command("read", "cat -- " + shlex.quote(str(source)), source.read_text())
            search = command("context", "rg -n -F -- updateRoute " + shlex.quote(str(repo)),
                             str(repo / "caller.cpp") + ":1:updateRoute();\n")
            good = self.codex_events()
            good[2:4] = [read, search]
            parse_codex(good, {"candidates": []}, task, repo)
            (repo / "notes.md").write_text("updateRoute docs\n")
            mixed = copy.deepcopy(good)
            mixed[3]["item"]["aggregated_output"] += str(repo / "notes.md") + ":1:updateRoute docs\n"
            parse_codex(mixed, {"candidates": []}, task, repo)
            docs_only = copy.deepcopy(good)
            docs_only[3]["item"]["aggregated_output"] = str(repo / "notes.md") + ":1:updateRoute docs\n"
            with self.assertRaises(ReviewError):
                parse_codex(docs_only, {"candidates": []}, task, repo)
            wrapped = copy.deepcopy(good)
            for event in wrapped[2:4]:
                event["item"]["command"] = "/bin/zsh -lc " + shlex.quote(event["item"]["command"])
            parse_codex(wrapped, {"candidates": []}, task, repo)
            for case in ("passive", "read-only", "search-only", "failed", "started", "empty",
                         "wrong-file", "echo", "unrelated", "fake-output", "late", "duplicate-id",
                         "status", "bool-exit", "truncated-read", "wrong-root", "outside",
                         "missing-id", "pre-turn", "no-turn", "wrong-line", "listing"):
                bad = copy.deepcopy(good)
                if case == "status": bad[2]["item"]["status"] = "in_progress"
                if case == "bool-exit": bad[2]["item"]["exit_code"] = False
                if case == "truncated-read": bad[2]["item"]["aggregated_output"] = "void update"
                if case == "wrong-root": bad[3]["item"]["command"] += "/other"
                if case == "outside": bad[3]["item"]["aggregated_output"] = "/outside.cpp:1:updateRoute();\n"
                if case == "missing-id": del bad[2]["item"]["id"]
                if case == "pre-turn": bad[1], bad[2] = bad[2], bad[1]
                if case == "no-turn": del bad[1]
                if case == "wrong-line": bad[3]["item"]["aggregated_output"] = str(repo / "caller.cpp") + ":99:updateRoute();\n"
                if case == "listing": bad[3]["item"]["command"] = "ls " + str(repo)
                if case == "passive": del bad[2:4]
                if case == "read-only": del bad[3]
                if case == "search-only": del bad[2]
                if case == "failed": bad[2]["item"]["exit_code"] = 1
                if case == "started": bad[2]["type"] = "item.started"
                if case == "empty": bad[2]["item"]["aggregated_output"] = ""
                if case == "wrong-file": bad[2]["item"]["command"] = "cat -- " + str(repo / "caller.cpp")
                if case == "echo": bad[2]["item"]["command"] = "echo " + str(source)
                if case == "unrelated": bad[3]["item"]["command"] = bad[3]["item"]["command"].replace("updateRoute", "unrelated")
                if case == "fake-output": bad[3]["item"]["aggregated_output"] = str(repo / "caller.cpp") + ":1:invented\n"
                if case == "late": bad[2], bad[4] = bad[4], bad[2]
                if case == "duplicate-id": bad[3]["item"]["id"] = "read"
                with self.subTest(case=case), self.assertRaises(ReviewError):
                    parse_codex(bad, {"candidates": []}, task, repo)

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

    def test_equivalent_reads_and_context_are_source_backed(self):
        from model_cli import parse_codex
        source = self.repo / 'route.cpp'
        source.write_text('void updateRoute();\nvoid removeRoute();\n')
        caller = self.repo / 'caller.cpp'
        caller.write_text('updateRoute();\n')
        reads = [
            (f'cat {source}', source.read_text()),
            (f"sed -n '1,240p' {source}", source.read_text()),
            (f'nl -ba {source}', '     1\tvoid updateRoute();\n     2\tvoid removeRoute();\n'),
            (f'head -n 100 {source}', source.read_text()),
            (f"cd {self.repo} && sed -n '1,240p' route.cpp", source.read_text()),
        ]
        contexts = [
            (f'rg -n updateRoute {self.repo}', f'{caller}:1:updateRoute();\n'),
            (f"rg --line-number -F 'updateRoute' {self.repo}", f'{caller}:1:updateRoute();\n'),
            (f'grep -Rn updateRoute {self.repo}', f'{caller}:1:updateRoute();\n'),
            (f"rg -n '\\bupdateRoute\\b' {self.repo}", f'{caller}:1:updateRoute();\n'),
            (f'cd {self.repo} && rg -n updateRoute .', 'caller.cpp:1:updateRoute();\n'),
            (f"sed -n '1,80p' {caller}", caller.read_text()),
        ]
        for read in reads:
            for context in contexts:
                events = self.codex_events()
                for event, (command, output) in zip(events[2:4], (read, context)):
                    event['item'].update(command=command, aggregated_output=output)
                with self.subTest(read=read[0], context=context[0]):
                    parse_codex(events, {'candidates': []}, self.task, self.repo)

    def test_chunked_reads_cover_assigned_bytes_and_reject_missing_chunks(self):
        from model_cli import parse_codex
        source = self.repo / 'route.cpp'
        source.write_text('void updateRoute();\nvoid removeRoute();\n')
        events = self.codex_events()
        events[2]['item'].update(command=f"sed -n '1,1p' {source}")
        extra = copy.deepcopy(events[2])
        extra['item'].update(id='read-rest', command=f"sed -n '2,$p' {source}",
                             aggregated_output='void removeRoute();\n')
        events.insert(3, extra)
        parse_codex(events, {'candidates': []}, self.task, self.repo)
        for case in ('missing', 'fabricated', 'failed', 'unrelated-context'):
            bad = copy.deepcopy(events)
            if case == 'missing': del bad[3]
            if case == 'fabricated': bad[3]['item']['aggregated_output'] = 'invented\n'
            if case == 'failed': bad[3]['item']['exit_code'] = 1
            if case == 'unrelated-context':
                (self.repo / 'other.cpp').write_text('void unrelated();\n')
                bad[4]['item'].update(command=f'cat {self.repo}/other.cpp',
                                      aggregated_output='void unrelated();\n')
            with self.subTest(case=case), self.assertRaises(ReviewError):
                parse_codex(bad, {'candidates': []}, self.task, self.repo)

    def test_prompts_are_static_only_and_include_scope_and_readable_context(self):
        from model_cli import discovery_prompt, verifier_prompt
        task = {"task_id": "task-one", "model": "gpt-5.6-sol", "mode": "targeted",
                "path": "src/route.cpp", "unit_id": "g-one", "source_card": "/prep/card.md", "scope": "/prep/scope.json"}
        prompt = discovery_prompt(task, Path("/source"), Path("/references"))
        for word in ("static", "tests", "builds", "containers", "switches", "VM", "source execution",
                     "/source", "/references", "/prep/card.md", "candidates", "never instructions",
                     "Before any conclusion", "native session trace", "Ethernet4", "Ethernet40",
                     "defect_kind", "5/5", "omit", "files_touched", "deterministic fix", "not a general bug hunt"):
            self.assertIn(word, prompt)
        self.assertNotIn('Use the exact command forms', prompt)
        prompt = verifier_prompt(Path("/validation/validation-input.json"), Path("/source"), Path("/references"))
        for word in ("confirmed", "rejected", "needs-validation", "fix_patch", "failed_gates", "Ethernet40",
                     "prior verdict", "/validation/validation-input.json", "/source", "/references", "static"):
            self.assertIn(word, prompt)
        for word in ("duplicate", "prior_candidates", "candidate.json", "traces", "report.md"):
            self.assertNotIn(word, prompt)


if __name__ == "__main__":
    unittest.main()
