from __future__ import annotations

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

import pytest  # noqa: E402

from app.event_bus import EventBus  # noqa: E402
from app.storage import Storage  # noqa: E402

SCHEMA_PATH = str(PROJECT_ROOT / "schema" / "001_init.sql")


@pytest.fixture
async def storage(tmp_path):
    """A real Storage instance against a fresh temp SQLite file -- per
    CONTRIBUTING.md's stated preference, prefer a real file over mocking
    the thing under test. pytest's tmp_path gives each test its own
    directory, cleaned up automatically."""
    db_path = str(tmp_path / "test.db")
    s = Storage(db_path, SCHEMA_PATH, event_bus=EventBus())
    await s.connect()
    yield s
    await s.close()
