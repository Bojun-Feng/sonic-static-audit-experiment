#!/usr/bin/env python3
"""Local test double for native CLIs. Never contacts a provider."""
import fcntl
import json
import os
import shlex
import subprocess
from pathlib import Path
import sys
import time

clock_origin = time.monotonic_ns()
kind = Path(sys.argv[0]).name
if "--version" in sys.argv:
    print("codex-cli 0.157.1" if kind == "codex" else "2.1.302 (Claude Code)")
    sys.exit(0)

state = Path(sys.argv[0]).resolve().parent / "state"
scenario = json.loads((state / "scenario.json").read_text())
prompt = sys.stdin.read()
marker = "Review context:\n" if kind == "codex" else "Verification context:\n"
context = json.loads(prompt.split(marker, 1)[1])
if kind == "codex":
    task = context["task"]
    key = task["task_id"]
    path = task["path"]
    model = sys.argv[sys.argv.index("--model") + 1]
    assert model == task["model"]
    assert (Path(context["source_root"]) / path).is_file()
    assert Path(context["reference_root"]).is_dir()
else:
    # A blinded verifier sees one validation input: no sibling verdicts, no
    # discovery evidence, no trace or result paths, and no other results tree.
    assert set(context) == {"validation_input", "source_root", "reference_root"}, sorted(context)
    assert "prior_candidates" not in prompt
    validation_path = Path(context["validation_input"])
    validation = json.loads(validation_path.read_text())
    assert set(validation) == {"id", "path", "start_line", "end_line", "defect_kind", "claims"}, sorted(validation)
    assert all(set(claim) == {"title", "root_cause"} for claim in validation["claims"])
    key = validation["id"]
    path = validation["path"]
    model = sys.argv[sys.argv.index("--model") + 1]
    added = [sys.argv[i + 1] for i, arg in enumerate(sys.argv) if arg == "--add-dir"]
    assert str(validation_path.parent) in added, added
    results = validation_path.parent.parent.parent
    assert not any(Path(a) == results or Path(a) in (results / "verifier", results / "candidates", results / "discovery")
                   for a in added), added
    assert (Path(context["source_root"]) / path).is_file()

with (state / "count.lock").open("a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    counts_path = state / "counts.json"
    counts = json.loads(counts_path.read_text()) if counts_path.exists() else {}
    counts[key] = counts.get(key, 0) + 1
    attempt = counts[key]
    counts_path.write_text(json.dumps(counts))


def event(name):
    with (state / "events.jsonl").open("a") as handle:
        fcntl.flock(handle, fcntl.LOCK_EX)
        handle.write(json.dumps({"event": name, "kind": kind, "task": key, "model": model,
                                 "time": time.monotonic_ns() - (clock_origin if scenario.get("process_relative_clock") else 0),
                                 "pid": os.getpid(), "cwd": os.getcwd(),
                                 "argv": sys.argv[1:], "attempt": attempt}) + "\n")


def emit(value):
    print(json.dumps(value), flush=True)


event("start")
if kind == "codex" and scenario.get("start_gate"):
    # The first wave must occupy all requested slots before any CLI can finish.
    # Read under the same lock as writers; never use cross-process timestamps.
    deadline = time.monotonic() + 10
    while True:
        with (state / "events.jsonl").open() as handle:
            fcntl.flock(handle, fcntl.LOCK_SH)
            events = [json.loads(line) for line in handle]
        starts = [e for e in events if e["kind"] == kind and e["event"] == "start"
                  and (scenario.get("shared_gate") or e["task"].startswith(task["mode"] + "--"))]
        if len(starts) >= scenario["start_gate"]:
            break
        if time.monotonic() > deadline:
            raise RuntimeError("test start gate timeout")
        time.sleep(0.01)
time.sleep(scenario.get("sleep", 0.06))
behavior = scenario.get(kind, {}).get(model + ":" + path, scenario.get(kind, {}).get(path, {}))
failures = behavior.get('failures', [])
failure = behavior.get("always") or (failures[attempt - 1] if attempt <= len(failures) else
                                     behavior.get("first") if attempt == 1 else None)
session = f"{kind}-{key}-{attempt}"
if kind == "codex":
    answer = {"candidates": behavior.get("candidates", [])}
    emit({"type": "thread.started", "thread_id": session})
    emit({"type": "turn.started"})
    if failure != "passive":
        # Execute real read-only host commands against disposable source fixtures.
        # grep -F is the portable local stand-in for rg's literal line search;
        # retained events use the native rg command form.
        root = Path(context["source_root"])
        commands = [(["cat", "--", str(root / path)], None),
                    (["rg", "-n", "-F", "--", "key", str(root)],
                     ["grep", "-r", "-n", "-F", "--", "key", str(root)])]
        for number, (argv, local) in enumerate(commands):
            result = subprocess.run(local or argv, capture_output=True, text=True, check=True)
            # The native CLI sometimes records a completed search with exit 0 and no output.
            output = "" if number and behavior.get("search_output") == "lost" else result.stdout
            emit({"type": "item.completed", "item": {"id": f"command-{number}",
                  "type": "command_execution", "command": shlex.join(argv),
                  "status": "completed", "exit_code": result.returncode,
                  "aggregated_output": output}})
    if failure == "partial":
        emit({"type": "item.started", "item": {"type": "agent_message", "text": "INCOMPLETE-MARKER"}})
        Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text('{"candidates": [')
    else:
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(answer)}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10}})
        Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text(json.dumps(answer))
