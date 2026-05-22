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
    5. fix loop (NEW)     -> if tests fail and the failures are in tests
                             the agent generated, spawn FIX sub-agents with
                             the failure context. Re-run the suite. Repeat
                             up to `max_fix_iterations` times. If still red,
                             stop with exit code 3 and a "manual fix needed"
                             message.
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
    * Self-heal failing tests up to 3x. (NEW) bounded fix loop below

Exit codes:
    0    everything green (or nothing to do)
    2    legacy: tests failed with no fix loop attempt path (kept for safety)
    3    tests still failing after the fix loop exhausted iterations,
         or the failures are outside the tests we generated
    124  overall 20-min timeout fired
    127  `claude` CLI not on PATH
    130  KeyboardInterrupt
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
from typing import Iterable, List, Set

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
GEN_SUBAGENT_PROMPT = (ROOT / "agents" / "subagent_prompt.md").read_text()
FIX_SUBAGENT_PROMPT = (ROOT / "agents" / "fix_subagent_prompt.md").read_text()


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
    """Best-effort: kill the entire process group rooted at pid (POSIX only)."""
    if os.name != "posix":
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGTERM)
    except (ProcessLookupError, PermissionError):
        return
    time.sleep(0.5)
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (ProcessLookupError, PermissionError):
        return


def _remaining_budget(t0: float, overall_timeout: int) -> int:
    """Seconds left under the global wall-clock budget, never below 60."""
    return max(60, overall_timeout - int(time.time() - t0))


# --------------------------------------------------------------------------- #
# One sub-agent run = one `claude -p` subprocess                               #
# --------------------------------------------------------------------------- #
async def _spawn_claude_subagent(
    batch_id: int,
    user_prompt: str,
    system_prompt: str,
    repo_root: str,
    cfg: dict,
    claude_bin: str,
    semaphore: asyncio.Semaphore,
    label: str,
) -> dict:
    """Spawn one `claude -p` subprocess and return its parsed JSON summary."""

    async with semaphore:
        _log(f"{label} #{batch_id} starting")

        # Build the `claude -p` command line.
        #
        # --bare              : skip auto-discovery of plugins/MCP/skills/CLAUDE.md
        #                       so a developer's local config can't change behavior.
        # --append-system-prompt : inject the relevant rulebook (gen or fix).
        # --allowedTools      : NO Bash, NO Agent => sub-agents cannot recurse
        #                       and cannot run arbitrary commands.
        # --permission-mode acceptEdits : auto-accept Write/Edit without prompting.
        # --output-format json : machine-parseable result envelope.
        # --model             : pin the model from config.yaml.
        cmd = [
            claude_bin,
            "-p", user_prompt,
            "--bare",
            "--append-system-prompt", system_prompt,
            "--allowedTools", "Read,Write,Edit,Glob,Grep",
            "--permission-mode", "acceptEdits",
            "--output-format", "json",
            "--model", cfg["model"],
        ]

        per_subagent_timeout = int(cfg.get("subagent_timeout_sec", 600))

        # New process group so we can kill the entire tree on timeout (POSIX).
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            cwd=repo_root,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
            start_new_session=(os.name == "posix"),
        )

        try:
            stdout_b, stderr_b = await asyncio.wait_for(
                proc.communicate(), timeout=per_subagent_timeout
            )
        except asyncio.TimeoutError:
            _log(f"{label} #{batch_id} TIMEOUT after {per_subagent_timeout}s — killing process group")
            _kill_process_group(proc.pid)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            try:
                await proc.wait()
            except Exception:
                pass
            return {"batch_id": batch_id, "error": "subagent_timeout"}
        except asyncio.CancelledError:
            _kill_process_group(proc.pid)
            try:
                proc.kill()
            except ProcessLookupError:
                pass
            raise

        stdout = stdout_b.decode("utf-8", "replace")
        stderr = stderr_b.decode("utf-8", "replace")

        if proc.returncode != 0:
            _log(f"{label} #{batch_id} FAILED (exit {proc.returncode})")
            return {
                "batch_id": batch_id,
                "error": "claude_cli_failed",
                "exit_code": proc.returncode,
                "stderr_tail": stderr[-1500:],
            }

        _log(f"{label} #{batch_id} done")

        # `claude --output-format json` returns a single JSON object whose
        # `result` field is the model's final text.
        summary: dict = {"batch_id": batch_id}
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


