"""Workspace management: temp dir creation, repo cloning, branch setup."""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
import tempfile
from pathlib import Path

from .config import RepoGroupConfig
from .exceptions import WorkspaceError
from .models import GitHubIssue, GitHubPR, RepoRef, Workspace

logger = logging.getLogger(__name__)


def _slugify(text: str, max_length: int = 40) -> str:
    slug = re.sub(r"[^a-zA-Z0-9]+", "-", text.lower()).strip("-")
    return slug[:max_length]


class WorkspaceManager:
    def __init__(self, base_dir: str = "/tmp/foxpatch", dry_run: bool = False):
        self.base_dir = Path(base_dir)
        self.dry_run = dry_run

    async def _run_git(self, args: list[str], cwd: Path | None = None) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode != 0:
            raise WorkspaceError(
                f"git {' '.join(args)} failed: {stderr.decode().strip()}"
            )
        return stdout.decode().strip()

    async def create_workspace(
        self,
        issue: GitHubIssue,
        repo_group: RepoGroupConfig | None = None,
    ) -> Workspace:
        slug = _slugify(issue.title)
        branch_name = f"autodev/issue-{issue.number}-{slug}"
        task_dir = Path(tempfile.mkdtemp(
            prefix=f"issue-{issue.number}-",
            dir=self._ensure_base_dir(),
        ))

        logger.info("Creating workspace at %s", task_dir)

        primary_dir = task_dir / issue.repo.name
        await self._clone_repo(issue.repo, primary_dir)
        await self._run_git(["checkout", "-b", branch_name], cwd=primary_dir)

        additional_dirs: list[Path] = []
        if repo_group:
            for repo_str in repo_group.repos:
                repo = RepoRef.from_string(repo_str)
                if repo.full_name == issue.repo.full_name:
                    continue
                sibling_dir = task_dir / repo.name
                await self._clone_repo(repo, sibling_dir)
                additional_dirs.append(sibling_dir)

        return Workspace(
            base_dir=task_dir,
            primary_repo_dir=primary_dir,
            additional_dirs=additional_dirs,
            branch_name=branch_name,
        )

    async def create_review_workspace(self, pr: GitHubPR) -> Workspace:
        task_dir = Path(tempfile.mkdtemp(
            prefix=f"review-{pr.number}-",
            dir=self._ensure_base_dir(),
        ))

        logger.info("Creating review workspace at %s", task_dir)

        repo_dir = task_dir / pr.repo.name
        await self._clone_repo(pr.repo, repo_dir, shallow=True, branch=pr.head_ref)

        return Workspace(
            base_dir=task_dir,
            primary_repo_dir=repo_dir,
        )

    async def _clone_repo(
        self, repo: RepoRef, dest: Path, shallow: bool = False, branch: str | None = None,
    ) -> None:
        url = f"https://github.com/{repo.full_name}.git"
        args = ["clone"]
        if shallow:
            args.extend(["--depth", "1"])
        if branch:
            args.extend(["--branch", branch, "--single-branch"])
        args.extend([url, str(dest)])
        logger.info("Cloning %s to %s", repo, dest)
        await self._run_git(args)

    def _ensure_base_dir(self) -> Path:
        self.base_dir.mkdir(parents=True, exist_ok=True)
        return self.base_dir

    def cleanup(self, workspace: Workspace) -> None:
        if workspace.base_dir.exists():
            logger.info("Cleaning up workspace %s", workspace.base_dir)
            shutil.rmtree(workspace.base_dir, ignore_errors=True)
