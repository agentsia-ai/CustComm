"""Appointment storage + slot-availability helpers.

v1 scope is intentionally small: CustComm tracks appointments that have
been PROPOSED / CONFIRMED / RESCHEDULED / CANCELLED against threads, and
exposes a stub `available_slots_for()` that generates business-hours slots.
Real calendar integration (Google Calendar, Outlook) is a post-v1 concern;
the `AppointmentHandler.propose` API accepts any list of slots so the
integration point is already clean.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Optional

from custcomm.config.business_hours import parse_hhmm
from custcomm.config.loader import CustCommConfig
from custcomm.crm.database import ThreadDatabase
from custcomm.models import (
    Appointment,
    AppointmentStatus,
    TimeSlot,
)

logger = logging.getLogger(__name__)


class AppointmentStore:
    def __init__(self, config: CustCommConfig, db: ThreadDatabase) -> None:
        self.config = config
        self.db = db

    async def propose(
        self,
        thread_id: str,
        customer_id: str,
        start_at: datetime,
        end_at: datetime,
        timezone: str = "UTC",
        location_or_link: Optional[str] = None,
        notes: str = "",
    ) -> Appointment:
        appt = Appointment(
            thread_id=thread_id,
            customer_id=customer_id,
            status=AppointmentStatus.PROPOSED,
            start_at=start_at,
            end_at=end_at,
            timezone=timezone,
            location_or_link=location_or_link,
            notes=notes,
        )
        await self.db.upsert_appointment(appt)
        return appt

    async def confirm(self, appointment_id: str) -> Optional[Appointment]:
        appt = await self.db.get_appointment(appointment_id)
        if not appt:
            return None
        appt.status = AppointmentStatus.CONFIRMED
        await self.db.upsert_appointment(appt)
        return appt

    async def reschedule(
        self,
        appointment_id: str,
        new_start: datetime,
        new_end: datetime,
    ) -> Optional[Appointment]:
        appt = await self.db.get_appointment(appointment_id)
        if not appt:
            return None
        appt.status = AppointmentStatus.RESCHEDULED
        appt.start_at = new_start
        appt.end_at = new_end
        await self.db.upsert_appointment(appt)
        return appt

    async def cancel(self, appointment_id: str) -> Optional[Appointment]:
        appt = await self.db.get_appointment(appointment_id)
        if not appt:
            return None
        appt.status = AppointmentStatus.CANCELLED
        await self.db.upsert_appointment(appt)
        return appt

    def available_slots_for(
        self,
        day: date,
        count: int = 3,
        tz_name: Optional[str] = None,
    ) -> list[TimeSlot]:
        """Generate `count` naive-UTC slots within business hours on `day`.

        Intentionally naive — no real calendar integration in v1. Downstream
        deployments can replace this method when they wire up Google Calendar
        or similar.
        """
        bh = self.config.scheduler.business_hours
        tz = tz_name or bh.timezone
        windows = bh.windows_for_date(day)
        if not windows:
            return []

        slot_mins = self.config.scheduler.appointment_slot_minutes
        window = windows[0]
        start_t = parse_hhmm(window.open_at)
        end_t = parse_hhmm(window.close_at)

        cursor = datetime.combine(day, start_t)
        end_of_day = datetime.combine(day, end_t)
        slots: list[TimeSlot] = []
        while cursor + timedelta(minutes=slot_mins) <= end_of_day and len(slots) < count:
            end = cursor + timedelta(minutes=slot_mins)
            slots.append(TimeSlot(start_at=cursor, end_at=end, timezone=tz))
            cursor = end + timedelta(
                minutes=self.config.scheduler.appointment_buffer_minutes
            )
        return slots
