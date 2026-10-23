#!/usr/bin/env python3
"""Local test double for native CLIs. Never contacts a provider."""
import fcntl
import json
import os
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
    candidate = json.loads(Path(context["candidate"]).read_text())
    key = candidate["id"]
    path = candidate["finding"]["path"]
    model = sys.argv[sys.argv.index("--model") + 1]
    for occurrence in candidate["occurrences"]:
        assert Path(occurrence["trace"]).is_file()
        assert Path(occurrence["result"]).is_file()
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
failure = behavior.get("always") or (behavior.get("first") if attempt == 1 else None)
session = f"{kind}-{key}-{attempt}"
if kind == "codex":
    answer = {"candidates": behavior.get("candidates", [])}
    emit({"type": "thread.started", "thread_id": session})
    emit({"type": "turn.started"})
    if failure == "partial":
        emit({"type": "item.started", "item": {"type": "agent_message", "text": "INCOMPLETE-POISON"}})
        Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text('{"candidates": [')
    else:
        emit({"type": "item.completed", "item": {"type": "agent_message", "text": json.dumps(answer)}})
        emit({"type": "turn.completed", "usage": {"input_tokens": 100, "cached_input_tokens": 20, "output_tokens": 10}})
        Path(sys.argv[sys.argv.index("--output-last-message") + 1]).write_text(json.dumps(answer))
else:
    verdict = {"status": behavior.get("status", "confirmed"), "rationale": "Checked source and callers.",
               "duplicate_of": behavior.get("duplicate_of")}
    if verdict["duplicate_of"] == "previous":
        verdict["duplicate_of"] = context["prior_candidates"][0]["id"]
    emit({"type": "system", "subtype": "init", "session_id": session, "model": model})
    if failure != "partial":
        emit({"type": "result", "subtype": "success", "is_error": False, "session_id": session,
              "structured_output": verdict, "usage": {"input_tokens": 70, "cache_read_input_tokens": 10},
              "modelUsage": {model: {"inputTokens": 70, "outputTokens": 9}}, "total_cost_usd": 0.01})
event("end")
sys.exit(1 if failure == "exit" else 0)
