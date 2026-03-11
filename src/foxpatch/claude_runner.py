"""Claude Code CLI subprocess management."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from pathlib import Path

from .exceptions import ClaudeProcessError, ClaudeTimeoutError
from .models import ClaudeResult

logger = logging.getLogger(__name__)


class ClaudeRunner:
    def __init__(self, dry_run: bool = False, env_vars: dict[str, str] | None = None):
        self.dry_run = dry_run
        self.env_vars = env_vars or {}

    def _build_command(
        self,
        *,
        model: str = "sonnet",
        max_turns: int = 50,
        max_budget_usd: float = 5.0,
        timeout_seconds: int = 1800,
        allowed_tools: list[str] | None = None,
        system_prompt: str = "",
        add_dirs: list[Path] | None = None,
    ) -> list[str]:
        # Prompt is passed via stdin (using -p -) to avoid arg length limits
        cmd = [
            "claude",
            "-p", "-",
            "--dangerously-skip-permissions",
            "--output-format", "json",
            "--model", model,
            "--max-turns", str(max_turns),
        ]

        if max_budget_usd > 0:
            cmd.extend(["--max-budget-usd", str(max_budget_usd)])

        if allowed_tools:
            cmd.extend(["--allowedTools", ",".join(allowed_tools)])

        if system_prompt:
            cmd.extend(["--append-system-prompt", system_prompt])

        if add_dirs:
            for d in add_dirs:
                cmd.extend(["--add-dir", str(d)])

        return cmd

    async def run(
        self,
        prompt: str,
        *,
        cwd: Path | None = None,
        model: str = "sonnet",
        max_turns: int = 50,
        max_budget_usd: float = 5.0,
        timeout_seconds: int = 1800,
        allowed_tools: list[str] | None = None,
        system_prompt: str = "",
        add_dirs: list[Path] | None = None,
    ) -> ClaudeResult:
        if self.dry_run:
            logger.info("[DRY RUN] Would run Claude with model=%s, budget=$%.2f", model, max_budget_usd)
            return ClaudeResult(success=True, output="[dry run]")

        cmd = self._build_command(
            model=model,
            max_turns=max_turns,
            max_budget_usd=max_budget_usd,
            timeout_seconds=timeout_seconds,
            allowed_tools=allowed_tools,
            system_prompt=system_prompt,
            add_dirs=add_dirs,
        )

        logger.info("Starting Claude (model=%s, budget=$%.2f, timeout=%ds)", model, max_budget_usd, timeout_seconds)
        start = time.monotonic()

        try:
            # Merge env_vars with current environment
            env = os.environ.copy()
            env.update(self.env_vars)

            proc = await asyncio.create_subprocess_exec(
                *cmd,
                cwd=cwd,
                stdin=asyncio.subprocess.PIPE,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
                env=env,
            )
            stdout, stderr = await asyncio.wait_for(
                proc.communicate(input=prompt.encode()),
                timeout=timeout_seconds,
            )
        except asyncio.TimeoutError:
            proc.kill()  # type: ignore[union-attr]
            await proc.wait()  # type: ignore[union-attr]
            duration = time.monotonic() - start
            raise ClaudeTimeoutError(
                f"Claude process timed out after {duration:.0f}s"
            )

        duration = time.monotonic() - start
        stdout_str = stdout.decode()
        stderr_str = stderr.decode()

        if proc.returncode != 0:
            raise ClaudeProcessError(
                f"Claude exited with code {proc.returncode}: {stderr_str[:500]}",
                returncode=proc.returncode or -1,
                stderr=stderr_str,
            )

        return self._parse_output(stdout_str, duration)

    def _parse_output(self, stdout: str, duration: float) -> ClaudeResult:
        if not stdout.strip():
            return ClaudeResult(
                success=True,
                output="",
                duration_seconds=duration,
            )

        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            logger.warning("Could not parse Claude JSON output, using raw text")
            return ClaudeResult(
                success=True,
                output=stdout.strip(),
                duration_seconds=duration,
            )

        result_text = data.get("result", "")
        cost_usd = data.get("total_cost_usd", data.get("cost_usd", 0.0))
        num_turns = data.get("num_turns", 0)
        is_error = data.get("is_error", False)
        stop_reason = data.get("stop_reason", "unknown")

        logger.info(
            "Claude finished: turns=%d, cost=$%.2f, duration=%.0fs, stop=%s, error=%s, output=%s",
            num_turns, cost_usd, duration, stop_reason, is_error, result_text[:500],
        )

        return ClaudeResult(
            success=not is_error,
            output=result_text,
            cost_usd=cost_usd,
            duration_seconds=duration,
            num_turns=num_turns,
        )
