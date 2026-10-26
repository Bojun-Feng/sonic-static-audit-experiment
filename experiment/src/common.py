"""Shared source boundaries, deterministic formats, and source-card rendering."""

import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import stat


class ReviewError(Exception):
    """An invalid input or incomplete artifact set."""


class AttemptError(ReviewError):
    """A bounded public reason plus a private diagnostic message."""

    def __init__(self, category, stage, reason, retryable, detail):
        super().__init__(detail)
        self.category, self.stage, self.reason = category, stage, reason
        self.retryable = retryable

    def summary(self):
        return {key: getattr(self, key) for key in ('category', 'stage', 'reason', 'retryable')}


EXTENSIONS = {".c", ".cc", ".cpp", ".cxx", ".h", ".hh", ".hpp", ".hxx"}
EXCLUDED = {
    "test", "tests", "unittest", "unittests", "unit_test", "unit_tests",
    "mock", "mocks", "build", "builds", "cmakefiles", "dist", "target",
    "debian", "docker", "dockers", "testbed", "vs", "venv", "env",
    "node_modules", "__pycache__",
}
RESTRICTIONS = {
    "read_only_source": True, "static_only": True,
    "local_only": True, "provider_calls": False,
}


def require(condition, message):
    if not condition:
        raise ReviewError(message)


def keys(value, expected, label):
    require(isinstance(value, dict) and set(value) == set(expected),
            f"{label}: unexpected or missing fields")


def sha256(data):
    return hashlib.sha256(data).hexdigest()


