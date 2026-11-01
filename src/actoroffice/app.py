"""Composition root: builds every service once and wires them together."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from importlib import resources
from typing import Any

import yaml

from actoroffice.config import Settings
from actoroffice.core.events import EventBus
from actoroffice.domain.chat import MessageKind
from actoroffice.domain.errors import NotFound, PolicyDenied
from actoroffice.domain.team import LOCAL_OWNER_ID
from actoroffice.domain.ids import new_id
from actoroffice.domain.org import Actor, Position
from actoroffice.domain.work import Interaction, InteractionKind, Priority, ProjectAccess
from actoroffice.engine.executor import Executor
from actoroffice.engine.reaper import reap_orphans
from actoroffice.engine.scheduler import Scheduler
from actoroffice.engine.timers import run_timers
from actoroffice.engine.workspace import WorkspaceManager
from actoroffice.runtimes.base import AgentRuntime
from actoroffice.runtimes.claude_sdk import ClaudeSdkRuntime
from actoroffice.runtimes.kiro_acp import KiroAcpRuntime
from actoroffice.runtimes.router import RuntimeRouter
from actoroffice.security.audit import AuditLog
from actoroffice.security.policy import PolicyEngine
from actoroffice.security.redact import Redactor
from actoroffice.services.automations import AutomationService
from actoroffice.services.chat import ChatService
from actoroffice.services.interactions import InteractionService
from actoroffice.services.access import AccessService
from actoroffice.services.atlassian import Atlassian
from actoroffice.services.jira import JiraMirror
from actoroffice.services.infra import InfraService
from actoroffice.services.knowledge import KnowledgeService
from actoroffice.services.logs import LogService
from actoroffice.services.prwatch import PullRequestWatcher
from actoroffice.services.review import ReviewService
from actoroffice.services.memory import MemoryService
from actoroffice.services.merges import MergeService
from actoroffice.services.notices import ChatNotifier
from actoroffice.services.projects import ProjectService
from actoroffice.services.rules import RuleService
from actoroffice.domain.knowledge import SkillRef, SkillScope
from actoroffice.services.skills import SkillService, split_front_matter
from actoroffice.services.stages import StageService
from actoroffice.services.teams import TeamService
from actoroffice.services.org import OrgService
from actoroffice.services.runs import RunService
from actoroffice.services.tickets import TicketService
from actoroffice.services.wakes import WakeService
from actoroffice.store import Database

log = logging.getLogger(__name__)


class Office:
    def __init__(self, settings: Settings, *, runtime: AgentRuntime | None = None) -> None:
        self.settings = settings
        self.db = Database(settings.db_path)
        self.bus = EventBus()
        self.redact = Redactor()
        self.audit = AuditLog(self.db, self.redact)
        self.policy = PolicyEngine(
            protected_dirs=[
                settings.data_dir / "office.db",
                settings.config_path,
                settings.data_dir / "secrets",
                settings.data_dir / "skills",  # edited only through skill tools (versioned), never directly
                settings.data_dir / "automations",  # changed only through automation_save (reviewed by the user)
                settings.data_dir / "rules",  # team rules: changed only through the rules service (owners approve)
            ]
        )
        self.org = OrgService(self.db)
        self.wakes = WakeService(self.db)
        self.tickets = TicketService(self.db, self.wakes, self.bus, key_prefix=settings.key_prefix)
        self.interactions = InteractionService(self.db, self.wakes, self.bus)
        self.runs = RunService(self.db, self.bus, self.redact)
        self.chat = ChatService(self.db, self.bus, self.org, self.wakes, max_unattended=settings.max_unattended_agent_messages)
        self.projects = ProjectService(self.db, self.bus, forbidden=[settings.data_dir, *self.policy.protected])
        self.memory = MemoryService(self.db, self.bus, self.redact)
        self.skills = SkillService(settings.data_dir / "skills", self.bus)
        self.automations = AutomationService(self, settings.data_dir / "automations")
        self.merges = MergeService(self)
        self.workspaces = WorkspaceManager(settings.workspaces_dir)
        self.teams = TeamService(self.db, self.bus)
        self.stages = StageService(self)
        self.jira = JiraMirror(self)
        self.rules = RuleService(self, settings.data_dir / "rules")
        self.atlassian = Atlassian(settings.data_dir)
        self.prs = PullRequestWatcher(self)
        self.review = ReviewService(self)
        self.access = AccessService(self)
        self.knowledge = KnowledgeService(self)
        self.infra = InfraService(self)
        self.logs = LogService(self)
        self.gates: dict[str, Any] = {}  # run id → OfficeGate of a running ticket run (tools adjust it live)
        self.runtime: AgentRuntime = runtime or RuntimeRouter(
            ClaudeSdkRuntime(cli_path=settings.claude_cli_path),
            KiroAcpRuntime(cli_path=settings.kiro_cli_path, engine=settings.kiro_engine),
        )
        self.executor = Executor(self)
        self.scheduler = Scheduler(self, settings.max_concurrent_runs)
        self.notifier = ChatNotifier(self)
        self.telegram: Any = None  # channels.telegram.TelegramBridge once a bot token is configured
        self._tasks: list[asyncio.Task[None]] = []

    async def open(self) -> None:
        await self.db.open()
        default = await self.projects.ensure_default()
        self.chat.default_project_id = default.id
        await self.teams.ensure_local_owner()

    async def start(self, *, schedule: bool = True) -> None:
        """Open the store, recover orphaned runs, mirror events into chat and start scheduling."""
        await self.open()
        recovered = await reap_orphans(self)
        await self.automations.reap()
        if recovered:
            log.warning("recovered %d orphaned run(s)", recovered)
        await self.start_notifier()
        if schedule:
            self._tasks.append(asyncio.create_task(self.scheduler.run_forever(), name="scheduler"))
            self._tasks.append(asyncio.create_task(run_timers(self), name="timers"))
            if self.jira.ready():
                self._tasks.append(asyncio.create_task(self.jira.run_forever(), name="jira"))
            self._tasks.append(asyncio.create_task(self.prs.run_forever(), name="pull-requests"))
            self._tasks.append(asyncio.create_task(self.access.run_forever(), name="access-expiry"))

    async def start_notifier(self) -> None:
        ready = asyncio.Event()
        self._tasks.append(asyncio.create_task(self.notifier.run(ready), name="notifier"))
        await ready.wait()

    async def _answer_access(self, item: Interaction, response: dict[str, Any], *, by: str) -> Interaction:
        """A read-only project becomes writable (git worktree or direct edits) when the user approves. The project
        changes before the card resolves, so the actor woken by the answer already works with the new access."""
        access = str(response.get("access") or "")
        if response.get("approved") and item.status.value == "pending":
            if access not in item.payload.get("options", []):
                raise PolicyDenied("Chọn một trong các cách được đề xuất trên thẻ")
            project = await self.projects.get(item.payload["project_id"])
            await self.projects.update(
                project.id, name=project.name, path=project.path, access=ProjectAccess(access), description=project.description
            )
            await self.audit.record("project:access", "granted", actor_id=item.actor_id, target=project.name, detail={"access": access, "by": by})
        return await self.interactions.resolve(item.id, {"approved": bool(response.get("approved")), "access": access or None}, by=by)

    async def rename_actor(self, actor_id: str, name: str, *, by: str = "user") -> Actor:
        """Rename an actor everywhere the name works as a reference: @mentions, its own skills folder, DM titles,
        ticket comment authors and automation steps. Chat history keeps the old name (it is history)."""
        before = await self.org.get_actor(actor_id)
        actor = await self.org.rename_actor(actor_id, name)
        if actor.name == before.name:
            return actor
        await self.skills.rename_owner(SkillScope.ACTOR, before.name, actor.name, author=by)
        await self.db.execute(
            "UPDATE comments SET author_id = ? WHERE author_kind = 'actor' AND author_id = ?", (actor.name, before.name)
        )
        rows = await self.db.fetchall(
            "SELECT c.id FROM conversations c JOIN participants p ON p.conversation_id = c.id "
            "WHERE c.kind = 'dm' AND p.member_kind = 'actor' AND p.member_id = ?",
            (actor_id,),
        )
        for row in rows:
            await self.db.execute("UPDATE conversations SET title = ? WHERE id = ?", (actor.name, row["id"]))
            self.bus.publish("chat.conversation", conversation=(await self.chat.get(row["id"])).model_dump())
        await self.automations.rename_actor(before.name, actor.name)
        await self.audit.record("actor:rename", "renamed", actor_id=actor_id, target=actor.name, detail={"from": before.name, "by": by})
        self.bus.publish("team.changed")
        return actor

    async def start_telegram(self, token: str) -> None:
        """Mirror the office into a Telegram forum group (see channels/telegram.py)."""
        from actoroffice.channels.telegram import BotApi, TelegramBridge

        self.redact.register(token)
        self.telegram = TelegramBridge(self, BotApi(token))
        ready = asyncio.Event()
        self._tasks.append(asyncio.create_task(self.telegram.run(ready), name="telegram"))
        await ready.wait()

    async def stop_telegram(self) -> None:
        """Stop the Telegram bridge (to switch to another bot or turn Telegram off) without stopping the office."""
        for task in [t for t in self._tasks if t.get_name() == "telegram"]:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            self._tasks.remove(task)
        if self.telegram is not None:
            await self.telegram.api.close()
            self.telegram = None

    async def close(self) -> None:
        await self.scheduler.shutdown()
        await self.automations.shutdown()
        for task in self._tasks:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._tasks.clear()
        if self.telegram is not None:
            await self.telegram.api.close()
        await self.jira.close()
        await self.atlassian.close()
        await self.access.close()
        await self.db.close()

    async def answer(self, interaction_id: str, response: dict[str, Any], *, by: str = "user") -> Interaction:
        """Resolve anything waiting for the user. Ticket proposals become real tickets here, and an approval
        can be remembered as an "always allow" rule for that actor (like Claude Code's permission prompt).

        The interaction is always resolved FIRST: resolving is atomic, so a double click (or two clients)
        gets a 409 instead of creating a second ticket, rule or automation run."""
        item = await self.interactions.get(interaction_id)
        if item.kind == InteractionKind.APPROVE_TOOL and response.get("approved") and response.get("remember"):
            rule = str(response["remember"]).strip()
            offered = {s["rule"] for s in item.payload.get("suggestions", [])}
            if rule not in offered:
                raise PolicyDenied("Chỉ được ghi nhớ một trong các luật được đề xuất")
            resolved = await self.interactions.resolve(interaction_id, response, by=by)
            if item.actor_id:
                await self.org.add_allow_rule(item.actor_id, rule)
                await self.audit.record("permission:allow_rule", "added", actor_id=item.actor_id, target=rule, detail={"by": by})
                self.bus.publish("team.changed")
            return resolved
        if item.kind == InteractionKind.APPROVE_AUTOMATION:
            return await self._answer_automation(item, response, by=by)
        if item.kind == InteractionKind.APPROVE_ACCESS:
            return await self._answer_access(item, response, by=by)
        if item.kind == InteractionKind.APPROVE_STAGE:
            return await self.stages.answer_gate(item, response, by=by)
        if item.kind == InteractionKind.ENV_ACCESS:
            return await self.access.answer(item, response, by=by)
        if item.kind == InteractionKind.QUESTION and item.payload.get("roles") and by != LOCAL_OWNER_ID:
            ticket = await self.tickets.get(item.ticket_id) if item.ticket_id else None
            if not await self.teams.can_act(by, ticket, list(item.payload["roles"])):
                raise PolicyDenied(f"Câu hỏi này dành cho vai {item.payload.get('roles_label') or ', '.join(item.payload['roles'])}")
            name = (await self.teams.get_user(by)).name if by != LOCAL_OWNER_ID else "Owner"
            response = {**response, "answered_by": name}
        if item.kind != InteractionKind.CONFIRM_TICKET:
            return await self.interactions.resolve(interaction_id, response, by=by)
        p = item.payload
        if not response.get("confirmed"):
            resolved = await self.interactions.resolve(interaction_id, {"confirmed": False}, by=by)
            if p.get("conversation_id"):
                await self.chat.post_system(p["conversation_id"], f"Đã bỏ đề xuất ticket: {p['title']}")
            return resolved
        await self.interactions.resolve(interaction_id, {"confirmed": True}, by=by)
        parent = await self.tickets.get(p["parent"]) if p.get("parent") else None
        ticket = await self.tickets.create(
            str(response.get("title") or p["title"]),
            description=str(response.get("description") or p.get("description", "")),
            type=str(p.get("type") or "task"),
            priority=Priority(p.get("priority", "medium")),
            assignee_actor_id=p["assignee_id"],
            parent_id=parent.id if parent else None,
            created_by=p.get("proposed_by", "user"),
            origin_conversation_id=p.get("conversation_id"),
        )
        resolved = await self.interactions.set_response(interaction_id, {"confirmed": True, "ticket_key": ticket.key})
        if p.get("conversation_id"):
            await self.chat.post_system(
                p["conversation_id"],
                f"{ticket.key} · {ticket.title} — đã tạo, giao cho {p['assignee']}",
                kind=MessageKind.TICKET,
                meta={"ticket_key": ticket.key, "status": ticket.status.value, "event": "created"},
            )
        return resolved

    async def guidelines_for(self, ticket: Any, project: Any, stage: str) -> list[str]:
        """Team guidelines in effect for a stage (rules service, P2). Empty until rules exist."""
        compiled = await self.rules.compile(ticket=ticket, project=project, stage=stage)
        return compiled.guideline_lines()

    async def ensure_ai(self) -> Actor:
        """The single AI engineer every team works with (created on first need from the engineer template)."""
        actor = await self.org.find_actor(self.settings.ai_name)
        if actor is not None:
            return actor
        position = await self.org.find_position("engineer")
        if position is None:
            folder = resources.files("actoroffice.templates.positions")
            data = yaml.safe_load((folder / "engineer.yaml").read_text(encoding="utf-8"))
            position = await self.org.create_position(Position(id=new_id("pos"), **data))
        actor = await self.org.hire(position.id, self.settings.ai_name, emoji="🤖", model=self.settings.ai_model)
        self.bus.publish("team.changed")
        return actor

    async def post_user_message(self, conversation_id: str, body: str, *, user_id: str = LOCAL_OWNER_ID) -> Any:
        """A person writes in a chat. In a ticket's own room (a ticket running through stages), the message is also
        a comment on the ticket: the AI hears it in its stage session (running: at its next wake)."""
        conv = await self.chat.get(conversation_id)
        ticket = None
        if conv.ticket_id:
            try:
                ticket = await self.tickets.get(conv.ticket_id)
            except NotFound:
                ticket = None
        if ticket is None or not ticket.stage:
            return await self.chat.post_user(conversation_id, body, user_id=user_id)
        message = await self.chat.post_user(conversation_id, body, user_id=user_id, wake=False)
        name = "Owner" if user_id == LOCAL_OWNER_ID else (await self.teams.get_user(user_id)).name
        await self.tickets.add_comment(ticket.id, body, author_kind="user", author_id=name)
        self.scheduler.kick()
        return message

    async def _answer_automation(self, item: Interaction, response: dict[str, Any], *, by: str) -> Interaction:
        approved = bool(response.get("approved"))
        if approved:
            self.automations.check_review(item.payload)
        resolved = await self.interactions.resolve(item.id, {"approved": approved}, by=by)
        if approved:
            automation = await self.automations.approve(
                item.payload["automation_id"], item.payload["fingerprint"], first_at=item.payload.get("first_at"), by=by
            )
        else:
            automation = await self.automations.set_enabled(item.payload["automation_id"], False, by=by)
        conv = item.payload.get("conversation_id")
        if conv:
            text = f"Đã duyệt automation {automation.name}" if approved else f"Đã từ chối automation {automation.name}"
            await self.chat.post_system(conv, text)
        if approved and response.get("run_now", True) and automation.id not in self.automations.running_ids:
            self.automations.start(automation, "manual")
        if approved and automation.ticket_id:
            await self.stages.external_passed(automation.ticket_id, "script", by=by, note=f"automation {automation.name} đã được duyệt")
        return resolved

    async def install_default_positions(self, *, update: bool = False, skills: bool = True) -> list[Position]:
        """Create the template positions that are missing (update=True also refreshes existing ones from the
        templates), and the shared standard skills."""
        created = []
        folder = resources.files("actoroffice.templates.positions")
        for item in sorted(folder.iterdir(), key=lambda f: f.name):
            if not item.name.endswith(".yaml"):
                continue
            data = yaml.safe_load(item.read_text(encoding="utf-8"))
            existing = await self.org.find_position(data["name"])
            if existing is None:
                created.append(await self.org.create_position(Position(id=new_id("pos"), **data)))
            elif update:
                await self.org.update_position(Position(id=existing.id, **data))
        if skills:
            await self.install_default_skills(update=update)
        return created

    async def install_default_skills(self, *, update: bool = False) -> list[str]:
        installed = []
        root = resources.files("actoroffice.templates") / "skills"
        for folder in sorted(root.iterdir(), key=lambda f: f.name):
            if not folder.is_dir() or folder.name.startswith(("_", ".")):
                continue
            ref = SkillRef(scope=SkillScope.SHARED, name=folder.name)
            if not update and self.skills.exists(ref):
                continue
            meta, body = split_front_matter((folder / "SKILL.md").read_text(encoding="utf-8"))
            await self.skills.save(ref, str(meta.get("description", folder.name)), body, author="office", commit=False)
            for path, text in _walk_files(folder):
                await self.skills.save_file(ref, path, text, author="office", commit=False)
            installed.append(folder.name)
        if installed:
            await self.skills.commit(f"office: install standard skills ({', '.join(installed)})", "office")
        return installed


def _walk_files(folder: Any, prefix: str = "") -> list[tuple[str, str]]:
    files: list[tuple[str, str]] = []
    for item in sorted(folder.iterdir(), key=lambda f: f.name):
        rel = f"{prefix}{item.name}"
        if item.is_dir():
            files += _walk_files(item, rel + "/")
        elif item.name != "SKILL.md":
            files.append((rel, item.read_text(encoding="utf-8")))
    return files
