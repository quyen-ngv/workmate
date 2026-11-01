"""OfficeGate: policy + human approval for every tool call of one run."""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.domain.org import Actor, PermissionMode, Position
from actoroffice.core.stats import RunStats
from actoroffice.domain.work import InteractionKind
from actoroffice.runtimes.base import GateDecision, GateResult
from actoroffice.security.policy import Decision, Risk, ToolCallContext, suggest_rules, target_of
from actoroffice.services.interactions import input_hash

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)

# Our own tools that act outside the machine: they follow the position's "external" rule (usually ask).
ENV_DUMP = re.compile(r"^\s*(env|printenv|set|export\s+-p|declare\s+-x|Get-ChildItem\s+env:|gci\s+env:|dir\s+env:)\s*(\||$)|\becho\s+\$\w*(DSN|TOKEN|SECRET|PASSWORD|KEY)\b|\bcat\s+/proc/(self|\d+)/environ", re.IGNORECASE)
EXTERNAL_OFFICE_TOOLS = {"jira_comment", "pr_create", "pr_reply", "repo_fetch"}


@dataclass
class OfficeGate:
    office: Office
    actor: Actor
    position: Position
    run_id: str
    workspace: Path
    ticket_id: int | None = None
    ticket_key: str | None = None
    read_dirs: list[Path] = field(default_factory=list)
    tainted: bool = False
    parked_interaction: str | None = None
    asked: list[str] = field(default_factory=list)
    seen_change_id: int = 0  # requirement changes already shown to this run
    seen_steer_id: int = 0  # directions from people already shown to this run
    policies: list[Any] = field(default_factory=list)  # team policy rules in effect (services/rules.py)
    watchdog: Any = None  # engine/watchdog.py: a detected loop stops the next call
    stats: RunStats = field(default_factory=RunStats)

    async def before_tool(self, tool: str, args: dict[str, Any], tool_use_id: str | None) -> GateResult:
        office = self.office
        if self.ticket_id is not None:
            steers = await office.tickets.comments_after(self.ticket_id, "steer", self.seen_steer_id)
            if steers:
                # A person steered the work while this run was going: stop this one call and pass the direction on.
                self.seen_steer_id = steers[-1].id
                notes = "\n".join(f"- {c.body}" for c in steers)
                await self._audit(tool, tool, Risk.READ, "interrupted", "steer")
                return GateResult(
                    GateDecision.DENY,
                    f"NEW DIRECTION from the team while you were working. This tool call was NOT run.\n{notes}\n"
                    "Adjust your plan to it, then continue.",
                )
            # A requirement changed while this run was working: stop the next call and say so, once. The deny
            # reason is how a running agent hears about it without waiting for the next wake.
            changes = await office.tickets.changes_after(self.ticket_id, self.seen_change_id)
            if changes:
                self.seen_change_id = changes[-1].id
                notes = "\n".join(f"- {c.body}" for c in changes)
                await self._audit(tool, tool, Risk.READ, "interrupted", "requirement change")
                return GateResult(
                    GateDecision.DENY,
                    "REQUIREMENTS CHANGED while you were working. This tool call was NOT run.\n"
                    f"{notes}\nRe-read the ticket and its documents (ticket_get, doc_get), adjust your plan and work "
                    "to the change, then continue. Mention the change in your final comment.",
                )
        if self.watchdog is not None and not tool.endswith(("ask_human", "dossier_update", "ticket_comment", "ticket_set_status")):
            message = self.watchdog.stop_message()
            if message:
                await self._audit(tool, tool, Risk.READ, "interrupted", "watchdog loop")
                return GateResult(GateDecision.DENY, message)
        if self.ticket_id is not None and tool in ("Bash", "PowerShell") and ENV_DUMP.search(str(args.get("command") or "")):
            await self._audit(tool, str(args.get("command"))[:200], Risk.READ, "denied", "env dump")
            return GateResult(GateDecision.DENY, "Printing the environment is not allowed (it may hold granted credentials). Use access_list to see what you can reach.")
        if self.policies:
            from actoroffice.services.rules import policy_denial

            rule = policy_denial(self.policies, tool, args)
            if rule is not None:
                await self._audit(tool, target_of(tool, args) or tool, Risk.READ, "denied", f"rule {rule.ref}")
                await office.runs.event(self.run_id, "rule", {"id": rule.id, "ref": rule.ref, "title": rule.title, "tool": tool, "effect": "deny"})
                return GateResult(
                    GateDecision.DENY,
                    f"Team rule {rule.id} ({rule.title}) forbids this action. {rule.body[:400]}\nDo it another way, or ask a person (ask_human).",
                )
        # Re-read the actor: an "always allow" chosen on a previous card applies right away.
        actor = await office.org.get_actor(self.actor.id)
        ctx = ToolCallContext(
            position=self.position,
            workspace=self.workspace,
            tainted=self.tainted,
            extra_read_dirs=self.read_dirs,
            allow_rules=actor.allow_rules,
            trust_all=actor.permission_mode == PermissionMode.TRUST_ALL,
        )
        verdict = office.policy.decide(tool, args, ctx, external=tool.removeprefix("mcp__office__") in EXTERNAL_OFFICE_TOOLS)
        target = target_of(tool, args) or tool

        if verdict.decision == Decision.ALLOW:
            self._count(tool, args)
            if verdict.taints:
                self.tainted = True
            if verdict.risk != Risk.READ:
                await self._audit(tool, target, verdict.risk, "allowed")
            return GateResult(GateDecision.ALLOW)
        if verdict.decision == Decision.DENY:
            await self._audit(tool, target, verdict.risk, "denied", verdict.reason)
            return GateResult(GateDecision.DENY, verdict.reason)

        # ASK: a human decision recorded earlier for exactly this call wins (resume after a park).
        granted = await office.interactions.consume_grant(self.actor.id, self.ticket_id, tool, args)
        if granted:
            await self._audit(tool, target, verdict.risk, f"human:{granted}")
            if granted == "allow":
                self.tainted = self.tainted or verdict.taints
                self._count(tool, args)
                return GateResult(GateDecision.ALLOW)
            return GateResult(GateDecision.DENY, "The user rejected this action. Do not retry it; continue another way or ask.")

        item = await office.interactions.create(
            InteractionKind.APPROVE_TOOL,
            {
                "tool": tool,
                "input": _shown_input(args, office.redact),
                "input_hash": input_hash(tool, args),
                "target": office.redact(target)[:300],
                "suggestions": suggest_rules(tool, office.redact(target)),
                "reason": verdict.reason,
                "risk": verdict.risk.value,
                "actor": self.actor.name,
                "ticket": self.ticket_key,
            },
            actor_id=self.actor.id,
            ticket_id=self.ticket_id,
            run_id=self.run_id,
        )
        self.asked.append(item.id)
        answered = await office.interactions.wait(item.id, office.settings.approval_wait_s)
        if answered is None:
            await office.interactions.mark_parked(item.id)
            self.parked_interaction = item.id
            await self._audit(tool, target, verdict.risk, "parked", item.id)
            return GateResult(GateDecision.DEFER, "Waiting for the user's approval")
        decision = await office.interactions.consume_grant(self.actor.id, self.ticket_id, tool, args)
        await self._audit(tool, target, verdict.risk, f"human:{decision}")
        if decision == "allow":
            self.tainted = self.tainted or verdict.taints
            self._count(tool, args)
            return GateResult(GateDecision.ALLOW)
        return GateResult(GateDecision.DENY, "The user rejected this action. Do not retry it; continue another way or ask.")

    def _count(self, tool: str, args: dict[str, Any]) -> None:
        if self.stats.workspace is None:
            self.stats.workspace = self.workspace
        try:
            self.stats.record(tool, args)
        except Exception:  # statistics must never break a run
            log.debug("could not count %s", tool, exc_info=True)

    async def after_tool(self, tool: str, args: dict[str, Any], result: Any) -> None:
        return None

    async def _audit(self, tool: str, target: str, risk: Risk, outcome: str, detail: str = "") -> None:
        await self.office.audit.record(
            f"tool:{tool}",
            outcome,
            actor_id=self.actor.id,
            run_id=self.run_id,
            ticket_id=self.ticket_id,
            target=target,
            detail={"risk": risk.value, "detail": detail},
        )


def _shown_input(args: dict[str, Any], redact: Any, limit: int = 4000) -> dict[str, Any]:
    """What the approver sees: redacted, and summarized when large."""
    raw = redact(json.dumps(args, ensure_ascii=False))
    if len(raw) <= limit:
        try:
            return json.loads(raw)
        except json.JSONDecodeError:
            pass
    return {"summary": raw[: limit // 2] + "…"}
