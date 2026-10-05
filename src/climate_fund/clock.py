"""可控时钟：所有时间戳统一由时钟注入，测试可随意拨动时间。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


class Clock:
    """时钟接口。"""

    def now(self) -> datetime:  # pragma: no cover - 接口定义
        raise NotImplementedError


class SystemClock(Clock):
    """真实时钟（UTC）。"""

    def now(self) -> datetime:
        return datetime.now(timezone.utc)


class ManualClock(Clock):
    """手动时钟，测试用：可设定、可步进。"""

    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 1, 0, 0, 0)

    def now(self) -> datetime:
        return self._now

    def set(self, moment: datetime) -> None:
        self._now = moment

    def advance(self, **kwargs) -> datetime:
        """按 timedelta 参数前进，如 advance(days=2, hours=3)。"""
        self._now = self._now + timedelta(**kwargs)
        return self._now
