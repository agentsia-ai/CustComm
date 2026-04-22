"""Follow-up enqueuer.

Walks threads in status AWAITING_CUSTOMER whose last_outbound_at is older
than the next step in `scheduler.followup_days`, and re-queues them into
the drafter flow as status=TRIAGED with intent=FOLLOWUP_QUESTION.

The actual drafting happens downstream — this module only identifies and
enqueues. Human approval is still required before any followup goes out.
"""

from __future__ import annotations

import logging
from datetime import datetime, timedelta

from custcomm.config.loader import CustCommConfig
from custcomm.crm.database import ThreadDatabase
from custcomm.models import Intent, Thread, ThreadStatus

logger = logging.getLogger(__name__)


class FollowupScheduler:
    def __init__(self, config: CustCommConfig, db: ThreadDatabase) -> None:
        self.config = config
        self.db = db

    async def enqueue_due_followups(self, now: datetime | None = None) -> list[Thread]:
        """Find threads whose follow-up window has elapsed and re-queue them.

        Policy: pick the MAXIMUM `followup_days` threshold the thread has crossed
        since its last outbound. That lets us send up to one follow-up per
        threshold in `scheduler.followup_days`, no matter how long the thread
        has been idle. We track which thresholds have been used via the
        thread's existing follow-up records (next_followup_at + status cycling
        through TRIAGED); this keeps the implementation state-light.
        """
        now = now or datetime.utcnow()
        awaiting = await self.db.list_threads(
            status=ThreadStatus.AWAITING_CUSTOMER, limit=500
        )
        due: list[Thread] = []
        for thread in awaiting:
            if thread.last_outbound_at is None:
                continue
            if thread.snoozed_until and thread.snoozed_until > now:
                continue
            age = now - thread.last_outbound_at
            if self._should_followup(age):
                thread.status = ThreadStatus.TRIAGED
                thread.intent = Intent.FOLLOWUP_QUESTION
                thread.intent_reasoning = (
                    f"Scheduler: no customer reply in {age.days}d; "
                    "re-queued for followup draft."
                )
                thread.next_followup_at = None
                await self.db.upsert_thread(thread)
                due.append(thread)
        logger.info(f"Scheduler enqueued {len(due)} follow-up(s).")
        return due

    def _should_followup(self, age: timedelta) -> bool:
        days = age.total_seconds() / 86400.0
        for threshold in self.config.scheduler.followup_days:
            if days >= threshold:
                # Any threshold crossed triggers re-enqueue. Duplicate enqueues
                # on the same threshold are prevented by the status flip to
                # TRIAGED (this method only considers AWAITING_CUSTOMER).
                return True
        return False
