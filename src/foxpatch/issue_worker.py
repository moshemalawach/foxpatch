"""Issue resolution worker: claim, clone, run Claude, push, create PR."""

from __future__ import annotations

import logging
from pathlib import Path

from .claude_runner import ClaudeRunner
from .config import AppConfig
from .exceptions import AutoDevError
from .github_client import GitHubClient
from .gitutil import run_git
from .models import GitHubIssue, RepoRef, TaskResult
from .prompts import build_issue_resolution_prompt
from .state import StateManager
from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class IssueWorker:
    def __init__(
        self,
        config: AppConfig,
        github: GitHubClient,
        state: StateManager,
        claude: ClaudeRunner,
        workspaces: WorkspaceManager,
    ):
        self.config = config
        self.github = github
        self.state = state
        self.claude = claude
        self.workspaces = workspaces

    async def process_issue(self, issue: GitHubIssue) -> TaskResult:
        logger.info("Processing issue %s#%d: %s", issue.repo, issue.number, issue.title)
        workspace = None

        try:
            # 1. Claim
            claimed = await self.state.transition_to_in_progress(issue.repo, issue.number)
            if not claimed:
                return TaskResult(
                    success=False, error_message="Could not claim issue (race condition)",
                )

            # 2. Comment
            await self.github.post_comment(
                issue.repo, issue.number,
                "🤖 **autodev** is working on this issue. A PR will be created when ready.",
            )

            # 3. Workspace
            group_result = self.config.find_repo_group(issue.repo.full_name)
            repo_group = group_result[1] if group_result else None
            workspace = await self.workspaces.create_workspace(issue, repo_group)

            # 4. Fetch comments for context
            comments = await self.github.get_issue_comments(issue.repo, issue.number)
            issue.comments = comments

            # 5. Build prompt and run Claude
            prompt = build_issue_resolution_prompt(issue)
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

            # 6. Verify commits exist
            default_branch = await self.github.get_default_branch(issue.repo)
            has_commits = await self._has_new_commits(workspace.primary_repo_dir, default_branch)
            if not has_commits:
                raise AutoDevError("Claude produced no commits")

            # 7. Push and create PR (try direct push, fall back to fork)
            fork_user = await self._push_branch_or_fork(
                workspace.primary_repo_dir, workspace.branch_name, issue.repo,
            )
            head = (
                f"{fork_user}:{workspace.branch_name}" if fork_user
                else workspace.branch_name
            )
            pr_url = await self.github.create_pr(
                issue.repo,
                title=f"Fix #{issue.number}: {issue.title}",
                body=self._build_pr_body(issue, claude_result.output),
                head=head,
                base=default_branch,
                labels=[self.config.github.labels.trigger],
            )

            # 8. Done
            await self.state.transition_to_done(issue.repo, issue.number)
            await self.github.post_comment(
                issue.repo, issue.number,
                f"✅ PR created: {pr_url}",
            )

            logger.info("Successfully processed issue %s#%d → %s", issue.repo, issue.number, pr_url)
            return TaskResult(success=True, pr_url=pr_url, cost_usd=claude_result.cost_usd)

        except Exception as e:
            logger.error("Failed to process issue %s#%d: %s", issue.repo, issue.number, e)
            try:
                await self.state.transition_to_failed(issue.repo, issue.number)
                # Sanitize error: only include the exception class name, not full details
                # which may contain file paths, hostnames, or other sensitive info
                error_type = type(e).__name__
                await self.github.post_comment(
                    issue.repo, issue.number,
                    f"❌ **autodev** failed to resolve this issue.\n\n"
                    f"**Error type:** `{error_type}`\n\n"
                    f"Remove the `{self.config.github.labels.failed}` label to retry.",
                )
            except Exception as post_err:
                logger.error("Failed to post failure comment: %s", post_err)
            return TaskResult(success=False, error_message=str(e))

        finally:
            if workspace:
                await self.workspaces.cleanup(workspace)

    @staticmethod
    def _build_pr_body(issue: GitHubIssue, claude_output: str, max_summary: int = 10_000) -> str:
        """PR description: closing keyword so the issue auto-closes on merge,
        plus Claude's final summary of what it did."""
        parts = [f"Fixes #{issue.number}."]
        summary = claude_output.strip()
        if summary:
            if len(summary) > max_summary:
                summary = summary[:max_summary] + "\n\n_(summary truncated)_"
            parts.extend(["", "## Summary", "", summary])
        parts.extend(["", "---", "_Created automatically by autodev._"])
        return "\n".join(parts)

    async def _has_new_commits(self, repo_dir: Path, default_branch: str = "main") -> bool:
        result = await run_git(
            ["log", f"origin/{default_branch}..HEAD", "--oneline"], cwd=repo_dir, check=False,
        )
        return bool(result.stdout)

    async def _push_branch_or_fork(
        self, repo_dir: Path, branch: str, repo: RepoRef,
    ) -> str:
        """Push branch, forking if direct push fails.

        Returns the fork owner username if a fork was used, or empty string
        if pushed directly.
        """
        result = await run_git(["push", "-u", "origin", branch], cwd=repo_dir, check=False)
        if result.returncode == 0:
            return ""

        logger.info("Direct push to %s failed, forking: %s", repo, result.stderr)

        # Fork and push there instead
        fork_full = await self.github.fork_repo(repo)
        fork_url = f"https://github.com/{fork_full}.git"

        await run_git(["remote", "add", "fork", fork_url], cwd=repo_dir)
        push = await run_git(["push", "-u", "fork", branch], cwd=repo_dir, check=False)
        if push.returncode != 0:
            raise AutoDevError(f"git push to fork failed: {push.stderr}")

        return fork_full.split("/")[0]
