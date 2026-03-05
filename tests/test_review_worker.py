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
