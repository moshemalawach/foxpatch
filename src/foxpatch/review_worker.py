"""PR review worker: fetch diff, run Claude review, post review."""

from __future__ import annotations

import json
import logging

from .claude_runner import ClaudeRunner
from .config import AppConfig
from .github_client import GitHubClient
from .models import GitHubPR, ReviewVerdict, TaskResult
from .prompts import build_pr_review_prompt
from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class ReviewWorker:
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
        # Tracks reviewed PRs as (repo_full_name, pr_number, head_sha).
        # Bounded: entries are pruned when exceeding _MAX_REVIEWED_SIZE.
        self._reviewed: set[tuple[str, int, str]] = set()
        self._MAX_REVIEWED_SIZE = 10_000

    def _review_key(self, pr: GitHubPR) -> tuple[str, int, str]:
        return (pr.repo.full_name, pr.number, pr.head_sha)

    def should_review(self, pr: GitHubPR) -> bool:
        review_cfg = self.config.github.review

        if not review_cfg.enabled:
            return False

        if pr.draft:
            return False

        if review_cfg.skip_bot_prs and pr.author.endswith("[bot]"):
            return False

        if pr.author in review_cfg.skip_authors:
            return False

        # Skip autodev's own PRs
        labels = self.config.github.labels
        if labels.trigger in pr.labels:
            return False

        key = self._review_key(pr)
        if key in self._reviewed:
            # Exact (repo, number, sha) match means we already reviewed this version
            return False

        if not review_cfg.re_review_on_push:
            # Check if we reviewed any version of this PR
            if any(r[0] == pr.repo.full_name and r[1] == pr.number for r in self._reviewed):
                return False

        return True

    async def review_pr(self, pr: GitHubPR) -> TaskResult:
        logger.info("Reviewing PR %s#%d: %s", pr.repo, pr.number, pr.title)
        workspace = None

        try:
            # 1. Fetch diff
            pr.diff = await self.github.get_pr_diff(pr.repo, pr.number)
            if not pr.diff.strip():
                logger.info("PR %s#%d has empty diff, skipping", pr.repo, pr.number)
                return TaskResult(success=True)

            # 2. Create review workspace
            workspace = await self.workspaces.create_review_workspace(pr)

            # 3. Run Claude review
            prompt = build_pr_review_prompt(pr)
            review_tools = ["Read", "Glob", "Grep"]
            claude_result = await self.claude.run(
                prompt,
                cwd=workspace.primary_repo_dir,
                model=self.config.claude.model_for_reviews,
                max_turns=20,
                max_budget_usd=self.config.claude.max_budget_usd_review,
                timeout_seconds=self.config.claude.timeout_seconds,
                allowed_tools=review_tools,
                system_prompt=self.config.claude.append_system_prompt,
            )

            # 4. Parse verdict and post review
            verdict, body = self._parse_review(claude_result.output)
            await self.github.post_review(
                pr.repo, pr.number,
                body=body,
                event=verdict.value,
            )

            # 5. Track as reviewed (with bounded size)
            if len(self._reviewed) >= self._MAX_REVIEWED_SIZE:
                # Evict ~half the entries to avoid unbounded growth
                to_keep = list(self._reviewed)[self._MAX_REVIEWED_SIZE // 2 :]
                self._reviewed = set(to_keep)
            self._reviewed.add(self._review_key(pr))

            logger.info(
                "Reviewed PR %s#%d: %s ($%.2f)",
                pr.repo, pr.number, verdict.value, claude_result.cost_usd,
            )
            return TaskResult(success=True, cost_usd=claude_result.cost_usd)

        except Exception as e:
            logger.error("Failed to review PR %s#%d: %s", pr.repo, pr.number, e)
            return TaskResult(success=False, error_message=str(e))

        finally:
            if workspace:
                self.workspaces.cleanup(workspace)

    def _parse_review(self, output: str) -> tuple[ReviewVerdict, str]:
        # Try to extract JSON from the output
        try:
            # Find JSON block in output
            start = output.find("{")
            end = output.rfind("}") + 1
            if start >= 0 and end > start:
                data = json.loads(output[start:end])
                verdict_str = data.get("verdict", "COMMENT").upper()
                try:
                    verdict = ReviewVerdict(verdict_str)
                except ValueError:
                    verdict = ReviewVerdict.COMMENT

                summary = data.get("summary", "")
                comments = data.get("comments", [])

                body_parts = [summary] if summary else []
                for c in comments:
                    path = c.get("path", "")
                    line = c.get("line", "")
                    comment_body = c.get("body", "")
                    if path:
                        body_parts.append(f"**`{path}`** (line {line}): {comment_body}")
                    else:
                        body_parts.append(comment_body)

                return verdict, "\n\n".join(body_parts) if body_parts else output
        except (json.JSONDecodeError, KeyError, TypeError):
            pass

        # Fallback: use raw output as comment
        return ReviewVerdict.COMMENT, output