def json_bytes(value):
    return (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n").encode()


def read_json(path):
    def unique(pairs):
        result = {}
        for key, value in pairs:
            require(key not in result, f"{path.name}: duplicate JSON key {key}")
            result[key] = value
        return result

    def invalid_constant(value):
        raise ReviewError(f"{path.name}: invalid JSON constant {value}")

    require(not path.is_symlink() and path.is_file(), f"missing regular file: {path}")
    try:
        return json.loads(path.read_bytes(), object_pairs_hook=unique,
                          parse_constant=invalid_constant)
    except (ValueError, UnicodeError) as exc:
        raise ReviewError(f"invalid JSON: {path.name}") from exc


def relative_path(value):
    require(isinstance(value, str) and value and "\\" not in value
            and all(ord(c) >= 32 and ord(c) != 127 for c in value),
            "expected a plain relative POSIX path")
    path = PurePosixPath(value)
    require(not path.is_absolute() and str(path) == value
            and all(part not in (".", "..") for part in path.parts)
            and value != ".", f"invalid relative path: {value}")
    return path


def within(root, relative):
    """Reject symlinks in every relative component before reading or writing."""
    path = root
    for part in relative_path(relative).parts:
        path = path / part
        require(not path.is_symlink(), f"symlink is not allowed: {path}")
    return path


def source_and_output(repo, out):
    require(not repo.is_symlink() and not out.is_symlink(),
            "source/output roots must not be symlinks")
    repo, out = repo.resolve(), out.resolve()
    require(repo.is_dir(), f"source repository does not exist: {repo}")
    require(not out.is_relative_to(repo) and not repo.is_relative_to(out),
            "source and output directories must be disjoint")
    return repo, out


def production_path(path):
    parts = PurePosixPath(path).parts
    if any(p.startswith(".") or p.lower() in EXCLUDED
           or p.lower().startswith("cmake-build-") for p in parts):
        return False
    stem = PurePosixPath(path).stem.lower()
    return (PurePosixPath(path).suffix.lower() in EXTENSIONS
            and stem not in EXCLUDED
            and not stem.startswith(("test_", "mock_"))
            and not stem.endswith(("_test", "_tests", "_unittest")))


def validate_config(config):
    fields = ["profile", "intended_source_commit", "source_roots",
              "domain_anchors", "patterns", "grouping", "restrictions"]
    # Static-only profiles may omit VM tooling.
    if isinstance(config, dict) and "guest_cli_versions" in config:
        fields.append("guest_cli_versions")
        versions = config["guest_cli_versions"]
        keys(versions, ["codex", "claude_code"], "guest_cli_versions")
        require(all(isinstance(v, str) and re.fullmatch(r"(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", v)
                    for v in versions.values()),
                "guest CLI versions must be explicit stable npm versions (X.Y.Z)")
    keys(config, fields, "config")
    require(isinstance(config["profile"], str) and config["profile"], "config: empty profile")
    pin = config["intended_source_commit"]
    require(isinstance(pin, str) and (pin == "UNPINNED" or
            re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", pin)),
            "intended_source_commit must be UNPINNED or a full lowercase commit ID")
    for field in ("source_roots", "domain_anchors"):
        values = config[field]
        require(isinstance(values, list) and values
                and all(isinstance(v, str) and v for v in values)
                and len(set(values)) == len(values), f"config: invalid {field}")
    for root in config["source_roots"]:
        parts = relative_path(root).parts
        require(not any(p.startswith(".") or p.lower() in EXCLUDED for p in parts),
                f"excluded source root: {root}")
    require(all(a == a.casefold() for a in config["domain_anchors"]),
            "domain anchors must be lowercase literals")
    keys(config["grouping"], ["policy", "context_lines", "scope"], "grouping")
    grouping = config["grouping"]
    require(grouping["policy"] == "per-file" and grouping["scope"] == "line-window",
            "only per-file line-window grouping is supported")
    require(type(grouping["context_lines"]) is int and 0 <= grouping["context_lines"] <= 1000,
            "context_lines must be an integer between 0 and 1000")
    keys(config["restrictions"], RESTRICTIONS, "restrictions")
    require(all(config["restrictions"][k] is v for k, v in RESTRICTIONS.items()),
            "read-only, static-only, local-only restrictions cannot be disabled")
    patterns = config["patterns"]
    require(isinstance(patterns, list) and patterns, "config: patterns must not be empty")
    ids = []
    for pattern in patterns:
        keys(pattern, ["id", "ast", "regex"], "pattern")
        require(isinstance(pattern["id"], str)
                and re.fullmatch(r"[a-z][a-z0-9-]*", pattern["id"]), "invalid pattern ID")
        require(isinstance(pattern["ast"], list) and pattern["ast"]
                and all(isinstance(p, str) and p for p in pattern["ast"]),
                "ast must contain structural pattern strings")
        require(isinstance(pattern["regex"], str) and pattern["regex"], "empty regex")
        try:
            regex = re.compile(pattern["regex"].encode("ascii"), re.MULTILINE)
            require(regex.search(b"") is None, "regex must not match empty input")
        except (re.error, UnicodeError) as exc:
            raise ReviewError(f"invalid ASCII regex: {pattern['id']}") from exc
        ids.append(pattern["id"])
    require(len(ids) == len(set(ids)), "duplicate pattern IDs")
    return config


def source_commit(repo):
    """Read only HEAD and refs, never Git configuration, hooks, or history.

    Linked worktrees whose metadata lives outside this input are deliberately
    unsupported. A plain source snapshot has no commit, represented as null.
    """
    git = within(repo, ".git")
    if not git.exists():
        return None
    require(git.is_dir(), "source must be a standalone checkout or snapshot; external Git metadata is unsupported")
    head_path = within(git, "HEAD")
    require(head_path.is_file(), "Git HEAD must be a regular file")
    head = head_path.read_text(encoding="ascii").strip()
    if head.startswith("ref: "):
        ref = head[5:]
        require(ref.startswith("refs/"), "invalid Git HEAD reference")
        loose = within(git, ref)
        if loose.is_file():
            head = loose.read_text(encoding="ascii").strip()
        else:
            packed = within(git, "packed-refs")
            require(packed.is_file(), "HEAD reference is missing")
            matches = [line.split()[0] for line in packed.read_text(encoding="ascii").splitlines()
                       if not line.startswith(("#", "^")) and len(line.split()) == 2
                       and line.split()[1] == ref]
            require(len(matches) == 1, "HEAD reference is absent or duplicated in packed-refs")
            head = matches[0]
    require(re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", head), "invalid source commit ID")
    return head


def source_lines(data):
    lines = data.decode("utf-8").split("\n")
    if lines[-1] == "":
        lines.pop()
    return [line.removesuffix("\r") for line in lines]


def collect_sources(repo, config):
    paths = set()
    for root in sorted(config["source_roots"]):
        directory = within(repo, root)
        require(directory.is_dir(), f"configured source root does not exist: {root}")

        def walk_error(error):
            raise error

        for current, dirs, files in os.walk(directory, followlinks=False, onerror=walk_error):
            dirs[:] = sorted(d for d in dirs if not d.startswith(".")
                             and d.lower() not in EXCLUDED
                             and not d.lower().startswith("cmake-build-")
                             and not (Path(current) / d).is_symlink())
            for filename in sorted(files):
                path = Path(current) / filename
                relative = path.relative_to(repo).as_posix()
                if production_path(relative) and not path.is_symlink():
                    require(stat.S_ISREG(path.stat().st_mode), f"not a regular source file: {relative}")
                    paths.add(relative)
    entries, contents = [], {}
    for path in sorted(paths):
        data = within(repo, path).read_bytes()
        try:
            text = data.decode("utf-8").casefold()
        except UnicodeError as exc:
            raise ReviewError(f"source is not UTF-8: {path}") from exc
        reasons = sorted(f"{kind}:{anchor}" for anchor in config["domain_anchors"]
                         for kind, value in (("path", path.casefold()), ("content", text))
                         if anchor in value)
        if reasons:
            entries.append({"path": path, "sha256": sha256(data),
                            "line_count": len(source_lines(data)), "inclusion_reasons": reasons})
            contents[path] = data
    require(entries, "no production files matched the configured domain anchors")
    return entries, contents


def match_location(pattern_id, start, end, data):
    require(type(start) is int and type(end) is int and 0 <= start < end <= len(data),
            "invalid match byte range")
    return {"pattern_id": pattern_id, "start_byte": start, "end_byte": end,
            "start_line": data.count(b"\n", 0, start) + 1,
            "end_line": data.count(b"\n", 0, end - 1) + 1}


def sorted_matches(matches):
    unique = {json_bytes(match): match for match in matches}
    return sorted(unique.values(), key=lambda m: (m["start_byte"], m["end_byte"], m["pattern_id"]))


def regex_matches(data, patterns):
    return sorted_matches([
        match_location(pattern["id"], match.start(), match.end(), data)
        for pattern in patterns
        for match in re.finditer(pattern["regex"].encode("ascii"), data, re.MULTILINE)
    ])


def group_for(entry, matches, context):
    payload = {"path": entry["path"], "sha256": entry["sha256"],
               "matches": matches, "context_lines": context}
    return {"id": "g-" + sha256(json_bytes(payload))[:20],
            "path": entry["path"], "matches": matches}


def merged_ranges(matches, context, line_count):
    ranges = []
    windows = sorted((max(1, m["start_line"] - context),
                      min(line_count, m["end_line"] + context)) for m in matches)
    for start, end in windows:
        if ranges and start <= ranges[-1]["end_line"]:
            ranges[-1]["end_line"] = max(ranges[-1]["end_line"], end)
        else:
            ranges.append({"start_line": start, "end_line": end})
    return ranges


def render_group(manifest, index, group, data):
    context = manifest["profile"]["grouping"]["context_lines"]
    lines = source_lines(data)
    ranges = merged_ranges(group["matches"], context, len(lines))
    scope = {"schema_version": 1, "group_id": group["id"], "kind": "line-window",
             "source_root": manifest["source_root"], "source_commit": manifest["source_commit"],
             "scanner": index["scanner"], "context_lines": context, "matches": group["matches"],
             "source": {"path": group["path"], "sha256": sha256(data), "ranges": ranges}}
    card = [f"# Source card {group['id']}", "",
            f"Source: `{group['path']}`", f"SHA-256: `{sha256(data)}`",
            f"Commit: `{manifest['source_commit'] or 'unversioned snapshot'}`",
            f"Scanner: `{index['scanner']['name']}` ({index['scanner']['version']})",
            f"Scope: line-window; {context} context lines before and after each match.",
            "No enclosing function is inferred. Candidates are not confirmed defects.", "", "Matches:", ""]
    for match in group["matches"]:
        card.append(f"- `{match['pattern_id']}`: lines {match['start_line']}-{match['end_line']}; "
                    f"bytes [{match['start_byte']}, {match['end_byte']})")
    for window in ranges:
        start, end = window["start_line"], window["end_line"]
        excerpt = lines[start - 1:end]
        # A source comment containing a fence must not terminate the excerpt.
        longest = max((len(run) for line in excerpt for run in re.findall(r"`+", line)), default=0)
        fence = "`" * max(3, longest + 1)
        card.extend(["", f"## {group['path']}:{start}-{end}", "", fence + "text"])
        card.extend(f"{number:>6} | {line}" for number, line in enumerate(excerpt, start))
        card.append(fence)
    return json_bytes(scope), ("\n".join(card) + "\n").encode("utf-8")


def exact_children(directory, expected):
    require(not directory.is_symlink() and directory.is_dir(), f"missing directory: {directory}")
    actual = {child.name for child in directory.iterdir()}
    require(actual == set(expected),
            f"{directory.name}: missing {sorted(set(expected) - actual)}, extra {sorted(actual - set(expected))}")
    require(all(not child.is_symlink() for child in directory.iterdir()),
            f"symlink in artifact directory: {directory}")


def write_new(path, data):
    # Exclusive creation never follows/overwrites an existing artifact or hardlink.
    with path.open("xb") as handle:
        handle.write(data)
