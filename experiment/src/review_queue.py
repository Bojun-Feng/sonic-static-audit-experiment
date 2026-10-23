#!/usr/bin/env python3
"""Validated production units and explicit six-unit pilot queue files."""

import argparse
from pathlib import Path
import sys

from common import ReviewError, json_bytes, keys, read_json, require, sha256, write_new
from validate import validate, load_selection

EXPERIMENT = Path(__file__).resolve().parents[1]


def runner_config():
    value = read_json(EXPERIMENT / "runner-config.json")
    keys(value, ["schema_version", "discovery_models", "verifier_model", "discovery_slots",
                 "verifier_slots", "max_attempts", "timeout_seconds"], "runner config")
    require(value["schema_version"] == 1
            and value["discovery_models"] == ["gpt-6-astra", "gpt-5.6-sol"]
            and value["verifier_model"] == "claude-opus-5-5"
            and type(value["discovery_slots"]) is int and value["discovery_slots"] == 16
            and type(value["verifier_slots"]) is int and value["verifier_slots"] == 1,
            "runner models and global concurrency must match production")
    require(all(type(value[k]) is int and value[k] > 0 for k in ("max_attempts", "timeout_seconds")),
            "runner attempts and timeout must be positive integers")
    return value


def units(prepared, mode):
    validate(prepared, mode=mode)
    manifest, index, _ = load_selection(prepared)
    if mode == "exhaustive":
        result = [{"unit_id": "f-" + sha256(f["path"].encode())[:20], "path": f["path"]}
                  for f in manifest["files"]]
    else:
        result = [{"unit_id": g["id"], "path": g["path"],
                   "source_card": str(prepared.resolve() / "groups" / g["id"] / "source-card.md"),
                   "scope": str(prepared.resolve() / "groups" / g["id"] / "scope.json")}
                  for g in index["groups"]]
    return result


def build_tasks(prepared, mode, queue=None):
    selected = units(prepared, mode)
    if queue is not None:
        value = read_json(queue)
        keys(value, ["schema_version", "mode", "manifest_sha256", "units"], "queue")
        require(type(value["schema_version"]) is int and value["schema_version"] == 1
                and value["mode"] == mode, "queue version or mode mismatch")
        require(value["manifest_sha256"] == sha256((prepared / "manifest.json").read_bytes()),
                "queue belongs to a different manifest")
        ids = value["units"]
        available = {unit["unit_id"] for unit in selected}
        require(isinstance(ids, list) and ids and all(isinstance(u, str) and u in available for u in ids)
                and len(set(ids)) == len(ids), "queue has empty, unknown or duplicate units")
        selected = [unit for unit in selected if unit["unit_id"] in ids]
    # Interleave models so neither model monopolizes the first slots.
    return [{**unit, "model": model, "mode": mode,
             "task_id": f"{mode}--{model}--{unit['unit_id']}"}
            for unit in selected for model in runner_config()["discovery_models"]]


def make_pilot_queue(prepared, mode, out, paths=None):
    selected = units(prepared, mode)
    if paths is not None:
        require(len(paths) == 6 and len(set(paths)) == 6, "pilot requires six distinct explicit paths")
        selected = [unit for unit in selected if unit["path"] in paths]
    else:
        selected = selected[:6]
    require(len(selected) == 6, "pilot requires six available review units")
    value = {"schema_version": 1, "mode": mode,
             "manifest_sha256": sha256((prepared / "manifest.json").read_bytes()),
             "units": [unit["unit_id"] for unit in selected]}
    write_new(out, json_bytes(value))
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepared", type=Path, required=True)
    parser.add_argument("--mode", choices=("exhaustive", "targeted"), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--path", action="append", dest="paths")
    args = parser.parse_args()
    try:
        make_pilot_queue(args.prepared, args.mode, args.out, args.paths)
    except (ReviewError, OSError, UnicodeError) as exc:
        print(f"FAIL pilot queue: {exc}", file=sys.stderr)
        return 1
    print(f"Pilot queue: six {args.mode} units, twelve discovery sessions: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
