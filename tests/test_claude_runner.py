"""Tests for the Claude runner."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from foxpatch.claude_runner import ClaudeRunner


def test_build_command_basic() -> None:
    runner = ClaudeRunner()
    cmd = runner._build_command(
        "Fix the bug",
        model="opus",
        max_turns=10,
        max_budget_usd=2.0,
        timeout_seconds=600,
    )
    assert cmd[0] == "claude"
    assert "-p" in cmd
    assert "Fix the bug" in cmd
    assert "--model" in cmd
    assert "opus" in cmd
    assert "--max-turns" in cmd
    assert "10" in cmd


def test_build_command_with_tools() -> None:
    runner = ClaudeRunner()
    cmd = runner._build_command(
        "prompt",
        allowed_tools=["Read", "Edit", "Grep"],
    )
    assert "--allowedTools" in cmd
    idx = cmd.index("--allowedTools")
    assert cmd[idx + 1] == "Read,Edit,Grep"


def test_build_command_with_add_dirs() -> None:
    runner = ClaudeRunner()
    cmd = runner._build_command(
        "prompt",
        add_dirs=[Path("/tmp/repo1"), Path("/tmp/repo2")],
    )
    assert "--add-dir" in cmd
    assert "/tmp/repo1" in cmd
    assert "/tmp/repo2" in cmd


def test_build_command_with_system_prompt() -> None:
    runner = ClaudeRunner()
    cmd = runner._build_command(
        "prompt",
        system_prompt="You are a helpful assistant",
    )
    assert "--append-system-prompt" in cmd
    assert "You are a helpful assistant" in cmd


def test_parse_output_valid_json() -> None:
    runner = ClaudeRunner()
    data = {
        "result": "Fixed the bug",
        "cost_usd": 0.42,
        "num_turns": 5,
        "is_error": False,
    }
    result = runner._parse_output(json.dumps(data), 10.0)
    assert result.success is True
    assert result.output == "Fixed the bug"
    assert result.cost_usd == 0.42
    assert result.num_turns == 5
    assert result.duration_seconds == 10.0


def test_parse_output_error() -> None:
    runner = ClaudeRunner()
    data = {"result": "Something went wrong", "is_error": True}
    result = runner._parse_output(json.dumps(data), 5.0)
    assert result.success is False


def test_parse_output_invalid_json() -> None:
    runner = ClaudeRunner()
    result = runner._parse_output("not json at all", 5.0)
    assert result.success is True
    assert result.output == "not json at all"


def test_parse_output_empty() -> None:
    runner = ClaudeRunner()
    result = runner._parse_output("", 5.0)
    assert result.success is True
    assert result.output == ""


@pytest.mark.asyncio
async def test_run_dry_run() -> None:
    runner = ClaudeRunner(dry_run=True)
    result = await runner.run("Fix the bug", model="opus", max_budget_usd=1.0)
    assert result.success is True
    assert result.output == "[dry run]"
