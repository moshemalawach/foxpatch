"""PR revision worker: address review feedback on foxpatch's own PRs."""

from __future__ import annotations

import logging
from pathlib import Path

from .claude_runner import ClaudeRunner
from .config import AppConfig
from .exceptions import AutoDevError
from .github_client import GitHubClient
from .gitutil import run_git
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
        # Failed revision attempts per (repo, number, head_sha); after
        # _MAX_REVISION_ATTEMPTS we stop retrying that PR version.
        self._failures: dict[tuple[str, int, str], int] = {}
        self._MAX_REVISION_ATTEMPTS = 2
        self._MAX_FAILURES_SIZE = 5_000

    def _revision_key(self, pr: GitHubPR) -> tuple[str, int, str]:
        return (pr.repo.full_name, pr.number, pr.head_sha)

    async def needs_revision(self, pr: GitHubPR) -> bool:
        """True if this is our PR and the current head has unaddressed
        CHANGES_REQUESTED feedback.

        A review belongs to the commit it was made on (commit_sha). Once we
        push revision commits the head moves, so old feedback stops
        triggering and only a fresh review on the new head re-triggers.
        This derives entirely from GitHub state: no warm-up is needed and
        restarts lose nothing.
        """
        labels = self.config.github.labels

        # Only revise PRs with the trigger label (our own PRs)
        if labels.trigger not in pr.labels:
            return False

        if self._failures.get(self._revision_key(pr), 0) >= self._MAX_REVISION_ATTEMPTS:
            return False

        reviews = await self.github.get_pr_reviews(pr.repo, pr.number)
        return any(
            r.state == "CHANGES_REQUESTED" and r.commit_sha == pr.head_sha
            for r in reviews
        )

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

            # 7. Done — pushing moved the PR head, so the feedback we just
            # addressed no longer matches the new head and won't re-trigger.
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
            key = self._revision_key(pr)
            if len(self._failures) >= self._MAX_FAILURES_SIZE:
                self._failures.clear()
            self._failures[key] = self._failures.get(key, 0) + 1
            gave_up = self._failures[key] >= self._MAX_REVISION_ATTEMPTS
            logger.error(
                "Failed to revise PR %s#%d (attempt %d/%d): %s",
                pr.repo, pr.number, self._failures[key], self._MAX_REVISION_ATTEMPTS, e,
            )
            if gave_up:
                try:
                    error_type = type(e).__name__
                    await self.github.post_comment(
                        pr.repo, pr.number,
                        f"❌ **autodev** failed to address review feedback.\n\n"
                        f"**Error type:** `{error_type}`\n\n"
                        f"Post a new review to retry.",
                    )
                except Exception as post_err:
                    logger.error("Failed to post failure comment: %s", post_err)
            return TaskResult(success=False, error_message=str(e))

        finally:
            if workspace:
                await self.workspaces.cleanup(workspace)

    async def _has_new_commits(self, repo_dir: Path, base_sha: str) -> bool:
        """Check if there are new commits since base_sha."""
        result = await run_git(["log", f"{base_sha}..HEAD", "--oneline"], cwd=repo_dir, check=False)
        return bool(result.stdout)

    async def _push_revision(self, repo_dir: Path, pr: GitHubPR) -> None:
        """Push revision commits to the PR branch."""
        # The workspace was checked out via `gh pr checkout`, which configures
        # the branch's push target (including fork remotes) — try that first.
        result = await run_git(["push"], cwd=repo_dir, check=False)
        if result.returncode == 0:
            return

        result = await run_git(["push", "origin", pr.head_ref], cwd=repo_dir, check=False)
        if result.returncode == 0:
            return

        logger.info(
            "Direct push failed for revision of %s#%d, trying fork: %s",
            pr.repo, pr.number, result.stderr,
        )

        fork_full = await self.github.fork_repo(pr.repo)
        fork_url = f"https://github.com/{fork_full}.git"

        # Add fork remote if not already present
        await run_git(["remote", "add", "fork", fork_url], cwd=repo_dir, check=False)
        push = await run_git(["push", "fork", pr.head_ref], cwd=repo_dir, check=False)
        if push.returncode != 0:
            raise AutoDevError(f"git push revision to fork failed: {push.stderr}")
