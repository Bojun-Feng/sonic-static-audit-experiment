#!/usr/bin/env python3
"""Validate source provenance, index integrity, and exact generated artifacts."""

import argparse
from pathlib import Path
import re
import sys

from common import (AuditError, collect_sources, exact_children, group_for, json_bytes,
                    keys, match_location, read_json, regex_matches, render_group, require, sha256,
                    sorted_matches, source_and_output, source_commit, validate_config, within)


def load_selection(out, repo=None):
    require(not out.is_symlink(), "output root must not be a symlink")
    out = out.resolve()
    manifest = read_json(out / "manifest.json")
    index = read_json(out / "selected-groups.json")
    keys(manifest, ["schema_version", "mode", "source_root", "source_commit", "profile", "files"], "manifest")
    keys(index, ["schema_version", "manifest_sha256", "scanner", "groups"], "selected-groups")
    require(type(manifest["schema_version"]) is int and manifest["schema_version"] == 1
            and type(index["schema_version"]) is int and index["schema_version"] == 1,
            "unsupported schema version")
    require(manifest["mode"] in ("exhaustive", "targeted"), "invalid preparation mode")
    require(isinstance(manifest["source_root"], str) and Path(manifest["source_root"]).is_absolute(),
            "source_root must be absolute")
    repo, out = source_and_output(repo or Path(manifest["source_root"]), out)
    require(str(repo) == manifest["source_root"], "--repo disagrees with manifest source_root")
    config = validate_config(manifest["profile"])
    require(manifest["source_commit"] == source_commit(repo), "source commit changed")
    require(config["intended_source_commit"] in ("UNPINNED", manifest["source_commit"]),
            "source commit does not match intended_source_commit")
    require(index["manifest_sha256"] == sha256((out / "manifest.json").read_bytes()),
            "selected-groups manifest hash mismatch")
    require((out / "manifest.json").read_bytes() == json_bytes(manifest), "manifest is not canonical JSON")
    require((out / "selected-groups.json").read_bytes() == json_bytes(index), "selected-groups is not canonical JSON")
    entries, contents = collect_sources(repo, config)
    require(json_bytes(manifest["files"]) == json_bytes(entries),
            "manifest source set, file hash, line count, or inclusion reasons changed")
    keys(index["scanner"], ["name", "version"], "scanner")
    scanner = index["scanner"]
    require(isinstance(scanner["version"], str) and scanner["version"], "missing scanner version")
    require(isinstance(index["groups"], list), "groups must be an array")
    if manifest["mode"] == "exhaustive":
        require(scanner == {"name": "not-run", "version": "none"} and not index["groups"],
                "exhaustive preparation must not contain targeted candidates")
    else:
        require(scanner["name"] in ("ast-grep", "python-regex"), "invalid targeted scanner")
        require(scanner["name"] != "python-regex" or scanner["version"] == "1", "unknown regex version")
    entry_by_path = {entry["path"]: entry for entry in entries}
    patterns = {pattern["id"]: pattern for pattern in config["patterns"]}
    paths, ids = [], []
    for group in index["groups"]:
        keys(group, ["id", "path", "matches"], "group")
        require(isinstance(group["path"], str) and group["path"] in entry_by_path,
                "group references a file outside the manifest")
        require(isinstance(group["id"], str) and re.fullmatch(r"g-[0-9a-f]{20}", group["id"]),
                "invalid group ID")
        matches = group["matches"]
        require(isinstance(matches, list) and matches, "group has no matches")
        data = contents[group["path"]]
        for match in matches:
            keys(match, ["pattern_id", "start_byte", "end_byte", "start_line", "end_line"], "match")
            require(isinstance(match["pattern_id"], str) and match["pattern_id"] in patterns,
                    "unknown match pattern")
            require(type(match["start_line"]) is int and type(match["end_line"]) is int,
                    "match line numbers must be integers")
            require(match == match_location(match["pattern_id"], match["start_byte"], match["end_byte"], data),
                    "match line/byte locations disagree")
        require(matches == sorted_matches(matches), "matches are unsorted or duplicated")
        require(group == group_for(entry_by_path[group["path"]], matches, config["grouping"]["context_lines"]),
                "group ID does not match its contents")
        paths.append(group["path"])
        ids.append(group["id"])
    require(paths == sorted(set(paths)) and len(ids) == len(set(ids)), "duplicate or unsorted groups")
    # Regex discovery is cheap and dependency-free, so also detect omitted candidates.
    # AST validation deliberately does not need the original external binary.
    if scanner["name"] == "python-regex":
        expected_groups = []
        for entry in entries:
            data = contents[entry["path"]]
            matches = regex_matches(data, config["patterns"])
            if matches:
                expected_groups.append(group_for(entry, matches, config["grouping"]["context_lines"]))
        require(index["groups"] == expected_groups, "regex candidate set is incomplete or altered")
    return manifest, index, contents


def validate_cards(out, manifest, index, contents):
    directory = within(out, "groups")
    exact_children(directory, [group["id"] for group in index["groups"]])
    for group in index["groups"]:
        group_dir = within(directory, group["id"])
        exact_children(group_dir, ["scope.json", "source-card.md"])
        scope_bytes, card_bytes = render_group(manifest, index, group, contents[group["path"]])
        scope = read_json(group_dir / "scope.json")
        require(json_bytes(scope) == scope_bytes and (group_dir / "scope.json").read_bytes() == scope_bytes,
                f"scope metadata mismatch: {group['id']}")
        card = group_dir / "source-card.md"
        require(card.is_file() and card.read_bytes() == card_bytes,
                f"source card mismatch: {group['id']}")


def validate(out, repo=None, mode=None):
    manifest, index, contents = load_selection(out, repo)
    require(mode is None or mode == manifest["mode"], "requested mode disagrees with manifest")
    expected = {"manifest.json", "selected-groups.json"}
    if manifest["mode"] == "targeted":
        expected.add("groups")
    exact_children(out, expected)
    if manifest["mode"] == "targeted":
        validate_cards(out, manifest, index, contents)
    return len(manifest["files"]), len(index["groups"])


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--repo", type=Path)
    parser.add_argument("--mode", choices=("exhaustive", "targeted"))
    args = parser.parse_args()
    try:
        files, groups = validate(args.out, args.repo, args.mode)
    except (AuditError, OSError, UnicodeError) as exc:
        print(f"FAIL validation: {exc}", file=sys.stderr)
        return 1
    print(f"PASS validation: {files} files, {groups} groups")
    return 0


if __name__ == "__main__":
    sys.exit(main())
