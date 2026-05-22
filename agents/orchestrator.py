"""
orchestrator.py
---------------
Main entry-point for the Spring Boot test-generation agent.

This version drives the locally-installed `claude` CLI in non-interactive
mode (`claude -p`) instead of the Claude Agent SDK. Use this when you are
authenticated through your Claude.ai subscription and do NOT have an
ANTHROPIC_API_KEY available for the SDK.

Pipeline:
    1. detect_changes     -> list of changed Java source files (deterministic)
    2. batch_planner      -> split into batches of <=4, capped at 5 sub-agents
    3. for each batch     -> spawn ONE `claude -p` subprocess (parallel)
       sub-agents return  -> JSON summary of created/updated/skipped tests
    4. test_runner        -> compile + run the test suite (mvn/gradle)
    5. fix loop           -> while the suite is red, spawn fix sub-agents that
                             read the failing test + its source and patch the
                             test (only). Capped at `max_fix_iterations`
                             attempts; after that the run exits non-zero with
                             MANUAL FIX REQUIRED.
    6. auto_commit        -> commit generated tests on green (tagged so the
                             post-commit hook bails out and we don't loop)

Hard guarantees (matching the user's requirements):
    * Only sub-agents touch test files. (1, 7)
    * Spring Boot Java only.            (2)
    * JUnit 5 + Mockito only.           (3)  enforced in subagent_prompt.md
    * >4 files -> multiple sub-agents.  (4)  enforced in batch_planner
    * Max 5 parallel sub-agents.        (4)  asyncio.Semaphore + planner cap
    * Force-stop everything after 20m.  (5)  asyncio.wait_for + process kill
    * Tests appended with `Test`.       (6)  test_resolver
    * Auto-fix capped at N attempts.    (NEW) max_fix_iterations in config.yaml
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import signal
import sys
import time
from dataclasses import asdict
from pathlib import Path
from typing import List

import yaml

# project tools
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from tools.detect_changes import detect as detect_changes  # noqa: E402
from tools.batch_planner import plan as plan_batches       # noqa: E402
from tools.test_resolver import resolve as resolve_test    # noqa: E402
from tools import test_runner                              # noqa: E402
from tools import auto_commit as auto_commit_tool          # noqa: E402
from tools import failure_parser                           # noqa: E402


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "config" / "config.yaml"
SUBAGENT_PROMPT = (ROOT / "agents" / "subagent_prompt.md").read_text()
FIX_SUBAGENT_PROMPT = (ROOT / "agents" / "fix_subagent_prompt.md").read_text()

# Exit codes — surfaced to the post-commit hook log.
EXIT_OK = 0
EXIT_TESTS_FAILED = 2          # generation finished, suite still red, no fix loop ran
EXIT_MANUAL_FIX_REQUIRED = 3   # fix loop exhausted max_fix_iterations without going green
EXIT_OVERALL_TIMEOUT = 124
EXIT_CLI_MISSING = 127


def load_cfg() -> dict:
    with CFG_PATH.open() as f:
        return yaml.safe_load(f)


def _log(msg: str) -> None:
    print(f"[orchestrator] {msg}", flush=True)


def _find_claude_cli() -> str:
    """Locate the `claude` binary. Fail fast if missing."""
    path = shutil.which("claude")
    if not path:
        raise RuntimeError(
            "`claude` CLI not found on PATH. Install Claude Code first: "
            "https://docs.claude.com/en/docs/claude-code/overview"
        )
    return path


def _kill_process_group(pid: int) -> None:
    """Best-effort: kill the entire process group rooted at pid."""
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    time.sleep(0.5)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


# --------------------------------------------------------------------------- #
# One sub-agent run = one `claude -p` subprocess                               #
# --------------------------------------------------------------------------- #
async def run_subagent(
    batch_id: int,
    files: List[str],
    repo_root: str,
    cfg: dict,
    claude_bin: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Spawn one `claude -p` subprocess for a batch of <=4 source files."""

    async with semaphore:
        _log(f"sub-agent #{batch_id} starting ({len(files)} files)")

        # Pre-resolve test paths so the sub-agent gets unambiguous targets.
        targets = [asdict(resolve_test(fp, repo_root=repo_root)) for fp in files]

        user_prompt = (
            "Here is your batch. Generate or update tests for each entry. "
            "Test paths and existence flags are pre-computed — trust them.\n\n"
            f"```json\n{json.dumps(targets, indent=2)}\n```\n\n"
            "Return ONLY the JSON summary specified in your system prompt."
        )

        # Build the `claude -p` command line.
        cmd = [
            claude_bin,
            "-p", user_prompt,
            "--append-system-prompt", SUBAGENT_PROMPT,
            "--allowedTools", "Read,Write,Edit,Glob,Grep",
            "--permission-mode", "acceptEdits",
            "--output-format", "json",
            "--model", cfg["model"],
        ]

        per_subagent_timeout = int(cfg.get("subagent_timeout_sec", 600))

        # New process group so we can kill the entire tree on timeout.
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=per_subagent_timeout
            )
        except asyncio.TimeoutError:
            _log(f"sub-agent #{batch_id} TIMEOUT after {per_subagent_timeout}s — killing process group")
            _kill_process_group(proc.pid)
            try:
                await proc.wait()
            except Exception:
                pass
            return {"batch_id": batch_id, "error": "subagent_timeout", "files": files}
        except asyncio.CancelledError:
            _kill_process_group(proc.pid)
            raise

        stdout = stdout_b.decode("utf-8", "replace")
        stderr = stderr_b.decode("utf-8", "replace")

        if proc.returncode != 0:
            _log(f"sub-agent #{batch_id} FAILED (exit {proc.returncode})")
            return {
                "batch_id": batch_id,
                "error": "claude_cli_failed",
                "exit_code": proc.returncode,
                "stdout_tail": stdout[-1500:],
                "stderr_tail": stderr[-1500:],
                "files": files,
            }

        _log(f"sub-agent #{batch_id} done")

        # `claude --output-format json` returns a single JSON object whose
        # `result` field is the model's final text.
        summary: dict = {"batch_id": batch_id, "files": files}
        try:
            envelope = json.loads(stdout)
            final_text = envelope.get("result", "")
            summary["claude_meta"] = {
                "total_cost_usd": envelope.get("total_cost_usd"),
                "duration_ms": envelope.get("duration_ms"),
                "num_turns": envelope.get("num_turns"),
                "session_id": envelope.get("session_id"),
            }
        except json.JSONDecodeError:
            final_text = stdout
            summary["envelope_parse"] = "fallback_to_raw_stdout"

        # Extract the JSON summary the sub-agent emits at the end of its result.
        try:
            start = final_text.find("{")
            end = final_text.rfind("}")
            if start != -1 and end != -1:
                summary["parsed"] = json.loads(final_text[start : end + 1])
            else:
                summary["raw"] = final_text[-2000:]
        except json.JSONDecodeError as e:
            summary["parse_error"] = str(e)
            summary["raw"] = final_text[-2000:]
        return summary