else:
    status = behavior.get("status", "confirmed")
    default_failed = {"confirmed": [], "rejected": ["fix_unique"], "needs-validation": ["impact_reachable"]}[status]
    failed = behavior.get("failed_gates", default_failed)
    patch = (f"--- a/{path}\n+++ b/{path}\n@@ -2,1 +2,1 @@\n"
             '-auto x = key.find("Vrf");\n+auto x = (key == "Vrf") ? 0 : std::string::npos;\n')
    verdict = {"status": status, "rationale": f"Checked {path}:{validation['start_line']} and its callers by reading only.",
               "failed_gates": failed,
               "wg_fit": "wg_fit" not in failed, "kind_fit": "defect_kind" not in failed,
               "intention_clear": "intention_clear" not in failed, "impact_reachable": "impact_reachable" not in failed,
               "fix_unique": "fix_unique" not in failed,
               "files_touched": 2 if "files_touched" in failed else 1,
               "fix_logic_lines": 80 if "fix_logic_lines" in failed else 2,
               "defect_confidence": 3 if "defect_confidence" in failed else 5,
               "remedy_confidence": 3 if "remedy_confidence" in failed else 5,
               "fix_patch": patch if status == "confirmed" else ""}
    if behavior.get("inconsistent"):
        # A confirmed verdict that also names a failed gate must be refused by the runner.
        verdict["failed_gates"] = ["fix_unique"]
    emit({"type": "system", "subtype": "init", "session_id": session, "model": model,
          "tools": ["Glob", "Grep", "Read", "StructuredOutput"]})
    # Native-style tool events: what the session read, and whether the OS boundary let it.
    reads = behavior.get("reads", [str(validation_path), str(Path(context["source_root"]) / path)])
    for number, target in enumerate(reads + behavior.get("peek", [])):
        use_id = f"toolu_{number}"
        emit({"type": "assistant", "message": {"content": [{"type": "tool_use", "id": use_id, "name": "Read",
                                                             "input": {"file_path": target}}]}})
        try:
            text = f"READ {len(Path(target).read_bytes())} bytes from {target}"
        except OSError as exc:
            text = f"DENIED {type(exc).__name__} {target}"
        emit({"type": "user", "message": {"content": [{"type": "tool_result", "tool_use_id": use_id,
                                                        "content": text + behavior.get("result_suffix", "")}]}})
    if failure != "partial":
        emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
              "structured_output": verdict, "usage": {"input_tokens": 70, "cache_read_input_tokens": 10},
              "modelUsage": {model: {"inputTokens": 70, "outputTokens": 9}}, "total_cost_usd": 0.01,
              "duration_ms": 1200})
event("end")
sys.exit(2 if failure == 'usage' else 1 if failure == "exit" else 0)
