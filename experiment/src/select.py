#!/usr/bin/env python3
"""Build the exhaustive manifest and deterministic targeted candidate index."""

import sys
from pathlib import Path

# This required filename collides with the standard-library select extension.
# Import subprocess/its select dependency without this directory on sys.path.
_import_path = sys.path[:]
sys.path[:] = [p for p in sys.path if Path(p).resolve() != Path(__file__).resolve().parent]
try:
    import subprocess
finally:
    sys.path[:] = _import_path

import argparse
import json
import os
import re
import shutil
import tempfile

from common import (ReviewError, collect_sources, exact_children, group_for, json_bytes,
                    match_location, read_json, regex_matches, require, sha256, sorted_matches,
                    source_and_output, source_commit, validate_config, write_new)


def tool_environment():
    # Keep interpreter lookup working for packaged CLI launchers, but do not
    # pass provider tokens or other inherited environment variables to subprocesses.
    return {"PATH": os.environ.get("PATH", os.defpath), "LC_ALL": "C"}


def find_ast_grep(repo):
    repo = repo.resolve()
    for name in ("ast-grep", "sg"):
        binary = shutil.which(name)
        if not binary:
            continue
        # PATH entries may be relative; searches run from isolated scratch space.
        binary = str(Path(binary).resolve())
        require(not Path(binary).is_relative_to(repo), "refusing to execute a tool from source input")
        result = subprocess.run([binary, "--version"], capture_output=True, text=True,
                                env=tool_environment(), timeout=10, check=False)
        version = result.stdout.strip()
        if result.returncode == 0 and re.fullmatch(r"ast-grep \S+", version):
            return binary, version.split()[1]
        require(name != "ast-grep", "installed ast-grep failed its version check; no fallback was used")
    return None, None


def ast_matches(data, patterns, binary, config_path):
    matches = []
    for pattern in patterns:
        for structural in pattern["ast"]:
            result = subprocess.run(
                [binary, "run", "--config", str(config_path), "--lang", "cpp",
                 "--pattern", structural, "--json=compact", "--stdin"],
                input=data, capture_output=True, cwd=config_path.parent,
                env=tool_environment(), timeout=60, check=False)
            # ast-grep run uses 1 for a successful search with no matches.
            require(result.returncode in (0, 1),
                    f"ast-grep failed on {pattern['id']} (exit {result.returncode}); no fallback was used")
            try:
                hits = json.loads(result.stdout)
                require(isinstance(hits, list), "ast-grep output must be an array")
                require(result.returncode == 0 or not hits, "ast-grep returned matches with failure status")
                for hit in hits:
                    offsets = hit["range"]["byteOffset"]
                    location = match_location(pattern["id"], offsets["start"], offsets["end"], data)
                    require(data[location["start_byte"]:location["end_byte"]].decode("utf-8") == hit["text"],
                            "ast-grep match text disagrees with source bytes")
                    matches.append(location)
            except (ValueError, KeyError, TypeError, UnicodeError) as exc:
                raise ReviewError(f"invalid ast-grep JSON for {pattern['id']}") from exc
    return sorted_matches(matches)


def select(repo, config_path, out, mode="targeted", identity=None):
    repo, out = source_and_output(repo, out)
    config = validate_config(read_json(config_path))
    if identity is not None:
        from source_identity import verify
        commit = verify(repo, identity, config["intended_source_commit"])["source_commit"]
    else:
        commit = source_commit(repo)
    require(config["intended_source_commit"] in ("UNPINNED", commit),
            "source commit does not match intended_source_commit")
    entries, contents = collect_sources(repo, config)
    manifest = {"schema_version": 1, "mode": mode, "source_root": str(repo),
                "source_commit": commit, "profile": config, "files": entries}
    if identity is not None:
        manifest["source_identity"] = {"path": str(identity.resolve()), "sha256": sha256(identity.read_bytes())}
    out.mkdir(parents=True, exist_ok=True)
    groups = []
    scanner = {"name": "not-run", "version": "none"}
    if mode == "targeted":
        binary, version = find_ast_grep(repo)
        scanner = {"name": "ast-grep" if binary else "python-regex",
                   "version": version if binary else "1"}
        if not binary:
            print("selection: ast-grep unavailable; using deterministic Python regex fallback (line-window scope)",
                  file=sys.stderr)
        # Explicit empty ast-grep config prevents reading project/user configs.
        # The temporary directory stays inside --out and is removed on normal exit.
        with tempfile.TemporaryDirectory(prefix=".ast-grep-", dir=out) as scratch:
            ast_config = Path(scratch) / "sgconfig.json"
            if binary:
                write_new(ast_config, json_bytes({"ruleDirs": []}))
            for entry in entries:
                data = contents[entry["path"]]
                if binary:
                    matches = ast_matches(data, config["patterns"], binary, ast_config)
                else:
                    matches = regex_matches(data, config["patterns"])
                if matches:
                    groups.append(group_for(entry, matches, config["grouping"]["context_lines"]))
    index = {"schema_version": 1, "manifest_sha256": sha256(json_bytes(manifest)),
             "scanner": scanner, "groups": groups}
    artifacts = {"manifest.json": json_bytes(manifest), "selected-groups.json": json_bytes(index)}
    if any(out.iterdir()):
        expected = set(artifacts)
        if mode == "targeted" and (out / "groups").exists():
            expected.add("groups")
        exact_children(out, expected)
        for name, data in artifacts.items():
            require((out / name).is_file() and (out / name).read_bytes() == data,
                    "existing selection differs or is incomplete; use a fresh --out directory")
    else:
        for name, data in artifacts.items():
            write_new(out / name, data)
    print(f"selection: {len(entries)} files, {len(groups)} groups ({scanner['name']})")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=("exhaustive", "targeted"), default="targeted")
    parser.add_argument("--identity", type=Path)
    args = parser.parse_args()
    try:
        select(args.repo, args.config, args.out, args.mode, args.identity)
    except (ReviewError, OSError, UnicodeError, subprocess.SubprocessError) as exc:
        print(f"FAIL selection: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
