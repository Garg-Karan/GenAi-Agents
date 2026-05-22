"""
detect_changes.py
-----------------
Returns the list of Java SOURCE files added/modified in a commit.
Test files and ignored paths are filtered out here so the LLM never sees them.

Usage:
    python -m tools.detect_changes <commit_sha>          # default HEAD
"""

from __future__ import annotations

import fnmatch
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List

import yaml


@dataclass
class ChangedFile:
    path: str            # repo-relative
    status: str          # A=added, M=modified, R=renamed
    is_test: bool        # already a test file?


def _load_cfg() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    with cfg_path.open() as f:
        return yaml.safe_load(f)


def _git_changed(commit: str, repo_root: str = ".") -> List[tuple[str, str]]:
    """Return [(status, path), ...] for files changed in `commit` vs its parent.

    Handles the root-commit case (no parent) by diffing against the empty tree.
    """
    # Does the commit have a parent? If not, diff against git's empty tree.
    has_parent = subprocess.run(
        ["git", "-C", repo_root, "rev-parse", "--verify", f"{commit}^"],
        capture_output=True,
        text=True,
    ).returncode == 0

    if has_parent:
        cmd = [
            "git", "-C", repo_root, "show",
            "--name-status", "--pretty=format:", "--diff-filter=AMR", commit,
        ]
    else:
        # Empty-tree SHA is a git constant; lists every file in the commit.
        empty_tree = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        cmd = [
            "git", "-C", repo_root, "diff-tree", "-r",
            "--name-status", "--diff-filter=AMR", empty_tree, commit,
        ]

    out = subprocess.run(cmd, check=True, capture_output=True, text=True).stdout
    rows: List[tuple[str, str]] = []
    for line in out.splitlines():
        line = line.strip()
        if not line:
            continue
        parts = line.split("\t")
        status = parts[0][0]                       # 'R100' -> 'R'
        path = parts[-1]                           # last column is the new path
        rows.append((status, path))
    return rows


def _matches_any(path: str, patterns: List[str]) -> bool:
    return any(fnmatch.fnmatch(path, pat) for pat in patterns)


def detect(commit: str = "HEAD", repo_root: str = ".") -> List[ChangedFile]:
    cfg = _load_cfg()
    source_globs = cfg["source_globs"]
    ignore = cfg["ignore_patterns"]
    test_root = cfg["test_root"]

    results: List[ChangedFile] = []
    for status, path in _git_changed(commit, repo_root=repo_root):
        # Only Java files we explicitly track as source
        if not _matches_any(path, source_globs):
            continue
        if _matches_any(path, ignore):
            continue
        is_test = path.startswith(test_root + "/")
        # Tests *changed* by the commit aren't candidates for new test generation,
        # but we still surface them so the orchestrator can avoid double-work.
        results.append(ChangedFile(path=path, status=status, is_test=is_test))
    return results


def main() -> int:
    commit = sys.argv[1] if len(sys.argv) > 1 else "HEAD"
    repo_root = sys.argv[2] if len(sys.argv) > 2 else "."
    import json
    files = detect(commit, repo_root=repo_root)
    json.dump([asdict(f) for f in files], sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
