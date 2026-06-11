"""Shared async git subprocess helper."""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
from pathlib import Path

from .exceptions import AutoDevError


@dataclass
class GitResult:
    returncode: int
    stdout: str
    stderr: str


async def run_git(
    args: list[str],
    cwd: Path | None = None,
    *,
    check: bool = True,
    exc_type: type[AutoDevError] = AutoDevError,
) -> GitResult:
    """Run a git command, returning (returncode, stdout, stderr).

    With check=True (default), raises exc_type on non-zero exit.
    """
    proc = await asyncio.create_subprocess_exec(
        "git", *args,
        cwd=cwd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    result = GitResult(
        returncode=proc.returncode if proc.returncode is not None else -1,
        stdout=stdout.decode().strip(),
        stderr=stderr.decode().strip(),
    )
    if check and result.returncode != 0:
        raise exc_type(f"git {' '.join(args)} failed: {result.stderr}")
    return result
