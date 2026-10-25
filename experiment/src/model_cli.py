"""Pinned standalone CLI adapters. No authentication or provider SDK calls."""

import json

from review_queue import EXPERIMENT, runner_config
from common import AttemptError, ReviewError, json_bytes, require


def discovery_command(binary, model, work):
    require(model in runner_config()["discovery_models"], "unconfigured discovery model")
    return [binary, "exec", "--model", model, "--ephemeral", "--json",
            "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
            "--sandbox", "danger-full-access", "--color", "never", "--cd", str(work),
            "-c", 'model_reasoning_effort="max"',
            "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
            "-c", "features.multi_agent=false", "-c", "features.apps=false",
            "-c", "project_doc_max_bytes=0", "-c", "check_for_update_on_startup=false",
            "--output-schema", str(EXPERIMENT / "schemas/discovery.json"),
            "--output-last-message", str(work / "answer.json"), "-"]


def verifier_command(binary, work, readable):
    return [binary, "--print", "--model", runner_config()["verifier_model"], "--effort", "max",
            "--output-format", "stream-json", "--verbose", "--no-session-persistence",
            "--safe-mode", "--strict-mcp-config", "--mcp-config", '{"mcpServers":{}}',
            "--settings", '{"disableAllHooks":true}', "--setting-sources", "",
            "--tools", "Read,Grep,Glob", "--allowedTools", "Read,Grep,Glob",
            "--permission-mode", "dontAsk", "--disable-slash-commands",
            "--json-schema", (EXPERIMENT / "schemas/verifier.json").read_text(),
            *[part for path in readable for part in ("--add-dir", str(path))]]


def discovery_prompt(task, repo, reference):
    context = {"task": task, "source_root": str(repo), "reference_root": str(reference)}
    return (EXPERIMENT / "prompts/discovery.txt").read_text() + "\nReview context:\n" + json_bytes(context).decode()


def verifier_prompt(validation_input, repo, reference):
    """Blinded validation prompt: one validation input, no prior verdicts or discovery context."""
    context = {"validation_input": str(validation_input), "source_root": str(repo), "reference_root": str(reference)}
    return (EXPERIMENT / "prompts/verifier.txt").read_text() + "\nVerification context:\n" + json_bytes(context).decode()



def parse_codex(events, answer):
    require(events and all(isinstance(e, dict) for e in events), "empty or invalid Codex events")
    started = [e for e in events if e.get("type") == "thread.started"]
    turns = [e for e in events if e.get("type") == "turn.started"]
    completed = [e for e in events if e.get("type") == "turn.completed"]
    require(len(started) == len(turns) == len(completed) == 1
            and events[:2] == [started[0], turns[0]] and events[-1] == completed[0]
            and isinstance(started[0].get("thread_id"), str) and started[0]["thread_id"],
            "incomplete Codex session")
    require(not any(e.get("type") == "turn.failed" for e in events), "Codex turn failed")
    messages = [e["item"].get("text") for e in events if e.get("type") == "item.completed"
                and isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message"]
    try:
        require(messages and json.loads(messages[-1]) == answer, "Codex final message does not match result")
    except (ValueError, TypeError) as exc:
        raise ReviewError("invalid Codex final message") from exc
    return {"session_id": started[0]["thread_id"], "usage": completed[0].get("usage")}


def parse_claude(events):
    initial = [e for e in events if e.get("type") == "system" and e.get("subtype") == "init"]
    final = [e for e in events if e.get("type") == "result"]
    require(len(initial) == len(final) == 1 and events[-1] == final[0], "incomplete verifier session")
    init, result = initial[0], final[0]
    require(result.get("subtype") == "success" and result.get("is_error") is False
            and isinstance(result.get("structured_output"), dict), "verifier did not return a successful verdict")
    require(isinstance(result.get("session_id"), str) and result["session_id"]
            and result["session_id"] == init.get("session_id"), "verifier session mismatch")
    if init.get('model') != runner_config()['verifier_model']:
        raise AttemptError('configuration', 'session', 'model_mismatch', False, 'verifier model mismatch')
    metadata = {"session_id": result["session_id"], "observed_model": init["model"],
                "usage": result.get("usage")}
    for field in ("modelUsage", "total_cost_usd", "duration_ms", "duration_api_ms", "num_turns"):
        if field in result:
            metadata[field] = result[field]
    return result["structured_output"], metadata