async def run_gen_subagent(
    batch_id: int,
    files: List[str],
    repo_root: str,
    cfg: dict,
    claude_bin: str,
    semaphore: asyncio.Semaphore,
) -> dict:
    """Spawn one generation sub-agent for a batch of <=4 source files."""
    targets = [asdict(resolve_test(fp, repo_root=repo_root)) for fp in files]
    user_prompt = (
        "Here is your batch. Generate or update tests for each entry. "
        "Test paths and existence flags are pre-computed — trust them.\n\n"
        f"```json\n{json.dumps(targets, indent=2)}\n```\n\n"
        "Return ONLY the JSON summary specified in your system prompt."
    )
    result = await _spawn_claude_subagent(
        batch_id, user_prompt, GEN_SUBAGENT_PROMPT,
        repo_root, cfg, claude_bin, semaphore, label="gen sub-agent",
    )
    result["files"] = files
    return result


async def run_fix_subagent(
    batch_id: int,
    batch_items: List[dict],
    repo_root: str,
    cfg: dict,
    claude_bin: str,
    semaphore: asyncio.Semaphore,
    iteration: int,
) -> dict:
    """Spawn one fix sub-agent for a batch of <=4 failing test files."""
    user_prompt = (
        "Here is your batch of failing tests. Read the source, read the test, "
        "understand the failure(s), and fix the TEST file. "
        "NEVER modify the source.\n\n"
        f"```json\n{json.dumps(batch_items, indent=2)}\n```\n\n"
        "Return ONLY the JSON summary specified in your system prompt."
    )
    result = await _spawn_claude_subagent(
        batch_id, user_prompt, FIX_SUBAGENT_PROMPT,
        repo_root, cfg, claude_bin, semaphore,
        label=f"fix sub-agent (iter {iteration})",
    )
    result["test_files"] = [it["test_path"] for it in batch_items]
    return result


# --------------------------------------------------------------------------- #
# Sub-agent result plumbing                                                    #
# --------------------------------------------------------------------------- #
def _collect_owned_tests(summaries: Iterable) -> Set[str]:
    """Test file paths the generation agents created or updated.

    The fix loop only touches files in this set so we never modify
    pre-existing tests that happen to be red.
    """
    owned: Set[str] = set()
    for s in summaries:
        if not isinstance(s, dict):
            continue
        parsed = s.get("parsed") or {}
        for r in parsed.get("results", []) or []:
            test = r.get("test")
            action = r.get("action")
            if test and action in ("created", "updated"):
                owned.add(test)
    return owned


def _test_to_source_path(test_path: str) -> str:
    """src/test/java/com/acme/OrderServiceTest.java -> src/main/java/com/acme/OrderService.java"""
    p = test_path.replace("src/test/java", "src/main/java")
    suffix = "Test.java"
    if p.endswith(suffix):
        p = p[: -len(suffix)] + ".java"
    return p


def _build_fix_batches(failures, cfg: dict) -> List[List[dict]]:
    """Group failures by test file, then chunk into sub-agent batches."""
    bs = int(cfg["batch_size"])
    cap = int(cfg["max_subagents"])

    by_test: dict[str, list] = {}
    for f in failures:
        if not f.test_file:
            continue
        by_test.setdefault(f.test_file, []).append(f)

    items: List[dict] = []
    for test_file, fs in sorted(by_test.items()):
        items.append({
            "source_path": _test_to_source_path(test_file),
            "test_path": test_file,
            "test_class": fs[0].test_class,
            "failures": [
                {
                    "method": f.test_method,
                    "type": f.failure_type,
                    "message": f.message,
                    "detail": f.detail[:1500],
                }
                for f in fs
            ],
        })

    batches = [items[i : i + bs] for i in range(0, len(items), bs)]
    if len(batches) > cap:
        batches = batches[:cap]
    return batches


