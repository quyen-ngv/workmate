"""Clock for work without the LLM: starts automations whose time has come."""

from __future__ import annotations

import asyncio
import logging
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)


async def run_timers(office: Office, every_s: float = 15.0) -> None:
    while True:
        try:
            await office.automations.run_due()
        except Exception:
            log.exception("timer tick failed")
        await asyncio.sleep(every_s)
