"""Business hours parsing and timezone-aware checks."""

from __future__ import annotations

from datetime import date, datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from custcomm.config.business_hours import BusinessHours, is_business_hours, weekday_sunday0
from custcomm.config.loader import load_config


SAMPLE_BUSINESS_HOURS = {
    "timezone": "America/Chicago",
    "working_hours": [
        {"weekday": 0, "open_at": "09:00", "close_at": "21:00"},
        {"weekday": 1, "open_at": "17:00", "close_at": "21:00"},
        {"weekday": 2, "open_at": "17:00", "close_at": "21:00"},
        {"weekday": 3, "open_at": "17:00", "close_at": "21:00"},
        {"weekday": 4, "open_at": "17:00", "close_at": "21:00"},
        {"weekday": 5, "open_at": "17:00", "close_at": "21:00"},
        {"weekday": 6, "open_at": "09:00", "close_at": "21:00"},
    ],
}


@pytest.fixture
def sample_business_hours() -> BusinessHours:
    return BusinessHours.from_config(SAMPLE_BUSINESS_HOURS)


def test_weekday_sunday0_convention() -> None:
    assert weekday_sunday0(date(2025, 5, 18)) == 0  # Sunday
    assert weekday_sunday0(date(2025, 5, 19)) == 1  # Monday


def test_load_config_parses_numeric_working_hours(tmp_path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "client_name": "Example Co",
                "operator_name": "Alex",
                "operator_email": "alex@example.com",
                "scheduler": {"business_hours": SAMPLE_BUSINESS_HOURS},
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    bh = cfg.scheduler.business_hours
    assert bh.timezone == "America/Chicago"
    monday = next(w for w in bh.working_hours if w.weekday == 1)
    saturday = next(w for w in bh.working_hours if w.weekday == 6)
    assert monday.open_at == "17:00"
    assert saturday.open_at == "09:00"


def test_from_config_rejects_legacy_flat_format() -> None:
    with pytest.raises(ValueError, match="working_hours is required"):
        BusinessHours.from_config(
            {
                "timezone": "America/New_York",
                "start": "09:00",
                "end": "17:00",
                "weekdays_only": True,
            }
        )


def test_from_config_rejects_named_day_schedule() -> None:
    with pytest.raises(ValueError, match="working_hours is required"):
        BusinessHours.from_config(
            {
                "timezone": "America/New_York",
                "schedule": {
                    "monday": {"start": "09:00", "end": "17:00", "enabled": True},
                },
            }
        )


def test_is_business_hours_weekday_in_hours(sample_business_hours: BusinessHours) -> None:
    chicago = ZoneInfo("America/Chicago")
    moment = datetime(2025, 5, 19, 18, 0, tzinfo=chicago)
    assert is_business_hours(sample_business_hours, moment) is True


def test_is_business_hours_weekday_out_of_hours(sample_business_hours: BusinessHours) -> None:
    chicago = ZoneInfo("America/Chicago")
    moment = datetime(2025, 5, 19, 10, 0, tzinfo=chicago)
    assert is_business_hours(sample_business_hours, moment) is False


def test_is_business_hours_weekend_in_hours(sample_business_hours: BusinessHours) -> None:
    chicago = ZoneInfo("America/Chicago")
    moment = datetime(2025, 5, 24, 10, 0, tzinfo=chicago)
    assert is_business_hours(sample_business_hours, moment) is True


def test_is_business_hours_weekend_out_of_hours(sample_business_hours: BusinessHours) -> None:
    chicago = ZoneInfo("America/Chicago")
    moment = datetime(2025, 5, 24, 22, 0, tzinfo=chicago)
    assert is_business_hours(sample_business_hours, moment) is False
