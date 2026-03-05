"""Tests for the workspace manager."""

from __future__ import annotations

from pathlib import Path

import pytest

from foxpatch.workspace import WorkspaceManager, _slugify


def test_slugify() -> None:
    assert _slugify("Fix login bug!") == "fix-login-bug"
    assert _slugify("Add feature: dark mode") == "add-feature-dark-mode"
    assert _slugify("a" * 100) == "a" * 40


def test_slugify_special_chars() -> None:
    assert _slugify("issue #42: fix [auth]") == "issue-42-fix-auth"


def test_workspace_manager_base_dir(tmp_path: Path) -> None:
    mgr = WorkspaceManager(base_dir=str(tmp_path / "workspaces"))
    result = mgr._ensure_base_dir()
    assert result.exists()
    assert result == tmp_path / "workspaces"
