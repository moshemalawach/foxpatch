"""Tests for the state manager."""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from foxpatch.config import LabelConfig
from foxpatch.github_client import GitHubClient
from foxpatch.models import GitHubIssue, RepoRef
from foxpatch.state import StateManager


@pytest.fixture
def labels() -> LabelConfig:
    return LabelConfig()


@pytest.fixture
def state(mock_github: GitHubClient, labels: LabelConfig) -> StateManager:
    return StateManager(mock_github, labels)


def test_is_actionable_yes(state: StateManager, sample_issue: GitHubIssue) -> None:
    assert state.is_actionable(sample_issue) is True


def test_is_actionable_no_trigger(state: StateManager, repo_ref: RepoRef) -> None:
    issue = GitHubIssue(repo=repo_ref, number=1, title="test", body="", labels=["bug"])
    assert state.is_actionable(issue) is False


def test_is_actionable_already_in_progress(state: StateManager, repo_ref: RepoRef) -> None:
    issue = GitHubIssue(
        repo=repo_ref, number=1, title="test", body="",
        labels=["autodev", "autodev:in-progress"],
    )
    assert state.is_actionable(issue) is False


def test_is_actionable_already_done(state: StateManager, repo_ref: RepoRef) -> None:
    issue = GitHubIssue(
        repo=repo_ref, number=1, title="test", body="",
        labels=["autodev", "autodev:done"],
    )
    assert state.is_actionable(issue) is False


def test_is_actionable_already_failed(state: StateManager, repo_ref: RepoRef) -> None:
    issue = GitHubIssue(
        repo=repo_ref, number=1, title="test", body="",
        labels=["autodev", "autodev:failed"],
    )
    assert state.is_actionable(issue) is False


@pytest.mark.asyncio
async def test_transition_to_in_progress(state: StateManager, repo_ref: RepoRef) -> None:
    state.github.get_issue_labels = AsyncMock(return_value=["autodev", "bug"])
    state.github.add_label = AsyncMock()
    result = await state.transition_to_in_progress(repo_ref, 42)
    assert result is True
    state.github.add_label.assert_called_once_with(repo_ref, 42, "autodev:in-progress")


@pytest.mark.asyncio
async def test_transition_to_in_progress_race(state: StateManager, repo_ref: RepoRef) -> None:
    state.github.get_issue_labels = AsyncMock(return_value=["autodev", "autodev:in-progress"])
    result = await state.transition_to_in_progress(repo_ref, 42)
    assert result is False


@pytest.mark.asyncio
async def test_transition_to_done(state: StateManager, repo_ref: RepoRef) -> None:
    state.github.remove_label = AsyncMock()
    state.github.add_label = AsyncMock()
    await state.transition_to_done(repo_ref, 42)
    state.github.remove_label.assert_called_once_with(repo_ref, 42, "autodev:in-progress")
    state.github.add_label.assert_called_once_with(repo_ref, 42, "autodev:done")


@pytest.mark.asyncio
async def test_transition_to_failed(state: StateManager, repo_ref: RepoRef) -> None:
    state.github.remove_label = AsyncMock()
    state.github.add_label = AsyncMock()
    await state.transition_to_failed(repo_ref, 42)
    state.github.remove_label.assert_called_once_with(repo_ref, 42, "autodev:in-progress")
    state.github.add_label.assert_called_once_with(repo_ref, 42, "autodev:failed")
