# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**foxpatch** — an asyncio Python daemon that polls GitHub repos for labeled issues and PRs, then spawns Claude Code CLI instances to resolve issues as PRs and review incoming PRs. Uses GitHub labels as a state machine (no database).

## Commands

```bash
# Install (editable, with dev deps)
pip install -e ".[dev]"

# Run all tests
pytest tests/ -v

# Run a single test
pytest tests/test_config.py::test_load_sample_config -v

# Lint
ruff check src/ tests/

# Type check
mypy src/

# Run the daemon (requires gh + claude CLIs authenticated)
foxpatch --config config.example.yaml --once --dry-run
```

The venv is at `.venv/` — use `.venv/bin/python -m pytest` if not activated.

## Architecture

The system has two parallel pipelines that share the same polling loop:

### Issue Resolution Pipeline
`orchestrator._run_cycle()` → `state.is_actionable()` → `issue_worker.process_issue()`

1. **Claim** — `StateManager.transition_to_in_progress()` re-fetches labels to mitigate TOCTOU races, then adds `autodev:in-progress`
2. **Clone** — `WorkspaceManager.create_workspace()` creates a temp dir, clones the primary repo (and sibling repos from its repo group via `--add-dir`)
3. **Run Claude** — `ClaudeRunner.run()` invokes `claude -p` with `--dangerously-skip-permissions --output-format json`, parses JSON result
4. **Push & PR** — verifies commits exist (`git log origin/{default}..HEAD`), pushes branch, creates PR via `gh pr create`
5. **Finalize** — transitions label to `autodev:done` or `autodev:failed`, posts comment, cleans up workspace

### PR Review Pipeline
`orchestrator._run_cycle()` → `review_worker.should_review()` → `review_worker.review_pr()`

1. **Filter** — skips drafts, bot PRs, autodev's own PRs, already-reviewed PRs (tracked by `(repo, number, head_sha)` tuple in memory)
2. **Clone** — shallow clone of PR branch
3. **Run Claude** — review prompt with diff embedded, read-only tools only, lower budget
4. **Post** — parses Claude's JSON verdict (`APPROVE`/`REQUEST_CHANGES`/`COMMENT`), posts via `gh pr review`

### Concurrency Model
The `Orchestrator` uses two separate `asyncio.Semaphore` instances — one for issue tasks, one for reviews — so reviews don't starve issue resolution. Dispatch is fire-and-forget via `asyncio.create_task()` + `asyncio.gather()`.

### State Machine
GitHub labels are the sole state store. The trigger label (`autodev`) is never removed. State labels (`autodev:in-progress`, `autodev:done`, `autodev:failed`) are mutually exclusive. To retry a failed issue, remove the `autodev:failed` label.

### Multi-Repo Groups
When an issue's repo belongs to a `repo_group` in config, all group repos are cloned as siblings. The primary repo gets the working branch; sibling repos are passed as `--add-dir` (read-only context). PR is created only on the primary.

### External CLIs
All GitHub operations go through `gh` CLI (not the REST API). All AI operations go through `claude` CLI (not the API). Both are invoked via `asyncio.create_subprocess_exec`. The `dry_run` flag on `GitHubClient` and `ClaudeRunner` logs actions without executing them.

## Testing Patterns

- Tests use `unittest.mock.AsyncMock` to mock async subprocess calls
- The `mock_github` fixture creates a dry-run `GitHubClient` with `_run_gh` and `_run_gh_json` replaced by `AsyncMock`
- JSON fixtures in `tests/fixtures/` provide sample `gh` CLI responses
- `asyncio_mode = "auto"` in pyproject.toml — async tests just need `async def` (no `@pytest.mark.asyncio` required, though some tests still use it)

## Code Style

- Python 3.11+, `from __future__ import annotations` in every module
- `ruff` with rules `E, F, I, N, W`, line length 100
- `mypy --strict`
- Dataclasses for all models and config (no Pydantic)
- All I/O is async; sync helpers only for CPU-bound work (e.g., `_slugify`, `_parse_review`)
