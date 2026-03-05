"""Async wrapper around the gh CLI for GitHub operations."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from .exceptions import GitHubCLIError
from .models import GitHubIssue, GitHubPR, PRReview, RepoRef

logger = logging.getLogger(__name__)


class GitHubClient:
    def __init__(self, dry_run: bool = False):
        self.dry_run = dry_run

    async def _run_gh(self, args: list[str], check: bool = True) -> str:
        cmd = ["gh", *args]
        logger.debug("Running: %s", " ".join(cmd))

        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        stdout_str = stdout.decode().strip()
        stderr_str = stderr.decode().strip()

        if check and proc.returncode != 0:
            raise GitHubCLIError(
                f"gh command failed: {' '.join(args)}\n{stderr_str}",
                returncode=proc.returncode or -1,
                stderr=stderr_str,
            )

        return stdout_str

    async def _run_gh_json(self, args: list[str]) -> Any:
        output = await self._run_gh(args)
        if not output:
            return []
        return json.loads(output)

    async def list_org_repos(self, org: str) -> list[RepoRef]:
        data = await self._run_gh_json([
            "repo", "list", org,
            "--no-archived",
            "--json", "owner,name",
            "--limit", "200",
        ])
        return [RepoRef(owner=r["owner"]["login"], name=r["name"]) for r in data]

    async def list_issues(self, repo: RepoRef, label: str) -> list[GitHubIssue]:
        data = await self._run_gh_json([
            "issue", "list",
            "--repo", repo.full_name,
            "--label", label,
            "--state", "open",
            "--json", "number,title,body,labels",
            "--limit", "50",
        ])
        issues = []
        for item in data:
            labels = [lbl["name"] for lbl in item.get("labels", [])]
            issues.append(GitHubIssue(
                repo=repo,
                number=item["number"],
                title=item["title"],
                body=item.get("body", ""),
                labels=labels,
            ))
        return issues

    async def list_prs(self, repo: RepoRef) -> list[GitHubPR]:
        data = await self._run_gh_json([
            "pr", "list",
            "--repo", repo.full_name,
            "--state", "open",
            "--json", "number,title,body,author,headRefOid,headRefName,baseRefName,labels,isDraft",
            "--limit", "50",
        ])
        prs = []
        for item in data:
            labels = [lbl["name"] for lbl in item.get("labels", [])]
            prs.append(GitHubPR(
                repo=repo,
                number=item["number"],
                title=item["title"],
                body=item.get("body", ""),
                author=item.get("author", {}).get("login", ""),
                head_sha=item.get("headRefOid", ""),
                head_ref=item.get("headRefName", ""),
                base_ref=item.get("baseRefName", ""),
                labels=labels,
                draft=item.get("isDraft", False),
            ))
        return prs

    async def get_issue_comments(self, repo: RepoRef, number: int) -> list[str]:
        data = await self._run_gh_json([
            "issue", "view", str(number),
            "--repo", repo.full_name,
            "--json", "comments",
        ])
        return [c["body"] for c in data.get("comments", [])]

    async def get_issue_labels(self, repo: RepoRef, number: int) -> list[str]:
        data = await self._run_gh_json([
            "issue", "view", str(number),
            "--repo", repo.full_name,
            "--json", "labels",
        ])
        return [lbl["name"] for lbl in data.get("labels", [])]

    async def ensure_label_exists(self, repo: RepoRef, label: str) -> None:
        """Create a label on the repo if it doesn't already exist."""
        if self.dry_run:
            return
        # gh label create is idempotent — exits 0 if it already exists
        await self._run_gh([
            "label", "create", label,
            "--repo", repo.full_name,
            "--color", "5319E7",
            "--force",
        ])

    async def add_label(self, repo: RepoRef, number: int, label: str) -> None:
        if self.dry_run:
            logger.info("[DRY RUN] Would add label '%s' to %s#%d", label, repo, number)
            return
        await self.ensure_label_exists(repo, label)
        await self._run_gh([
            "issue", "edit", str(number),
            "--repo", repo.full_name,
            "--add-label", label,
        ])

    async def remove_label(self, repo: RepoRef, number: int, label: str) -> None:
        if self.dry_run:
            logger.info("[DRY RUN] Would remove label '%s' from %s#%d", label, repo, number)
            return
        await self._run_gh([
            "issue", "edit", str(number),
            "--repo", repo.full_name,
            "--remove-label", label,
        ])

    async def post_comment(self, repo: RepoRef, number: int, body: str) -> None:
        if self.dry_run:
            logger.info("[DRY RUN] Would post comment on %s#%d: %s", repo, number, body[:80])
            return
        await self._run_gh([
            "issue", "comment", str(number),
            "--repo", repo.full_name,
            "--body", body,
        ])

    async def create_pr(
        self,
        repo: RepoRef,
        title: str,
        body: str,
        head: str,
        base: str = "main",
        labels: list[str] | None = None,
    ) -> str:
        if self.dry_run:
            logger.info("[DRY RUN] Would create PR '%s' on %s (%s -> %s)", title, repo, head, base)
            return "https://github.com/dry-run/pr"
        args = [
            "pr", "create",
            "--repo", repo.full_name,
            "--title", title,
            "--body", body,
            "--head", head,
            "--base", base,
        ]
        for label in labels or []:
            args.extend(["--label", label])
        output = await self._run_gh(args)
        return output.strip()

    async def get_pr_diff(self, repo: RepoRef, number: int) -> str:
        return await self._run_gh([
            "pr", "diff", str(number),
            "--repo", repo.full_name,
        ])

    async def post_review(
        self,
        repo: RepoRef,
        number: int,
        body: str,
        event: str = "COMMENT",
    ) -> None:
        if self.dry_run:
            logger.info("[DRY RUN] Would post %s review on %s#%d", event, repo, number)
            return
        args = [
            "pr", "review", str(number),
            "--repo", repo.full_name,
            "--body", body,
        ]
        event_flag = {
            "APPROVE": "--approve",
            "REQUEST_CHANGES": "--request-changes",
            "COMMENT": "--comment",
        }.get(event, "--comment")
        args.append(event_flag)
        await self._run_gh(args)

    async def get_default_branch(self, repo: RepoRef) -> str:
        data = await self._run_gh_json([
            "repo", "view", repo.full_name,
            "--json", "defaultBranchRef",
        ])
        return data.get("defaultBranchRef", {}).get("name", "main")

    async def get_pr_reviews(self, repo: RepoRef, number: int) -> list[PRReview]:
        """Fetch reviews on a PR."""
        data = await self._run_gh_json([
            "pr", "view", str(number),
            "--repo", repo.full_name,
            "--json", "reviews",
        ])
        reviews = []
        for r in data.get("reviews", []):
            reviews.append(PRReview(
                author=r.get("author", {}).get("login", ""),
                state=r.get("state", ""),
                body=r.get("body", ""),
                commit_sha=r.get("commit", {}).get("oid", ""),
            ))
        return reviews

    async def get_pr_check_failures(self, repo: RepoRef, number: int) -> list[dict[str, str]]:
        """Fetch failed CI checks for a PR. Returns list of {name, state}."""
        data = await self._run_gh_json([
            "pr", "view", str(number),
            "--repo", repo.full_name,
            "--json", "statusCheckRollup",
        ])
        failures = []
        for check in data.get("statusCheckRollup", []):
            conclusion = check.get("conclusion", "")
            status = check.get("status", "")
            if conclusion == "FAILURE" or (status == "COMPLETED" and conclusion == "FAILURE"):
                failures.append({
                    "name": check.get("name", check.get("context", "unknown")),
                    "conclusion": conclusion,
                })
        return failures

    async def get_pr_comments(self, repo: RepoRef, number: int) -> list[dict[str, str]]:
        """Fetch comments on a PR (not review comments, just regular comments)."""
        data = await self._run_gh_json([
            "pr", "view", str(number),
            "--repo", repo.full_name,
            "--json", "comments",
        ])
        return [
            {"author": c.get("author", {}).get("login", ""), "body": c.get("body", "")}
            for c in data.get("comments", [])
        ]

    async def get_authenticated_user(self) -> str:
        # --jq returns a bare string, not JSON, so use _run_gh not _run_gh_json
        return await self._run_gh(["api", "user", "--jq", ".login"])

    async def fork_repo(self, repo: RepoRef) -> str:
        """Fork a repo. Returns the fork's full_name (e.g. 'botuser/repo')."""
        if self.dry_run:
            logger.info("[DRY RUN] Would fork %s", repo)
            return f"dry-run-fork/{repo.name}"
        output = await self._run_gh([
            "repo", "fork", repo.full_name,
            "--clone=false",
        ])
        logger.info("Forked %s: %s", repo, output)
        # Determine fork name from authenticated user
        user = await self.get_authenticated_user()
        return f"{user}/{repo.name}"
