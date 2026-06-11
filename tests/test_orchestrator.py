"""Tests for the orchestrator."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from foxpatch.config import AppConfig
from foxpatch.models import GitHubIssue, RepoRef
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

@pytest.mark.asyncio
async def test_run_cycle_polls_repos_concurrently(orchestrator: Orchestrator) -> None:
    import asyncio

    in_flight = 0
    max_in_flight = 0

    async def slow_list_issues(repo, label):
        nonlocal in_flight, max_in_flight
        in_flight += 1
        max_in_flight = max(max_in_flight, in_flight)
        await asyncio.sleep(0.01)
        in_flight -= 1
        return []

    orchestrator.github.list_issues = slow_list_issues
    orchestrator.github.list_prs = AsyncMock(return_value=[])
    repos = [RepoRef(owner="test-org", name=f"repo-{i}") for i in range(5)]
    await orchestrator._run_cycle(repos)
    assert max_in_flight > 1


@pytest.mark.asyncio
async def test_dispatch_revision_checks_inside_task(orchestrator: Orchestrator) -> None:
    from foxpatch.models import GitHubPR

    pr = GitHubPR(
        repo=RepoRef(owner="test-org", name="test-repo"),
        number=7, title="t", body="", author="bot",
        head_sha="sha", head_ref="b", base_ref="main",
        labels=["autodev"],
    )
    orchestrator.revision_worker.needs_revision = AsyncMock(return_value=False)
    orchestrator.revision_worker.revise_pr = AsyncMock()
    await orchestrator._dispatch_revision(pr)
    orchestrator.revision_worker.needs_revision.assert_called_once_with(pr)
    orchestrator.revision_worker.revise_pr.assert_not_called()
