#!/usr/bin/env python3
"""Bind an operator-attested candidate revision to immutable snapshot bytes.

The digest identifies the extracted archive independently of tar compression,
timestamps, ownership and mount path. This is an attestation, not a way to infer
a Git revision from arbitrary bytes. Create it only for the known frozen archive.
"""

import argparse
import os
from pathlib import Path
import re
import stat
import sys

from common import ReviewError, json_bytes, keys, read_json, require, sha256, write_new


def snapshot_digest(repo):
    require(not repo.is_symlink() and repo.is_dir(), "snapshot must be a regular directory")
    entries = []

    def walk(directory):
        for path in sorted(directory.iterdir()):
            relative = path.relative_to(repo).as_posix()
            if relative == ".git":
                continue  # Never require or inspect Git metadata in an archive.
            mode = path.lstat().st_mode
            if stat.S_ISLNK(mode):
                entries.append([relative, "symlink", os.readlink(path)])
            elif stat.S_ISDIR(mode):
                entries.append([relative, "directory"])
                walk(path)
            else:
                require(stat.S_ISREG(mode), f"special file in source snapshot: {relative}")
                entries.append([relative, "file", bool(mode & 0o111), sha256(path.read_bytes())])
    walk(repo)
    require(entries, "empty source snapshot")
    return sha256(json_bytes(entries))


def bind(repo, commit, identity):
    require(isinstance(commit, str) and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", commit),
            "identity requires a full candidate commit SHA")
    require(not identity.is_symlink() and not identity.exists(), "identity already exists; never overwrite it")
    require(not identity.resolve().is_relative_to(repo.resolve()), "identity must be outside frozen source")
    value = {"schema_version": 1, "source_commit": commit,
             "snapshot_sha256": snapshot_digest(repo)}
    write_new(identity, json_bytes(value))
    identity.chmod(0o444)
    return value


def verify(repo, identity, expected_commit):
    value = read_json(identity)
    keys(value, ["schema_version", "source_commit", "snapshot_sha256"], "source identity")
    require(type(value["schema_version"]) is int and value["schema_version"] == 1,
            "unsupported source identity version")
    require(not identity.stat().st_mode & 0o222, "source identity must be immutable (no write bits)")
    require(isinstance(value["source_commit"], str)
            and re.fullmatch(r"[0-9a-f]{40}|[0-9a-f]{64}", value["source_commit"])
            and value["source_commit"] == expected_commit, "source identity candidate SHA mismatch")
    require(value["snapshot_sha256"] == snapshot_digest(repo), "source archive snapshot changed")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--commit", required=True)
    parser.add_argument("--out", type=Path, required=True)
    args = parser.parse_args()
    try:
        bind(args.repo, args.commit, args.out)
    except (ReviewError, OSError) as exc:
        print(f"FAIL identity: {exc}", file=sys.stderr)
        return 1
    print(f"Bound source identity: {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
