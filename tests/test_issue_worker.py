"""Tests for the issue worker."""

from __future__ import annotations

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from foxpatch.claude_runner import ClaudeRunner
from foxpatch.config import AppConfig
from foxpatch.github_client import GitHubClient
from foxpatch.issue_worker import IssueWorker
from foxpatch.models import ClaudeResult, GitHubIssue, RepoRef, Workspace
from foxpatch.state import StateManager
from foxpatch.workspace import WorkspaceManager


@pytest.fixture
def worker(sample_config: AppConfig, mock_github: GitHubClient) -> IssueWorker:
    state = StateManager(mock_github, sample_config.github.labels)
    claude = ClaudeRunner(dry_run=True)
    workspaces = WorkspaceManager(dry_run=True)
    return IssueWorker(sample_config, mock_github, state, claude, workspaces)


@pytest.mark.asyncio
async def test_process_issue_claim_fails(
    worker: IssueWorker, sample_issue: GitHubIssue
) -> None:
    worker.state.transition_to_in_progress = AsyncMock(return_value=False)
    result = await worker.process_issue(sample_issue)
    assert result.success is False
    assert "race condition" in result.error_message


@pytest.mark.asyncio
async def test_process_issue_success(
    worker: IssueWorker, sample_issue: GitHubIssue, tmp_path: Path
) -> None:
    workspace = Workspace(
        base_dir=tmp_path,
        primary_repo_dir=tmp_path / "repo",
        branch_name="autodev/issue-42-fix-login",
    )
    (tmp_path / "repo").mkdir()

    worker.state.transition_to_in_progress = AsyncMock(return_value=True)
    worker.state.transition_to_done = AsyncMock()
    worker.github.post_comment = AsyncMock()
    worker.github.get_issue_comments = AsyncMock(return_value=[])
    worker.github.get_default_branch = AsyncMock(return_value="main")
    worker.github.create_pr = AsyncMock(return_value="https://github.com/test/pr/1")
    worker.workspaces.create_workspace = AsyncMock(return_value=workspace)
    worker.workspaces.cleanup = MagicMock()
    worker.claude.run = AsyncMock(return_value=ClaudeResult(
        success=True, output="Fixed", cost_usd=0.5, num_turns=3, duration_seconds=30,
    ))
    worker._has_new_commits = AsyncMock(return_value=True)
    worker._push_branch_or_fork = AsyncMock(return_value="")

    result = await worker.process_issue(sample_issue)
    assert result.success is True
    assert result.pr_url == "https://github.com/test/pr/1"
    worker.state.transition_to_done.assert_called_once()
    worker.workspaces.cleanup.assert_called_once()


@pytest.mark.asyncio
async def test_process_issue_claude_fails(
    worker: IssueWorker, sample_issue: GitHubIssue, tmp_path: Path
) -> None:
    workspace = Workspace(
        base_dir=tmp_path,
        primary_repo_dir=tmp_path / "repo",
        branch_name="autodev/issue-42-fix-login",
    )
    (tmp_path / "repo").mkdir()

    worker.state.transition_to_in_progress = AsyncMock(return_value=True)
    worker.state.transition_to_failed = AsyncMock()
    worker.github.post_comment = AsyncMock()
    worker.github.get_issue_comments = AsyncMock(return_value=[])
    worker.workspaces.create_workspace = AsyncMock(return_value=workspace)
    worker.workspaces.cleanup = MagicMock()
    worker.claude.run = AsyncMock(return_value=ClaudeResult(
        success=False, output="Error occurred",
    ))

    result = await worker.process_issue(sample_issue)
    assert result.success is False
    worker.state.transition_to_failed.assert_called_once()
