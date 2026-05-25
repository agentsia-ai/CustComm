"""Business hours config parsing and timezone-aware checks."""

from __future__ import annotations

from datetime import date, datetime, time
from typing import Any

from pydantic import BaseModel, Field
from zoneinfo import ZoneInfo

DAY_NAMES = (
    "monday",
    "tuesday",
    "wednesday",
    "thursday",
    "friday",
    "saturday",
    "sunday",
)


class DaySchedule(BaseModel):
    start: str = "09:00"
    end: str = "17:00"
    enabled: bool = True


class BusinessHours(BaseModel):
    timezone: str = "America/New_York"
    schedule: dict[str, DaySchedule] = Field(default_factory=dict)

    @classmethod
    def from_config(cls, raw: dict[str, Any]) -> BusinessHours:
        tz = raw.get("timezone", "America/New_York")

        if "schedule" in raw:
            schedule: dict[str, DaySchedule] = {}
            raw_schedule = raw.get("schedule") or {}
            normalized = {str(k).lower(): v for k, v in raw_schedule.items()}
            for day in DAY_NAMES:
                day_raw = normalized.get(day)
                if day_raw is None:
                    schedule[day] = DaySchedule(enabled=False)
                elif isinstance(day_raw, DaySchedule):
                    schedule[day] = day_raw
                else:
                    schedule[day] = DaySchedule(**day_raw)
            return cls(timezone=tz, schedule=schedule)

        start = raw.get("start", "09:00")
        end = raw.get("end", "17:00")
        weekdays_only = raw.get("weekdays_only", True)
        schedule = {}
        for i, day in enumerate(DAY_NAMES):
            enabled = (i < 5) if weekdays_only else True
            schedule[day] = DaySchedule(start=start, end=end, enabled=enabled)
        return cls(timezone=tz, schedule=schedule)

    @classmethod
    def default_legacy(cls) -> BusinessHours:
        return cls.from_config(
            {
                "timezone": "America/New_York",
                "start": "09:00",
                "end": "17:00",
                "weekdays_only": True,
            }
        )

    def day_name_for(self, d: date) -> str:
        return DAY_NAMES[d.weekday()]

    def schedule_for_date(self, d: date) -> DaySchedule:
        return self.schedule[self.day_name_for(d)]

    def is_business_hours(self, moment: datetime | None = None) -> bool:
        tz = ZoneInfo(self.timezone)
        local = (moment or datetime.now(tz)).astimezone(tz)
        day_sched = self.schedule_for_date(local.date())
        if not day_sched.enabled:
            return False
        start_t = parse_hhmm(day_sched.start)
        end_t = parse_hhmm(day_sched.end)
        now_t = local.time()
        return start_t <= now_t <= end_t


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