# --------------------------------------------------------------------------- #
# Fix-mode sub-agent — runs after a red test suite                             #
# --------------------------------------------------------------------------- #
def _build_test_map(summaries: List[dict]) -> dict:
    """{simple_test_class_name: {source, test}} from the generation summaries.

    The fix loop uses this to find the source file that goes with each
    failing test class. Tests the agent never generated/updated are absent
    from the map and the fix loop refuses to touch them (likely a
    pre-existing test broken by the source change — developer's call)."""
    out: dict = {}
    for s in summaries:
        if isinstance(s, BaseException) or not isinstance(s, dict):
            continue
        parsed = s.get("parsed") or {}
        for r in parsed.get("results", []) or []:
            test_path = r.get("test") or ""
            source_path = r.get("source") or ""
            if not test_path:
                continue
            simple = Path(test_path).stem  # e.g. "OrderServiceTest"
            out[simple] = {"source": source_path, "test": test_path}
    return out


async def run_fix_subagent(
    iteration: int,
    batch_id: int,
    items: List[dict],
    repo_root: str,
    cfg: dict,
    claude_bin: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Spawn one `claude -p` subprocess to repair a batch of failing tests."""

    async with semaphore:
        _log(
            f"fix sub-agent iter={iteration} batch={batch_id} "
            f"({len(items)} failing test class(es))"
        )

        user_prompt = (
            f"Fix attempt {iteration} of {cfg.get('max_fix_iterations', 3)}.\n"
            "The tests below failed in the last build. For each entry, read "
            "the test file and its source, diagnose the cause, and patch "
            "ONLY the test file. Never modify any source under "
            "`src/main/java/`. Preserve passing tests.\n\n"
            f"```json\n{json.dumps(items, indent=2)}\n```\n\n"
            "Return ONLY the JSON summary specified in your system prompt."
        )

        cmd = [
            claude_bin,
            "-p", user_prompt,
            "--append-system-prompt", FIX_SUBAGENT_PROMPT,
            "--allowedTools", "Read,Write,Edit,Glob,Grep",
            "--permission-mode", "acceptEdits",
            "--output-format", "json",
            "--model", cfg["model"],
        ]

        per_subagent_timeout = int(cfg.get("subagent_timeout_sec", 600))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=True,
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=per_subagent_timeout
            )
        except asyncio.TimeoutError:
            _log(
                f"fix sub-agent iter={iteration} batch={batch_id} "
                f"TIMEOUT after {per_subagent_timeout}s — killing process group"
            )
            _kill_process_group(proc.pid)
            try:
                await proc.wait()
            except Exception:
                pass
            return {
                "iteration": iteration, "batch_id": batch_id,
                "error": "subagent_timeout",
                "tests_targeted": [it["test_class"] for it in items],
            }
        except asyncio.CancelledError:
            _kill_process_group(proc.pid)
            raise

        stdout = stdout_b.decode("utf-8", "replace")
        stderr = stderr_b.decode("utf-8", "replace")

        summary: dict = {
            "iteration": iteration,
            "batch_id": batch_id,
            "tests_targeted": [it["test_class"] for it in items],
        }

        if proc.returncode != 0:
            _log(
                f"fix sub-agent iter={iteration} batch={batch_id} "
                f"FAILED (exit {proc.returncode})"
            )
            summary["error"] = "claude_cli_failed"
            summary["exit_code"] = proc.returncode
            summary["stdout_tail"] = stdout[-1500:]
            summary["stderr_tail"] = stderr[-1500:]
            return summary

        _log(f"fix sub-agent iter={iteration} batch={batch_id} done")

        try:
            envelope = json.loads(stdout)
            final_text = envelope.get("result", "")
            summary["claude_meta"] = {
                "total_cost_usd": envelope.get("total_cost_usd"),
                "duration_ms": envelope.get("duration_ms"),
                "num_turns": envelope.get("num_turns"),
                "session_id": envelope.get("session_id"),
            }
        except json.JSONDecodeError:
            final_text = stdout
            summary["envelope_parse"] = "fallback_to_raw_stdout"

        try:
            start = final_text.find("{")
            end = final_text.rfind("}")
            if start != -1 and end != -1:
                summary["parsed"] = json.loads(final_text[start : end + 1])
            else:
                summary["raw"] = final_text[-2000:]
        except json.JSONDecodeError as e:
            summary["parse_error"] = str(e)
            summary["raw"] = final_text[-2000:]

        return summary


async def run_fix_iteration(
    iteration: int,
    failures: List[failure_parser.TestFailure],
    test_map: dict,
    repo_root: str,
    cfg: dict,
    claude_bin: str,
) -> List[dict]:
    """Build batches from failures and dispatch fix sub-agents in parallel."""

    actionable: List[dict] = []
    skipped: List[str] = []
    for f in failures:
        meta = test_map.get(f.test_class)
        if not meta:
            skipped.append(f.test_class)
            continue
        actionable.append({
            "test_class": f.test_class,
            "fq_test_class": f.fq_test_class,
            "test_path": meta["test"],
            "source_path": meta["source"],
            "is_compile_error": f.is_compile_error,
            "errors": f.error_messages,
        })

    if skipped:
        _log(
            f"iter={iteration}: skipping {len(skipped)} failing test(s) "
            f"this run did not generate — pre-existing or unrelated: {skipped}"
        )
    if not actionable:
        _log(f"iter={iteration}: nothing for the fix loop to do")
        return []

    bs = int(cfg["batch_size"])
    cap = int(cfg["max_subagents"])
    batches = [actionable[i : i + bs] for i in range(0, len(actionable), bs)]
    if len(batches) > cap:
        _log(
            f"iter={iteration}: {len(batches)} fix batches exceeds cap {cap} — "
            "truncating; remaining failures will be retried next iteration"
        )
        batches = batches[:cap]

    sem = asyncio.Semaphore(min(len(batches), cap))
    tasks = [
        asyncio.create_task(
            run_fix_subagent(iteration, i, batch, repo_root, cfg, claude_bin, sem),
            name=f"fix-iter{iteration}-batch{i}",
        )
        for i, batch in enumerate(batches, start=1)
    ]
    return await asyncio.gather(*tasks, return_exceptions=True)


# --------------------------------------------------------------------------- #
# Top-level run                                                                #
# --------------------------------------------------------------------------- #
async def run(commit: str, repo_root: str) -> int:
    cfg = load_cfg()
    overall_timeout = int(cfg["overall_timeout_sec"])
    t0 = time.time()

    try:
        claude_bin = _find_claude_cli()
    except RuntimeError as e:
        _log(f"ERROR: {e}")
        return 127

    _log(f"detecting changes in {commit}…")
    changed = detect_changes(commit, repo_root=repo_root)
    source_files = [c.path for c in changed if not c.is_test]
    if not source_files:
        _log("no eligible source files changed — nothing to do.")
        return 0

    _log(f"{len(source_files)} candidate source file(s)")

    p = plan_batches(source_files)
    _log(
        f"plan: {len(p.batches)} sub-agent(s) "
        f"(batch_size={p.batch_size}, cap={p.max_subagents})"
        + (f", deferred={len(p.deferred)}" if p.deferred else "")
    )
    if p.deferred:
        _log("deferred files (over cap, will not be processed this run):")
        for fp in p.deferred:
            _log(f"  - {fp}")

    sem = asyncio.Semaphore(min(len(p.batches), int(cfg["max_subagents"])))

    tasks = [
        asyncio.create_task(
            run_subagent(i, batch, repo_root, cfg, claude_bin, sem),
            name=f"subagent-{i}",
        )
        for i, batch in enumerate(p.batches, start=1)
    ]

    try:
        # GLOBAL kill switch — requirement #5
        summaries = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=overall_timeout,
        )
    except asyncio.TimeoutError:
        _log(f"GLOBAL TIMEOUT after {overall_timeout}s — cancelling all sub-agents")
        for t in tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        _log("ERROR: agent run aborted due to overall timeout")
        return 124

    print("\n===== sub-agent summaries =====")
    for s in summaries:
        if isinstance(s, BaseException):
            print(json.dumps({"error": str(s)}, indent=2))
        else:
            print(json.dumps(s, indent=2))

    # Map every test class this run produced -> the (source, test) pair, so
    # the fix loop knows which file to patch and where to find the contract.
    test_map = _build_test_map(
        [s for s in summaries if isinstance(s, dict)]
    )

    # ------------------------------------------------------------------- #
    # Test + auto-fix loop                                                 #
    # ------------------------------------------------------------------- #
    # attempt 0 = the initial run (no fixes yet).
    # attempts 1..max_fix_iters = fix iterations, each followed by a re-run.
    # After max_fix_iters fixes have not gone green, exit with
    # EXIT_MANUAL_FIX_REQUIRED so the developer sees the prompt to step in.
    max_fix_iters = int(cfg.get("max_fix_iterations", 3))

    result = None
    for attempt in range(max_fix_iters + 1):
        label = "initial" if attempt == 0 else f"after fix {attempt}/{max_fix_iters}"
        _log(f"running test suite ({label})…")
        remaining = max(60, overall_timeout - int(time.time() - t0))
        result = test_runner.run(timeout_sec=remaining, cwd=repo_root)
        print(f"\n===== test runner ({label}) =====")
        print(json.dumps(asdict(result), indent=2))

        if result.success:
            if attempt > 0:
                _log(f"tests green after {attempt} fix iteration(s)")
            break

        if attempt >= max_fix_iters:
            _log(
                f"ERROR: tests still failing after {max_fix_iters} fix "
                "iteration(s) — MANUAL FIX REQUIRED. Review the test runner "
                "output above and patch the failing tests yourself."
            )
            break

        failures = failure_parser.parse(result.stdout_tail, result.stderr_tail)
        if not failures:
            _log(
                "WARN: build is red but no failing test class could be "
                "parsed from the runner output — aborting fix loop. "
                "Check the test runner stdout/stderr above."
            )
            break

        _log(
            f"detected {len(failures)} failing test class(es): "
            f"{[f.test_class for f in failures]}"
        )

        fix_summaries = await run_fix_iteration(
            iteration=attempt + 1,
            failures=failures,
            test_map=test_map,
            repo_root=repo_root,
            cfg=cfg,
            claude_bin=claude_bin,
        )
        print(f"\n===== fix iteration {attempt + 1} summaries =====")
        for s in fix_summaries:
            if isinstance(s, BaseException):
                print(json.dumps({"error": str(s)}, indent=2))
            else:
                print(json.dumps(s, indent=2))

        if not fix_summaries:
            # run_fix_iteration logged the reason already (nothing actionable).
            _log("no fix sub-agents ran — exiting fix loop")
            break

    # ------------------------------------------------------------------- #
    # Final outcome                                                        #
    # ------------------------------------------------------------------- #
    if result and result.success and cfg.get("auto_commit_on_green", False):
        _log("tests green — auto-committing generated tests…")
        commit_res = auto_commit_tool.commit(repo_root)
        print("\n===== auto-commit =====")
        print(json.dumps(asdict(commit_res), indent=2))
    elif not (result and result.success):
        _log(
            "tests failed — NOT auto-committing. Review the generated tests "
            "manually (see failures above)."
        )

    elapsed = round(time.time() - t0, 1)
    passed = bool(result and result.success)
    _log(f"done in {elapsed}s — tests {'PASSED' if passed else 'FAILED'}")
    if passed:
        return EXIT_OK
    return EXIT_MANUAL_FIX_REQUIRED


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--commit", default="HEAD", help="commit SHA (default HEAD)")
    parser.add_argument("--repo", default=os.getcwd(), help="repo root")
    args = parser.parse_args()
    try:
        return asyncio.run(run(args.commit, args.repo))
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
