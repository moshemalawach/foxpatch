"""Main polling loop, semaphore dispatch, graceful shutdown."""

from __future__ import annotations

import asyncio
import logging
import signal

from .claude_runner import ClaudeRunner
from .config import AppConfig
from .costs import CostTracker
from .github_client import GitHubClient
from .issue_worker import IssueWorker
from .models import GitHubIssue, GitHubPR, RepoRef
from .review_worker import ReviewWorker
from .revision_worker import RevisionWorker
from .state import StateManager
from .workspace import WorkspaceManager

logger = logging.getLogger(__name__)


class Orchestrator:
    def __init__(self, config: AppConfig, dry_run: bool = False):
        self.config = config
        self.dry_run = dry_run
        self._shutdown = asyncio.Event()

        self.github = GitHubClient(dry_run=dry_run)
        self.state = StateManager(self.github, config.github.labels)
        self.claude = ClaudeRunner(dry_run=dry_run, env_vars=config.claude.env_vars)
        self.workspaces = WorkspaceManager(
            base_dir=config.concurrency.workspace_base_dir,
            dry_run=dry_run,
        )

        self.issue_worker = IssueWorker(
            config, self.github, self.state, self.claude, self.workspaces,
        )
        self.review_worker = ReviewWorker(config, self.github, self.claude, self.workspaces)
        self.revision_worker = RevisionWorker(config, self.github, self.claude, self.workspaces)

        self._task_semaphore = asyncio.Semaphore(config.concurrency.max_parallel_tasks)
        self._review_semaphore = asyncio.Semaphore(config.concurrency.max_parallel_reviews)
        self.costs = CostTracker(config.claude.max_daily_cost_usd)

    async def start(self, once: bool = False) -> None:
        loop = asyncio.get_running_loop()
        for sig in (signal.SIGINT, signal.SIGTERM):
            loop.add_signal_handler(sig, self._handle_signal)

        logger.info(
            "Starting orchestrator (dry_run=%s, once=%s, tasks=%d, reviews=%d)",
            self.dry_run, once, self.config.concurrency.max_parallel_tasks,
            self.config.concurrency.max_parallel_reviews,
        )

        repos = await self._resolve_repos()
        logger.info("Monitoring %d repositories", len(repos))

        # Cache the authenticated user so ReviewWorker can detect when it
        # has been added back to a PR's reviewer list ("Re-request review").
        try:
            bot_user = await self.github.get_authenticated_user()
            if bot_user:
                self.review_worker.set_bot_user(bot_user)
                logger.info("Authenticated as %s", bot_user)
        except Exception as e:
            logger.warning("Could not resolve authenticated user: %s", e)

        # Startup: recover stale in-progress issues (e.g. from a crash/restart)
        await self._recover_stale_issues(repos)

        if once:
            await self._run_cycle(repos)
            return

        while not self._shutdown.is_set():
            await self._run_cycle(repos)
            try:
                await asyncio.wait_for(
                    self._shutdown.wait(),
                    timeout=min(
                        self.config.github.poll_interval_issues,
                        self.config.github.poll_interval_prs,
                    ),
                )
                break  # shutdown requested
            except asyncio.TimeoutError:
                pass  # normal polling interval elapsed

        logger.info("Orchestrator shutting down gracefully")

    async def _run_cycle(self, repos: list[RepoRef]) -> None:
        if self.costs.limit_reached():
            logger.warning(
                "Daily cost limit reached ($%.2f spent, limit $%.2f) — pausing dispatch "
                "until midnight",
                self.costs.spent_today_usd, self.costs.daily_limit_usd,
            )
            return

        # Poll all repos concurrently; each returns the tasks it dispatched.
        per_repo = await asyncio.gather(*[self._poll_repo(repo) for repo in repos])
        tasks = [task for repo_tasks in per_repo for task in repo_tasks]

        if tasks:
            logger.info("Dispatched %d tasks this cycle", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            logger.debug("No actionable items this cycle")

    async def _poll_repo(self, repo: RepoRef) -> list[asyncio.Task[None]]:
        tasks: list[asyncio.Task[None]] = []

        try:
            issues = await self.github.list_issues(repo, self.config.github.labels.trigger)
            for issue in issues:
                if self.state.is_actionable(issue):
                    tasks.append(asyncio.create_task(self._dispatch_issue(issue)))
        except Exception as e:
            logger.error("Error polling issues for %s: %s", repo, e)

        try:
            prs = await self.github.list_prs(repo)
            for pr in prs:
                if self.config.github.review.enabled and self.review_worker.should_review(pr):
                    tasks.append(asyncio.create_task(self._dispatch_review(pr)))
                # needs_revision hits the GitHub API, so only the cheap label
                # check happens here; the real check runs inside the task.
                if self.config.github.labels.trigger in pr.labels:
                    tasks.append(asyncio.create_task(self._dispatch_revision(pr)))
        except Exception as e:
            logger.error("Error polling PRs for %s: %s", repo, e)

        return tasks

    async def _dispatch_issue(self, issue: GitHubIssue) -> None:
        async with self._task_semaphore:
            result = await self.issue_worker.process_issue(issue)
            self.costs.record(result.cost_usd)

    async def _dispatch_review(self, pr: GitHubPR) -> None:
        async with self._review_semaphore:
            result = await self.review_worker.review_pr(pr)
            self.costs.record(result.cost_usd)

    async def _dispatch_revision(self, pr: GitHubPR) -> None:
        try:
            if not await self.revision_worker.needs_revision(pr):
                return
        except Exception as e:
            logger.error(
                "Error checking revision need for %s#%d: %s", pr.repo, pr.number, e,
            )
            return
        async with self._task_semaphore:
            result = await self.revision_worker.revise_pr(pr)
            self.costs.record(result.cost_usd)

    async def _resolve_repos(self) -> list[RepoRef]:
        repos: list[RepoRef] = []
        for org_cfg in self.config.github.orgs:
            if org_cfg.repos:
                for repo_str in org_cfg.repos:
                    # Support both "repo-name" and "owner/repo-name" formats
                    if "/" in repo_str:
                        repos.append(RepoRef.from_string(repo_str))
                    else:
                        repos.append(RepoRef(owner=org_cfg.name, name=repo_str))
            else:
                try:
                    org_repos = await self.github.list_org_repos(org_cfg.name)
                    repos.extend(org_repos)
                except Exception as e:
                    logger.error("Error listing repos for org %s: %s", org_cfg.name, e)
        return repos

    async def _recover_stale_issues(self, repos: list[RepoRef]) -> None:
        """Requeue (or fail) issues left in-progress by a crash/restart."""
        labels = self.config.github.labels
        max_attempts = self.config.github.max_issue_attempts

        async def check_repo(repo: RepoRef) -> int:
            try:
                issues = await self.github.list_issues(repo, labels.in_progress)
                for issue in issues:
                    interrupted = self.state.attempt_number(issue.labels) + 1
                    if interrupted < max_attempts:
                        logger.warning(
                            "Recovering stale issue %s#%d, retrying (attempt %d/%d)",
                            repo, issue.number, interrupted + 1, max_attempts,
                        )
                        await self.state.transition_to_retry(repo, issue.number, interrupted)
                        await self.github.post_comment(
                            repo, issue.number,
                            f"⚠️ **autodev** was restarted while working on this issue. "
                            f"It will be retried automatically "
                            f"(attempt {interrupted + 1}/{max_attempts}).",
                        )
                    else:
                        logger.warning(
                            "Stale issue %s#%d exhausted %d attempts, marking failed",
                            repo, issue.number, max_attempts,
                        )
                        await self.state.transition_to_failed(repo, issue.number)
                        await self.github.post_comment(
                            repo, issue.number,
                            f"⚠️ **autodev** was restarted while working on this issue "
                            f"and the retry limit was reached.\n\n"
                            f"Remove the `{labels.failed}` label to retry.",
                        )
                return len(issues)
            except Exception as e:
                logger.error("Error recovering stale issues for %s: %s", repo, e)
                return 0

        results = await asyncio.gather(*[check_repo(r) for r in repos])
        count = sum(results)
        if count:
            logger.info("Recovered %d stale in-progress issues", count)

    def _handle_signal(self) -> None:
        logger.info("Received shutdown signal")
        self._shutdown.set()
