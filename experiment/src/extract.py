#!/usr/bin/env python3
"""Extract exact context windows from a validated selection into source cards."""

import argparse
from pathlib import Path
import sys

from common import AuditError, exact_children, render_group, require, write_new
from validate import load_selection, validate_cards


def extract(repo, index_path):
    require(index_path.name == "selected-groups.json" and not index_path.is_symlink(),
            "--index must name the generated selected-groups.json")
    out = index_path.parent
    manifest, index, contents = load_selection(out, repo)
    require(manifest["mode"] == "targeted", "extraction requires a targeted selection")
    if (out / "groups").exists() or (out / "groups").is_symlink():
        exact_children(out, ["manifest.json", "selected-groups.json", "groups"])
        validate_cards(out, manifest, index, contents)
    else:
        exact_children(out, ["manifest.json", "selected-groups.json"])
        (out / "groups").mkdir()
        for group in index["groups"]:
            scope, card = render_group(manifest, index, group, contents[group["path"]])
            group_dir = out / "groups" / group["id"]
            group_dir.mkdir()
            write_new(group_dir / "source-card.md", card)
            write_new(group_dir / "scope.json", scope)
    print(f"extraction: {len(index['groups'])} source cards")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", type=Path, required=True)
    parser.add_argument("--index", type=Path, required=True)
    args = parser.parse_args()
    try:
        extract(args.repo, args.index)
    except (AuditError, OSError, UnicodeError) as exc:
        print(f"FAIL extraction: {exc}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
