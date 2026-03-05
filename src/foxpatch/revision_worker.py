"""PR revision worker: address review feedback on foxpatch's own PRs."""

from __future__ import annotations

import asyncio
import logging
from pathlib import Path

from .claude_runner import ClaudeRunner
from .config import AppConfig
from .exceptions import AutoDevError
from .github_client import GitHubClient
from .models import GitHubPR, TaskResult
from .prompts import build_pr_revision_prompt
from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class RevisionWorker:
    def __init__(
        self,
        config: AppConfig,
        github: GitHubClient,
        claude: ClaudeRunner,
        workspaces: WorkspaceManager,
    ):
        self.config = config
        self.github = github
        self.claude = claude
        self.workspaces = workspaces
        # Track which (repo, pr_number, head_sha) we've already revised.
        # If head_sha matches, the review feedback hasn't been addressed yet
        # (or we already pushed fixes and the sha changed).
        self._revised: set[tuple[str, int, str]] = set()
        self._MAX_REVISED_SIZE = 5_000

    def _revision_key(self, pr: GitHubPR) -> tuple[str, int, str]:
        return (pr.repo.full_name, pr.number, pr.head_sha)

    def mark_seen(self, pr: GitHubPR) -> None:
        """Mark a PR as already seen (used for warm-up)."""
        self._revised.add(self._revision_key(pr))

    async def needs_revision(self, pr: GitHubPR) -> bool:
        """Check if a PR is ours and has unaddressed REQUEST_CHANGES reviews."""
        labels = self.config.github.labels

        # Only revise PRs with the trigger label (our own PRs)
        if labels.trigger not in pr.labels:
            return False

        # Skip if we already revised this exact version
        key = self._revision_key(pr)
        if key in self._revised:
            return False

        # Check for REQUEST_CHANGES reviews
        reviews = await self.github.get_pr_reviews(pr.repo, pr.number)
        has_changes_requested = any(
            r.state == "CHANGES_REQUESTED" for r in reviews
        )
        if not has_changes_requested:
            return False

        # Check if any REQUEST_CHANGES review is on the current head
        # (meaning it hasn't been addressed by a new push yet)
        current_reviews = [
            r for r in reviews
            if r.state == "CHANGES_REQUESTED" and r.commit_sha == pr.head_sha
        ]
        # Also check for CI failures on current head
        check_failures = await self.github.get_pr_check_failures(pr.repo, pr.number)

        return bool(current_reviews or check_failures)

    async def revise_pr(self, pr: GitHubPR) -> TaskResult:
        logger.info("Revising PR %s#%d: %s", pr.repo, pr.number, pr.title)
        workspace = None

        try:
            # 1. Fetch review data
            reviews = await self.github.get_pr_reviews(pr.repo, pr.number)
            check_failures = await self.github.get_pr_check_failures(pr.repo, pr.number)
            pr_comments = await self.github.get_pr_comments(pr.repo, pr.number)

            # 2. Comment that we're working on it
            await self.github.post_comment(
                pr.repo, pr.number,
                "🔄 **autodev** is addressing review feedback.",
            )

            # 3. Create workspace (full clone of PR branch)
            group_result = self.config.find_repo_group(pr.repo.full_name)
            repo_group = group_result[1] if group_result else None
            workspace = await self.workspaces.create_revision_workspace(pr, repo_group)

            # 4. Build prompt and run Claude
            prompt = build_pr_revision_prompt(pr, reviews, check_failures, pr_comments)
            claude_result = await self.claude.run(
                prompt,
                cwd=workspace.primary_repo_dir,
                model=self.config.claude.model_for_issues,
                max_turns=self.config.claude.max_turns,
                max_budget_usd=self.config.claude.max_budget_usd,
                timeout_seconds=self.config.claude.timeout_seconds,
                allowed_tools=self.config.claude.allowed_tools,
                system_prompt=self.config.claude.append_system_prompt,
                add_dirs=workspace.additional_dirs or None,
            )

            if not claude_result.success:
                raise AutoDevError(f"Claude reported failure: {claude_result.output[:300]}")

            # 5. Verify new commits exist
            has_commits = await self._has_new_commits(
                workspace.primary_repo_dir, pr.head_sha,
            )
            if not has_commits:
                raise AutoDevError("Claude produced no commits for revision")

            # 6. Push to the same branch (try direct, fall back to fork remote)
            await self._push_revision(workspace.primary_repo_dir, pr)

            # 7. Track as revised
            if len(self._revised) >= self._MAX_REVISED_SIZE:
                to_keep = list(self._revised)[self._MAX_REVISED_SIZE // 2 :]
                self._revised = set(to_keep)
            self._revised.add(self._revision_key(pr))

            await self.github.post_comment(
                pr.repo, pr.number,
                "✅ Review feedback addressed. Pushed new commits.",
            )

            logger.info(
                "Successfully revised PR %s#%d ($%.2f)",
                pr.repo, pr.number, claude_result.cost_usd,
            )
            return TaskResult(success=True, pr_url="", cost_usd=claude_result.cost_usd)

        except Exception as e:
            logger.error("Failed to revise PR %s#%d: %s", pr.repo, pr.number, e)
            try:
                error_type = type(e).__name__
                await self.github.post_comment(
                    pr.repo, pr.number,
                    f"❌ **autodev** failed to address review feedback.\n\n"
                    f"**Error type:** `{error_type}`",
                )
                # Mark as revised so we don't retry endlessly
                self._revised.add(self._revision_key(pr))
            except Exception as post_err:
                logger.error("Failed to post failure comment: %s", post_err)
            return TaskResult(success=False, error_message=str(e))

        finally:
            if workspace:
                self.workspaces.cleanup(workspace)

    async def _has_new_commits(self, repo_dir: Path, base_sha: str) -> bool:
        """Check if there are new commits since base_sha."""
        proc = await asyncio.create_subprocess_exec(
            "git", "log", f"{base_sha}..HEAD", "--oneline",
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _ = await proc.communicate()
        return bool(stdout.decode().strip())

    async def _push_revision(self, repo_dir: Path, pr: GitHubPR) -> None:
        """Push revision commits to the PR branch."""
        # Try direct push to origin first
        proc = await asyncio.create_subprocess_exec(
            "git", "push", "origin", pr.head_ref,
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, stderr = await proc.communicate()
        if proc.returncode == 0:
            return

        logger.info(
            "Direct push failed for revision of %s#%d, trying fork: %s",
            pr.repo, pr.number, stderr.decode().strip(),
        )

        # If head_ref contains ":", it was pushed from a fork (e.g. "forkuser:branch")
        # We need to push to the fork remote
        fork_full = await self.github.fork_repo(pr.repo)
        fork_url = f"https://github.com/{fork_full}.git"

        # Add fork remote if not already present
        await self._run_git(["remote", "add", "fork", fork_url], cwd=repo_dir, check=False)
        push_proc = await asyncio.create_subprocess_exec(
            "git", "push", "fork", pr.head_ref,
            cwd=repo_dir,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _, push_stderr = await push_proc.communicate()
        if push_proc.returncode != 0:
            raise AutoDevError(
                f"git push revision to fork failed: {push_stderr.decode().strip()}"
            )

    async def _run_git(
        self, args: list[str], cwd: Path, check: bool = True,
    ) -> str:
        proc = await asyncio.create_subprocess_exec(
            "git", *args,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if check and proc.returncode != 0:
            raise AutoDevError(f"git {' '.join(args)} failed: {stderr.decode().strip()}")
        return stdout.decode().strip()
