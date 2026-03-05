"""Shared test fixtures."""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

from foxpatch.config import AppConfig
from foxpatch.github_client import GitHubClient
from foxpatch.models import GitHubIssue, GitHubPR, RepoRef

FIXTURES_DIR = Path(__file__).parent / "fixtures"


@pytest.fixture
def fixtures_dir() -> Path:
    return FIXTURES_DIR


@pytest.fixture
def sample_config() -> AppConfig:
    return AppConfig.from_yaml(FIXTURES_DIR / "sample_config.yaml")


@pytest.fixture
def repo_ref() -> RepoRef:
    return RepoRef(owner="test-org", name="test-repo")


@pytest.fixture
def sample_issue(repo_ref: RepoRef) -> GitHubIssue:
    return GitHubIssue(
        repo=repo_ref,
        number=42,
        title="Fix login bug",
        body="Users cannot log in when password contains special chars",
        labels=["autodev", "bug"],
    )


@pytest.fixture
def sample_pr(repo_ref: RepoRef) -> GitHubPR:
    return GitHubPR(
        repo=repo_ref,
        number=100,
        title="Add user avatar support",
        body="Implements avatar upload and display",
        author="contributor1",
        head_sha="abc123def456",
        head_ref="feature/avatar",
        base_ref="main",
    )


@pytest.fixture
def mock_github() -> GitHubClient:
    client = GitHubClient(dry_run=True)
    client._run_gh = AsyncMock(return_value="")
    client._run_gh_json = AsyncMock(return_value=[])
    return client


@pytest.fixture
def gh_issues_json() -> list[dict]:
    return json.loads((FIXTURES_DIR / "gh_issues_response.json").read_text())


@pytest.fixture
def gh_prs_json() -> list[dict]:
    return json.loads((FIXTURES_DIR / "gh_prs_response.json").read_text())
