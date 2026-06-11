"""Tests for the GitHub client."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from foxpatch.github_client import GitHubClient
from foxpatch.models import RepoRef


@pytest.fixture
def client() -> GitHubClient:
    return GitHubClient(dry_run=False)


@pytest.fixture
def dry_client() -> GitHubClient:
    return GitHubClient(dry_run=True)


@pytest.mark.asyncio
async def test_list_issues(client: GitHubClient, gh_issues_json: list[dict]) -> None:
    client._run_gh_json = AsyncMock(return_value=gh_issues_json)
    repo = RepoRef(owner="test-org", name="test-repo")
    issues = await client.list_issues(repo, "autodev")
    assert len(issues) == 2
    assert issues[0].number == 42
    assert "autodev" in issues[0].labels
    assert issues[1].number == 43


@pytest.mark.asyncio
async def test_list_prs(client: GitHubClient, gh_prs_json: list[dict]) -> None:
    client._run_gh_json = AsyncMock(return_value=gh_prs_json)
    repo = RepoRef(owner="test-org", name="test-repo")
    prs = await client.list_prs(repo)
    assert len(prs) == 2
    assert prs[0].author == "contributor1"
    assert prs[1].draft is True


@pytest.mark.asyncio
async def test_list_prs_parses_requested_reviewers(client: GitHubClient) -> None:
    client._run_gh_json = AsyncMock(return_value=[{
        "number": 200,
        "title": "Test",
        "body": "",
        "author": {"login": "alice"},
        "headRefOid": "sha",
        "headRefName": "feat",
        "baseRefName": "main",
        "labels": [],
        "isDraft": False,
        "reviewRequests": [
            {"__typename": "User", "login": "foxpatch-bot"},
            {"__typename": "Team", "slug": "reviewers"},
            {"__typename": "User", "login": "human-reviewer"},
        ],
    }])
    repo = RepoRef(owner="test-org", name="test-repo")
    prs = await client.list_prs(repo)
    assert prs[0].requested_reviewers == ["foxpatch-bot", "human-reviewer"]


@pytest.mark.asyncio
async def test_list_prs_missing_review_requests(client: GitHubClient) -> None:
    client._run_gh_json = AsyncMock(return_value=[{
        "number": 201,
        "title": "T",
        "body": "",
        "author": {"login": "a"},
        "headRefOid": "s",
        "headRefName": "b",
        "baseRefName": "main",
        "labels": [],
        "isDraft": False,
    }])
    repo = RepoRef(owner="test-org", name="test-repo")
    prs = await client.list_prs(repo)
    assert prs[0].requested_reviewers == []


@pytest.mark.asyncio
async def test_add_label_dry_run(dry_client: GitHubClient) -> None:
    repo = RepoRef(owner="test-org", name="test-repo")
    # Should not raise, should be a no-op
    await dry_client.add_label(repo, 1, "test-label")


@pytest.mark.asyncio
async def test_post_comment_dry_run(dry_client: GitHubClient) -> None:
    repo = RepoRef(owner="test-org", name="test-repo")
    await dry_client.post_comment(repo, 1, "test comment")


@pytest.mark.asyncio
async def test_create_pr_dry_run(dry_client: GitHubClient) -> None:
    repo = RepoRef(owner="test-org", name="test-repo")
    url = await dry_client.create_pr(repo, "title", "body", "branch")
    assert "dry-run" in url


@pytest.mark.asyncio
async def test_get_issue_comments(client: GitHubClient) -> None:
    client._run_gh_json = AsyncMock(return_value={
        "comments": [{"body": "comment 1"}, {"body": "comment 2"}]
    })
    repo = RepoRef(owner="test-org", name="test-repo")
    comments = await client.get_issue_comments(repo, 42)
    assert comments == ["comment 1", "comment 2"]


@pytest.mark.asyncio
async def test_list_org_repos(client: GitHubClient) -> None:
    client._run_gh_json = AsyncMock(return_value=[
        {"owner": {"login": "test-org"}, "name": "repo-a"},
        {"owner": {"login": "test-org"}, "name": "repo-b"},
    ])
    repos = await client.list_org_repos("test-org")
    assert len(repos) == 2
    assert repos[0].full_name == "test-org/repo-a"

@pytest.mark.asyncio
async def test_fork_repo_parses_created_fork(client: GitHubClient) -> None:
    client._run_gh_streams = AsyncMock(return_value=("", "✓ Created fork bot-user/test-repo-1"))
    repo = RepoRef(owner="test-org", name="test-repo")
    assert await client.fork_repo(repo) == "bot-user/test-repo-1"


@pytest.mark.asyncio
async def test_fork_repo_parses_already_exists(client: GitHubClient) -> None:
    client._run_gh_streams = AsyncMock(return_value=("", "bot-user/test-repo already exists"))
    repo = RepoRef(owner="test-org", name="test-repo")
    assert await client.fork_repo(repo) == "bot-user/test-repo"


@pytest.mark.asyncio
async def test_fork_repo_falls_back_to_user_guess(client: GitHubClient) -> None:
    client._run_gh_streams = AsyncMock(return_value=("", "unexpected output"))
    client.get_authenticated_user = AsyncMock(return_value="bot-user")
    repo = RepoRef(owner="test-org", name="test-repo")
    assert await client.fork_repo(repo) == "bot-user/test-repo"
