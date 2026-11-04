"""Automations: flows an actor designs (notify / script / agent steps) and the office runs on a schedule.

Code first: deterministic work runs as reviewed scripts without the LLM; messages are posted without the LLM;
only `agent` steps spend an employee's time (as a normal ticket). Flows that run code or hand out work need the
user's review of exactly that version (a fingerprint of the steps and the script code); message-only flows,
such as reminders, start right away. A failing flow opens one fix ticket for its owner (self-healing).

Layout: <data>/automations/<name>/<script files>  plus  out/ (files the scripts write; also $AO_OUTPUT_DIR)
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import os
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.core.events import EventBus
from actoroffice.core.schedule import describe, from_iso, local_now, next_after, normalize_repeat, parse_when, to_utc_iso
from actoroffice.domain.automation import Automation, AutomationRun, AutomationStatus, Step, StepType
from actoroffice.domain.errors import Conflict, NotFound, OfficeError
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.work import InteractionKind, Priority, TicketStatus
from actoroffice.proctree import kill_tree
from actoroffice.store import Database

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)
NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
MIN_EVERY_MINUTES = 5  # for flows that run code or hand out work
MAX_FAILURES = 3  # consecutive failures before a scheduled automation is paused
MAX_STEPS = 10
TAIL = 4000
SAFE_ENV = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "LOCALAPPDATA", "APPDATA",
    "USERPROFILE", "HOMEDRIVE", "HOMEPATH", "HOME", "NUMBER_OF_PROCESSORS", "PROCESSOR_ARCHITECTURE", "LANG", "TZ",
    "UV_CACHE_DIR", "UV_PYTHON_INSTALL_DIR",
)


class AutomationError(OfficeError):
    code = 422


def slug(name: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", name.strip().lower()).strip("-")
    if not NAME.match(value):
        raise AutomationError("Automation name: 2-49 chars, letters, digits and '-'")
    return value


class _Blank(dict[str, str]):
    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


class AutomationService:
    def __init__(self, office: Office, root: Path) -> None:
        self.office = office
        self.db: Database = office.db
        self.bus: EventBus = office.bus
        self.root = root
        self._running: dict[str, asyncio.Task[AutomationRun]] = {}

    # ---- definitions --------------------------------------------------------------------------

    async def save(
        self,
        name: str,
        *,
        steps: list[dict[str, Any]] | list[Step],
        description: str,
        owner_actor_id: str,
        conversation_id: str | None,
        schedule: str | None = None,
        when: str | None = None,
        workspace: Path | None = None,
        ticket_id: int | None = None,
        project_id: str | None = None,
        run_id: str | None = None,
    ) -> Automation:
        """Create or replace a flow. Flows with script or agent steps (re)enter review; message-only flows start."""
        name = slug(name)
        try:
            parsed = [s if isinstance(s, Step) else Step.model_validate(s) for s in steps]
        except ValueError as exc:
            raise AutomationError(f"Invalid steps: {exc}") from exc
        if not parsed or len(parsed) > MAX_STEPS:
            raise AutomationError(f"An automation has 1 to {MAX_STEPS} steps")
        try:
            schedule = normalize_repeat(schedule)
            first = parse_when(when) if when else None
        except ValueError as exc:
            raise AutomationError(str(exc)) from exc
        if first is not None and first < local_now().replace(second=0, microsecond=0):
            raise AutomationError(f"{first:%Y-%m-%d %H:%M} is in the past")
        heavy = any(s.type != StepType.NOTIFY for s in parsed)
        if heavy and schedule and schedule.startswith("every:") and int(schedule.split(":")[1]) < MIN_EVERY_MINUTES:
            raise AutomationError(f"Flows with scripts or agent steps run at most every {MIN_EVERY_MINUTES} minutes")
        for step in parsed:
            if step.type == StepType.AGENT and await self.office.org.find_actor(step.actor) is None:
                raise AutomationError(f"No employee named {step.actor} (agent step)")
        existing = await self._by_name(name)
        if existing and existing.owner_actor_id != owner_actor_id:
            raise Conflict(f"Automation '{name}' belongs to another employee")
        if existing and existing.id in self._running:
            raise Conflict(f"Automation '{name}' is running; try again when it finishes")
        scripts = [s for s in parsed if s.type == StepType.SCRIPT]
        if scripts and workspace is None:
            raise AutomationError("Script steps need a ticket (the script must be written and tested in its working directory)")
        sources: list[tuple[Path, str]] = []
        for step in scripts:
            assert workspace is not None
            source = (workspace / step.file).resolve()
            if workspace.resolve() not in source.parents or source.suffix != ".py" or not source.is_file():
                raise AutomationError(f"Script {step.file} must be a .py file inside your working directory")
            if source.stat().st_size > 200_000:
                raise AutomationError(f"Script {step.file} is too large (max 200 KB)")
            step.file = source.name
            step.timeout_s = max(10, min(int(step.timeout_s), 3600))
            sources.append((source, source.name))
        if existing:
            # Older review cards describe a version that is about to be replaced: they can no longer be approved.
            await self.db.execute(
                "UPDATE interactions SET status = 'cancelled', resolved_at = ? WHERE kind = 'approve_automation' "
                "AND status = 'pending' AND json_extract(payload_json, '$.automation_id') = ?",
                (utcnow(), existing.id),
            )
        folder = self.root / name
        (folder / "out").mkdir(parents=True, exist_ok=True)
        for source, target in sources:
            shutil.copyfile(source, folder / target)
        steps_json = json.dumps([_compact(s) for s in parsed], ensure_ascii=False)
        draft = Automation(id="-", name=name, steps=parsed)
        status = AutomationStatus.PENDING_REVIEW if draft.needs_review else AutomationStatus.ENABLED
        approved = None if draft.needs_review else self.fingerprint(name, parsed)
        next_at = self._first_slot(schedule, first) if status == AutomationStatus.ENABLED else None
        now = utcnow()
        if existing:
            await self.db.execute(
                "UPDATE automations SET description = ?, steps_json = ?, schedule = ?, status = ?, approved_sha = ?, next_at = ?, "
                "failures = 0, conversation_id = COALESCE(?, conversation_id), ticket_id = COALESCE(?, ticket_id), updated_at = ? "
                "WHERE id = ?",
                (description, steps_json, schedule, status.value, approved, next_at, conversation_id, ticket_id, now, existing.id),
            )
            automation_id = existing.id
        else:
            automation_id = new_id("au")
            await self.db.execute(
                "INSERT INTO automations(id, name, description, owner_actor_id, project_id, conversation_id, ticket_id, steps_json, "
                "schedule, next_at, status, approved_sha, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (automation_id, name, description, owner_actor_id, project_id, conversation_id, ticket_id, steps_json, schedule,
                 next_at, status.value, approved, now, now),
            )
        automation = await self.get(automation_id)
        if automation.needs_review:
            await self._request_review(automation, first=first, run_id=run_id)
        self.bus.publish("automation.changed", automation=automation.model_dump())
        return automation

    def _first_slot(self, schedule: str | None, first: datetime | None) -> str | None:
        if first is not None:
            return to_utc_iso(first)
        return to_utc_iso(next_after(schedule, local_now())) if schedule else None

    def fingerprint(self, name: str, steps: list[Step]) -> str:
        digest = hashlib.sha256(json.dumps([_compact(s) for s in steps], sort_keys=True).encode())
        for step in steps:
            if step.type == StepType.SCRIPT:
                path = self.root / name / step.file
                digest.update(path.read_bytes() if path.is_file() else b"<missing>")
        return digest.hexdigest()

    async def _request_review(self, automation: Automation, *, first: datetime | None, run_id: str | None) -> None:
        owner = await self.office.org.get_actor(automation.owner_actor_id) if automation.owner_actor_id else None
        code = {
            s.file: self.office.redact(self.script_code(automation, s.file)[:8000])
            for s in automation.steps
            if s.type == StepType.SCRIPT
        }
        await self.office.interactions.create(
            InteractionKind.APPROVE_AUTOMATION,
            {
                "automation_id": automation.id,
                "name": automation.name,
                "description": automation.description,
                "schedule": automation.schedule,
                "schedule_text": schedule_text(automation.schedule, to_utc_iso(first) if first else None),
                "first_at": to_utc_iso(first) if first else None,
                "steps": [_compact(s) for s in automation.steps],
                "code": code,
                "fingerprint": self.fingerprint(automation.name, automation.steps),
                "owner": owner.name if owner else None,
                "conversation_id": automation.conversation_id,
            },
            actor_id=automation.owner_actor_id,
            ticket_id=automation.ticket_id,
            run_id=run_id,
        )

    async def get(self, ref: str) -> Automation:
        row = await self.db.fetchone("SELECT * FROM automations WHERE id = ? OR name = ?", (ref, ref))
        if not row:
            raise NotFound(f"Automation {ref} not found")
        return Automation.from_row(row)

    async def _by_name(self, name: str) -> Automation | None:
        row = await self.db.fetchone("SELECT * FROM automations WHERE name = ?", (name,))
        return Automation.from_row(row) if row else None

    async def list(
        self,
        *,
        owner_actor_id: str | None = None,
        project_id: str | None = None,
        conversation_id: str | None = None,
        active_only: bool = False,
    ) -> list[Automation]:
        where, params = [], []
        for column, value in (("owner_actor_id", owner_actor_id), ("project_id", project_id), ("conversation_id", conversation_id)):
            if value:
                where.append(f"{column} = ?")
                params.append(value)
        if active_only:
            where.append("status IN ('enabled', 'pending_review')")
        sql = "SELECT * FROM automations" + (f" WHERE {' AND '.join(where)}" if where else "")
        sql += " ORDER BY COALESCE(next_at, '9'), name"
        return [Automation.from_row(r) for r in await self.db.fetchall(sql, params)]

    def script_code(self, automation: Automation, file: str) -> str:
        path = self.root / automation.name / file
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""

    def outputs(self, automation: Automation) -> list[dict[str, Any]]:
        out = self.root / automation.name / "out"
        if not out.is_dir():
            return []
        files = sorted((p for p in out.rglob("*") if p.is_file()), key=lambda p: p.stat().st_mtime, reverse=True)
        return [{"path": p.relative_to(out).as_posix(), "size": p.stat().st_size} for p in files[:100]]

    def read_output(self, automation: Automation, path: str, limit: int = 1_000_000) -> str:
        out = (self.root / automation.name / "out").resolve()
        target = (out / path).resolve()
        if out not in target.parents or not target.is_file():
            raise NotFound(f"No output file {path}")
        return self.office.redact(target.read_bytes()[:limit].decode("utf-8", "replace"))

    # ---- review & switches --------------------------------------------------------------------

    def check_review(self, payload: dict[str, Any]) -> None:
        """The card being approved must show the version that is current now."""
        steps = [Step.model_validate(s) for s in payload.get("steps", [])]
        if not payload.get("fingerprint") or payload["fingerprint"] != self.fingerprint(str(payload.get("name")), steps):
            raise Conflict("Automation đã thay đổi sau thẻ duyệt này; hãy duyệt thẻ mới nhất")

    async def approve(self, ref: str, fingerprint: str, *, first_at: str | None = None, by: str = "user") -> Automation:
        automation = await self.get(ref)
        if fingerprint != self.fingerprint(automation.name, automation.steps):
            raise Conflict("Automation đã thay đổi sau thẻ duyệt này; hãy duyệt thẻ mới nhất")
        await self.db.execute("UPDATE automations SET approved_sha = ? WHERE id = ?", (fingerprint, automation.id))
        return await self.set_enabled(automation.id, True, by=by, first_at=first_at)

    async def rename_actor(self, old: str, new: str) -> int:
        """Agent steps name the actor who gets the ticket; keep them pointing at the same person. An approval
        stays valid when the name is the only thing that changed (the user renamed the actor, nothing else)."""
        changed = 0
        for a in await self.list():
            hits = [s.type == StepType.AGENT and (s.actor or "").lower() == old.lower() for s in a.steps]
            if not any(hits):
                continue
            was_approved = a.approved_sha is not None and a.approved_sha == self.fingerprint(a.name, a.steps)
            steps = [s.model_copy(update={"actor": new}) if hit else s for s, hit in zip(a.steps, hits, strict=True)]
            approved = self.fingerprint(a.name, steps) if was_approved else a.approved_sha
            await self.db.execute(
                "UPDATE automations SET steps_json = ?, approved_sha = ?, updated_at = ? WHERE id = ?",
                (json.dumps([_compact(s) for s in steps], ensure_ascii=False), approved, utcnow(), a.id),
            )
            changed += 1
        return changed

    async def set_enabled(self, ref: str, enabled: bool, *, by: str = "user", first_at: str | None = None) -> Automation:
        automation = await self.get(ref)
        if enabled and automation.approved_sha != self.fingerprint(automation.name, automation.steps):
            raise Conflict("Automation khác bản đã duyệt; cần duyệt lại")
        if enabled:
            if first_at and from_iso(first_at) > datetime.now(UTC):
                next_at: str | None = first_at
            else:
                next_at = to_utc_iso(next_after(automation.schedule, local_now())) if automation.schedule else None
            await self.db.execute(
                "UPDATE automations SET status = 'enabled', next_at = ?, failures = 0, updated_at = ? WHERE id = ?",
                (next_at, utcnow(), automation.id),
            )
        else:
            await self.db.execute(
                "UPDATE automations SET status = 'disabled', next_at = NULL, updated_at = ? WHERE id = ?", (utcnow(), automation.id)
            )
        await self.office.audit.record(
            "automation:" + ("enable" if enabled else "disable"),
            "ok",
            actor_id=automation.owner_actor_id,
            target=automation.name,
            detail={"by": by},
        )
        updated = await self.get(automation.id)
        self.bus.publish("automation.changed", automation=updated.model_dump())
        return updated

    async def delete(self, ref: str) -> None:
        automation = await self.get(ref)
        if automation.id in self._running:
            raise Conflict("Automation is running")
        await self.db.execute("DELETE FROM automations WHERE id = ?", (automation.id,))
        shutil.rmtree(self.root / automation.name, ignore_errors=True)
        self.bus.publish("automation.changed", automation={**automation.model_dump(), "deleted": True})

    async def runs(self, ref: str, limit: int = 20) -> list[AutomationRun]:
        automation = await self.get(ref)
        rows = await self.db.fetchall(
            "SELECT * FROM automation_runs WHERE automation_id = ? ORDER BY id DESC LIMIT ?", (automation.id, limit)
        )
        return [AutomationRun.model_validate(r) for r in rows]

    # ---- running ------------------------------------------------------------------------------

    @property
    def running_ids(self) -> set[str]:
        return set(self._running)

    def start(self, automation: Automation, trigger: str) -> asyncio.Task[AutomationRun]:
        if automation.id in self._running:
            raise Conflict(f"Automation '{automation.name}' is already running")
        task = asyncio.create_task(self.run(automation, trigger), name=f"automation-{automation.name}")
        self._running[automation.id] = task
        task.add_done_callback(lambda _t: self._running.pop(automation.id, None))
        return task

    async def run_due(self, now: datetime | None = None) -> int:
        now = now or datetime.now(UTC)
        rows = await self.db.fetchall(
            "SELECT * FROM automations WHERE status = 'enabled' AND next_at IS NOT NULL AND next_at <= ?", (to_utc_iso(now),)
        )
        started = 0
        for row in rows:
            automation = Automation.from_row(row)
            if automation.schedule:
                # A missed slot (office was off) runs once and moves to the next future slot.
                upcoming = next_after(automation.schedule, from_iso(automation.next_at or to_utc_iso(now)))
                while upcoming <= now:
                    upcoming = next_after(automation.schedule, upcoming)
                await self.db.execute("UPDATE automations SET next_at = ? WHERE id = ?", (to_utc_iso(upcoming), automation.id))
            else:
                await self.db.execute(
                    "UPDATE automations SET next_at = NULL, status = 'finished', updated_at = ? WHERE id = ?", (utcnow(), automation.id)
                )
            if automation.id in self._running:
                continue  # the previous run is still going: skip this slot
            self.start(automation, "schedule")
            started += 1
        return started

    async def run(self, automation: Automation, trigger: str) -> AutomationRun:
        automation = await self.get(automation.id)  # the caller's copy may be stale (re-saved, disabled)
        if automation.status == AutomationStatus.PENDING_REVIEW:
            raise AutomationError(f"Automation '{automation.name}' has not been approved yet")
        if trigger == "actor" and automation.status != AutomationStatus.ENABLED:
            raise AutomationError(f"Automation '{automation.name}' is {automation.status.value}; only the user can run it now")
        if automation.approved_sha != self.fingerprint(automation.name, automation.steps):
            raise AutomationError(f"'{automation.name}' differs from the approved version; it needs a new review")
        started = time.monotonic()
        cur = await self.db.execute(
            "INSERT INTO automation_runs(automation_id, trigger, status, started_at) VALUES (?, ?, 'running', ?)",
            (automation.id, trigger, utcnow()),
        )
        run_id = int(cur.lastrowid or 0)
        self.bus.publish("automation.run", automation_id=automation.id, run_id=run_id, status="running")
        try:
            code, log_text, error, posted = await self._steps(automation)
        except BaseException:
            await self.db.execute(
                "UPDATE automation_runs SET status = 'failed', error = 'interrupted', finished_at = ? WHERE id = ?", (utcnow(), run_id)
            )
            raise
        duration = int((time.monotonic() - started) * 1000)
        status = "succeeded" if code == 0 else "failed"
        redact = self.office.redact
        await self.db.execute(
            "UPDATE automation_runs SET status = ?, exit_code = ?, output = ?, error = ?, finished_at = ?, duration_ms = ? WHERE id = ?",
            (status, code, redact(log_text[-TAIL:]), redact(error[-TAIL:]), utcnow(), duration, run_id),
        )
        await self.db.execute(
            "UPDATE automations SET failures = CASE WHEN ? THEN 0 ELSE failures + 1 END WHERE id = ?", (code == 0, automation.id)
        )
        current = await self.get(automation.id)
        if current.failures >= MAX_FAILURES and current.status == AutomationStatus.ENABLED and current.schedule:
            # Stop a broken schedule from failing forever; the fix ticket brings it back after review.
            await self.set_enabled(current.id, False, by="office")
        row = await self.db.fetchone("SELECT * FROM automation_runs WHERE id = ?", (run_id,))
        result = AutomationRun.model_validate(row)
        self.bus.publish("automation.run", automation_id=automation.id, run_id=run_id, status=status)
        try:
            await self._report(await self.get(automation.id), result, posted=posted)
        except OfficeError:
            log.exception("could not report automation %s", automation.name)
        return result

    async def _steps(self, automation: Automation) -> tuple[int, str, str, bool]:
        """Run the steps in order. Returns (exit code, log, error, whether a notify step posted something)."""
        office = self.office
        now = local_now()
        context = _Blank(date=f"{now:%d/%m/%Y}", time=f"{now:%H:%M}", weekday=f"{now:%A}", output="", name=automation.name)
        lines: list[str] = []
        posted = False
        owner = await office.org.get_actor(automation.owner_actor_id) if automation.owner_actor_id else None
        conv_id = await self._conversation(automation)
        for index, step in enumerate(automation.steps, 1):
            if step.type == StepType.NOTIFY:
                if owner and conv_id:
                    text = step.text.format_map(context)
                    await office.chat.post_actor(conv_id, owner, text, meta={"automation_id": automation.id}, wake=False)
                    posted = True
                lines.append(f"[{index}] notify")
            elif step.type == StepType.SCRIPT:
                code, out, err = await _execute(self.root / automation.name, step.file, step.timeout_s, automation.name)
                lines.append(f"[{index}] script {step.file} → exit {code}\n{out.strip()[-TAIL:]}")
                if code != 0:
                    return code, "\n".join(lines), err or out, posted
                context["output"] = out.strip()[-2000:]
            elif step.type == StepType.AGENT:
                actor = await office.org.find_actor(step.actor)
                if actor is None:
                    return 2, "\n".join(lines), f"No employee named {step.actor}", posted
                description = step.prompt.format_map(context)
                if context["output"] and "{output}" not in step.prompt:
                    description += f"\n\nResult of the previous step:\n```\n{context['output']}\n```"
                ticket = await office.tickets.create(
                    (step.title.format_map(context) if step.title else f"{automation.name} — {context['date']}")[:200],
                    description=description + f"\n\n(Created by automation `{automation.name}`.)",
                    assignee_actor_id=actor.id,
                    created_by=f"automation:{automation.name}",
                    origin_conversation_id=conv_id,
                    project_id=automation.project_id,
                )
                lines.append(f"[{index}] agent → {ticket.key} for {actor.name}")
        return 0, "\n".join(lines), "", posted

    async def _conversation(self, automation: Automation) -> str | None:
        if automation.conversation_id:
            return automation.conversation_id
        if automation.owner_actor_id:
            return (await self.office.chat.dm(automation.owner_actor_id, automation.project_id)).id
        return None

    async def _report(self, automation: Automation, run: AutomationRun, *, posted: bool) -> None:
        office = self.office
        if not automation.owner_actor_id:
            return
        owner = await office.org.get_actor(automation.owner_actor_id)
        conv_id = await self._conversation(automation)
        if conv_id is None:
            return
        seconds = (run.duration_ms or 0) / 1000
        meta = {"automation_id": automation.id, "automation_run": run.id}
        if run.status == "succeeded":
            if posted or not any(s.type == StepType.SCRIPT for s in automation.steps):
                return  # the flow already said what it had to say (or had nothing to report)
            tail = "\n".join((run.output or "").strip().splitlines()[-15:]) or "(không in gì)"
            body = f"⚙️ Automation **{automation.name}** chạy xong ({seconds:.1f}s, {run.trigger}).\n```\n{tail}\n```"
            await office.chat.post_actor(conv_id, owner, body, meta=meta, wake=False)
            return
        tail = "\n".join((run.error or run.output or "").strip().splitlines()[-15:])
        body = f"❌ Automation **{automation.name}** lỗi (exit {run.exit_code}, {seconds:.1f}s).\n```\n{tail}\n```"
        if automation.failures >= MAX_FAILURES and automation.status == AutomationStatus.DISABLED:
            body += f"\nĐã tạm dừng lịch chạy sau {automation.failures} lần lỗi liên tiếp."
        await office.chat.post_actor(conv_id, owner, body, meta=meta, wake=False)
        await self._fix_ticket(automation, run, owner.id, conv_id)

    async def _fix_ticket(self, automation: Automation, run: AutomationRun, owner_id: str, conv_id: str) -> None:
        """Self-healing: one open fix ticket per automation; later failures add a comment to it."""
        tickets = self.office.tickets
        open_fix = await self.db.fetchone(
            "SELECT id FROM tickets WHERE type = 'automation_fix' AND title LIKE ? AND status NOT IN ('done', 'cancelled')",
            (f"%[{automation.name}]%",),
        )
        detail = f"Run #{run.id} ({run.trigger}) exit {run.exit_code}.\n\nerror (tail):\n```\n{(run.error or '')[-2000:]}\n```"
        if open_fix:
            await tickets.add_comment(open_fix["id"], detail, author_kind="system", author_id=None)
            return
        steps = json.dumps([_compact(s) for s in automation.steps], ensure_ascii=False)
        await tickets.create(
            f"Sửa automation [{automation.name}]",
            description=(
                f"Automation `{automation.name}` failed. Reproduce it, find the cause, fix the script in your working "
                "directory, test it, then save the flow again with automation_save (it goes back to the user for review)."
                f"\n\nSteps: {steps}\n\n{detail}"
            ),
            type="automation_fix",
            priority=Priority.HIGH,
            assignee_actor_id=owner_id,
            created_by="office",
            origin_conversation_id=conv_id,
            status=TicketStatus.TODO,
        )

    async def reap(self) -> int:
        """Runs left 'running' by a stopped office never finished."""
        cur = await self.db.execute(
            "UPDATE automation_runs SET status = 'failed', error = 'interrupted (office stopped)', finished_at = ? "
            "WHERE status = 'running'",
            (utcnow(),),
        )
        return cur.rowcount

    async def shutdown(self) -> None:
        for task in list(self._running.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task


def _compact(step: Step) -> dict[str, Any]:
    return {**step.model_dump(mode="json", exclude_defaults=True), "type": step.type.value}


def schedule_text(schedule: str | None, first_at: str | None = None) -> str:
    if schedule:
        return describe(schedule)
    if first_at:
        return f"một lần lúc {from_iso(first_at).astimezone():%H:%M %d/%m/%Y}"
    return "chạy tay"


def _command(script: Path) -> list[str]:
    head = script.read_text(encoding="utf-8", errors="replace")[:4000]
    if "# /// script" in head and shutil.which("uv"):
        return ["uv", "run", "--quiet", "--script", str(script)]
    if shutil.which("py"):
        return ["py", "-3", str(script)]
    return [sys.executable, str(script)]


def _env(folder: Path, name: str) -> dict[str, str]:
    env = {k: v for k, v in os.environ.items() if k.upper() in SAFE_ENV}
    env.update(
        PYTHONUTF8="1",
        PYTHONIOENCODING="utf-8",
        AO_AUTOMATION=name,
        AO_OUTPUT_DIR=str(folder / "out"),
        GIT_CEILING_DIRECTORIES=str(folder.parent),
    )
    return env


async def _execute(folder: Path, entry: str, timeout_s: int, name: str) -> tuple[int, str, str]:
    script = (folder / entry).resolve()
    out_dir = folder / "out"  # cwd: files written to relative paths (".", "report.csv") land with the outputs
    out_dir.mkdir(parents=True, exist_ok=True)
    try:
        proc = await asyncio.create_subprocess_exec(
            *_command(script),
            cwd=str(out_dir),
            env=_env(folder, name),
            stdin=asyncio.subprocess.DEVNULL,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
    except OSError as exc:
        return 127, "", f"Cannot start the script: {exc}"
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
    except TimeoutError:
        kill_tree(proc.pid)
        with contextlib.suppress(Exception):
            out, err = await asyncio.wait_for(proc.communicate(), 10)
            return 124, out.decode("utf-8", "replace"), err.decode("utf-8", "replace") + f"\nTimed out after {timeout_s}s"
        return 124, "", f"Timed out after {timeout_s}s"
    except asyncio.CancelledError:
        kill_tree(proc.pid)
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace"), err.decode("utf-8", "replace")
