# Spring Boot Test-Generation Agent

A multi-agent system that, the moment a developer commits Java code, writes or
updates JUnit 5 + Mockito tests for the changed source files. Built on the
Claude Agent SDK with a Python orchestrator.

## How the requirements map to the code

| # | Requirement                                                       | Where it lives |
|---|-------------------------------------------------------------------|----------------|
| 1 | Trigger on commit; write tests for committed code; update existing tests only where gaps exist; test files never contain the source code | `hooks/post-commit`, `agents/subagent_prompt.md` |
| 2 | Spring Boot Java only                                             | `config/config.yaml` source_globs + ignore_patterns |
| 3 | JUnit 5 + Mockito only                                            | `agents/subagent_prompt.md` (hard rules) |
| 4 | >4 files → multiple sub-agents; max 5 sub-agents in parallel      | `tools/batch_planner.py` + `asyncio.Semaphore` in orchestrator |
| 5 | Force-stop everything after 20 min                                | `asyncio.wait_for(..., overall_timeout_sec)` + process-group kill |
| 6 | Create `<Class>Test.java` if missing                              | `tools/test_resolver.py` |
| 7 | Always run the required tools                                     | Orchestrator calls tools deterministically before/after the LLM |
| — | Write → run → auto-commit on green                                | `tools/auto_commit.py`, guarded by `auto_commit_on_green` |

## Architecture

```
git commit
    │
    ▼
.git/hooks/post-commit ──► nohup python -m agents.orchestrator
                                      │
                       ┌──────────────┼──────────────┐
                       ▼              ▼              ▼
              detect_changes   batch_planner   test_resolver   (deterministic Python tools)
                       │              │              │
                       └──────────────┼──────────────┘
                                      ▼
                       up to 5 `claude -p` subprocesses in parallel
                       (each owns ≤4 files, JUnit 5 + Mockito only,
                        no Bash/Agent tools, --bare for clean context)
                                      │
                                      ▼
                              test_runner (mvn test)
                                      │
                                  green? ── no ──► stop, surface failures
                                      │
                                     yes
                                      │
                                      ▼
                              auto_commit  (message tagged [skip test-agent]
                                            so the hook bails on the next loop)
```

### How the sub-agents work

Each sub-agent is a separate `claude -p` subprocess spawned by Python's
`asyncio.create_subprocess_exec` with `start_new_session=True` (its own
process group). The orchestrator passes:

* `--bare` — skip auto-discovery of plugins/MCP/CLAUDE.md so behavior is
  identical on every machine
* `--append-system-prompt` — inject the `subagent_prompt.md` rulebook
* `--allowedTools "Read,Write,Edit,Glob,Grep"` — deliberately no `Bash`
  and no `Agent`, so sub-agents can't recurse or run arbitrary commands
* `--permission-mode acceptEdits` — auto-accept file edits, no prompts
* `--output-format json` — structured envelope we can parse

When the 20-minute global timeout fires, each task's `CancelledError` handler
calls `os.killpg(...)` on the subprocess's process group, killing `claude`
and anything it spawned. This is the part the SDK version got "for free" —
in the CLI version we have to do it explicitly.

### Loop prevention

The auto-commit creates a new HEAD, which fires the post-commit hook again.
The hook reads the commit message and exits early when it sees
`[skip test-agent]`. The tag is set in `config.yaml` (`auto_commit_message`)
and matched in `hooks/post-commit` — keep them in sync if you change either.

## Why this design

* **Deterministic tools do the routing.** Detecting changed files, picking
  test paths, and splitting batches are pure Python — no LLM needed. The LLM
  only writes the tests. That keeps cost, latency, and flakiness down.
* **Each sub-agent gets a fresh context** with at most 4 files in it. The
  Claude Agent SDK runs sub-agents in isolated conversations, so the main
  orchestrator never sees the full source of every file — exactly the
  context optimization the brief asked for.
* **The orchestrator owns the kill switch.** `asyncio.wait_for` around the
  parallel `gather` guarantees that no matter what a sub-agent does, the
  whole job is dead at 20 minutes and exits with code 124 + an `ERROR`
  log line.
* **Sub-agents cannot recurse.** Their `allowed_tools` list does not include
  `Agent`, so they can't spawn further sub-agents.

## Install

This agent drives your locally-installed `claude` CLI in non-interactive mode
(`claude -p`). It reuses whatever authentication the CLI already has — so if
you can run `claude` from your terminal today, the agent will work.

```bash
# 1. Verify the CLI is installed and authenticated
claude --version
claude -p "say hello"          # should respond without prompting for login

# 2. Install Python deps (just PyYAML)
cd ~/spring-test-agent
pip install -r requirements.txt

# 3. Point a Spring Boot repo at this agent
./hooks/install.sh /path/to/your/spring-boot/repo

# 4. Commit code as usual — the hook runs detached, logs go to
#    <repo>/.test-agent/logs/agent-<sha>.log
```

### Authentication note

This agent uses **`claude -p`** (the CLI's non-interactive mode), not the
Claude Agent SDK. That means it reuses your existing Claude.ai (Pro/Max)
subscription auth — no `ANTHROPIC_API_KEY` required.

A few subscription-related caveats worth knowing:

* **Don't run unattended on accounts you can't afford to lose.** Anthropic's
  consumer terms restrict programmatic/automated use of Claude.ai
  subscriptions. A local post-commit hook that fires when *you* commit is in
  a grey area; running this in CI or on a server clearly isn't allowed.
  If you want to run this on CI, get an API key and use the SDK variant.
* **Starting June 15, 2026**, `claude -p` usage on subscription plans draws
  from a new monthly Agent SDK credit, separate from interactive usage. See
  the Claude Code headless mode docs for the current details.
* **Rate limits apply.** Five parallel `claude` sub-agents from one commit
  count against your subscription like five conversations opened at once.

## Manual run (no hook)

```bash
cd /path/to/spring-boot/repo
python -m agents.orchestrator --commit HEAD --repo .
```

## Tuning

Everything tunable is in `config/config.yaml`: model, batch size, sub-agent
cap, overall timeout, ignore patterns, build command. No code changes needed
to switch between Maven and Gradle, or to change the JUnit/Mockito stance.

## What's intentionally not in here

* **Auto-commit / PR creation.** Generated tests are written to disk and run
  locally; we don't push them. That decision is policy-sensitive and best
  left to a separate CI job.
* **Coverage thresholds.** The sub-agent is told to cover happy / boundary /
  error paths per public method, which is concrete and reviewable. A JaCoCo
  coverage gate can be layered on top in CI without touching this agent.
* **Spring-context test detection.** Controllers get `@WebMvcTest`,
  services get plain Mockito. Anything more elaborate (e.g.
  `@DataJpaTest`, Testcontainers) is left to the sub-agent's judgment of
  the source code.
