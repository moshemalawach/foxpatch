"""Tests for the cost tracker."""

from __future__ import annotations

from datetime import date, timedelta

from foxpatch.costs import CostTracker


def test_no_limit_never_blocks() -> None:
    tracker = CostTracker(daily_limit_usd=0.0)
    tracker.record(1000.0)
    assert tracker.limit_reached() is False


def test_limit_reached() -> None:
    tracker = CostTracker(daily_limit_usd=10.0)
    tracker.record(4.0)
    assert tracker.limit_reached() is False
    tracker.record(6.0)
    assert tracker.limit_reached() is True
    assert tracker.spent_today_usd == 10.0


def test_zero_cost_not_counted() -> None:
    tracker = CostTracker(daily_limit_usd=10.0)
    tracker.record(0.0)
    assert tracker.spent_today_usd == 0.0


def test_resets_at_midnight() -> None:
    tracker = CostTracker(daily_limit_usd=10.0)
    tracker.record(15.0)
    assert tracker.limit_reached() is True
    # Simulate the day rolling over
    tracker._day = date.today() - timedelta(days=1)
    assert tracker.limit_reached() is False
    assert tracker.spent_today_usd == 0.0
