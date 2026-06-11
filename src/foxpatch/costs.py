"""Daily spend tracking for Claude invocations."""

from __future__ import annotations

import logging
from datetime import date

logger = logging.getLogger(__name__)


class CostTracker:
    """Accumulates per-task costs and enforces a daily spend limit.

    The counter resets at local midnight. The limit is checked at dispatch
    time, so in-flight tasks can overshoot it by at most one cycle's worth
    of work — it's a brake, not an exact budget.
    """

    def __init__(self, daily_limit_usd: float = 0.0):
        self.daily_limit_usd = daily_limit_usd
        self._day = date.today()
        self._spent_usd = 0.0
        self._tasks = 0

    def _roll_day(self) -> None:
        today = date.today()
        if today != self._day:
            if self._tasks:
                logger.info(
                    "Cost summary for %s: $%.2f across %d task(s)",
                    self._day, self._spent_usd, self._tasks,
                )
            self._day = today
            self._spent_usd = 0.0
            self._tasks = 0

    def record(self, cost_usd: float) -> None:
        self._roll_day()
        if cost_usd <= 0:
            return
        self._spent_usd += cost_usd
        self._tasks += 1
        logger.info(
            "Spent $%.2f (today: $%.2f across %d task(s)%s)",
            cost_usd, self._spent_usd, self._tasks,
            f", limit ${self.daily_limit_usd:.2f}" if self.daily_limit_usd > 0 else "",
        )

    @property
    def spent_today_usd(self) -> float:
        self._roll_day()
        return self._spent_usd

    def limit_reached(self) -> bool:
        self._roll_day()
        return self.daily_limit_usd > 0 and self._spent_usd >= self.daily_limit_usd
