"""Main polling loop, semaphore dispatch, graceful shutdown."""

from __future__ import annotations

import asyncio
import logging
import signal

from .claude_runner import ClaudeRunner
from .config import AppConfig
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
        self.claude = ClaudeRunner(dry_run=dry_run)
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

        # Startup: recover stale in-progress issues (e.g. from a crash/restart)
        await self._recover_stale_issues(repos)

        # Warm-up: mark all existing open PRs as already seen so we don't
        # review the entire backlog or revise all existing PRs on startup.
        if not self.review_worker._warmed_up:
            await self._warmup_prs(repos)
            self.review_worker._warmed_up = True

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
        tasks: list[asyncio.Task[None]] = []

        # Poll issues
        for repo in repos:
            try:
                issues = await self.github.list_issues(repo, self.config.github.labels.trigger)
                for issue in issues:
                    if self.state.is_actionable(issue):
                        task = asyncio.create_task(self._dispatch_issue(issue))
                        tasks.append(task)
            except Exception as e:
                logger.error("Error polling issues for %s: %s", repo, e)

        # Poll PRs (reviews + revisions)
        for repo in repos:
            try:
                prs = await self.github.list_prs(repo)
                for pr in prs:
                    if self.config.github.review.enabled:
                        if self.review_worker.should_review(pr):
                            task = asyncio.create_task(self._dispatch_review(pr))
                            tasks.append(task)
                    # Check if our own PRs need revision
                    if await self.revision_worker.needs_revision(pr):
                        task = asyncio.create_task(self._dispatch_revision(pr))
                        tasks.append(task)
            except Exception as e:
                logger.error("Error polling PRs for %s: %s", repo, e)

        if tasks:
            logger.info("Dispatched %d tasks this cycle", len(tasks))
            await asyncio.gather(*tasks, return_exceptions=True)
        else:
            logger.debug("No actionable items this cycle")

    async def _dispatch_issue(self, issue: GitHubIssue) -> None:
        async with self._task_semaphore:
            await self.issue_worker.process_issue(issue)

    async def _dispatch_review(self, pr: GitHubPR) -> None:
        async with self._review_semaphore:
            await self.review_worker.review_pr(pr)

    async def _dispatch_revision(self, pr: GitHubPR) -> None:
        async with self._task_semaphore:
            await self.revision_worker.revise_pr(pr)

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
        """Reset in-progress issues back to actionable state on startup."""
        labels = self.config.github.labels

        async def check_repo(repo: RepoRef) -> int:
            try:
                issues = await self.github.list_issues(repo, labels.in_progress)
                for issue in issues:
                    logger.warning(
                        "Recovering stale in-progress issue %s#%d", repo, issue.number,
                    )
                    await self.state.transition_to_failed(repo, issue.number)
                    await self.github.post_comment(
                        repo, issue.number,
                        f"⚠️ **autodev** was restarted while working on this issue.\n\n"
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

    async def _warmup_prs(self, repos: list[RepoRef]) -> None:
        """Mark all existing open PRs as seen so we only review/revise new activity."""

        async def check_repo(repo: RepoRef) -> int:
            try:
                prs = await self.github.list_prs(repo)
                for pr in prs:
                    self.review_worker.mark_seen(pr)
                    await self.revision_worker.mark_seen(pr)
                return len(prs)
            except Exception as e:
                logger.error("Error during PR warm-up for %s: %s", repo, e)
                return 0

        results = await asyncio.gather(*[check_repo(r) for r in repos])
        count = sum(results)
        logger.info("PR warm-up complete: marked %d existing PRs as seen", count)

    def _handle_signal(self) -> None:
        logger.info("Received shutdown signal")
        self._shutdown.set()
