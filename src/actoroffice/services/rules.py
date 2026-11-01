"""Rules: files under <data>/rules/<scope>/<id>.md (front matter + markdown body), versioned with git; the
database is the index (search, compile). Anyone may propose a rule (draft); the owner of its scope activates
it (a team's lead for team/project/repo rules, an admin for global rules and for every policy). Rules the AI
proposes are always drafts.

`compile(ticket, project, stage)` returns the rules in effect for one stage; the run records them as id@version
so any decision can be traced back to the exact rule text.
"""

from __future__ import annotations

import asyncio
import fnmatch
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

import yaml

from actoroffice.domain.errors import NotFound, OfficeError, PolicyDenied
from actoroffice.domain.ids import utcnow
from actoroffice.domain.rules import SCOPE_RE, CompiledRules, Rule, RuleKind, RuleStatus, scope_dir, scope_kind
from actoroffice.domain.team import LOCAL_OWNER_ID
from actoroffice.services.skills import _git, split_front_matter

if TYPE_CHECKING:
    from actoroffice.app import Office

GUIDELINE_BUDGET = 8000  # characters of guideline text in a prompt; the rest is listed for rule_search
CONVENTION_FILES = ("CLAUDE.md", "AGENTS.md", ".github/copilot-instructions.md")
URL_HOST = re.compile(r"https?://([^/\s:\"']+)", re.IGNORECASE)


class RuleError(OfficeError):
    code = 422


