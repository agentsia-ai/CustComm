"""Business hours config parsing and timezone-aware checks."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo


def weekday_sunday0(d: date) -> int:
    """Map a calendar date to 0=Sunday .. 6=Saturday."""
    return (d.weekday() + 1) % 7


class WorkingWindow(BaseModel):
    weekday: int  # 0=Sunday .. 6=Saturday
    open_at: str = "09:00"
    close_at: str = "17:00"


class BusinessHours(BaseModel):
    timezone: str = "America/New_York"
    working_hours: list[WorkingWindow] = Field(default_factory=list)

    @classmethod
    def default(cls) -> BusinessHours:
        """Mon–Fri 09:00–17:00 in America/New_York (weekday 0 = Sunday)."""
        return cls(
            timezone="America/New_York",
            working_hours=[
                WorkingWindow(weekday=i, open_at="09:00", close_at="17:00")
                for i in range(1, 6)
            ],
        )

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> BusinessHours:
        tz = raw.get("timezone", "America/New_York")
        if "working_hours" not in raw:
            raise ValueError(
                "business_hours.working_hours is required "
                "(numeric weekday list; weekday 0 = Sunday)"
            )

        windows: list[WorkingWindow] = []
        for entry in raw.get("working_hours") or []:
            if isinstance(entry, WorkingWindow):
                windows.append(entry)
            else:
                windows.append(WorkingWindow(**entry))
        return cls(timezone=tz, working_hours=windows)

    def windows_for_date(self, d: date) -> list[WorkingWindow]:
        wd = weekday_sunday0(d)
        return [w for w in self.working_hours if w.weekday == wd]

    def is_business_hours(self, moment: datetime | None = None) -> bool:
        tz = ZoneInfo(self.timezone)
        local = (moment or datetime.now(tz)).astimezone(tz)
        now_t = local.time()
        for window in self.windows_for_date(local.date()):
            start_t = parse_hhmm(window.open_at)
            end_t = parse_hhmm(window.close_at)
            if start_t <= now_t <= end_t:
                return True
        return False


def is_business_hours(
    business_hours: BusinessHours | dict[str, Any],
    moment: datetime | None = None,
) -> bool:
    bh = (
        business_hours
        if isinstance(business_hours, BusinessHours)
        else BusinessHours.from_config(business_hours)
    )
    return bh.is_business_hours(moment)


def parse_hhmm(s: str) -> time:
    h, m = s.split(":")
    return time(hour=int(h), minute=int(m))
