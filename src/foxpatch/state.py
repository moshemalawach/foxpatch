"""Label-based state machine for issue tracking."""

from __future__ import annotations

import logging

from .config import LabelConfig
from .github_client import GitHubClient
from .models import GitHubIssue, RepoRef

logger = logging.getLogger(__name__)


class StateManager:
    def __init__(self, github: GitHubClient, labels: LabelConfig):
        self.github = github
        self.labels = labels

    @property
    def _state_labels(self) -> set[str]:
        return {self.labels.in_progress, self.labels.done, self.labels.failed}

    def is_actionable(self, issue: GitHubIssue) -> bool:
        has_trigger = self.labels.trigger in issue.labels
        has_state = bool(self._state_labels & set(issue.labels))
        return has_trigger and not has_state

    async def _refresh_labels(self, repo: RepoRef, number: int) -> list[str]:
        return await self.github.get_issue_labels(repo, number)

    async def transition_to_in_progress(self, repo: RepoRef, number: int) -> bool:
        # Note: TOCTOU race between label check and add is inherent to a label-based
        # state machine. Multiple daemon instances could both claim the same issue.
        # This is acceptable for the design; use a single daemon instance in production.
        labels = await self._refresh_labels(repo, number)
        if self._state_labels & set(labels):
            logger.warning(
                "Issue %s#%d already has a state label, skipping claim",
                repo, number,
            )
            return False
        await self.github.add_label(repo, number, self.labels.in_progress)
        logger.info("Claimed issue %s#%d (in-progress)", repo, number)
        return True

    def attempt_number(self, labels: list[str]) -> int:
        """Number of interrupted attempts recorded on the issue's labels."""
        best = 0
        for label in labels:
            if label.startswith(self.labels.attempt_prefix):
                suffix = label[len(self.labels.attempt_prefix):]
                try:
                    best = max(best, int(suffix))
                except ValueError:
                    continue
        return best

    async def transition_to_retry(self, repo: RepoRef, number: int, attempt: int) -> None:
        """Clear in-progress and record the attempt count, making the issue
        actionable again (used by crash recovery)."""
        await self.github.remove_label(repo, number, self.labels.in_progress)
        if attempt > 1:
            await self.github.remove_label(
                repo, number, f"{self.labels.attempt_prefix}{attempt - 1}",
            )
        await self.github.add_label(repo, number, f"{self.labels.attempt_prefix}{attempt}")
        logger.info("Issue %s#%d queued for retry (attempt %d)", repo, number, attempt)

    async def transition_to_done(self, repo: RepoRef, number: int) -> None:
        await self.github.remove_label(repo, number, self.labels.in_progress)
        await self.github.add_label(repo, number, self.labels.done)
        logger.info("Marked issue %s#%d as done", repo, number)

    async def transition_to_failed(self, repo: RepoRef, number: int) -> None:
        await self.github.remove_label(repo, number, self.labels.in_progress)
        await self.github.add_label(repo, number, self.labels.failed)
        logger.info("Marked issue %s#%d as failed", repo, number)