# --------------------------------------------------------------------------- #
# Top-level run                                                                #
# --------------------------------------------------------------------------- #
async def run(commit: str, repo_root: str) -> int:
    cfg = load_cfg()
    overall_timeout = int(cfg["overall_timeout_sec"])
    max_fix_iter = int(cfg.get("max_fix_iterations", 3))
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

    # === Phase 1: generate tests =========================================== #
    gen_sem = asyncio.Semaphore(min(len(p.batches), int(cfg["max_subagents"])))
    gen_tasks = [
        asyncio.create_task(
            run_gen_subagent(i, batch, repo_root, cfg, claude_bin, gen_sem),
            name=f"gen-subagent-{i}",
        )
        for i, batch in enumerate(p.batches, start=1)
    ]

    try:
        # GLOBAL kill switch — requirement #5
        summaries = await asyncio.wait_for(
            asyncio.gather(*gen_tasks, return_exceptions=True),
            timeout=_remaining_budget(t0, overall_timeout),
        )
    except asyncio.TimeoutError:
        _log(f"GLOBAL TIMEOUT after {overall_timeout}s — cancelling all sub-agents")
        for t in gen_tasks:
            if not t.done():
                t.cancel()
        await asyncio.gather(*gen_tasks, return_exceptions=True)
        _log("ERROR: agent run aborted due to overall timeout")
        return 124

    print("\n===== generation sub-agent summaries =====")
    for s in summaries:
        if isinstance(s, BaseException):
            print(json.dumps({"error": str(s)}, indent=2))
        else:
            print(json.dumps(s, indent=2))

    agent_owned_tests = _collect_owned_tests(summaries)
    if not agent_owned_tests:
        _log("no test files were created or updated by the generation phase")
    else:
        _log(f"agent owns {len(agent_owned_tests)} test file(s) for the fix loop")

    # === Phase 2: initial test run ========================================= #
    _log("running test suite to validate generated tests…")
    result = test_runner.run(timeout_sec=_remaining_budget(t0, overall_timeout))
    print("\n===== test runner (initial) =====")
    print(json.dumps(asdict(result), indent=2))

    # === Phase 3: bounded fix loop ========================================= #
    iterations_used = 0
    manual_fix_reason: str | None = None

    while not result.success and iterations_used < max_fix_iter:
        failures = failure_parser.parse(repo_root, result, cfg)

        if not failures:
            manual_fix_reason = (
                "test run failed but the failure parser found nothing in "
                f"{cfg.get('test_report_dir')} or stdout — manual fix required"
            )
            _log(f"ERROR: {manual_fix_reason}")
            break

        our_failures = [f for f in failures if f.test_file in agent_owned_tests]
        if not our_failures:
            manual_fix_reason = (
                f"{len(failures)} test failure(s) detected but none are in "
                "files the agent generated — manual fix required"
            )
            _log(f"ERROR: {manual_fix_reason}")
            break

        iterations_used += 1
        n_files = len({f.test_file for f in our_failures})
        _log(
            f"fix iteration {iterations_used}/{max_fix_iter}: "
            f"fixing {len(our_failures)} failure(s) across {n_files} test file(s)"
        )

        fix_batches = _build_fix_batches(our_failures, cfg)
        if not fix_batches:
            manual_fix_reason = (
                "could not form any fix batches from the failures — manual fix required"
            )
            _log(f"ERROR: {manual_fix_reason}")
            break

        fix_sem = asyncio.Semaphore(min(len(fix_batches), int(cfg["max_subagents"])))
        fix_tasks = [
            asyncio.create_task(
                run_fix_subagent(
                    i, batch, repo_root, cfg, claude_bin, fix_sem, iterations_used
                ),
                name=f"fix-subagent-iter{iterations_used}-{i}",
            )
            for i, batch in enumerate(fix_batches, start=1)
        ]

        try:
            fix_summaries = await asyncio.wait_for(
                asyncio.gather(*fix_tasks, return_exceptions=True),
                timeout=_remaining_budget(t0, overall_timeout),
            )
        except asyncio.TimeoutError:
            _log(
                f"GLOBAL TIMEOUT during fix iteration {iterations_used} "
                f"after {overall_timeout}s — cancelling"
            )
            for t in fix_tasks:
                if not t.done():
                    t.cancel()
            await asyncio.gather(*fix_tasks, return_exceptions=True)
            return 124

        print(f"\n===== fix sub-agent summaries (iteration {iterations_used}) =====")
        for s in fix_summaries:
            if isinstance(s, BaseException):
                print(json.dumps({"error": str(s)}, indent=2))
            else:
                print(json.dumps(s, indent=2))

        _log(f"re-running test suite after fix iteration {iterations_used}…")
        result = test_runner.run(timeout_sec=_remaining_budget(t0, overall_timeout))
        print(f"\n===== test runner (after fix iteration {iterations_used}) =====")
        print(json.dumps(asdict(result), indent=2))

    # === Phase 4: outcome ================================================== #
    if not result.success:
        if not manual_fix_reason and iterations_used >= max_fix_iter:
            manual_fix_reason = (
                f"tests still failing after {max_fix_iter} fix iteration(s) "
                "— manual fix required"
            )
            _log(f"ERROR: {manual_fix_reason}")
        elapsed = round(time.time() - t0, 1)
        _log(
            f"done in {elapsed}s — tests FAILED "
            f"({iterations_used} fix iteration(s) used). "
            f"reason: {manual_fix_reason or 'unknown'}"
        )
        return 3

    # Green path — auto-commit if configured.
    if cfg.get("auto_commit_on_green", False):
        _log(
            f"tests green after {iterations_used} fix iteration(s) — "
            "auto-committing generated tests…"
        )
        commit_res = auto_commit_tool.commit(repo_root)
        print("\n===== auto-commit =====")
        print(json.dumps(asdict(commit_res), indent=2))

    elapsed = round(time.time() - t0, 1)
    _log(
        f"done in {elapsed}s — tests PASSED "
        f"({iterations_used} fix iteration(s) used)"
    )
    return 0


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
