"""Pinned standalone CLI adapters. No authentication or provider SDK calls."""

import json
import re
import shlex
from pathlib import Path

from audit_queue import EXPERIMENT, runner_config
from common import AuditError, json_bytes, require, within


def discovery_command(binary, model, work):
    require(model in runner_config()["discovery_models"], "unconfigured discovery model")
    return [binary, "exec", "--model", model, "--ephemeral", "--json",
            "--ignore-user-config", "--ignore-rules", "--skip-git-repo-check",
            "--sandbox", "read-only", "--color", "never", "--cd", str(work),
            "-c", 'approval_policy="never"', "-c", 'web_search="disabled"',
            "-c", "features.multi_agent=false", "-c", "features.apps=false",
            "-c", "project_doc_max_bytes=0", "-c", "check_for_update_on_startup=false",
            "--output-schema", str(EXPERIMENT / "schemas/discovery.json"),
            "--output-last-message", str(work / "answer.json"), "-"]


def verifier_command(binary, work, readable):
    return [binary, "--print", "--model", runner_config()["verifier_model"],
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


def verifier_prompt(candidate, repo, reference, prior):
    context = {"candidate": str(candidate), "source_root": str(repo),
               "reference_root": str(reference), "prior_candidates": prior}
    return (EXPERIMENT / "prompts/verifier.txt").read_text() + "\nVerification context:\n" + json_bytes(context).decode()


def inspection_argv(command):
    """Recognize only the documented direct reads, optionally CLI shell-wrapped."""
    if not isinstance(command, str):
        return []
    try:
        argv = shlex.split(command)
        if len(argv) == 3 and argv[0] in ("/bin/sh", "/bin/bash", "/bin/zsh", "sh", "bash", "zsh") and argv[1] in ("-c", "-lc"):
            argv = shlex.split(argv[2])
        return argv
    except ValueError:
        return []


def validate_inspection(events, task, repo):
    """Require native completed reads, not claims of review in agent prose.

    The deliberately small grammar is also printed in the discovery prompt.
    Full-file output proves assigned coverage; a literal identifier search with
    source-backed output records context exploration (not semantic correctness).
    Never execute a command from a trace while validating it.
    """
    source = within(repo, task["path"])
    text = source.read_text()
    identifiers = set(re.findall(r"\b[A-Za-z_][A-Za-z_0-9]{2,}\b", text))
    identifiers -= {"auto", "bool", "break", "case", "char", "class", "const", "continue",
                    "default", "delete", "else", "false", "float", "for", "include", "int",
                    "namespace", "nullptr", "private", "public", "return", "sizeof", "static",
                    "std", "struct", "switch", "template", "this", "true", "unsigned", "using",
                    "virtual", "void", "while"}
    read = context = False
    ids = set()
    source_cache = {}
    # Only activity before the final answer counts. Commentary is allowed.
    final_index = max(i for i, e in enumerate(events) if e.get("type") == "item.completed"
                      and isinstance(e.get("item"), dict) and e["item"].get("type") == "agent_message")
    for event in events[:final_index]:
        item = event.get("item")
        if event.get("type") != "item.completed" or not isinstance(item, dict) or item.get("type") != "command_execution":
            continue
        identity = item.get("id")
        require(isinstance(identity, str) and identity and identity not in ids,
                "missing or duplicate inspection command ID")
        ids.add(identity)
        if item.get("status") != "completed" or type(item.get("exit_code")) is not int or item["exit_code"] != 0:
            continue
        output = item.get("aggregated_output")
        if not isinstance(output, str) or not output.strip():
            continue
        argv = inspection_argv(item.get("command"))
        if argv == ["cat", "--", str(source)] and output == text:
            read = True
        if len(argv) != 6 or argv[:4] != ["rg", "-n", "-F", "--"] or argv[5] != str(repo) or argv[4] not in identifiers:
            continue
        symbol = argv[4]
        lines = output.splitlines()
        valid = True
        source_match = False
        for line in lines:
            match = re.fullmatch(r"(.+?):([1-9][0-9]*):(.*)", line)
            if not match:
                valid = False
                break
            path, number, content = match.groups()
            candidate = Path(path)
            if not candidate.is_absolute() or not candidate.is_relative_to(repo):
                valid = False
                break
            candidate = within(repo, str(candidate.relative_to(repo)))
            if not candidate.is_file():
                valid = False
                break
            if candidate not in source_cache:
                source_cache[candidate] = candidate.read_text().splitlines()
            actual = source_cache[candidate]
            if int(number) > len(actual) or actual[int(number) - 1] != content or symbol not in content:
                valid = False
                break
            source_match |= candidate.suffix in (".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx")
        context |= source_match and valid
    require(read and context, "incomplete source inspection evidence: require assigned-file read and source context search")


def parse_codex(events, answer, task, repo):
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
        raise AuditError("invalid Codex final message") from exc
    validate_inspection(events, task, repo)
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
    require(init.get("model") == runner_config()["verifier_model"], "verifier model mismatch")
    metadata = {"session_id": result["session_id"], "observed_model": init["model"],
                "usage": result.get("usage")}
    for field in ("modelUsage", "total_cost_usd", "duration_ms", "duration_api_ms", "num_turns"):
        if field in result:
            metadata[field] = result[field]
    return result["structured_output"], metadata
