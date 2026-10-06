"""可控时钟：所有业务时间戳均从这里取，便于测试暂停/恢复等时间相关流程。"""

from datetime import datetime, timedelta, timezone


class Clock:
    def __init__(self, start: datetime | None = None):
        self._now = start or datetime(2026, 1, 1, 9, 0, tzinfo=timezone.utc)

    def now(self) -> datetime:
        return self._now

    def advance(self, seconds: int = 0, **kwargs) -> datetime:
        if seconds:
            kwargs["seconds"] = kwargs.get("seconds", 0) + seconds
        self._now += timedelta(**kwargs)
        return self._now

    def set(self, value: datetime) -> None:
        self._now = value
