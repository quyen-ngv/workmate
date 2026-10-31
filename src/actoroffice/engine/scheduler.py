"""Turns pending wake requests into runs, within the concurrency and quota limits.

Limits: one run per actor, one run per ticket (enforced by checkout), at most `max_runs`
in total. A rate-limit warning drops the cap to 1; a rejection pauses until the reset.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from typing import TYPE_CHECKING, Any

from actoroffice.domain.ids import new_id
from actoroffice.domain.org import ActorStatus
from actoroffice.domain.run import RunStatus, WakeRequest
from actoroffice.domain.work import ProjectAccess, TicketStatus

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)
CLOSED = {TicketStatus.DONE, TicketStatus.CANCELLED}


class Scheduler:
    def __init__(self, office: Office, max_runs: int) -> None:
        self.office = office
        self.max_runs = max_runs
        self._running: dict[str, asyncio.Task[RunStatus]] = {}
        # (actor_id, "ticket" | "chat") → run_id. Chat turns are read-only, so an actor may answer a chat
        # while working on a ticket; each kind still runs one at a time per actor.
        self._busy: dict[tuple[str, str], str] = {}
        self._direct_busy: dict[str, str] = {}  # run_id → project_id, for projects edited in place
        self._paused_until = 0.0
        self._quota_file = office.settings.data_dir / "quota.json"
        self.last_rate_limit: dict[str, Any] | None = self._load_quota()
        self._throttled = False
        self._kick = asyncio.Event()
        self._stopping = False

    # ---- quota awareness -------------------------------------------------------------------

    def observe_rate_limit(self, info: dict[str, Any]) -> None:
        self.last_rate_limit = {**info, "seen_at": time.time()}
        try:  # kept across restarts so the quota line is never blank
            self._quota_file.write_text(json.dumps(self.last_rate_limit), encoding="utf-8")
        except OSError:
            log.warning("could not save the last rate limit")
        status = info.get("status")
        if status == "rejected" and info.get("resets_at"):
            self._paused_until = float(info["resets_at"])
            log.warning("usage limit reached; pausing new runs until %s", info["resets_at"])
        self._throttled = status in ("allowed_warning", "rejected")

    def _load_quota(self) -> dict[str, Any] | None:
        try:
            info = json.loads(self._quota_file.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None
        resets = info.get("resets_at")
        if info.get("status") == "rejected" and resets and float(resets) > time.time():
            self._paused_until = float(resets)  # still over the limit: do not start runs that would fail
        return info

    @property
    def capacity(self) -> int:
        if time.time() < self._paused_until:
            return 0
        return 1 if self._throttled else self.max_runs

    @property
    def running(self) -> dict[str, str]:
        """actor_id → run_id of its ticket run (or chat run when it has no ticket run)."""
        out: dict[str, str] = {}
        for (actor_id, kind), run_id in self._busy.items():
            if kind == "ticket" or actor_id not in out:
                out[actor_id] = run_id
        return out

    def busy(self) -> list[dict[str, str]]:
        return [{"actor_id": a, "kind": k, "run_id": r} for (a, k), r in self._busy.items()]

    # ---- loop -----------------------------------------------------------------------------

    def kick(self) -> None:
        self._kick.set()

    async def run_forever(self, poll_s: float = 2.0) -> None:
        while not self._stopping:
            try:
                await self.tick()
            except Exception:
                log.exception("scheduler tick failed")
            self._kick.clear()
            try:
                await asyncio.wait_for(self._kick.wait(), poll_s)
            except TimeoutError:
                pass

    async def tick(self) -> list[str]:
        started: list[str] = []
        for wake in await self.office.wakes.pending(due_only=True):
            kind = "chat" if wake.ticket_id is None else "ticket"
            # One extra slot is reserved for chat so the user always gets an answer.
            limit = self.capacity + (1 if kind == "chat" and self.capacity > 0 else 0)
            if len(self._running) >= limit:
                continue
            if (wake.actor_id, kind) in self._busy:
                continue
            if not await self._eligible(wake):
                continue
            direct_project = await self._direct_project(wake)
            if direct_project and direct_project in self._direct_busy.values():
                continue  # someone is already editing that folder directly
            run_id = new_id("run")
            async with self.office.db.tx() as tx:
                if not await self.office.wakes.claim(wake.id, run_id, tx):
                    continue
            self._busy[(wake.actor_id, kind)] = run_id
            if direct_project:
                self._direct_busy[run_id] = direct_project
            self._running[run_id] = asyncio.create_task(self._execute(wake, run_id, kind), name=run_id)
            started.append(run_id)
        return started

    async def _direct_project(self, wake: WakeRequest) -> str | None:
        if wake.ticket_id is None:
            return None
        ticket = await self.office.tickets.get(wake.ticket_id)
        if not ticket.project_id:
            return None
        project = await self.office.projects.get(ticket.project_id)
        return project.id if project.access == ProjectAccess.DIRECT and project.path else None

    async def _eligible(self, wake: WakeRequest) -> bool:
        office = self.office
        actor = await office.org.get_actor(wake.actor_id)
        if actor.status != ActorStatus.ACTIVE:
            return False  # stays pending until the actor is resumed
        if wake.ticket_id is None:
            if wake.conversation_id is None:
                await office.wakes.drop(wake.id, "nothing to work on")
                return False
            conv = await office.chat.get(wake.conversation_id)
            if conv.archived or wake.actor_id not in conv.actor_ids:
                await office.wakes.drop(wake.id, "conversation archived / actor left")
                return False
            return True
        ticket = await office.tickets.get(wake.ticket_id)
        if ticket.status in CLOSED or ticket.assignee_actor_id != wake.actor_id:
            await office.wakes.drop(wake.id, f"ticket is {ticket.status.value} / reassigned")
            return False
        if ticket.paused:
            return False  # stays pending until someone resumes the ticket
        return True

    async def _execute(self, wake: WakeRequest, run_id: str, kind: str) -> RunStatus:
        try:
            if kind == "chat":
                return await self.office.executor.run_chat(wake, run_id)
            return await self.office.executor.run_ticket(wake, run_id)
        finally:
            self._running.pop(run_id, None)
            self._direct_busy.pop(run_id, None)
            if self._busy.get((wake.actor_id, kind)) == run_id:
                del self._busy[(wake.actor_id, kind)]
            self.kick()

    async def wait_idle(self, timeout: float | None = None) -> None:
        """Wait until no run is active and nothing is due (tests and one-shot CLI runs)."""
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            if self._running:
                await asyncio.wait(list(self._running.values()), timeout=1.0)
            else:
                if not await self.tick():
                    return
            if deadline is not None and time.monotonic() > deadline:
                raise TimeoutError("scheduler did not become idle")

    async def stop_run(self, run_id: str) -> None:
        await self.office.runtime.cancel(run_id)

    async def shutdown(self) -> None:
        self._stopping = True
        self.kick()
        for run_id in list(self._running):
            await self.office.runtime.cancel(run_id)
        if self._running:
            await asyncio.wait(list(self._running.values()), timeout=30)
