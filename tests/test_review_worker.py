"""Tests for the review worker."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

import pytest

from foxpatch.claude_runner import ClaudeRunner
from foxpatch.config import AppConfig
from foxpatch.github_client import GitHubClient
from foxpatch.models import ClaudeResult, GitHubPR, RepoRef, ReviewVerdict, Workspace
from foxpatch.review_worker import ReviewWorker
from foxpatch.workspace import WorkspaceManager


@pytest.fixture
def worker(sample_config: AppConfig, mock_github: GitHubClient) -> ReviewWorker:
    claude = ClaudeRunner(dry_run=True)
    workspaces = WorkspaceManager(dry_run=True)
    return ReviewWorker(sample_config, mock_github, claude, workspaces)


def test_should_review_normal_pr(worker: ReviewWorker, sample_pr: GitHubPR) -> None:
    assert worker.should_review(sample_pr) is True


def test_should_review_draft(worker: ReviewWorker, sample_pr: GitHubPR) -> None:
    sample_pr.draft = True
    assert worker.should_review(sample_pr) is False


def test_should_review_bot(worker: ReviewWorker, sample_pr: GitHubPR) -> None:
    sample_pr.author = "dependabot[bot]"
    assert worker.should_review(sample_pr) is False


def test_should_review_autodev_pr(worker: ReviewWorker, sample_pr: GitHubPR) -> None:
    sample_pr.labels = ["autodev"]
    assert worker.should_review(sample_pr) is False


def test_should_review_already_reviewed_same_sha(worker: ReviewWorker, sample_pr: GitHubPR) -> None:
    worker._reviewed.add(("test-org/test-repo", 100, "abc123def456"))
    # Same SHA already reviewed — skip regardless of re_review_on_push
    assert worker.should_review(sample_pr) is False


def test_should_review_new_push(worker: ReviewWorker, sample_pr: GitHubPR) -> None:
    # Reviewed at old SHA
    worker._reviewed.add(("test-org/test-repo", 100, "old_sha"))
    # re_review_on_push is True, new SHA should be reviewed
    assert worker.should_review(sample_pr) is True


def test_should_review_no_re_review_on_push(worker: ReviewWorker, sample_pr: GitHubPR) -> None:
    worker.config.github.review.re_review_on_push = False
    # Reviewed at old SHA
    worker._reviewed.add(("test-org/test-repo", 100, "old_sha"))
    # re_review_on_push is False — don't re-review even with new SHA
    assert worker.should_review(sample_pr) is False


def test_should_review_disabled(worker: ReviewWorker, sample_pr: GitHubPR) -> None:
    worker.config.github.review.enabled = False
    assert worker.should_review(sample_pr) is False


def test_parse_review_valid_json(worker: ReviewWorker) -> None:
    output = '{"verdict": "APPROVE", "summary": "Looks good", "comments": []}'
    verdict, body = worker._parse_review(output)
    assert verdict == ReviewVerdict.APPROVE
    assert "Looks good" in body


def test_parse_review_with_comments(worker: ReviewWorker) -> None:
    output = (
        '{"verdict": "REQUEST_CHANGES", "summary": "Issues found", '
        '"comments": [{"path": "src/main.py", "line": 10, "body": "Bug here"}]}'
    )
    verdict, body = worker._parse_review(output)
    assert verdict == ReviewVerdict.REQUEST_CHANGES
    assert "src/main.py" in body
    assert "Bug here" in body


def test_parse_review_invalid_json(worker: ReviewWorker) -> None:
    output = "This is just a plain text review"
    verdict, body = worker._parse_review(output)
    assert verdict == ReviewVerdict.COMMENT
    assert body == output


def test_parse_review_fenced_json_with_scratchpad(worker: ReviewWorker) -> None:
    # Reproduces aleph-rs#191: model emits chain-of-thought (with stray `{:>15}`)
    # before the final structured JSON in a ```json fence.
    output = (
        "Now let me check the `Amount` column - printed with `{:>15}` which is "
        "right-aligned, so the bracket might confuse a naive parser.\n\n"
        "Let me compile the review.\n\n"
        "```json\n"
        '{"verdict": "COMMENT", "summary": "Clean PR with one concern.",\n'
        ' "comments": [{"path": "src/lib.rs", "line": 42, "body": "consider X"}]}\n'
        "```\n\n"
        "**Additional context:** trailing prose."
    )
    verdict, body = worker._parse_review(output)
    assert verdict == ReviewVerdict.COMMENT
    assert "Clean PR with one concern." in body
    assert "src/lib.rs" in body and "consider X" in body
    assert "Now let me check" not in body  # scratchpad must not leak
    assert "{:>15}" not in body


def test_parse_review_array_only_with_prose_verdict(worker: ReviewWorker) -> None:
    # Reproduces aleph-rs#190: prose declares verdict, JSON block is a bare array.
    output = (
        "**Verdict:** `REQUEST_CHANGES`\n\n"
        "**Summary:** This PR has a real bug in the forget envelope.\n\n"
        "**Comments:**\n\n"
        "```json\n"
        '[{"path": "src/forget.rs", "line": 144, "body": "BUG: empty hashes"},\n'
        ' {"path": "src/forget.rs", "line": 166, "body": "PERF: serial loop"}]\n'
        "```"
    )
    verdict, body = worker._parse_review(output)
    assert verdict == ReviewVerdict.REQUEST_CHANGES
    assert "src/forget.rs" in body
    assert "BUG: empty hashes" in body
    assert "PERF: serial loop" in body


def test_parse_review_stray_braces_before_json(worker: ReviewWorker) -> None:
    # No fences: prose contains stray braces, then unfenced JSON object at end.
    output = (
        "Looking at the format string `{:>15}` and the dict `{key: value}`,\n"
        "I think this is fine.\n\n"
        '{"verdict": "APPROVE", "summary": "All good", "comments": []}'
    )
    verdict, body = worker._parse_review(output)
    assert verdict == ReviewVerdict.APPROVE
    assert "All good" in body


def test_parse_review_prose_verdict_request_changes_dash(worker: ReviewWorker) -> None:
    # Tolerate "Verdict: Request-Changes" / "Request Changes" variants.
    output = (
        "Verdict: Request-Changes\n\n"
        "```json\n"
        '[{"path": "a.py", "line": 1, "body": "x"}]\n'
        "```"
    )
    verdict, _ = worker._parse_review(output)
    assert verdict == ReviewVerdict.REQUEST_CHANGES


def test_parse_review_unknown_verdict_falls_back_to_comment(worker: ReviewWorker) -> None:
    output = '{"verdict": "MAYBE", "summary": "unsure", "comments": []}'
    verdict, body = worker._parse_review(output)
    assert verdict == ReviewVerdict.COMMENT
    assert "unsure" in body


@pytest.mark.asyncio
async def test_review_pr_success(
    worker: ReviewWorker, sample_pr: GitHubPR, tmp_path: Path
) -> None:
    workspace = Workspace(
        base_dir=tmp_path,
        primary_repo_dir=tmp_path / "repo",
    )
    (tmp_path / "repo").mkdir()

    worker.github.get_pr_diff = AsyncMock(return_value="diff --git a/file.py b/file.py\n+new line")
    worker.github.post_review = AsyncMock()
    worker.workspaces.create_review_workspace = AsyncMock(return_value=workspace)
    worker.workspaces.cleanup = MagicMock()
    worker.claude.run = AsyncMock(return_value=ClaudeResult(
        success=True,
        output='{"verdict": "APPROVE", "summary": "LGTM", "comments": []}',
        cost_usd=0.1,
    ))

    result = await worker.review_pr(sample_pr)
    assert result.success is True
    worker.github.post_review.assert_called_once()
    assert worker._review_key(sample_pr) in worker._reviewed


@pytest.mark.asyncio
async def test_review_pr_empty_diff(
    worker: ReviewWorker, sample_pr: GitHubPR
) -> None:
    worker.github.get_pr_diff = AsyncMock(return_value="")
    result = await worker.review_pr(sample_pr)
    assert result.success is True


@pytest.mark.asyncio
async def test_review_pr_large_diff_explore_mode(
    worker: ReviewWorker, sample_pr: GitHubPR, tmp_path: Path
) -> None:
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace = Workspace(base_dir=tmp_path, primary_repo_dir=repo_dir)

    large_diff = "diff --git a/big.py b/big.py\n" + ("+x\n" * 10_000)
    worker.config.github.review.max_diff_size = 1_000  # force explore mode

    worker.github.get_pr_diff = AsyncMock(return_value=large_diff)
    worker.github.get_pr_files = AsyncMock(return_value=[
        {"path": "big.py", "additions": 10000, "deletions": 0},
    ])
    worker.github.post_review = AsyncMock()
    worker.workspaces.create_review_workspace = AsyncMock(return_value=workspace)
    worker.workspaces.cleanup = MagicMock()
    worker.claude.run = AsyncMock(return_value=ClaudeResult(
        success=True,
        output='{"verdict": "COMMENT", "summary": "Explored", "comments": []}',
        cost_usd=0.2,
    ))

    result = await worker.review_pr(sample_pr)

    assert result.success is True
    # Diff file was written inside the repo dir so Claude can Read it
    diff_file = repo_dir / ".foxpatch_pr.diff"
    assert diff_file.exists()
    assert diff_file.read_text() == large_diff
    # File list was fetched
    worker.github.get_pr_files.assert_called_once()
    # Claude was invoked (not skipped) with explore prompt
    worker.claude.run.assert_called_once()
    prompt_arg = worker.claude.run.call_args.args[0]
    assert "too large to embed" in prompt_arg
    assert ".foxpatch_pr.diff" in prompt_arg
    assert "big.py" in prompt_arg
    worker.github.post_review.assert_called_once()


@pytest.mark.asyncio
async def test_review_pr_large_diff_file_list_failure(
    worker: ReviewWorker, sample_pr: GitHubPR, tmp_path: Path
) -> None:
    """If get_pr_files fails, explore mode still proceeds with an empty file list."""
    repo_dir = tmp_path / "repo"
    repo_dir.mkdir()
    workspace = Workspace(base_dir=tmp_path, primary_repo_dir=repo_dir)

    worker.config.github.review.max_diff_size = 10
    worker.github.get_pr_diff = AsyncMock(return_value="diff --git a/x b/x\n+big" * 100)
    worker.github.get_pr_files = AsyncMock(side_effect=RuntimeError("gh failed"))
    worker.github.post_review = AsyncMock()
    worker.workspaces.create_review_workspace = AsyncMock(return_value=workspace)
    worker.workspaces.cleanup = MagicMock()
    worker.claude.run = AsyncMock(return_value=ClaudeResult(
        success=True,
        output='{"verdict": "APPROVE", "summary": "ok", "comments": []}',
        cost_usd=0.1,
    ))

    result = await worker.review_pr(sample_pr)
    assert result.success is True
    worker.claude.run.assert_called_once()
