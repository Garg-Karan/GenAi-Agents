"""
test_resolver.py
----------------
Given a Spring Boot source file like
    src/main/java/com/acme/svc/OrderService.java
returns:
    src/test/java/com/acme/svc/OrderServiceTest.java
plus a flag indicating whether that test file already exists.

This is the deterministic implementation of requirement #6
("make test classes if not exist appending Test after class name").

Usage:
    python -m tools.test_resolver <source_path>
"""

from __future__ import annotations

import json
import sys
from dataclasses import dataclass, asdict
from pathlib import Path

import yaml


@dataclass
class TestTarget:
    source_path: str
    test_path: str
    test_exists: bool
    class_name: str
    test_class_name: str


def _load_cfg() -> dict:
    cfg_path = Path(__file__).resolve().parent.parent / "config" / "config.yaml"
    with cfg_path.open() as f:
        return yaml.safe_load(f)


def resolve(source_path: str, repo_root: str = ".") -> TestTarget:
    cfg = _load_cfg()
    test_root = cfg["test_root"]
    suffix = cfg["test_suffix"]

    src = Path(source_path)
    if "src/main/java" not in src.as_posix():
        raise ValueError(f"Not a main-source Java file: {source_path}")

    # Swap src/main/java -> src/test/java, append `Test` to the filename stem.
    parts = list(src.parts)
    idx = parts.index("main")
    parts[idx] = "test"
    test_dir = Path(*parts).parent
    class_name = src.stem
    test_class_name = f"{class_name}{suffix}"
    test_path = test_dir / f"{test_class_name}.java"

    return TestTarget(
        source_path=src.as_posix(),
        test_path=test_path.as_posix(),
        test_exists=(Path(repo_root) / test_path).is_file(),
        class_name=class_name,
        test_class_name=test_class_name,
    )


def main() -> int:
    if len(sys.argv) < 2:
        print("usage: test_resolver.py <source_path>", file=sys.stderr)
        return 2
    target = resolve(sys.argv[1])
    json.dump(asdict(target), sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
