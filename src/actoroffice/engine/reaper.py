"""Startup recovery: runs that were alive when the process died are finished, their tickets
released and re-queued, so nothing stays locked forever."""

from __future__ import annotations

from typing import TYPE_CHECKING

from actoroffice.domain.run import RunStatus, WakeSource
from actoroffice.domain.work import TicketStatus

if TYPE_CHECKING:
    from actoroffice.app import Office


async def reap_orphans(office: Office) -> int:
    orphans = await office.runs.by_status(RunStatus.RUNNING, RunStatus.QUEUED)
    for run in orphans:
        await office.runs.finish(run.id, RunStatus.FAILED, error="process ended while the run was active")
        await office.interactions.cancel_for_run(run.id)
        await office.wakes.finish(run.id)
        if run.ticket_id is None:
            continue
        ticket = await office.tickets.get(run.ticket_id)
        if ticket.checkout_run_id != run.id:
            continue
        await office.tickets.release(ticket.id, run.id)
        if ticket.status == TicketStatus.IN_PROGRESS:
            await office.tickets.transition(
                ticket.id, TicketStatus.TODO, by="office", comment="Recovered after a restart; work will resume."
            )
        if ticket.assignee_actor_id:
            await office.wakes.enqueue(ticket.assignee_actor_id, WakeSource.MANUAL, ticket_id=ticket.id, payload={"recovery": True})
    return len(orphans)