class RuleService:
    def __init__(self, office: Office, root: Path) -> None:
        self.office = office
        self.root = root
        self._lock = asyncio.Lock()
        self._git_ready: bool | None = None

    # ---- read -------------------------------------------------------------------------------------------

    async def get(self, rule_id: str) -> Rule:
        row = await self.office.db.fetchone("SELECT * FROM rules WHERE id = ? COLLATE NOCASE", (rule_id,))
        if not row:
            raise NotFound(f"Rule {rule_id} không tồn tại")
        return _rule(row)

    async def list(self, *, scope: str | None = None, status: str | None = None, kind: str | None = None) -> list[Rule]:
        where, params = [], []
        for col, val in (("scope", scope), ("status", status), ("kind", kind)):
            if val:
                where.append(f"{col} = ?")
                params.append(val)
        rows = await self.office.db.fetchall(
            "SELECT * FROM rules" + (f" WHERE {' AND '.join(where)}" if where else "") + " ORDER BY scope, CAST(substr(id, 3) AS INTEGER)", params
        )
        return [_rule(r) for r in rows]

    async def search(self, query: str, *, limit: int = 10, active_only: bool = True) -> list[Rule]:
        terms = " OR ".join(f'"{t}"' for t in re.findall(r"\w{2,}", query)[:12])
        if not terms:
            return []
        rows = await self.office.db.fetchall(
            "SELECT r.* FROM rules_fts f JOIN rules r ON r.id = f.rule_id WHERE rules_fts MATCH ?"
            + (" AND r.status = 'active'" if active_only else "")
            + " ORDER BY rank LIMIT ?",
            (terms, limit),
        )
        return [_rule(r) for r in rows]

    async def versions(self, rule_id: str) -> list[dict[str, Any]]:
        rows = await self.office.db.fetchall("SELECT * FROM rule_versions WHERE rule_id = ? ORDER BY version", (rule_id,))
        return [{**r, "spec": json.loads(r.pop("spec_json"))} for r in rows]

    # ---- write ------------------------------------------------------------------------------------------

    async def propose(
        self,
        *,
        title: str,
        body: str,
        kind: str,
        scope: str = "global",
        stage: str = "any",
        spec: dict[str, Any] | None = None,
        by: str,
        activate: bool = False,
        source: str | None = None,
        ai: bool = False,
    ) -> Rule:
        rule = Rule(
            id="R-0",
            title=title.strip()[:160],
            kind=RuleKind(kind),
            scope=scope.strip() or "global",
            stage=stage.strip() or "any",
            spec=spec or {},
            body=body.strip(),
            created_by=by,
            owner=None,
            source=source,
        )
        await self._validate(rule)
        if activate and not ai and await self.can_approve(by, rule.scope, rule.kind):
            rule.status, rule.approved_by = RuleStatus.ACTIVE, by
        async with self._lock:
            row = await self.office.db.fetchone("SELECT COALESCE(MAX(CAST(substr(id, 3) AS INTEGER)), 0) + 1 AS n FROM rules")
            rule.id = f"R-{int(row['n']) if row else 1}"
            rule.updated_at = utcnow()
            await self._save(rule, author=by, message=f"{by}: add {rule.id} {rule.title}")
        self.office.bus.publish("rules.changed", rule=rule.model_dump())
        await self.office.audit.record("rule:add", rule.status.value, target=rule.id, detail={"by": by, "scope": rule.scope, "kind": rule.kind.value})
        return rule

    async def update(
        self,
        rule_id: str,
        *,
        by: str,
        title: str | None = None,
        body: str | None = None,
        spec: dict[str, Any] | None = None,
        stage: str | None = None,
    ) -> Rule:
        rule = await self.get(rule_id)
        changed = rule.model_copy(
            update={
                "title": title.strip()[:160] if title else rule.title,
                "body": body.strip() if body is not None else rule.body,
                "spec": spec if spec is not None else rule.spec,
                "stage": stage.strip() if stage else rule.stage,
                "version": rule.version + 1,
                "updated_at": utcnow(),
            }
        )
        await self._validate(changed)
        if changed.status == RuleStatus.ACTIVE and not await self.can_approve(by, rule.scope, rule.kind):
            changed.status, changed.approved_by = RuleStatus.DRAFT, None  # an edit by someone else needs approval again
        async with self._lock:
            await self._save(changed, author=by, message=f"{by}: edit {rule.id} v{changed.version}")
        self.office.bus.publish("rules.changed", rule=changed.model_dump())
        return changed

    async def approve(self, rule_id: str, *, by: str) -> Rule:
        rule = await self.get(rule_id)
        if not await self.can_approve(by, rule.scope, rule.kind):
            raise PolicyDenied(self._who_approves(rule))
        return await self._set_status(rule, RuleStatus.ACTIVE, by, approved_by=by)

    async def disable(self, rule_id: str, *, by: str) -> Rule:
        rule = await self.get(rule_id)
        if not await self.can_approve(by, rule.scope, rule.kind):
            raise PolicyDenied(self._who_approves(rule))
        return await self._set_status(rule, RuleStatus.DISABLED, by, approved_by=rule.approved_by)

    async def _set_status(self, rule: Rule, status: RuleStatus, by: str, *, approved_by: str | None) -> Rule:
        updated = rule.model_copy(update={"status": status, "approved_by": approved_by, "version": rule.version + 1, "updated_at": utcnow()})
        async with self._lock:
            await self._save(updated, author=by, message=f"{by}: {status.value} {rule.id}")
        self.office.bus.publish("rules.changed", rule=updated.model_dump())
        await self.office.audit.record("rule:status", status.value, target=rule.id, detail={"by": by})
        return updated

    def _who_approves(self, rule: Rule) -> str:
        if rule.kind == RuleKind.POLICY or rule.scope == "global":
            return "Chỉ admin được duyệt rule này"
        return "Cần lead của team (hoặc admin) để duyệt rule này"

    async def can_approve(self, by: str, scope: str, kind: RuleKind | str) -> bool:
        if by == LOCAL_OWNER_ID:
            return True
        teams = self.office.teams
        roles = await teams.roles_for(by, None)
        if "admin" in roles:
            return True
        if RuleKind(kind) == RuleKind.POLICY or scope == "global":
            return False
        team_id = await self._team_of(scope)
        if team_id is None:
            return False
        member = next((m for m in await teams.members(team_id) if m.user.id == by), None)
        return bool(member and "lead" in member.roles)

    async def _team_of(self, scope: str) -> str | None:
        kind, _, ref = scope.partition(":")
        if kind == "team":
            return ref
        if kind in ("project", "repo"):
            try:
                project = await self.office.projects.get(ref)
            except NotFound:
                return None
            return project.team_id
        return None

    async def _validate(self, rule: Rule) -> None:
        if not rule.title:
            raise RuleError("Rule cần tiêu đề")
        if not SCOPE_RE.match(rule.scope):
            raise RuleError("Phạm vi: global | team:<id> | project:<id> | repo:<id>")
        kind, _, ref = rule.scope.partition(":")
        if kind == "team":
            await self.office.teams.get_team(ref)
        elif kind in ("project", "repo"):
            await self.office.projects.get(ref)
        spec = rule.spec
        if rule.kind == RuleKind.GUIDELINE and not rule.body:
            raise RuleError("Guideline cần nội dung")
        if rule.kind == RuleKind.CHECK and not (spec.get("builtin") or spec.get("cmd")):
            raise RuleError("Check cần spec.builtin (vd. tests_changed) hoặc spec.cmd (argv)")
        if rule.kind == RuleKind.POLICY and not any(spec.get(k) for k in ("deny_tools", "deny_patterns", "deny_hosts")):
            raise RuleError("Policy cần spec.deny_tools, spec.deny_patterns hoặc spec.deny_hosts")
        if rule.kind == RuleKind.POLICY:
            for pattern in spec.get("deny_patterns") or []:
                try:
                    re.compile(str(pattern))
                except re.error as exc:
                    raise RuleError(f"Regex sai: {pattern}: {exc}") from exc
        if rule.kind == RuleKind.GATE and not (spec.get("roles") and (spec.get("when") or {}).get("paths")):
            raise RuleError("Gate cần spec.roles và spec.when.paths")

    async def _save(self, rule: Rule, *, author: str, message: str) -> None:
        folder = self.root / scope_dir(rule.scope)
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{rule.id}.md"
        meta = {
            "id": rule.id,
            "title": rule.title,
            "kind": rule.kind.value,
            "scope": rule.scope,
            "stage": rule.stage,
            "status": rule.status.value,
            "owner": rule.owner,
            "created_by": rule.created_by,
            "approved_by": rule.approved_by,
            "version": rule.version,
            "source": rule.source,
            "spec": rule.spec,
        }
        meta = {k: v for k, v in meta.items() if v not in (None, {}, [])}
        text = "---\n" + yaml.safe_dump(meta, sort_keys=False, allow_unicode=True) + "---\n\n" + rule.body + "\n"
        path.write_text(text, encoding="utf-8")
        db = self.office.db
        async with db.tx() as tx:
            await tx.execute(
                "INSERT INTO rules(id, title, kind, scope, stage, status, owner, created_by, approved_by, version, spec_json, body, source, path, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(id) DO UPDATE SET title = excluded.title, kind = excluded.kind, "
                "scope = excluded.scope, stage = excluded.stage, status = excluded.status, owner = excluded.owner, approved_by = excluded.approved_by, "
                "version = excluded.version, spec_json = excluded.spec_json, body = excluded.body, source = excluded.source, path = excluded.path, "
                "updated_at = excluded.updated_at",
                (
                    rule.id, rule.title, rule.kind.value, rule.scope, rule.stage, rule.status.value, rule.owner, rule.created_by, rule.approved_by,
                    rule.version, json.dumps(rule.spec, ensure_ascii=False), rule.body, rule.source, str(path.relative_to(self.root)), rule.updated_at,
                ),
            )
            await tx.execute(
                "INSERT OR REPLACE INTO rule_versions(rule_id, version, title, body, spec_json, status, author, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (rule.id, rule.version, rule.title, rule.body, json.dumps(rule.spec, ensure_ascii=False), rule.status.value, author, rule.updated_at),
            )
            await tx.execute("DELETE FROM rules_fts WHERE rule_id = ?", (rule.id,))
            await tx.execute("INSERT INTO rules_fts(title, body, rule_id) VALUES (?, ?, ?)", (rule.title, rule.body, rule.id))
        await self._commit(message, author)

    async def _commit(self, message: str, author: str) -> None:
        if self._git_ready is None:
            import shutil

            self._git_ready = shutil.which("git") is not None
            if self._git_ready and not (self.root / ".git").exists():
                self.root.mkdir(parents=True, exist_ok=True)
                self._git_ready = (await _git(self.root, "init", "-q"))[0] == 0
        if not self._git_ready:
            return
        await _git(self.root, "add", "-A")
        name = re.sub(r"[^\w .-]", "", author) or "office"
        await _git(self.root, "-c", f"user.name={name}", "-c", "user.email=rules@actoroffice.local", "commit", "-q", "-m", message)

    # ---- compile ----------------------------------------------------------------------------------------

    def scopes_for(self, ticket: Any, project: Any) -> list[str]:
        scopes = ["global"]
        if ticket is not None and getattr(ticket, "team_id", None):
            scopes.append(f"team:{ticket.team_id}")
        if project is not None:
            if project.team_id and f"team:{project.team_id}" not in scopes:
                scopes.append(f"team:{project.team_id}")
            scopes += [f"project:{project.id}", f"repo:{project.id}"]
        return scopes

    async def compile(self, *, ticket: Any, project: Any, stage: str) -> CompiledRules:
        scopes = self.scopes_for(ticket, project)
        rows = await self.office.db.fetchall(
            f"SELECT * FROM rules WHERE status = 'active' AND scope IN ({','.join('?' * len(scopes))})", scopes
        )
        rules = [r for r in map(_rule, rows) if r.applies_to_stage(stage)]
        rules.sort(key=lambda r: (-r.specificity, _desc(r.updated_at)))
        compiled = CompiledRules()
        seen_titles: set[str] = set()
        seen_checks: set[str] = set()
        budget = GUIDELINE_BUDGET
        for rule in rules:
            if rule.kind == RuleKind.GUIDELINE:
                key = _norm(rule.title)
                if key in seen_titles:
                    continue  # a narrower rule with the same title already won
                seen_titles.add(key)
                cost = len(rule.title) + len(rule.body)
                if cost <= budget:
                    compiled.guidelines.append(rule)
                    budget -= cost
                else:
                    compiled.overflow.append(rule)
            elif rule.kind == RuleKind.CHECK:
                key = str(rule.spec.get("builtin") or _norm(rule.title))
                if key not in seen_checks:
                    seen_checks.add(key)
                    compiled.checks.append(rule)
            elif rule.kind == RuleKind.POLICY:
                compiled.policies.append(rule)
            else:
                compiled.gates.append(rule)
        return compiled

    # ---- repo conventions -------------------------------------------------------------------------------

    async def import_conventions(self, project: Any, *, by: str) -> list[Rule]:
        """CLAUDE.md, AGENTS.md, .kiro/steering/*.md of a repo become active guidelines of scope repo:<project>.
        Re-importing updates the same rules (matched by source file)."""
        if not project.path:
            return []
        base = Path(project.path)
        files = [base / f for f in CONVENTION_FILES if (base / f).is_file()]
        steering = base / ".kiro" / "steering"
        if steering.is_dir():
            files += sorted(steering.glob("*.md"))
        scope = f"repo:{project.id}"
        imported = []
        for file in files:
            rel = file.relative_to(base).as_posix()
            meta, body = split_front_matter(file.read_text(encoding="utf-8", errors="replace")[:20_000])
            if not body.strip():
                continue
            existing = await self.office.db.fetchone("SELECT id FROM rules WHERE scope = ? AND source = ?", (scope, f"file:{rel}"))
            title = str(meta.get("title") or f"Quy ước repo: {rel}")
            if existing:
                rule = await self.get(existing["id"])
                if rule.body.strip() != body.strip():
                    rule = await self.update(rule.id, by=by, body=body)
            else:
                rule = await self.propose(title=title, body=body, kind="guideline", scope=scope, by=by, activate=True, source=f"file:{rel}")
            imported.append(rule)
        return imported


