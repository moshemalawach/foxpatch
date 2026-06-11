"""Tests for the PR revision worker."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from foxpatch.claude_runner import ClaudeRunner
from foxpatch.config import AppConfig
from foxpatch.github_client import GitHubClient
from foxpatch.models import ClaudeResult, GitHubPR, PRReview, Workspace
from foxpatch.revision_worker import RevisionWorker
from foxpatch.workspace import WorkspaceManager


@pytest.fixture
def revision_worker(sample_config: AppConfig, mock_github: GitHubClient) -> RevisionWorker:
    claude = ClaudeRunner(dry_run=True)
    workspaces = WorkspaceManager(dry_run=True)
    return RevisionWorker(sample_config, mock_github, claude, workspaces)


@pytest.fixture
def autodev_pr(repo_ref) -> GitHubPR:
    return GitHubPR(
        repo=repo_ref,
        number=100,
        title="Fix #42: Fix login bug",
        body="Automated fix for #42.",
        author="foxpatch-bot",
        head_sha="abc123",
        head_ref="autodev/issue-42-fix-login",
        base_ref="main",
        labels=["autodev", "autodev:done"],
    )


async def test_needs_revision_not_autodev_pr(
    revision_worker: RevisionWorker, sample_pr: GitHubPR,
) -> None:
    # PR without autodev label should not need revision
    assert await revision_worker.needs_revision(sample_pr) is False


async def test_needs_revision_no_changes_requested(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR,
) -> None:
    revision_worker.github.get_pr_reviews = AsyncMock(return_value=[
        PRReview(author="reviewer", state="APPROVED", body="LGTM"),
    ])
    assert await revision_worker.needs_revision(autodev_pr) is False


async def test_needs_revision_review_on_current_head(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR,
) -> None:
    revision_worker.github.get_pr_reviews = AsyncMock(return_value=[
        PRReview(
            author="reviewer", state="CHANGES_REQUESTED",
            body="Fix tests", commit_sha="abc123",
        ),
    ])
    assert await revision_worker.needs_revision(autodev_pr) is True


async def test_needs_revision_review_on_old_head_skipped(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR,
) -> None:
    # Feedback was given on an older commit; we already pushed past it,
    # so it must not re-trigger (prevents revision loops).
    revision_worker.github.get_pr_reviews = AsyncMock(return_value=[
        PRReview(
            author="reviewer", state="CHANGES_REQUESTED",
            body="Fix tests", commit_sha="old-sha",
        ),
    ])
    assert await revision_worker.needs_revision(autodev_pr) is False


async def test_needs_revision_new_review_on_new_head(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR,
) -> None:
    # Old addressed feedback plus a fresh review on the current head.
    revision_worker.github.get_pr_reviews = AsyncMock(return_value=[
        PRReview(
            author="reviewer1", state="CHANGES_REQUESTED",
            body="Fix tests", commit_sha="old-sha",
        ),
        PRReview(
            author="reviewer2", state="CHANGES_REQUESTED",
            body="Also fix docs", commit_sha="abc123",
        ),
    ])
    assert await revision_worker.needs_revision(autodev_pr) is True


async def test_needs_revision_gives_up_after_failures(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR,
) -> None:
    revision_worker.github.get_pr_reviews = AsyncMock(return_value=[
        PRReview(
            author="reviewer", state="CHANGES_REQUESTED",
            body="Fix tests", commit_sha="abc123",
        ),
    ])
    key = revision_worker._revision_key(autodev_pr)
    revision_worker._failures[key] = revision_worker._MAX_REVISION_ATTEMPTS
    assert await revision_worker.needs_revision(autodev_pr) is False


async def test_revise_pr_success(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR, tmp_path: Path,
) -> None:
    workspace = Workspace(
        base_dir=tmp_path,
        primary_repo_dir=tmp_path / "repo",
        branch_name="autodev/issue-42-fix-login",
    )
    (tmp_path / "repo").mkdir()

    revision_worker.github.get_pr_reviews = AsyncMock(return_value=[
        PRReview(
            author="reviewer", state="CHANGES_REQUESTED",
            body="Fix the tests", commit_sha="abc123",
        ),
    ])
    revision_worker.github.get_pr_check_failures = AsyncMock(return_value=[])
    revision_worker.github.get_pr_comments = AsyncMock(return_value=[])
    revision_worker.github.post_comment = AsyncMock()
    revision_worker.workspaces.create_revision_workspace = AsyncMock(
        return_value=workspace,
    )
    revision_worker.workspaces.cleanup = AsyncMock()
    revision_worker.claude.run = AsyncMock(return_value=ClaudeResult(
        success=True, output="Fixed tests", cost_usd=1.5,
    ))
    revision_worker._has_new_commits = AsyncMock(return_value=True)
    revision_worker._push_revision = AsyncMock()

    result = await revision_worker.revise_pr(autodev_pr)
    assert result.success is True
    revision_worker.workspaces.cleanup.assert_called_once()
    assert revision_worker._failures == {}


async def test_revise_pr_claude_fails_retries_then_gives_up(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR, tmp_path: Path,
) -> None:
    workspace = Workspace(
        base_dir=tmp_path,
        primary_repo_dir=tmp_path / "repo",
        branch_name="autodev/issue-42-fix-login",
    )
    (tmp_path / "repo").mkdir()

    reviews = [PRReview(
        author="reviewer", state="CHANGES_REQUESTED",
        body="Fix tests", commit_sha="abc123",
    )]
    revision_worker.github.get_pr_reviews = AsyncMock(return_value=reviews)
    revision_worker.github.get_pr_check_failures = AsyncMock(return_value=[])
    revision_worker.github.get_pr_comments = AsyncMock(return_value=[])
    revision_worker.github.post_comment = AsyncMock()
    revision_worker.workspaces.create_revision_workspace = AsyncMock(
        return_value=workspace,
    )
    revision_worker.workspaces.cleanup = AsyncMock()
    revision_worker.claude.run = AsyncMock(return_value=ClaudeResult(
        success=False, output="Something went wrong",
    ))

    result = await revision_worker.revise_pr(autodev_pr)
    assert result.success is False
    # First failure: no give-up comment yet, retry still allowed
    revision_worker.github.post_comment.assert_called_once()  # only "working on it"
    assert await revision_worker.needs_revision(autodev_pr) is True

    await revision_worker.revise_pr(autodev_pr)
    # Second failure: gave up — failure comment posted, no more retries
    assert await revision_worker.needs_revision(autodev_pr) is False
    failure_comments = [
        c for c in revision_worker.github.post_comment.call_args_list
        if "failed to address" in c.args[2]
    ]
    assert len(failure_comments) == 1
