"""Data models for foxpatch."""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from pathlib import Path


class TaskStatus(enum.Enum):
    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    DONE = "done"
    FAILED = "failed"


class ReviewVerdict(enum.Enum):
    APPROVE = "APPROVE"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    COMMENT = "COMMENT"


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @classmethod
    def from_string(cls, s: str) -> RepoRef:
        if "/" not in s:
            raise ValueError(f"Invalid repo reference '{s}': expected 'owner/name' format")
        owner, name = s.split("/", 1)
        return cls(owner=owner, name=name)

    def __str__(self) -> str:
        return self.full_name


@dataclass
class GitHubIssue:
    repo: RepoRef
    number: int
    title: str
    body: str
    labels: list[str] = field(default_factory=list)
    comments: list[str] = field(default_factory=list)


@dataclass
class GitHubPR:
    repo: RepoRef
    number: int
    title: str
    body: str
    author: str
    head_sha: str
    head_ref: str
    base_ref: str
    labels: list[str] = field(default_factory=list)
    draft: bool = False
    diff: str = ""


@dataclass
class ClaudeResult:
    success: bool
    output: str
    cost_usd: float = 0.0
    duration_seconds: float = 0.0
    num_turns: int = 0


@dataclass
class TaskResult:
    success: bool
    error_message: str = ""
    pr_url: str = ""
    cost_usd: float = 0.0


@dataclass
class Workspace:
    base_dir: Path
    primary_repo_dir: Path
    additional_dirs: list[Path] = field(default_factory=list)
    branch_name: str = ""