def policy_denial(policies: list[Rule], tool: str, args: dict[str, Any]) -> Rule | None:
    """The first active policy that forbids this tool call, if any."""
    name = re.sub(r"^mcp__\w+?__", "", tool)
    flat = json.dumps(args, ensure_ascii=False) if args else ""
    command = str(args.get("command") or "") if isinstance(args, dict) else ""
    hosts = {h.lower() for h in URL_HOST.findall(flat)}
    for rule in policies:
        spec = rule.spec
        if any(t in (tool, name) for t in spec.get("deny_tools") or []):
            return rule
        for pattern in spec.get("deny_patterns") or []:
            if re.search(str(pattern), command or flat):
                return rule
        for host in hosts:
            if any(fnmatch.fnmatch(host, str(p).lower()) for p in spec.get("deny_hosts") or []):
                return rule
    return None


def gate_rules_hit(gates: list[Rule], changed: list[str]) -> list[Rule]:
    hit = []
    for rule in gates:
        paths = (rule.spec.get("when") or {}).get("paths") or []
        if any(fnmatch.fnmatch(f, p) or fnmatch.fnmatch("/" + f, "*/" + p.lstrip("*/")) for f in changed for p in paths):
            hit.append(rule)
    return hit


def _norm(text: str) -> str:
    return re.sub(r"\W+", " ", text.lower()).strip()


def _desc(stamp: str) -> str:
    """Sort key for 'newest first' within the same specificity."""
    return "".join(chr(0x10FFFF - ord(c)) for c in stamp)


def _rule(row: dict[str, Any]) -> Rule:
    return Rule(
        id=row["id"],
        title=row["title"],
        kind=RuleKind(row["kind"]),
        scope=row["scope"],
        stage=row["stage"],
        status=RuleStatus(row["status"]),
        owner=row["owner"],
        created_by=row["created_by"],
        approved_by=row["approved_by"],
        version=row["version"],
        spec=json.loads(row["spec_json"] or "{}"),
        body=row["body"],
        source=row["source"],
        updated_at=row["updated_at"],
    )


__all__ = ["RuleService", "policy_denial", "gate_rules_hit", "scope_kind"]
