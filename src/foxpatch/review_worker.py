"""PR review worker: fetch diff, run Claude review, post review."""

from __future__ import annotations

import json
import logging
import re

from .claude_runner import ClaudeRunner
from .config import AppConfig
from .github_client import GitHubClient
from .models import GitHubPR, ReviewVerdict, TaskResult
from .prompts import build_pr_review_prompt, build_pr_review_prompt_explore
from .workspace import WorkspaceManager

_EXPLORE_DIFF_FILENAME = ".foxpatch_pr.diff"

_FENCED_JSON_RE = re.compile(r"```(?:json|JSON)?\s*\n?(.*?)```", re.DOTALL)
_PROSE_VERDICT_RE = re.compile(
    r"verdict[\s\"'`*:]+(approve|request[_\s-]changes|comment)\b",
    re.IGNORECASE,
)

logger = logging.getLogger(__name__)


def _find_matching_close(text: str, start: int) -> int | None:
    """Return the index of the bracket that closes the one at `start`, or None.

    Tracks JSON string literals so braces inside strings don't affect depth.
    """
    depth = 0
    in_string = False
    escape = False
    for i in range(start, len(text)):
        ch = text[i]
        if in_string:
            if escape:
                escape = False
            elif ch == "\\":
                escape = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
            if depth == 0:
                return i
    return None


