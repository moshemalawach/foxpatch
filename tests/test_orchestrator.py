"""Tests for the orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock, patch

import pytest

from foxpatch.config import AppConfig
from foxpatch.models import GitHubIssue, GitHubPR, RepoRef
from foxpatch.orchestrator import Orchestrator


@pytest.fixture
def orchestrator(sample_config: AppConfig) -> Orchestrator:
    return Orchestrator(sample_config, dry_run=True)


@pytest.mark.asyncio
async def test_resolve_repos(orchestrator: Orchestrator) -> None:
    orchestrator.github.list_org_repos = AsyncMock(return_value=[
        RepoRef(owner="test-org", name="repo-a"),
        RepoRef(owner="test-org", name="repo-b"),
    ])
    repos = await orchestrator._resolve_repos()
    assert len(repos) == 2
    assert repos[0].full_name == "test-org/repo-a"


@pytest.mark.asyncio
async def test_run_cycle_no_items(orchestrator: Orchestrator) -> None:
    orchestrator.github.list_issues = AsyncMock(return_value=[])
    orchestrator.github.list_prs = AsyncMock(return_value=[])
    repos = [RepoRef(owner="test-org", name="test-repo")]
    await orchestrator._run_cycle(repos)


@pytest.mark.asyncio
async def test_run_cycle_with_actionable_issue(orchestrator: Orchestrator) -> None:
    repo = RepoRef(owner="test-org", name="test-repo")
    issue = GitHubIssue(
        repo=repo, number=42, title="Fix bug", body="",
        labels=["autodev"],
    )
    orchestrator.github.list_issues = AsyncMock(return_value=[issue])
    orchestrator.github.list_prs = AsyncMock(return_value=[])
    orchestrator.issue_worker.process_issue = AsyncMock()

    await orchestrator._run_cycle([repo])
    orchestrator.issue_worker.process_issue.assert_called_once_with(issue)


@pytest.mark.asyncio
async def test_start_once(orchestrator: Orchestrator) -> None:
    orchestrator.github.list_org_repos = AsyncMock(return_value=[
        RepoRef(owner="test-org", name="test-repo"),
    ])
    orchestrator.github.list_issues = AsyncMock(return_value=[])
    orchestrator.github.list_prs = AsyncMock(return_value=[])
    await orchestrator.start(once=True)


def test_handle_signal(orchestrator: Orchestrator) -> None:
    assert not orchestrator._shutdown.is_set()
    orchestrator._handle_signal()
    assert orchestrator._shutdown.is_set()
