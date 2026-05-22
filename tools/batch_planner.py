"""
batch_planner.py
----------------
Splits the list of changed source files into batches that each map to ONE
sub-agent. Implements requirement #4:

    * batch_size = 4 files per sub-agent
    * max_subagents = 5 sub-agents total

If the commit changed more files than 4 * 5 = 20, the extra files spill over
into a "deferred" bucket that the orchestrator can either skip or queue.

Usage (CLI):
    python -m tools.batch_planner < paths.txt
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import List

import yaml


@dataclass
class Plan:
    batches: List[List[str]] = field(default_factory=list)
    deferred: List[str] = field(default_factory=list)
    batch_size: int = 0
    max_subagents: int = 0


def _load_cfg() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    with cfg_path.open() as f:
        return yaml.safe_load(f)


def plan(files: List[str]) -> Plan:
    cfg = _load_cfg()
    bs = int(cfg["batch_size"])
    cap = int(cfg["max_subagents"])

    # Stable order so reruns produce deterministic batches.
    files = sorted(set(files))

    batches: List[List[str]] = []
    for i in range(0, len(files), bs):
        batches.append(files[i : i + bs])

    deferred: List[str] = []
    if len(batches) > cap:
        # Flatten the overflow batches into deferred and trim.
        for extra in batches[cap:]:
            deferred.extend(extra)
        batches = batches[:cap]

    return Plan(batches=batches, deferred=deferred, batch_size=bs, max_subagents=cap)


def main() -> int:
    files = [ln.strip() for ln in sys.stdin if ln.strip()]
    p = plan(files)
    json.dump(asdict(p), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
