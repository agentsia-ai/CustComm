"""Shared pytest fixtures for CustComm tests.

All fixtures here must work offline — no real Anthropic or Gmail calls.
"""

from __future__ import annotations

import tempfile
from pathlib import Path

import pytest
import pytest_asyncio

from custcomm.config.loader import (
    AIConfig,
    APIKeys,
    CustCommConfig,
    DatabaseConfig,
    HistoryConfig,
    InboxConfig,
    OutreachConfig,
    SchedulerConfig,
)
from custcomm.crm.database import ThreadDatabase


@pytest.fixture
def tmp_db_path() -> str:
    """Temporary SQLite path. Cleanup handled by tempdir teardown."""
    tmp = tempfile.mkdtemp()
    return str(Path(tmp) / "test_custcomm.db")


@pytest.fixture
def test_config() -> CustCommConfig:
    return CustCommConfig(
        client_name="TestCorp",
        operator_name="Tester",
        operator_email="tester@example.com",
        ai=AIConfig(model="claude-sonnet-4-20250514"),
        inbox=InboxConfig(backend="gmail"),
        outreach=OutreachConfig(
            backend="gmail",
            require_approval=True,
            auto_send=False,
            signature="Thanks,\n{operator_name}",
        ),
        history=HistoryConfig(full_messages_kept=3, summarize_at_messages=5),
        scheduler=SchedulerConfig(),
        database=DatabaseConfig(backend="sqlite", sqlite_path="./data/test.db"),
    )


@pytest.fixture
def test_keys() -> APIKeys:
    return APIKeys(ANTHROPIC_API_KEY="sk-ant-test-offline")


@pytest_asyncio.fixture
async def initialized_db(tmp_db_path: str) -> ThreadDatabase:
    db = ThreadDatabase(tmp_db_path)
    await db.init()
    return db
