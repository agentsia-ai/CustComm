"""Business hours parsing and timezone-aware checks."""

from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
import yaml

from custcomm.config.business_hours import BusinessHours, is_business_hours
from custcomm.config.loader import load_config


ARIA_BUSINESS_HOURS = {
    "timezone": "America/Chicago",
    "schedule": {
        "monday": {"start": "17:00", "end": "21:00", "enabled": True},
        "tuesday": {"start": "17:00", "end": "21:00", "enabled": True},
        "wednesday": {"start": "17:00", "end": "21:00", "enabled": True},
        "thursday": {"start": "17:00", "end": "21:00", "enabled": True},
        "friday": {"start": "17:00", "end": "21:00", "enabled": True},
        "saturday": {"start": "09:00", "end": "21:00", "enabled": True},
        "sunday": {"start": "09:00", "end": "21:00", "enabled": True},
    },
}


@pytest.fixture
def aria_business_hours() -> BusinessHours:
    return BusinessHours.from_config(ARIA_BUSINESS_HOURS)


def test_load_config_parses_per_day_schedule(tmp_path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "client_name": "Example Co",
                "operator_name": "Alex",
                "operator_email": "alex@example.com",
                "scheduler": {"business_hours": ARIA_BUSINESS_HOURS},
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    bh = cfg.scheduler.business_hours
    assert bh.timezone == "America/Chicago"
    assert bh.schedule["monday"].start == "17:00"
    assert bh.schedule["saturday"].start == "09:00"
    assert bh.schedule["saturday"].enabled is True


def test_load_config_parses_legacy_flat_format(tmp_path) -> None:
    cfg_path = tmp_path / "config.yaml"
    cfg_path.write_text(
        yaml.safe_dump(
            {
                "client_name": "Example Co",
                "operator_name": "Alex",
                "operator_email": "alex@example.com",
                "scheduler": {
                    "business_hours": {
                        "timezone": "America/New_York",
                        "start": "09:00",
                        "end": "17:00",
                        "weekdays_only": True,
                    }
                },
            }
        ),
        encoding="utf-8",
    )

    cfg = load_config(cfg_path)
    bh = cfg.scheduler.business_hours
    assert bh.schedule["monday"].enabled is True
    assert bh.schedule["monday"].start == "09:00"
    assert bh.schedule["saturday"].enabled is False


def test_is_business_hours_weekday_in_hours(aria_business_hours: BusinessHours) -> None:
    chicago = ZoneInfo("America/Chicago")
    moment = datetime(2025, 5, 19, 18, 0, tzinfo=chicago)
    assert is_business_hours(aria_business_hours, moment) is True


def test_is_business_hours_weekday_out_of_hours(aria_business_hours: BusinessHours) -> None:
    chicago = ZoneInfo("America/Chicago")
    moment = datetime(2025, 5, 19, 10, 0, tzinfo=chicago)
    assert is_business_hours(aria_business_hours, moment) is False


def test_is_business_hours_weekend_in_hours(aria_business_hours: BusinessHours) -> None:
    chicago = ZoneInfo("America/Chicago")
    moment = datetime(2025, 5, 24, 10, 0, tzinfo=chicago)
    assert is_business_hours(aria_business_hours, moment) is True


def test_is_business_hours_weekend_out_of_hours(aria_business_hours: BusinessHours) -> None:
    chicago = ZoneInfo("America/Chicago")
    moment = datetime(2025, 5, 24, 22, 0, tzinfo=chicago)
    assert is_business_hours(aria_business_hours, moment) is False


def test_is_business_hours_accepts_legacy_dict() -> None:
    chicago = ZoneInfo("America/New_York")
    moment = datetime(2025, 5, 19, 10, 0, tzinfo=chicago)
    legacy = {
        "timezone": "America/New_York",
        "start": "09:00",
        "end": "17:00",
        "weekdays_only": True,
    }
    assert is_business_hours(legacy, moment) is True
