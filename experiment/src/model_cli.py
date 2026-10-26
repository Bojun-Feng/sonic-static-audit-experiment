"""Prompt rendering and native Codex completion parsing."""

import json

from review_validation import EXPERIMENT
from common import ReviewError, json_bytes, require


def discovery_prompt(task, repo, reference):
    context = {"task": task, "source_root": str(repo), "reference_root": str(reference)}
    return (EXPERIMENT / "prompts/discovery.txt").read_text() + "\nReview context:\n" + json_bytes(context).decode()


def verifier_prompt(validation_input, repo, reference):
    """Render one blinded validation input with source and reference paths."""
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
