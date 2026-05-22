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
    5. auto_commit        -> commit generated tests on green (tagged so the
                             post-commit hook bails out and we don't loop)

Hard guarantees (matching the user's requirements):
    * Only sub-agents touch test files. (1, 7)
    * Spring Boot Java only.            (2)
    * JUnit 5 + Mockito only.           (3)  enforced in subagent_prompt.md
    * >4 files -> multiple sub-agents.  (4)  enforced in batch_planner
    * Max 5 parallel sub-agents.        (4)  asyncio.Semaphore + planner cap
    * Force-stop everything after 20m.  (5)  asyncio.wait_for + process kill
    * Tests appended with `Test`.       (6)  test_resolver
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


# --------------------------------------------------------------------------- #
# Helpers                                                                      #
# --------------------------------------------------------------------------- #
ROOT = Path(__file__).resolve().parent.parent
CFG_PATH = ROOT / "config" / "config.yaml"
SUBAGENT_PROMPT = (ROOT / "agents" / "subagent_prompt.md").read_text()


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
        #
        # --bare              : skip auto-discovery of plugins/MCP/skills/CLAUDE.md
        #                       so a developer's local config can't change behavior.
        # --append-system-prompt : inject our sub-agent rulebook.
        # --allowedTools      : NO Bash, NO Agent => sub-agents cannot recurse
        #                       (requirement #4) and cannot run arbitrary commands.
        # --permission-mode acceptEdits : auto-accept Write/Edit without prompting.
        # --output-format json : machine-parseable result envelope.
        # --model             : pin the model from config.yaml.
        cmd = [
            claude_bin,
            "-p", user_prompt,
            "--bare",
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

    _log("running test suite to validate generated tests…")
    remaining = max(60, overall_timeout - int(time.time() - t0))
    result = test_runner.run(timeout_sec=remaining)
    print("\n===== test runner =====")
    print(json.dumps(asdict(result), indent=2))

    if result.success and cfg.get("auto_commit_on_green", False):
        _log("tests green — auto-committing generated tests…")
        commit_res = auto_commit_tool.commit(repo_root)
        print("\n===== auto-commit =====")
        print(json.dumps(asdict(commit_res), indent=2))
    elif not result.success:
        _log("tests failed — NOT auto-committing. Review the generated tests manually.")

    elapsed = round(time.time() - t0, 1)
    _log(f"done in {elapsed}s — tests {'PASSED' if result.success else 'FAILED'}")
    return 0 if result.success else 2


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
