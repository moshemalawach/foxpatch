"""Prompt templates for Claude Code invocations."""

from __future__ import annotations

from .models import GitHubIssue, GitHubPR, PRReview


def build_issue_resolution_prompt(issue: GitHubIssue) -> str:
    parts = [
        f"# Issue #{issue.number}: {issue.title}",
        "",
        f"**Repository:** {issue.repo.full_name}",
        "",
        "## Description",
        "",
        issue.body or "(no description provided)",
    ]

    if issue.comments:
        parts.extend(["", "## Additional Context from Comments", ""])
        for i, comment in enumerate(issue.comments, 1):
            parts.extend([f"### Comment {i}", "", comment, ""])

    parts.extend([
        "",
        "## Instructions",
        "",
        "You are an autonomous coding agent. Your job is to resolve this issue by"
        " making commits to the current branch. Nobody will review your work before"
        " it becomes a PR, so be thorough but efficient.",
        "",
        "1. **Understand** — Read the relevant source files and tests. Find similar"
        " patterns in the codebase. Be targeted: don't read everything, just what's"
        " needed for this issue.",
        "2. **Implement** — Make the changes. Follow existing code style and conventions."
        " Write tests if the project has a test suite. Keep changes minimal and focused"
        " on what the issue asks for (YAGNI).",
        "3. **Verify** — Run tests if they exist. Run the linter if configured. Fix"
        " any issues you find.",
        "4. **Commit** — Commit your changes with a clear message. You MUST create at"
        " least one git commit or your work will be lost.",
        "",
        "## Rules",
        "",
        f"- Reference this issue as #{issue.number} in your commit messages.",
        "- Use conventional commit style: `feat:`, `fix:`, `refactor:`, etc.",
        "- Do NOT push or create a PR — that will be handled externally.",
        "- Do NOT spend excessive time exploring. Get to implementation quickly.",
        "- If you're unsure about something, make a reasonable choice and move on.",
    ])

    return "\n".join(parts)


def build_pr_review_prompt(pr: GitHubPR) -> str:
    parts = [
        f"# PR Review: #{pr.number} — {pr.title}",
        "",
        f"**Repository:** {pr.repo.full_name}",
        f"**Author:** {pr.author}",
        f"**Base:** {pr.base_ref} ← **Head:** {pr.head_ref}",
        "",
        "## PR Description",
        "",
        pr.body or "(no description provided)",
        "",
        "## Diff",
        "",
        "```diff",
        pr.diff,
        "```",
        "",
        "## Review Instructions",
        "",
        "Review this pull request for:",
        "- **Correctness:** Logic errors, off-by-one errors, race conditions",
        "- **Security:** Injection vulnerabilities, credential exposure, unsafe operations",
        "- **Code quality:** Readability, naming, unnecessary complexity",
        "- **Testing:** Missing test coverage for new or changed behavior",
        "- **Documentation:** Missing or outdated comments for non-obvious logic",
        "",
        "## Output Format",
        "",
        "Respond with a JSON object:",
        "```json",
        '{',
        '  "verdict": "APPROVE" | "REQUEST_CHANGES" | "COMMENT",',
        '  "summary": "One-paragraph overall assessment",',
        '  "comments": [',
        '    {',
        '      "path": "file/path.py",',
        '      "line": 42,',
        '      "body": "Description of the issue or suggestion"',
        '    }',
        '  ]',
        '}',
        "```",
        "",
        "Only use REQUEST_CHANGES for genuine bugs or security issues.",
        "Use COMMENT for suggestions and style nits.",
        "Use APPROVE if the code is correct and well-written, even with minor nits.",
    ]

    return "\n".join(parts)


def build_pr_revision_prompt(
    pr: GitHubPR,
    reviews: list[PRReview],
    check_failures: list[dict[str, str]],
    pr_comments: list[dict[str, str]],
) -> str:
    parts = [
        f"# PR Revision: #{pr.number} — {pr.title}",
        "",
        f"**Repository:** {pr.repo.full_name}",
        f"**Branch:** {pr.head_ref}",
        "",
        "## Context",
        "",
        "This is a PR you previously created. It has received review feedback that"
        " needs to be addressed. Your job is to fix the issues and commit.",
        "",
    ]

    # Add review feedback
    change_requests = [r for r in reviews if r.state == "CHANGES_REQUESTED"]
    if change_requests:
        parts.extend(["## Review Feedback (REQUEST_CHANGES)", ""])
        for i, review in enumerate(change_requests, 1):
            parts.extend([
                f"### Review {i} by @{review.author}",
                "",
                review.body or "(no body)",
                "",
            ])

    # Add regular comments (may contain additional feedback)
    if pr_comments:
        parts.extend(["## PR Comments", ""])
        for comment in pr_comments:
            parts.extend([
                f"**@{comment['author']}:**",
                "",
                comment["body"],
                "",
            ])

    # Add CI failures
    if check_failures:
        parts.extend(["## CI Failures", ""])
        for check in check_failures:
            parts.append(f"- **{check['name']}**: {check['conclusion']}")
        parts.extend([
            "",
            "Run the test suite and linter locally to reproduce and fix these failures.",
            "",
        ])

    parts.extend([
        "## Instructions",
        "",
        "You are on the PR branch already. Fix the issues raised in the review"
        " feedback and CI failures above.",
        "",
        "1. **Read the feedback** — Understand what the reviewers want changed.",
        "2. **Fix** — Make the necessary changes. Run tests and linters.",
        "3. **Commit** — Commit with a clear message. You MUST create at least one"
        "   git commit or your fixes will be lost.",
        "",
        "## Rules",
        "",
        "- Do NOT push or create a new PR — that will be handled externally.",
        "- Do NOT rewrite the entire PR. Only address the specific feedback.",
        "- If CI is failing, reproduce locally and fix.",
        "- Use conventional commit style: `fix:`, `refactor:`, etc.",
    ])

    return "\n".join(parts)
