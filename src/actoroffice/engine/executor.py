"""Executes one ticket run: checkout → workspace → turn(s) → disposition → release."""

from __future__ import annotations

import logging
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import Conflict, OfficeError
from actoroffice.domain.run import Run, RunKind, RunStatus, TurnResult, TurnStatus, Usage, WakeRequest, WakeSource
from actoroffice.domain.work import InteractionKind, InteractionStatus, Ticket, TicketStatus
from actoroffice.runtimes.base import TurnSpec
from actoroffice.services.chat import handle
from actoroffice.services.checks import command_list
from actoroffice.services.runs import ticket_session_key
from actoroffice.tools.chat_tools import CHAT_TOOLS
from actoroffice.tools.knowledge_tools import KNOWLEDGE_TOOLS
from actoroffice.tools.office_tools import OFFICE_TOOLS
from actoroffice.tools.registry import BoundTool, ToolCtx
from actoroffice.tools.atlassian_tools import ATLASSIAN_TOOLS
from actoroffice.tools.infra_tools import INFRA_TOOLS
from actoroffice.tools.stage_tools import STAGE_TOOLS
from actoroffice.tools.work_tools import CHAT_WORK_TOOLS, WORK_TOOLS

from . import context, prompt_pack
from .watchdog import Watchdog
from .workspace import ticket_env
from .gate import OfficeGate

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)
S = TicketStatus
MAX_CONTINUATIONS = 3  # automatic "continue" wakes in a row after an actor sets its ticket back to todo


