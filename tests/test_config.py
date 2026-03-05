"""Tests for config loading and validation."""

from __future__ import annotations

from pathlib import Path

import pytest

from foxpatch.config import AppConfig, RepoGroupConfig
from foxpatch.exceptions import ConfigError


def test_load_sample_config(fixtures_dir: Path) -> None:
    config = AppConfig.from_yaml(fixtures_dir / "sample_config.yaml")
    assert len(config.github.orgs) == 1
    assert config.github.orgs[0].name == "test-org"
    assert config.github.labels.trigger == "autodev"
    assert config.claude.model_for_issues == "sonnet"
    assert config.concurrency.max_parallel_tasks == 2


def test_config_missing_file() -> None:
    with pytest.raises(ConfigError, match="Config file not found"):
        AppConfig.from_yaml("/nonexistent/path.yaml")


def test_config_no_orgs(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text("github:\n  orgs: []\n")
    with pytest.raises(ConfigError, match="At least one GitHub org"):
        AppConfig.from_yaml(cfg)


def test_config_invalid_concurrency(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        "github:\n  orgs:\n    - name: test\n"
        "concurrency:\n  max_parallel_tasks: 0\n  max_parallel_reviews: 1\n"
    )
    with pytest.raises(ConfigError, match="max_parallel_tasks must be >= 1"):
        AppConfig.from_yaml(cfg)


def test_config_bad_repo_group(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        "github:\n  orgs:\n    - name: test\n"
        "repo_groups:\n  mygroup:\n    description: test\n    repos: []\n    primary: x/y\n"
    )
    with pytest.raises(ConfigError, match="must have at least one repo"):
        AppConfig.from_yaml(cfg)


def test_find_repo_group(sample_config: AppConfig) -> None:
    sample_config.repo_groups["core"] = RepoGroupConfig(
        description="Core",
        repos=["test-org/repo-a", "test-org/repo-b"],
        primary="test-org/repo-a",
    )
    result = sample_config.find_repo_group("test-org/repo-a")
    assert result is not None
    assert result[0] == "core"

    result = sample_config.find_repo_group("test-org/unknown")
    assert result is None


def test_config_defaults() -> None:
    """Verify defaults from a minimal config."""
    from foxpatch.config import LabelConfig
    labels = LabelConfig()
    assert labels.trigger == "autodev"
    assert labels.in_progress == "autodev:in-progress"


def test_config_unknown_claude_key(tmp_path: Path) -> None:
    cfg = tmp_path / "bad.yaml"
    cfg.write_text(
        "github:\n  orgs:\n    - name: test\n"
        "claude:\n  max_tuns: 10\n"
    )
    with pytest.raises(ConfigError, match="Invalid 'claude' config"):
        AppConfig.from_yaml(cfg)


def test_repo_ref_from_string_invalid() -> None:
    from foxpatch.models import RepoRef
    with pytest.raises(ValueError, match="expected 'owner/name' format"):
        RepoRef.from_string("no-slash")