def _first_balanced_json(text: str) -> object | None:
    """Find the largest balanced {...} or [...] span in `text` that parses as JSON."""
    candidates: list[tuple[int, int]] = []
    for i, ch in enumerate(text):
        if ch in "{[":
            end = _find_matching_close(text, i)
            if end is not None:
                candidates.append((i, end + 1))
    candidates.sort(key=lambda p: -(p[1] - p[0]))
    for start, end in candidates:
        try:
            data: object = json.loads(text[start:end])
        except json.JSONDecodeError:
            continue
        return data
    return None


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
        self._warmed_up = False

    def _review_key(self, pr: GitHubPR) -> tuple[str, int, str]:
        return (pr.repo.full_name, pr.number, pr.head_sha)

    def mark_seen(self, pr: GitHubPR) -> None:
        """Mark a PR as already seen without reviewing it (used for warm-up)."""
        self._reviewed.add(self._review_key(pr))

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
                self._mark_reviewed(pr)
                return TaskResult(success=True)

            # 2. Create review workspace
            workspace = await self.workspaces.create_review_workspace(pr)

            # 3. Build prompt — small diffs are embedded inline, large diffs are
            # written to a file on disk and Claude navigates the workspace itself.
            diff_bytes = len(pr.diff.encode())
            max_inline = self.config.github.review.max_diff_size
            explore_mode = diff_bytes > max_inline

            if explore_mode:
                logger.info(
                    "PR %s#%d diff is large (%d bytes > %d), reviewing in explore mode",
                    pr.repo, pr.number, diff_bytes, max_inline,
                )
                diff_path = workspace.primary_repo_dir / _EXPLORE_DIFF_FILENAME
                diff_path.write_text(pr.diff)
                try:
                    files = await self.github.get_pr_files(pr.repo, pr.number)
                except Exception as e:
                    logger.warning(
                        "PR %s#%d: failed to fetch file list (%s), continuing without it",
                        pr.repo, pr.number, e,
                    )
                    files = []
                prompt = build_pr_review_prompt_explore(pr, files, _EXPLORE_DIFF_FILENAME)
                max_turns = 40
            else:
                prompt = build_pr_review_prompt(pr)
                max_turns = 20

            # 4. Run Claude review
            review_tools = ["Read", "Glob", "Grep"]
            claude_result = await self.claude.run(
                prompt,
                cwd=workspace.primary_repo_dir,
                model=self.config.claude.model_for_reviews,
                max_turns=max_turns,
                max_budget_usd=self.config.claude.max_budget_usd_review,
                timeout_seconds=self.config.claude.timeout_seconds,
                allowed_tools=review_tools,
                system_prompt=self.config.claude.append_system_prompt,
            )

            # 5. Parse verdict and post review
            verdict, body = self._parse_review(claude_result.output)
            if not body.strip():
                logger.warning(
                    "PR %s#%d: Claude returned empty review body, skipping post",
                    pr.repo, pr.number,
                )
            else:
                await self.github.post_review(
                    pr.repo, pr.number,
                    body=body,
                    event=verdict.value,
                )

            self._mark_reviewed(pr)

            logger.info(
                "Reviewed PR %s#%d: %s ($%.2f)",
                pr.repo, pr.number, verdict.value, claude_result.cost_usd,
            )
            return TaskResult(success=True, cost_usd=claude_result.cost_usd)

        except Exception as e:
            logger.error("Failed to review PR %s#%d: %s", pr.repo, pr.number, e)
            self._mark_reviewed(pr)
            return TaskResult(success=False, error_message=str(e))

        finally:
            if workspace:
                self.workspaces.cleanup(workspace)

    def _mark_reviewed(self, pr: GitHubPR) -> None:
        """Track a PR as reviewed (with bounded size)."""
        if len(self._reviewed) >= self._MAX_REVIEWED_SIZE:
            to_keep = list(self._reviewed)[self._MAX_REVIEWED_SIZE // 2 :]
            self._reviewed = set(to_keep)
        self._reviewed.add(self._review_key(pr))

    def _parse_review(self, output: str) -> tuple[ReviewVerdict, str]:
        payload, prose = self._extract_review_payload(output)
        if payload is None:
            return ReviewVerdict.COMMENT, output

        if isinstance(payload, dict):
            verdict = self._coerce_verdict(payload.get("verdict"))
            summary = payload.get("summary") or ""
            comments = payload.get("comments") or []
        elif isinstance(payload, list):
            # Array-only output (e.g. just the comments list); derive verdict from
            # surrounding prose and use the prose itself as the summary so the
            # model's narrative isn't lost.
            verdict = self._verdict_from_prose(prose) or ReviewVerdict.COMMENT
            summary = (prose or "").strip()
            comments = payload
        else:
            return ReviewVerdict.COMMENT, output

        body_parts: list[str] = []
        if isinstance(summary, str) and summary.strip():
            body_parts.append(summary.strip())
        for c in comments:
            if not isinstance(c, dict):
                continue
            path = c.get("path", "") or ""
            line = c.get("line", "")
            comment_body = c.get("body", "") or ""
            if path:
                body_parts.append(f"**`{path}`** (line {line}): {comment_body}")
            elif comment_body:
                body_parts.append(comment_body)

        body = "\n\n".join(p for p in body_parts if p)
        if not body.strip():
            return verdict, output
        return verdict, body

    def _extract_review_payload(self, output: str) -> tuple[object | None, str]:
        """Return (parsed_json, prose_with_json_removed).

        Tries fenced ```json blocks last-to-first (models often emit scratchpad
        before the final structured JSON), then falls back to scanning for the
        largest balanced bracket span that parses as JSON.
        """
        for match in reversed(list(_FENCED_JSON_RE.finditer(output))):
            candidate = match.group(1).strip()
            try:
                data = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(data, (dict, list)):
                prose = (output[: match.start()] + output[match.end() :]).strip()
                return data, prose

        data = _first_balanced_json(output)
        if data is None:
            return None, output
        return data, output

    @staticmethod
    def _coerce_verdict(raw: object) -> ReviewVerdict:
        if not isinstance(raw, str):
            return ReviewVerdict.COMMENT
        try:
            return ReviewVerdict(raw.strip().upper().replace(" ", "_").replace("-", "_"))
        except ValueError:
            return ReviewVerdict.COMMENT

    @staticmethod
    def _verdict_from_prose(text: str) -> ReviewVerdict | None:
        if not text:
            return None
        match = _PROSE_VERDICT_RE.search(text)
        if not match:
            return None
        normalized = match.group(1).upper().replace(" ", "_").replace("-", "_")
        try:
            return ReviewVerdict(normalized)
        except ValueError:
            return None
