from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from literature_workbench.db.database import Database
from literature_workbench.db.migrations import run_migrations
from literature_workbench.db.task_queue import TaskQueue


@dataclass
class FakeClock:
    current: datetime

    def now(self) -> datetime:
        return self.current

    def advance(self, *, seconds: float) -> None:
        self.current += timedelta(seconds=seconds)


@pytest.fixture
def fake_clock() -> FakeClock:
    return FakeClock(datetime(2026, 7, 15, 8, 0, tzinfo=UTC))


@pytest.fixture
def clock(fake_clock: FakeClock) -> FakeClock:
    return fake_clock


@pytest.fixture
def database(tmp_path: Path) -> Database:
    db = Database.open(tmp_path / "db" / "test.sqlite3")
    run_migrations(db)
    return db


@pytest.fixture
def queue(database: Database, fake_clock: FakeClock) -> TaskQueue:
    return TaskQueue(database, fake_clock.now)