class Executor:
    def __init__(self, office: Office) -> None:
        self.office = office

    async def run_ticket(self, wake: WakeRequest, run_id: str) -> RunStatus:
        office = self.office
        actor = await office.org.get_actor(wake.actor_id)
        position = await office.org.get_position(actor.position_id)
        assert wake.ticket_id is not None
        run = await office.runs.start(Run(id=run_id, actor_id=actor.id, kind=RunKind.TICKET, ticket_id=wake.ticket_id))

        change_wake = any(r.get("change") for r in wake.payload.get("reasons", []))
        try:
            ticket = await office.tickets.checkout(wake.ticket_id, actor.id, run_id, ignore_blockers=change_wake)
        except Conflict as exc:
            await office.runs.event(run_id, "skipped", {"reason": str(exc)})
            await office.runs.finish(run_id, RunStatus.CANCELLED, error=str(exc))
            await office.wakes.finish(run_id)
            return RunStatus.CANCELLED

        status = RunStatus.FAILED
        usage, cost, session_id, error = Usage(), 0.0, None, None
        gate: OfficeGate | None = None
        try:
            project = await office.projects.get(ticket.project_id) if ticket.project_id else None
            root = await office.tickets.root_of(ticket.id)
            ws = await office.workspaces.realize(ticket, project, position, root)
            stage_run = await office.stages.ensure_run(ticket) if ticket.stage else None
            read_dirs = list(ws.read_dirs)
            if stage_run is not None:
                read_dirs += await self._reference_dirs(ticket, project)
            session_key = ticket_session_key(actor.id, ticket.id)
            if stage_run is not None:
                # One session per stage attempt: a new stage (or a retry) starts fresh from the dossier.
                session_key += f":{stage_run.stage}:{stage_run.attempt}"
                await office.db.execute("UPDATE runs SET stage_run_id = ? WHERE id = ?", (stage_run.id, run_id))
            resume = await office.runs.get_session(session_key, str(ws.path))
            changes = await office.tickets.changes_after(ticket.id, 0)
            gate = OfficeGate(
                office,
                actor,
                position,
                run_id,
                ws.path,
                ticket_id=ticket.id,
                ticket_key=ticket.key,
                read_dirs=read_dirs,
                seen_change_id=changes[-1].id if changes else 0,
                seen_steer_id=await office.tickets.last_comment_id(ticket.id, "steer"),
            )
            new_comments = await self._new_comments(actor.id, actor.name, ticket.id, run_id)
            answered = await self._answered(wake)
            office.gates[run_id] = gate
            if stage_run is not None:
                gate.watchdog = Watchdog(office, run_id, office.settings.stall_after_s, office.settings.slow_command_s)
                gate.watchdog.start()
            tools = OFFICE_TOOLS + KNOWLEDGE_TOOLS + WORK_TOOLS
            mcp_servers: dict[str, Any] = {}
            if stage_run is not None:
                compiled = await office.rules.compile(ticket=ticket, project=project, stage=stage_run.stage)
                gate.policies = compiled.policies
                await office.stages.set_rules(stage_run.id, compiled.refs)
                if compiled.refs:
                    await office.runs.event(run_id, "rules", {"refs": compiled.refs, "titles": [r.title for r in compiled.guidelines + compiled.policies]})
                pack = await self._stage_prompt(
                    ticket, stage_run, actor, project, str(ws.path), wake, resume is not None, answered, new_comments, changes,
                    guidelines=compiled.guideline_lines(),
                )
                system, prompt = pack.system, pack.turn
                replaced = {t.name for t in INFRA_TOOLS}
                tools = [t for t in tools if t.name not in replaced] + STAGE_TOOLS + ATLASSIAN_TOOLS + INFRA_TOOLS
                model = office.settings.ai_model or actor.model or position.model or office.settings.default_model
                if ticket.team_id:
                    team = await office.teams.get_team(ticket.team_id)
                    model = team.settings.model or model
                    mcp_servers = dict(team.settings.mcp_servers)
            else:
                prompt = context.ticket_prompt(
                    ticket,
                    wake,
                    new_comments=new_comments,
                    answered=answered,
                    resumed=resume is not None,
                    project=context.project_line(project, str(ws.path)),
                    changes=changes,
                    root=root if root.id != ticket.id else None,
                    blockers=await office.tickets.open_blockers(ticket.id),
                )
                system = context.system_prompt(
                    position,
                    actor,
                    language=office.settings.language,
                    skills=office.skills.for_actor(actor, position),
                    memories=await office.memory.context_for(actor, f"{ticket.title} {ticket.description}"),
                )
                model = actor.model or position.model or office.settings.default_model
            spec = TurnSpec(
                run_id=run_id,
                prompt=prompt,
                cwd=ws.path,
                system_prompt=system,
                model=model,
                tools=position.tools,
                coding_preset=position.prompt_preset == "claude_code",
                resume_session_id=resume,
                internal_tools=[
                    BoundTool(t, ToolCtx(office, actor, position, run_id, ticket_id=ticket.id, workspace=ws.path)) for t in tools
                ],
                add_dirs=read_dirs,
                max_turns=position.policy.max_turns,
                hook_timeout_s=office.settings.approval_wait_s + 60,
                env={
                    **self._env(actor, position),
                    **ticket_env(ticket, project),
                    **(await office.access.env_for(ticket.id) if stage_run is not None else {}),
                },
                mcp_servers=mcp_servers,
            )
            await office.runs.event(
                run_id, "prompt", {"text": prompt, "resume": bool(resume), "cwd": str(ws.path), "stage": ticket.stage}
            )

            result = await self._turn(spec, gate)
            for _ in range(office.settings.corrective_turns):
                if not await self._needs_disposition(result, ticket.id, run_id):
                    break
                reminder = prompt_pack.disposition_reminder if stage_run is not None else context.disposition_reminder
                spec.prompt = reminder(ticket.key)
                spec.resume_session_id = result.session_id
                usage, cost = _add(usage, cost, result)
                result = await self._turn(spec, gate)
            usage, cost = _add(usage, cost, result)
            session_id = result.session_id
            if session_id and run_id not in office.stages.finished_runs:
                await office.runs.save_session(session_key, actor.id, session_id, str(ws.path))
            if stage_run is not None:
                await office.stages.add_usage(stage_run.id, usage, cost, session_id)
            status, error = await self._settle(result, ticket, run_id, actor.id, gate)
        except OfficeError as exc:
            error = str(exc)
            await self._fallback(ticket, run_id, f"Lượt chạy lỗi: {exc}")
        except Exception as exc:
            log.exception("run %s crashed", run_id)
            error = f"{type(exc).__name__}: {exc}"
            await self._fallback(ticket, run_id, "Lượt chạy bị dừng đột ngột; xem nhật ký ở mục Hoạt động.")
        finally:
            office.gates.pop(run_id, None)
            if gate is not None and gate.watchdog is not None:
                await gate.watchdog.stop()
            await office.tickets.release(ticket.id, run_id)
            await office.runs.finish(
                run_id,
                status,
                session_after=session_id,
                usage=usage,
                cost_usd=cost,
                tainted=bool(gate and gate.tainted),
                error=error,
                stats=gate.stats.summary() if gate else None,
            )
            await office.wakes.finish(run_id)
            if status != RunStatus.PARKED:
                await office.interactions.cancel_for_run(run_id)
            if status != RunStatus.CANCELLED:
                # Answers that arrived while this run was still going have not been seen yet.
                await self._rewake_if_answered(run_id, actor.id, ticket.id)
            if gate is not None and status != RunStatus.PARKED:
                await self._drop_seen_changes(actor.id, ticket.id, gate.seen_change_id)
        return status

    async def _ran_before(self, stage_run_id: int, wake: WakeRequest) -> bool:
        row = await self.office.db.fetchone("SELECT COUNT(*) AS n FROM runs WHERE stage_run_id = ?", (stage_run_id,))
        return bool(row and int(row["n"]) > 1)  # this run is already counted

    async def _reference_dirs(self, ticket: Ticket, project: Any) -> list[Path]:
        """Reference repositories of the ticket's team are readable in every run (callers, contracts, events)."""
        dirs = []
        for ref in await self.office.projects.list():
            if ref.kind == "reference" and ref.path and (ref.team_id in (None, ticket.team_id)) and (project is None or ref.id != project.id):
                path = Path(ref.path)
                if path.is_dir():
                    dirs.append(path.resolve())
        return dirs

    async def _stage_prompt(
        self,
        ticket: Ticket,
        stage_run: Any,
        actor: Any,
        project: Any,
        cwd: str,
        wake: WakeRequest,
        resumed: bool,
        answered: list,
        new_comments: list,
        changes: list,
        *,
        guidelines: list[str],
    ) -> prompt_pack.StagePrompt:
        office = self.office
        template = await office.stages.template_of(ticket)
        stage = await office.stages.stage_def(ticket)
        team = await office.teams.get_team(ticket.team_id) if ticket.team_id else None
        people = await office.teams.participants(ticket.id)
        if not people and team is not None:
            people = await office.teams.members(team.id)
        dossier = await office.tickets.get_document(ticket.id, "dossier")
        position = await office.org.get_position(actor.position_id)
        known = await office.stages.known(ticket, actor)
        if stage.id == "understand" and stage_run.attempt == 1 and not resumed and not await self._ran_before(stage_run.id, wake):
            await office.stages.post_known(ticket, known)
        return prompt_pack.build(
            actor=actor,
            ticket=ticket,
            template=template,
            stage=stage,
            stage_run=stage_run,
            language=office.settings.language,
            team_name=team.name if team else None,
            people=people,
            project_line=context.project_line(project, cwd),
            dossier=dossier.body if dossier else "",
            guidelines=guidelines,
            memories=known["memories"],
            similar=known["similar"],
            kb=known["kb"],
            approved=[
                f"{d.key} rev {d.approved_revision}" for d in await office.tickets.list_documents(ticket.id) if d.approved_revision
            ],
            skills=office.skills.for_actor(actor, position),
            wake=wake,
            resumed=resumed,
            answered=answered,
            new_comments=new_comments,
            changes=changes,
            office_runs_tests=bool(command_list((project.profile if project else None) or {}, "test")),
        )

    async def _drop_seen_changes(self, actor_id: str, ticket_id: int, seen: int) -> None:
        """A change delivered mid-run (gate interrupt) needs no second wake."""
        wake = await self.office.wakes.pending_for(actor_id, ticket_id)
        reasons = wake.payload.get("reasons", []) if wake else []
        if wake and reasons and all(r.get("change") for r in reasons):
            latest = await self.office.tickets.changes_after(ticket_id, seen)
            if not latest:
                await self.office.wakes.drop(wake.id, "change already seen by the previous run")

    async def run_chat(self, wake: WakeRequest, run_id: str) -> RunStatus:
        """One chat turn: read the new messages, reply once. Work goes through proposed tickets."""
        office = self.office
        assert wake.conversation_id is not None
        actor = await office.org.get_actor(wake.actor_id)
        position = await office.org.get_position(actor.position_id)
        conv = await office.chat.get(wake.conversation_id)
        await office.runs.start(Run(id=run_id, actor_id=actor.id, kind=RunKind.CHAT, conversation_id=conv.id))
        status, error, usage, cost, session_id, tainted = RunStatus.FAILED, None, Usage(), 0.0, None, False
        chat_gate: OfficeGate | None = None
        try:
            project = await office.projects.get(conv.project_id) if conv.project_id else None
            # Chat turns only have read tools, so they may look around the project folder itself.
            if project and project.path:
                cwd = Path(project.path)
            else:
                cwd = office.settings.workspaces_dir / f"chat-{conv.id}"
                cwd.mkdir(parents=True, exist_ok=True)
            session_key = f"{actor.id}:c{conv.id}"
            resume = await office.runs.get_session(session_key, str(cwd))
            since = await office.chat.last_actor_message_id(conv.id, actor.id)
            history = await office.chat.messages(conv.id, after_id=since) if since else await office.chat.messages(conv.id, limit=30)
            names = {a.id: a for a in await office.org.list_actors()}
            positions = {p.id: p.title for p in await office.org.list_positions()}
            lines = [_chat_line(m, names, positions) for m in history if not (m.author_kind == "actor" and m.author_id == actor.id)]
            members = ["You (the user)"] + [f"{names[a].name} ({positions[names[a].position_id]})" for a in conv.actor_ids if a in names]
            ticket_key = (await office.tickets.get(conv.ticket_id)).key if conv.ticket_id else None
            recent = [
                f"- {t.key} [{t.status.value}] {t.title} → {names[t.assignee_actor_id].name if t.assignee_actor_id in names else '-'}"
                for t in await office.tickets.list(limit=8, project_id=conv.project_id)
            ]
            spec = TurnSpec(
                run_id=run_id,
                prompt=context.chat_prompt(
                    conv.title,
                    conv.kind.value,
                    members,
                    lines[-40:] or ["(no new messages)"],
                    ticket_key,
                    recent,
                    project=context.project_line(project, str(cwd), chat=True),
                ),
                cwd=cwd,
                system_prompt=context.chat_system_prompt(
                    position,
                    actor,
                    language=office.settings.language,
                    skills=office.skills.for_actor(actor, position),
                    memories=await office.memory.context_for(actor, " ".join(m.body for m in history[-5:])),
                ),
                model=actor.model or position.model or office.settings.default_model,
                tools=["Read", "Glob", "Grep"],
                coding_preset=False,  # chat never needs the coding preset: about half the base tokens
                resume_session_id=resume,
                internal_tools=[
                    BoundTool(t, ToolCtx(office, actor, position, run_id, conversation_id=conv.id, workspace=cwd))
                    for t in CHAT_TOOLS + KNOWLEDGE_TOOLS + CHAT_WORK_TOOLS
                ],
                max_turns=12,
                hook_timeout_s=office.settings.approval_wait_s + 60,
                env=self._env(),
            )
            gate = chat_gate = OfficeGate(office, actor, position, run_id, cwd)
            replies: list[str] = []

            async def collect(kind: str, payload: dict[str, Any]) -> None:
                if kind == "tool_use":
                    replies.clear()  # only text after the last tool call is the reply
                elif kind == "text" and not payload.get("subagent"):
                    replies.append(payload["text"])

            result = await self._turn(spec, gate, extra_sink=collect)
            usage, cost, session_id, tainted = result.usage, result.cost_usd, result.session_id, gate.tainted
            if session_id:
                await office.runs.save_session(session_key, actor.id, session_id, str(cwd))
            reply = "\n\n".join(r.strip() for r in replies if r.strip()) or result.text.strip()
            if result.status == TurnStatus.RATE_LIMITED:
                await office.chat.post_system(conv.id, f"{actor.name} tạm dừng: đã chạm giới hạn sử dụng Claude.")
                error = "rate limited"
            elif result.status in (TurnStatus.ERROR, TurnStatus.MAX_TURNS):
                await office.chat.post_system(conv.id, f"{actor.name} gặp lỗi khi trả lời.")
                error = result.error or result.status.value
            else:
                if reply and reply != context.NO_REPLY:
                    await office.chat.post_actor(conv.id, actor, reply, meta={"run_id": run_id})
                status = RunStatus.SUCCEEDED
        except Exception as exc:
            log.exception("chat run %s crashed", run_id)
            error = f"{type(exc).__name__}: {exc}"
        finally:
            await office.runs.finish(
                run_id,
                status,
                session_after=session_id,
                usage=usage,
                cost_usd=cost,
                tainted=tainted,
                error=error,
                stats=chat_gate.stats.summary() if chat_gate else None,
            )
            await office.wakes.finish(run_id)
            await office.interactions.cancel_for_run(run_id)
        return status

    async def _rewake_if_answered(self, run_id: str, actor_id: str, ticket_id: int) -> None:
        """The user may answer while the run is still finishing; that answer must still wake the actor."""
        for item in await self.office.interactions.for_run(run_id):
            parked_or_question = item.kind in (
                InteractionKind.QUESTION,
                InteractionKind.ASK_COLLEAGUE,
                InteractionKind.APPROVE_AUTOMATION,
                InteractionKind.APPROVE_ACCESS,
                InteractionKind.ENV_ACCESS,
            ) or item.payload.get("parked")
            if item.status == InteractionStatus.ANSWERED and parked_or_question:
                await self.office.wakes.enqueue(
                    actor_id, WakeSource.INTERACTION, ticket_id=ticket_id, payload={"interaction_id": item.id}
                )

    def _env(self, actor: Any = None, position: Any = None) -> dict[str, str]:
        # Ticket folders live under the data dir, which may itself sit inside a git repo (e.g. a home folder
        # under version control). Stop git from walking up past the workspaces, so a `git commit` in a
        # scratch folder can never land in some enclosing repository.
        env = {"GIT_CEILING_DIRECTORIES": str(self.office.settings.workspaces_dir.resolve())}
        if actor is not None:
            # Commits are attributed to the employee who made them, not to the machine's git identity.
            name = f"{actor.name} ({position.title})" if position is not None else actor.name
            email = f"{handle(actor.name) or 'actor'}@actor-office.local"
            env.update(GIT_AUTHOR_NAME=name, GIT_AUTHOR_EMAIL=email, GIT_COMMITTER_NAME=name, GIT_COMMITTER_EMAIL=email)
        return env

    async def _turn(self, spec: TurnSpec, gate: OfficeGate, *, extra_sink: Any = None) -> TurnResult:
        async def sink(kind: str, payload: dict[str, Any]) -> None:
            activity = await self.office.runs.event(spec.run_id, kind, payload)
            if gate.watchdog is not None:
                await gate.watchdog.observe(activity)
            if kind == "rate_limit":
                self.office.scheduler.observe_rate_limit(payload)
            if extra_sink is not None:
                await extra_sink(kind, payload)

        result = await self.office.runtime.run_turn(spec, gate, sink)
        await self.office.runs.event(
            spec.run_id,
            "turn",
            {"status": result.status.value, "usage": result.usage.model_dump(), "cost_usd": result.cost_usd, "error": result.error},
        )
        return result

    async def _needs_disposition(self, result: TurnResult, ticket_id: int, run_id: str) -> bool:
        if result.status != TurnStatus.COMPLETED or run_id in self.office.stages.finished_runs:
            return False
        ticket = await self.office.tickets.get(ticket_id)
        waiting = (InteractionKind.QUESTION, InteractionKind.ENV_ACCESS)
        if ticket.stage and any(i.kind in waiting for i in await self.office.interactions.pending(run_id=run_id)):
            return False  # waiting for an answer: the office parks the ticket
        return ticket.status == S.IN_PROGRESS and ticket.checkout_run_id == run_id

    async def _settle(
        self, result: TurnResult, ticket: Ticket, run_id: str, actor_id: str, gate: OfficeGate
    ) -> tuple[RunStatus, str | None]:
        office = self.office
        current = await office.tickets.get(ticket.id)
        still_mine = current.status == S.IN_PROGRESS and current.checkout_run_id == run_id
        waiting = (InteractionKind.QUESTION, InteractionKind.ASK_COLLEAGUE, InteractionKind.APPROVE_AUTOMATION, InteractionKind.ENV_ACCESS)
        questions = [i for i in await office.interactions.pending(run_id=run_id) if i.kind in waiting]

        if result.status == TurnStatus.DEFERRED or (questions and still_mine):
            if still_mine:
                waiting = "approval of a tool call" if result.status == TurnStatus.DEFERRED else "an answer"
                await office.tickets.transition(
                    ticket.id, S.IN_REVIEW, by="office", run_id=run_id, comment=f"Đang chờ bạn: {waiting}."
                )
            return RunStatus.PARKED, None

        if result.status == TurnStatus.RATE_LIMITED:
            resets = result.rate_limit.resets_at if result.rate_limit else None
            not_before = datetime.fromtimestamp(resets, UTC).isoformat(timespec="seconds") if resets else None
            if still_mine:
                await office.tickets.transition(
                    ticket.id, S.TODO, by="office", run_id=run_id, comment="Tạm dừng: hết hạn mức Claude. Sẽ tự làm tiếp khi hạn mức được làm mới."
                )
            await office.wakes.enqueue(actor_id, WakeSource.MANUAL, ticket_id=ticket.id, payload={"retry": "rate_limit"}, not_before=not_before)
            return RunStatus.FAILED, "rate limited"

        if result.status == TurnStatus.MAX_TURNS and await self._can_continue(run_id):
            # Out of steps for one run, not stuck: the session keeps the progress, so carry on in a fresh run.
            await self._fallback(current, run_id, "Hết số bước của một lượt; tự làm tiếp từ chỗ đang dở.", to=S.TODO)
            await self._continue_later(actor_id, ticket.id, run_id)
            return RunStatus.SUCCEEDED, "hết số bước của một lượt, đang làm tiếp"

        if result.status in (TurnStatus.ERROR, TurnStatus.CANCELLED, TurnStatus.MAX_TURNS):
            note = {
                TurnStatus.ERROR: f"Lượt chạy lỗi: {result.error}",
                TurnStatus.CANCELLED: "Lượt chạy đã bị dừng.",
                TurnStatus.MAX_TURNS: "Đã hết số bước nhiều lượt liền mà chưa xong; bạn xem tiến độ rồi nhắn hướng đi tiếp.",
            }[result.status]
            await self._fallback(current, run_id, note, to=S.IN_REVIEW if result.status == TurnStatus.MAX_TURNS else S.TODO)
            status = RunStatus.CANCELLED if result.status == TurnStatus.CANCELLED else RunStatus.FAILED
            if result.status == TurnStatus.MAX_TURNS:
                return status, "hết số bước nhiều lượt liền; chờ bạn xem"
            return status, result.error

        if still_mine:
            # The actor never chose a disposition even after the reminder: surface it to the human.
            await office.tickets.transition(
                ticket.id, S.IN_REVIEW, by="office", run_id=run_id, comment="Lượt chạy kết thúc mà chưa chốt trạng thái; bạn xem giúp."
            )
        elif current.status == S.TODO and run_id not in office.stages.finished_runs:
            await self._continue_later(actor_id, ticket.id, run_id)
        office.stages.finished_runs.discard(run_id)
        return RunStatus.SUCCEEDED, None

    async def _streak(self, run_id: str) -> int:
        wake = await self.office.db.fetchone("SELECT payload_json FROM wake_requests WHERE run_id = ?", (run_id,))
        reasons = json.loads(wake["payload_json"]).get("reasons", []) if wake else []
        return max((int(r.get("continuation", 0)) for r in reasons), default=0)

    async def _can_continue(self, run_id: str) -> bool:
        return await self._streak(run_id) < MAX_CONTINUATIONS

    async def _continue_later(self, actor_id: str, ticket_id: int, run_id: str) -> None:
        """`todo` means "made progress, will continue": schedule the continuation (a few times in a row at most,
        then it waits for a human or another event)."""
        run = await self.office.runs.get(run_id)
        streak = await self._streak(run_id)
        if streak >= MAX_CONTINUATIONS or run is None:
            return
        not_before = (datetime.now(UTC) + timedelta(seconds=20)).isoformat(timespec="seconds")
        await self.office.wakes.enqueue(
            actor_id, WakeSource.MANUAL, ticket_id=ticket_id, payload={"continuation": streak + 1}, not_before=not_before
        )

    async def _fallback(self, ticket: Ticket, run_id: str, note: str, to: TicketStatus = S.TODO) -> None:
        try:
            current = await self.office.tickets.get(ticket.id)
            if current.status == S.IN_PROGRESS and current.checkout_run_id == run_id:
                await self.office.tickets.transition(ticket.id, to, by="office", run_id=run_id, comment=note)
            else:
                await self.office.tickets.add_comment(ticket.id, note, author_kind="system", author_id=None)
        except OfficeError:
            log.exception("could not record fallback for %s", ticket.key)

    async def _new_comments(self, actor_id: str, actor_name: str, ticket_id: int, run_id: str) -> list:
        since = await self.office.runs.previous_start(actor_id, ticket_id, run_id)
        if since is None:
            return []
        comments = await self.office.tickets.comments(ticket_id, limit=200)
        return [c for c in comments if c.created_at >= since and c.author_id != actor_name and c.author_kind != "system"]

    async def _answered(self, wake: WakeRequest) -> list:
        ids = [r["interaction_id"] for r in wake.payload.get("reasons", []) if r.get("interaction_id")]
        items = [await self.office.interactions.get(i) for i in ids]
        return [i for i in items if i.status == InteractionStatus.ANSWERED]


def _chat_line(m: Any, actors: dict[str, Any], positions: dict[str, str]) -> str:
    if m.author_kind == "user":
        who = "User"
    elif m.author_kind == "actor" and m.author_id in actors:
        a = actors[m.author_id]
        who = f"{a.name} ({positions.get(a.position_id, '')})"
    else:
        who = "System"
    return f"[{who}] {m.body}"


def _add(usage: Usage, cost: float, result: TurnResult) -> tuple[Usage, float]:
    u = result.usage
    return (
        Usage(
            input_tokens=usage.input_tokens + u.input_tokens,
            output_tokens=usage.output_tokens + u.output_tokens,
            cache_read_tokens=usage.cache_read_tokens + u.cache_read_tokens,
            cache_write_tokens=usage.cache_write_tokens + u.cache_write_tokens,
        ),
        cost + result.cost_usd,
    )

