"""Prompt templates for Claude Code invocations."""

from __future__ import annotations

from .models import GitHubIssue, GitHubPR


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
        "1. Analyze the issue and understand what needs to be fixed or implemented.",
        "2. Explore the codebase to understand the relevant code and architecture.",
        "3. Implement the fix or feature as described in the issue.",
        "4. Write or update tests to cover your changes.",
        "5. Make sure existing tests still pass.",
        "6. Commit your changes with a clear commit message referencing the issue number.",
        "",
        f"Reference this issue as #{issue.number} in your commit message.",
        "Do NOT push or create a PR — that will be handled externally.",
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
