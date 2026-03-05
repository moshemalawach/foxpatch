"""Tests for the PR revision worker."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock

from foxpatch.claude_runner import ClaudeRunner
from foxpatch.config import AppConfig
from foxpatch.github_client import GitHubClient
from foxpatch.models import ClaudeResult, GitHubPR, PRReview, Workspace
from foxpatch.revision_worker import RevisionWorker
from foxpatch.workspace import WorkspaceManager

import pytest


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


async def test_needs_revision_already_revised(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR,
) -> None:
    revision_worker.mark_seen(autodev_pr)
    assert await revision_worker.needs_revision(autodev_pr) is False


async def test_needs_revision_no_changes_requested(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR,
) -> None:
    revision_worker.github.get_pr_reviews = AsyncMock(return_value=[
        PRReview(author="reviewer", state="APPROVED", body="LGTM"),
    ])
    assert await revision_worker.needs_revision(autodev_pr) is False


async def test_needs_revision_with_changes_requested(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR,
) -> None:
    revision_worker.github.get_pr_reviews = AsyncMock(return_value=[
        PRReview(
            author="reviewer", state="CHANGES_REQUESTED",
            body="Fix tests", commit_sha="abc123",
        ),
    ])
    revision_worker.github.get_pr_check_failures = AsyncMock(return_value=[])
    assert await revision_worker.needs_revision(autodev_pr) is True


async def test_needs_revision_with_ci_failures(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR,
) -> None:
    # No REQUEST_CHANGES reviews on current head, but has CI failures
    revision_worker.github.get_pr_reviews = AsyncMock(return_value=[
        PRReview(
            author="reviewer", state="CHANGES_REQUESTED",
            body="Fix tests", commit_sha="old-sha",
        ),
    ])
    revision_worker.github.get_pr_check_failures = AsyncMock(return_value=[
        {"name": "test-docker", "conclusion": "FAILURE"},
    ])
    assert await revision_worker.needs_revision(autodev_pr) is True


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
    revision_worker.workspaces.cleanup = MagicMock()
    revision_worker.claude.run = AsyncMock(return_value=ClaudeResult(
        success=True, output="Fixed tests", cost_usd=1.5,
    ))
    revision_worker._has_new_commits = AsyncMock(return_value=True)
    revision_worker._push_revision = AsyncMock()

    result = await revision_worker.revise_pr(autodev_pr)
    assert result.success is True
    revision_worker.workspaces.cleanup.assert_called_once()
    # Should be tracked as revised
    assert revision_worker._revision_key(autodev_pr) in revision_worker._revised


async def test_revise_pr_claude_fails(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR, tmp_path: Path,
) -> None:
    workspace = Workspace(
        base_dir=tmp_path,
        primary_repo_dir=tmp_path / "repo",
        branch_name="autodev/issue-42-fix-login",
    )
    (tmp_path / "repo").mkdir()

    revision_worker.github.get_pr_reviews = AsyncMock(return_value=[])
    revision_worker.github.get_pr_check_failures = AsyncMock(return_value=[])
    revision_worker.github.get_pr_comments = AsyncMock(return_value=[])
    revision_worker.github.post_comment = AsyncMock()
    revision_worker.workspaces.create_revision_workspace = AsyncMock(
        return_value=workspace,
    )
    revision_worker.workspaces.cleanup = MagicMock()
    revision_worker.claude.run = AsyncMock(return_value=ClaudeResult(
        success=False, output="Something went wrong",
    ))

    result = await revision_worker.revise_pr(autodev_pr)
    assert result.success is False
    # Should still be tracked (to avoid retry loop)
    assert revision_worker._revision_key(autodev_pr) in revision_worker._revised


async def test_mark_seen(
    revision_worker: RevisionWorker, autodev_pr: GitHubPR,
) -> None:
    revision_worker.mark_seen(autodev_pr)
    key = revision_worker._revision_key(autodev_pr)
    assert key in revision_worker._revised
