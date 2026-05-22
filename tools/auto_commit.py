"""
auto_commit.py
--------------
After sub-agents finish and the test suite passes, commit the generated
test files. The commit message contains `[skip test-agent]` so the
post-commit hook bails out and we don't loop.

This is intentionally narrow:
  * Only files under `<test_root>/` are staged.
  * If nothing is staged after that filter, we exit 0 with no commit.
  * Author identity is set per-commit (-c flags) so the developer's
    git config is not modified.

Usage:
    python -m tools.auto_commit --repo /path/to/repo
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List

import yaml


@dataclass
class CommitResult:
    committed: bool
    sha: str | None
    files: List[str]
    message: str
    reason: str | None = None  # only set when committed=False


def _load_cfg() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    with cfg_path.open() as f:
        return yaml.safe_load(f)


def _git(repo: str, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(
        ["git", "-C", repo, *args],
        check=check,
        capture_output=True,
        text=True,
    )


def _changed_test_files(repo: str, test_root: str) -> List[str]:
    """Files under test_root that are new or modified vs HEAD."""
    out = _git(repo, "status", "--porcelain", "--", test_root).stdout
    files: List[str] = []
    for line in out.splitlines():
        # porcelain: "XY path"  (X=index, Y=worktree)
        if len(line) < 4:
            continue
        path = line[3:].strip()
        # Skip deletions
        if line[0] == "D" or line[1] == "D":
            continue
        files.append(path)
    return files


def commit(repo: str) -> CommitResult:
    cfg = _load_cfg()

    if not cfg.get("auto_commit_on_green", False):
        return CommitResult(
            committed=False, sha=None, files=[],
            message="", reason="auto_commit_on_green=false",
        )

    test_root = cfg["test_root"]
    files = _changed_test_files(repo, test_root)
    if not files:
        return CommitResult(
            committed=False, sha=None, files=[],
            message="", reason="no test files changed",
        )

    # Stage only the test files — never touch source.
    _git(repo, "add", "--", *files)

    # Confirm something is actually staged after the filter.
    staged = _git(repo, "diff", "--cached", "--name-only").stdout.strip().splitlines()
    if not staged:
        return CommitResult(
            committed=False, sha=None, files=[],
            message="", reason="nothing staged after filter",
        )

    msg = cfg["auto_commit_message"]
    # Per-commit author identity, doesn't mutate user's git config.
    name = cfg["auto_commit_author_name"]
    email = cfg["auto_commit_author_email"]
    _git(
        repo,
        "-c", f"user.name={name}",
        "-c", f"user.email={email}",
        "commit", "-m", msg,
    )
    sha = _git(repo, "rev-parse", "HEAD").stdout.strip()

    return CommitResult(committed=True, sha=sha, files=staged, message=msg)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=".", help="repo root")
    args = parser.parse_args()
    result = commit(args.repo)
    json.dump(asdict(result), sys.stdout, indent=2)
    print()
    return 0 if result.committed or result.reason else 1


if __name__ == "__main__":
    sys.exit(main())
