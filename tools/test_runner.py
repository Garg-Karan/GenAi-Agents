"""
test_runner.py
--------------
Runs the build tool's test target (mvn / gradle) and returns a structured result.
Called by the orchestrator after sub-agents finish, so we know whether the
generated tests actually pass.

Usage:
    python -m tools.test_runner                 # run all tests
    python -m tools.test_runner ClassA ClassB   # run only those test classes
"""

from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import List, Optional

import yaml


@dataclass
class TestRunResult:
    success: bool
    exit_code: int
    duration_sec: float
    cmd: str
    stdout_tail: str
    stderr_tail: str


def _load_cfg() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    with cfg_path.open() as f:
        return yaml.safe_load(f)


def _build_cmd(cfg: dict, classes: Optional[List[str]]) -> str:
    base = cfg["build_test_cmd"]
    if not classes:
        return base
    if cfg["build_tool"] == "maven":
        return f"{base} -Dtest={','.join(classes)}"
    if cfg["build_tool"] == "gradle":
        # one --tests flag per class
        return base + " " + " ".join(f"--tests {c}" for c in classes)
    return base


def run(classes: Optional[List[str]] = None, timeout_sec: int = 900) -> TestRunResult:
    cfg = _load_cfg()
    cmd = _build_cmd(cfg, classes)
    t0 = time.time()
    try:
        proc = subprocess.run(
            shlex.split(cmd),
            check=False,
            capture_output=True,
            text=True,
            timeout=timeout_sec,
        )
        return TestRunResult(
            success=(proc.returncode == 0),
            exit_code=proc.returncode,
            duration_sec=round(time.time() - t0, 2),
            cmd=cmd,
            stdout_tail=proc.stdout[-4000:],
            stderr_tail=proc.stderr[-4000:],
        )
    except subprocess.TimeoutExpired as e:
        return TestRunResult(
            success=False,
            exit_code=-1,
            duration_sec=round(time.time() - t0, 2),
            cmd=cmd,
            stdout_tail=(e.stdout or b"").decode("utf-8", "replace")[-2000:],
            stderr_tail=f"TIMEOUT after {timeout_sec}s",
        )


def main() -> int:
    classes = sys.argv[1:] or None
    result = run(classes)
    json.dump(asdict(result), sys.stdout, indent=2)
    print()
    return 0 if result.success else 1


if __name__ == "__main__":
    sys.exit(main())
