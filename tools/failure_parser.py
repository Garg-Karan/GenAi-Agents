"""
failure_parser.py
-----------------
Parse the captured stdout/stderr of a `mvn test` (or `gradle test`) run into
a structured list of `TestFailure` objects, one per failing test class.

The orchestrator uses this between fix iterations to figure out which test
files the fix sub-agents need to touch.

We deliberately parse stdout rather than `target/surefire-reports/*.xml` so
this also works for compile failures (which never produce surefire XML) and
for the gradle build tool without changing layout assumptions.

Usage (debugging):
    python -m tools.failure_parser < maven-output.txt
"""

from __future__ import annotations

import json
import re
import sys
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Dict, List, Optional


@dataclass
class TestFailure:
    test_class: str                                  # simple name, e.g. "OrderServiceTest"
    fq_test_class: Optional[str] = None              # e.g. "com.acme.OrderServiceTest"
    test_file: Optional[str] = None                  # path if we saw a compile error
    is_compile_error: bool = False
    error_messages: List[str] = field(default_factory=list)


# Maven summary line for an individual failed test:
#     [ERROR]   OrderServiceTest.should_returnOrder:42 expected: <1> but was: <2>
#     [ERROR]   ProductServiceTest.should_findProduct » NullPointer
# The optional ":<line>" is consumed without capturing so the trailing
# detail (assertion message, exception summary) ends up in group(3).
_MVN_TEST_FAIL_RE = re.compile(
    r"^\s*\[ERROR\]\s+(\w+(?:Test|IT|Tests))\.(\w+)(?::\d+)?\s*(.*)$"
)

# Maven / gradle compile error line:
#     [ERROR] /repo/src/test/java/com/acme/OrderServiceTest.java:[42,8] cannot find symbol
#     /repo/src/test/java/com/acme/OrderServiceTest.java:42: error: cannot find symbol
_MVN_COMPILE_RE = re.compile(
    r"^\s*(?:\[ERROR\]\s+)?(\S+\.java):\[?(\d+)[,:](\d+)\]?\s*[:]?\s*(.*)$"
)

# FQCN hints from surefire output:
#     <<< FAILURE! - in com.acme.OrderServiceTest
#     should_foo(com.acme.OrderServiceTest)  Time elapsed: ...
_FQCN_IN_RE = re.compile(r"\bin\s+([\w.]+\.(\w+(?:Test|IT|Tests)))\b")
_FQCN_PAREN_RE = re.compile(r"\(([\w.]+\.(\w+(?:Test|IT|Tests)))\)")


def _looks_like_compile_line(line: str) -> bool:
    """Heuristic: only treat a `.java:line:col` style line as a compile error
    when the message after it sounds like javac. Avoids matching stack frames
    like `at com.acme.Foo.bar(Foo.java:42)` which have `(...)` wrapping."""
    if "(" in line and line.rstrip().endswith(")"):
        return False  # almost certainly a stack trace frame
    return any(
        kw in line.lower()
        for kw in (
            "cannot find symbol",
            "error:",
            "incompatible types",
            "package ",
            "method ",
            "class ",
            "constructor ",
            "is not abstract",
            "unreported exception",
            "expected",
        )
    )


def parse(stdout: str, stderr: str = "") -> List[TestFailure]:
    """Build a list of TestFailure entries from a test-runner's captured output."""
    by_class: Dict[str, TestFailure] = {}

    combined = (stdout or "") + "\n" + (stderr or "")

    for raw in combined.splitlines():
        line = raw.rstrip()
        if not line:
            continue

        # 1. Compile error -- catch these first because they prevent any test
        # from running and the failure summary lines below will be empty.
        m = _MVN_COMPILE_RE.match(line)
        if m and _looks_like_compile_line(line):
            path = m.group(1).replace("\\", "/")
            row, col, msg = m.group(2), m.group(3), m.group(4)
            simple = Path(path).stem
            if not simple.endswith(("Test", "IT", "Tests")):
                # The compile error is in a non-test file (i.e. the source
                # the developer just wrote). The agent's contract is to
                # never edit source, so we skip this; the build break will
                # be surfaced to the user via the final ERROR log.
                continue
            entry = by_class.setdefault(simple, TestFailure(test_class=simple))
            entry.is_compile_error = True
            entry.test_file = entry.test_file or path
            entry.error_messages.append(f"{path}:[{row},{col}] {msg}".strip())
            continue

        # 2. Track an FQCN hint so we can attach it to the next summary line.
        fq = _FQCN_IN_RE.search(line) or _FQCN_PAREN_RE.search(line)
        if fq:
            fqcn, simple = fq.group(1), fq.group(2)
            entry = by_class.setdefault(simple, TestFailure(test_class=simple))
            entry.fq_test_class = entry.fq_test_class or fqcn

        # 3. Maven per-test failure / error summary line.
        m = _MVN_TEST_FAIL_RE.match(line)
        if m:
            simple = m.group(1)
            method = m.group(2)
            detail = (m.group(3) or "").strip()
            # Filter out the section headers ("[ERROR] Failures:", etc.)
            if simple.lower() in {"failures", "errors"}:
                continue
            entry = by_class.setdefault(simple, TestFailure(test_class=simple))
            short = f"{simple}.{method}"
            if detail:
                short = f"{short} -- {detail[:300]}"
            entry.error_messages.append(short)

    # De-duplicate error messages (Maven prints them twice: once inline,
    # once in the [ERROR] Failures: block).
    for f in by_class.values():
        seen = set()
        uniq: List[str] = []
        for msg in f.error_messages:
            if msg in seen:
                continue
            seen.add(msg)
            uniq.append(msg)
        f.error_messages = uniq[:20]  # cap to keep the prompt small

    return list(by_class.values())


def main() -> int:
    data = sys.stdin.read()
    failures = parse(data, "")
    json.dump([asdict(f) for f in failures], sys.stdout, indent=2)
    print()
    return 0


if __name__ == "__main__":
    sys.exit(main())
