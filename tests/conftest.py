from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from actoroffice.core.events import EventBus
from actoroffice.domain.org import Position
from actoroffice.services.org import OrgService
from actoroffice.services.tickets import TicketService
from actoroffice.services.wakes import WakeService
from actoroffice.store import Database

from . import seed


@pytest.fixture(scope="session", autouse=True)
def _seed_templates(tmp_path_factory) -> None:
    """Templates of this test process (one per xdist worker) live in its own basetemp."""
    seed.templates_dir = tmp_path_factory.mktemp("templates")


@pytest.fixture
async def db(tmp_path) -> AsyncIterator[Database]:
    database = Database(seed.seed_db(tmp_path / "test.db"))
    await database.open()
    yield database
    await database.close()


@pytest.fixture
def bus() -> EventBus:
    return EventBus()


@pytest.fixture
def org(db: Database) -> OrgService:
    return OrgService(db)


@pytest.fixture
def wakes(db: Database) -> WakeService:
    return WakeService(db)


@pytest.fixture
def tickets(db: Database, wakes: WakeService, bus: EventBus) -> TicketService:
    return TicketService(db, wakes, bus)


@pytest.fixture
async def dev_position(org: OrgService) -> Position:
    return await org.create_position(name="dev", title="Developer")
