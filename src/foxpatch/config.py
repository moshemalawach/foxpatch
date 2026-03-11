"""Configuration loading and validation."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .exceptions import ConfigError


@dataclass
class LabelConfig:
    trigger: str = "autodev"
    in_progress: str = "autodev:in-progress"
    done: str = "autodev:done"
    failed: str = "autodev:failed"


@dataclass
class ReviewConfig:
    enabled: bool = True
    skip_bot_prs: bool = False
    skip_authors: list[str] = field(default_factory=list)
    re_review_on_push: bool = True


@dataclass
class OrgConfig:
    name: str = ""
    repos: list[str] = field(default_factory=list)


@dataclass
class GitHubConfig:
    orgs: list[OrgConfig] = field(default_factory=list)
    labels: LabelConfig = field(default_factory=LabelConfig)
    poll_interval_issues: int = 120
    poll_interval_prs: int = 180
    review: ReviewConfig = field(default_factory=ReviewConfig)


@dataclass
class RepoGroupConfig:
    description: str = ""
    repos: list[str] = field(default_factory=list)
    primary: str = ""


@dataclass
class ClaudeConfig:
    model: str = "sonnet"
    model_for_issues: str = "opus"
    model_for_reviews: str = "sonnet"
    max_turns: int = 50
    max_budget_usd: float = 5.0
    max_budget_usd_review: float = 1.0
    timeout_seconds: int = 1800
    allowed_tools: list[str] = field(default_factory=lambda: [
        "Read", "Edit", "Write", "Glob", "Grep",
        "Bash(git *)", "Bash(python *)", "Bash(npm *)", "Bash(cargo *)",
    ])
    append_system_prompt: str = ""
    env_vars: dict[str, str] = field(default_factory=dict)


@dataclass
class ConcurrencyConfig:
    max_parallel_tasks: int = 3
    max_parallel_reviews: int = 5
    workspace_base_dir: str = "/tmp/foxpatch"


@dataclass
class LoggingConfig:
    level: str = "INFO"
    file: str | None = None
    format: str = "json"


@dataclass
class AppConfig:
    github: GitHubConfig = field(default_factory=GitHubConfig)
    repo_groups: dict[str, RepoGroupConfig] = field(default_factory=dict)
    claude: ClaudeConfig = field(default_factory=ClaudeConfig)
    concurrency: ConcurrencyConfig = field(default_factory=ConcurrencyConfig)
    logging: LoggingConfig = field(default_factory=LoggingConfig)

    @classmethod
    def from_yaml(cls, path: str | Path) -> AppConfig:
        path = Path(path)
        if not path.exists():
            raise ConfigError(f"Config file not found: {path}")
        try:
            with open(path) as f:
                raw = yaml.safe_load(f)
        except yaml.YAMLError as e:
            raise ConfigError(f"Invalid YAML in {path}: {e}") from e

        if not isinstance(raw, dict):
            raise ConfigError("Config file must be a YAML mapping")

        return cls._from_dict(raw)

    @classmethod
    def _from_dict(cls, data: dict[str, Any]) -> AppConfig:
        config = cls()

        if "github" in data:
            gh = data["github"]
            config.github = GitHubConfig(
                orgs=[OrgConfig(**o) for o in gh.get("orgs", [])],
                labels=LabelConfig(**gh.get("labels", {})),
                poll_interval_issues=gh.get("poll_interval_issues", 120),
                poll_interval_prs=gh.get("poll_interval_prs", 180),
                review=ReviewConfig(**gh.get("review", {})),
            )

        if "repo_groups" in data:
            for name, group_data in data["repo_groups"].items():
                config.repo_groups[name] = RepoGroupConfig(**group_data)

        if "claude" in data:
            try:
                config.claude = ClaudeConfig(**data["claude"])
            except TypeError as e:
                raise ConfigError(f"Invalid 'claude' config: {e}") from e

        if "concurrency" in data:
            try:
                config.concurrency = ConcurrencyConfig(**data["concurrency"])
            except TypeError as e:
                raise ConfigError(f"Invalid 'concurrency' config: {e}") from e

        if "logging" in data:
            try:
                config.logging = LoggingConfig(**data["logging"])
            except TypeError as e:
                raise ConfigError(f"Invalid 'logging' config: {e}") from e

        config.validate()
        return config

    def validate(self) -> None:
        if not self.github.orgs:
            raise ConfigError("At least one GitHub org must be configured")
        for org in self.github.orgs:
            if not org.name:
                raise ConfigError("Each org must have a name")
        if self.concurrency.max_parallel_tasks < 1:
            raise ConfigError("max_parallel_tasks must be >= 1")
        if self.concurrency.max_parallel_reviews < 1:
            raise ConfigError("max_parallel_reviews must be >= 1")
        if self.claude.timeout_seconds < 60:
            raise ConfigError("claude.timeout_seconds must be >= 60")
        for name, group in self.repo_groups.items():
            if not group.repos:
                raise ConfigError(f"Repo group '{name}' must have at least one repo")
            if not group.primary:
                raise ConfigError(f"Repo group '{name}' must have a primary repo")
            if group.primary not in group.repos:
                raise ConfigError(f"Primary repo '{group.primary}' not in group '{name}' repos")

    def find_repo_group(self, repo_full_name: str) -> tuple[str, RepoGroupConfig] | None:
        for name, group in self.repo_groups.items():
            if repo_full_name == group.primary or repo_full_name in group.repos:
                return name, group
        return None
