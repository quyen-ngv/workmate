"""Tools for Jira, Confluence, code hosts (Bitbucket, GitHub) and other repositories. They run server side with the
office's credentials (the AI never sees a token). Writes (jira_comment, pr_create, pr_reply, repo_fetch) follow the
position's "external" rule in the gate (usually: a person approves the exact call).

Each tool works through a connection of the ticket's scope that has the capability it needs (`ToolDef.needs`); a run
only gets the tools a connection can serve. `jira_issue`, `wiki_page` and `wiki_search` are kept for one version as other
names of `source_get` / `source_search`.
"""

from __future__ import annotations

import asyncio
import base64
import os
import re
from pathlib import Path
from typing import Any

from actoroffice.codeintel.sync import auth_env
from actoroffice.core.http import git_tls_config
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.work import ProjectAccess
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Scope
from actoroffice.services.atlassian import code_hosts, parse_pr, pr_host
from actoroffice.services.checks import ui_evidence_markdown

from .registry import ToolCtx, ToolDef, ToolError, schema, source_ref
from .source_tools import read_document

__all__ = ["ATLASSIAN_TOOLS", "parse_pr"]

MAX_TEXT = 12_000
NOT_THIS_HOST = ("forbidden", "invalid", "not_found")  # a code host saying the repository is not one of its own
READ_ONLY_HINT = "its `write` switch is off; a person turns it on in Cài đặt › Kết nối"
TRACKER = frozenset({Capability.TRACKER})
WIKI = frozenset({Capability.WIKI})
CODE_PR = frozenset({Capability.CODE_PR})


def _cut(text: str, limit: int = MAX_TEXT) -> str:
    return text if len(text) <= limit else text[:limit] + f"\n…({len(text) - limit} more characters: narrow your query)"


async def _scope(ctx: ToolCtx) -> Scope:
    ticket = await ctx.office.tickets.get(ctx.ticket_id) if ctx.ticket_id is not None else None
    return ctx.office.sourcehub.scope(ticket)


async def _connection(ctx: ToolCtx, capability: Capability, *, write: bool = False) -> tuple[ConnectionSpec, Any] | None:
    """The nearest connection of the ticket's scope with a capability (and with `write` on, for a call that writes),
    with its adapter. None: nothing connected can serve the call."""
    office = ctx.office
    for spec in await office.connections.candidates(capability, await _scope(ctx)):
        if write and not spec.write:
            continue
        try:
            return spec, await office.connections.adapter(spec.id)
        except ConnectorError:
            continue  # no secret yet: as good as not connected
    return None


def _outside_content(ctx: ToolCtx) -> None:
    """What a connection returned is content of an outside system: data, not instructions. The run is tainted from here
    on, exactly as after `source_get` (acting outside afterwards asks a person first)."""
    gate = ctx.office.gates.get(ctx.run_id)
    if gate is not None:
        gate.tainted = True


async def _document(ctx: ToolCtx, found: tuple[ConnectionSpec, Any], ref: str, what: str) -> str:
    """`jira_issue` / `wiki_page` through a connection: what `source_get` answers, the same "read" mark, the same taint."""
    if not ref:
        raise ToolError(f"{what} is required")
    text = await read_document(ctx, ref, direct=found)
    _outside_content(ctx)
    return text


NO_TRACKER = "No issue tracker (Jira) is connected for this ticket. A person adds one in Cài đặt › Kết nối."
NO_WIKI = "No wiki (Confluence) is connected for this ticket. A person adds one in Cài đặt › Kết nối."


async def jira_issue(args: dict[str, Any], ctx: ToolCtx) -> str:
    key = str(args.get("key") or "").strip().upper()
    found = await _connection(ctx, Capability.TRACKER)
    if found is None:
        raise ToolError(NO_TRACKER)
    return await _document(ctx, found, key, "key")


async def jira_search(args: dict[str, Any], ctx: ToolCtx) -> str:
    limit = min(int(args.get("limit") or 15), 30)
    found = await _connection(ctx, Capability.TRACKER)
    if found is None:
        raise ToolError(NO_TRACKER)
    try:
        listed = await found[1].search(str(args.get("jql") or ""), limit)
    except ConnectorError as exc:
        raise ToolError(str(exc)) from exc
    _outside_content(ctx)
    return "\n".join(f"{i.key} · {i.title} [{i.status}]" for i in listed) or "No issues."


async def jira_comment(args: dict[str, Any], ctx: ToolCtx) -> str:
    found = await _connection(ctx, Capability.TRACKER, write=True)
    if found is None:
        if await _connection(ctx, Capability.TRACKER) is not None:
            raise ToolError(f"The Jira connection is read-only ({READ_ONLY_HINT}). Until then you cannot comment on Jira.")
        raise ToolError(NO_TRACKER)
    key = str(args.get("key") or "").strip().upper()
    if not key and ctx.ticket_id is not None:
        key = (await ctx.office.tickets.get(ctx.ticket_id)).jira_key or ""
    if not key:
        raise ToolError("key is required (this ticket has no Jira issue)")
    body = str(args.get("body") or "")[:30000]
    try:
        await found[1].comment(key, body)
    except ConnectorError as exc:
        raise ToolError(str(exc)) from exc
    return f"Commented on {key}."


async def wiki_search(args: dict[str, Any], ctx: ToolCtx) -> str:
    limit = min(int(args.get("limit") or 10), 25)
    found = await _connection(ctx, Capability.WIKI)
    if found is None:
        raise ToolError(NO_WIKI)
    try:
        listed = await found[1].search(str(args.get("text") or ""), space=args.get("space") or None, limit=limit)
    except ConnectorError as exc:
        raise ToolError(str(exc)) from exc
    _outside_content(ctx)
    return "\n".join(f"{p.id} · {p.title} (space {p.space}) {p.url}" for p in listed) or "No pages."


async def wiki_page(args: dict[str, Any], ctx: ToolCtx) -> str:
    found = await _connection(ctx, Capability.WIKI)
    if found is None:
        raise ToolError(NO_WIKI)
    return await _document(ctx, found, str(args.get("id") or "").strip(), "id")


# ---- pull requests -----------------------------------------------------------------------------------------


async def _git(cwd: Path, *args: str, timeout: float = 120, env: dict[str, str] | None = None) -> tuple[int, str]:
    """`env`: extra variables for this one git process (credentials and TLS settings never go into argv or .git/config)."""
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env={**os.environ, **env} if env else None
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        proc.kill()
        return 124, "timeout"
    return proc.returncode or 0, out.decode("utf-8", "replace").strip()


async def pr_create(args: dict[str, Any], ctx: ToolCtx) -> str:
    office = ctx.office
    scope = await _scope(ctx)
    hosts = await code_hosts(office, scope, write=True)  # connections that may write
    if not hosts:
        if await code_hosts(office, scope):
            raise ToolError(
                f"The code host connection is read-only ({READ_ONLY_HINT}): you cannot push or open the pull request. Commit "
                "everything, then call stage_complete with the branch name and a short summary; a person opens the pull "
                "request and links it to the ticket. Do not push."
            )
        raise ToolError(
            "No code host is connected: this repository is reviewed on this machine. Commit everything, then call "
            "stage_complete with the branch name and a short summary; a person reviews the branch and merges it. "
            "Do not ask for a PR URL and do not push."
        )
    if ctx.ticket_id is None or ctx.workspace is None:
        raise ToolError("pr_create works inside a ticket's workspace.")
    ticket = await office.tickets.get(ctx.ticket_id)
    project = await office.projects.get(ticket.project_id) if ticket.project_id else None
    bound = hosts[0] if project is not None and project.code_connection else None  # the project's own code host
    repo = str(args.get("repo") or (project.repo_url if project else "") or "")
    if not repo:
        raise ToolError(
            "Which repository? Pass repo (PROJECT/repo on Bitbucket Server, owner/name elsewhere, or the repository's link), "
            "or a person sets the project's connection and repository in Cài đặt › Repo / dự án."
        )
    code, branch = await _git(ctx.workspace, "rev-parse", "--abbrev-ref", "HEAD")
    if code != 0 or branch in ("HEAD", ""):
        raise ToolError("The workspace is not on a branch.")
    destination = str(args.get("destination") or (project.main_branch if project else "main"))
    if branch == destination:
        raise ToolError(f"You are on {destination}; work on the ticket branch.")
    try:
        auth = await bound.git_auth(repo, write=True) if bound is not None else None
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    if auth is not None:
        # The branch goes to the connection's repository with the connection's credential, handed to git through its
        # environment only: nothing is written to .git/config and the token is redacted from whatever git prints.
        for secret in (auth.password, auth.bearer, base64.b64encode(f"{auth.username}:{auth.password}".encode()).decode() if auth.password else ""):
            office.redact.register(secret)
        code, out = await _git(ctx.workspace, "push", auth.url, f"HEAD:refs/heads/{branch}", env=auth_env(auth, git_tls_config()))
        out = office.redact(out)
        _code, origin = await _git(ctx.workspace, "remote", "get-url", "origin")
        if code == 0 and _code == 0 and origin and bound.same_repo(origin) is False:
            await office.tickets.add_comment(
                ticket.id,
                f"⚠️ origin của thư mục làm việc ({re.sub(r'://[^/@]+@', '://', origin)[:200]}) không phải repo «{bound.repo.slug}» của kết nối "
                f"«{bound.spec.id}». Nhánh {branch} đã được push lên repo của kết nối, pull request mở ở đó.",
                author_kind="system",
                author_id=None,
            )
    else:  # no connection bound to the project: the folder's own remote and whatever credential git has on this machine
        code, out = await _git(ctx.workspace, "push", "-u", "origin", branch)
    if code != 0:
        raise ToolError(f"git push failed: {out[-800:]}")
    description = str(args.get("description") or "")
    evidence = ui_evidence_markdown(await office.stages.ui_evidence(ticket.id)) if ticket.stage else ""
    if evidence:  # screenshots of the AC [ui] or why none could be taken (SI-23)
        description = f"{description.rstrip()}\n\n{evidence}".lstrip()
    pr: dict[str, Any] = {}
    for position, host in enumerate(hosts, 1):
        try:
            pr = await host.pr_create(repo, branch, destination, str(args.get("title") or ticket.title), description)
            break
        except OfficeError as exc:
            if getattr(exc, "kind", "") in NOT_THIS_HOST and position < len(hosts):
                continue  # another organisation's repository: the next code host may be the one that has it
            raise ToolError(str(exc)) from exc
    url = pr.get("url") or ""
    await office.db.execute("UPDATE tickets SET pr_url = ? WHERE id = ?", (url, ticket.id))
    await office.tickets.add_work_product(ticket.id, "link", url, {"note": f"pull request #{pr['id']}"})
    await office.tickets.add_comment(ticket.id, f"🔗 PR #{pr['id']}: {url}", author_kind="actor", author_id=ctx.actor.name)
    office.bus.publish("ticket.updated", ticket=(await office.tickets.get(ticket.id)).model_dump())
    return f"Opened PR #{pr['id']} {url} ({branch} → {destination}). It is linked to the ticket."


async def _ticket_pr(args: dict[str, Any], ctx: ToolCtx, *, write: bool = False) -> tuple[Any, str, int]:
    """The code host, repository and number of the ticket's pull request (or of the one given by url)."""
    office = ctx.office
    scope = await _scope(ctx)
    if not await code_hosts(office, scope, write=write):
        if write and await code_hosts(office, scope):
            raise ToolError(f"The code host connection is read-only ({READ_ONLY_HINT}). Until then you cannot reply on the pull request.")
        raise ToolError("No code host connected.")
    url = str(args.get("url") or "")
    if not url and ctx.ticket_id is not None:
        url = (await office.tickets.get(ctx.ticket_id)).pr_url or ""
    found = await pr_host(office, scope, url, write=write)
    if found is None:
        raise ToolError(f"No connected code host has the pull request {url[:200]}." if url else "No pull request linked (pass url).")
    return found


async def pr_comments(args: dict[str, Any], ctx: ToolCtx) -> str:
    host, repo, pr_id = await _ticket_pr(args, ctx)
    try:
        info = await host.pr_get(repo, pr_id)
        comments = await host.pr_comments(repo, pr_id)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    lines = [f"PR #{pr_id} [{info['state']}] {info['title']}"]
    if info.get("approvals") is not None:  # None: the connection of this code host does not tell who approved
        lines[0] += f" · approved by: {', '.join(info['approvals']) or 'nobody'}"
    for c in comments:
        where = f" {c['file']}:{c['line']}" if c.get("file") else ""
        reply = f" (reply to {c['parent']})" if c.get("parent") else ""
        done = " [resolved]" if c.get("resolved") else ""
        lines.append(f"- #{c['id']} {c['by']}{where}{reply}{done}: {c['text']}")
    return _cut("\n".join(lines))


async def pr_reply(args: dict[str, Any], ctx: ToolCtx) -> str:
    host, repo, pr_id = await _ticket_pr(args, ctx, write=True)
    text = str(args.get("text") or "").strip()
    if not text:
        raise ToolError("text is empty")
    try:
        reply = await host.pr_reply(repo, pr_id, text, parent_id=int(args["comment_id"]) if args.get("comment_id") else None)
    except OfficeError as exc:
        raise ToolError(str(exc)) from exc
    return f"Replied (#{reply['id']}) on PR #{pr_id}." if reply.get("id") else f"Replied on PR #{pr_id}."


# ---- other repositories -------------------------------------------------------------------------------------


async def repo_fetch(args: dict[str, Any], ctx: ToolCtx) -> str:
    """Clone (or update) another repository read-only so you can read its code (callers, contracts, events)."""
    office = ctx.office
    url = str(args.get("url") or "").strip()
    if not re.match(r"^(https://|git@)[\w.@:/\-]+$", url):
        raise ToolError("url must be an https:// or git@ repository URL")
    name = re.sub(r"[^a-z0-9\-]+", "-", (str(args.get("name") or "") or url.rstrip("/").split("/")[-1].removesuffix(".git")).lower()).strip("-")[:40] or "repo"
    target = office.settings.data_dir / "refs" / name
    if (target / ".git").exists():
        code, out = await _git(target, "pull", "--ff-only", timeout=300)
    else:
        target.parent.mkdir(parents=True, exist_ok=True)
        proc = await asyncio.create_subprocess_exec(
            "git", "clone", "--depth", "50", url, str(target), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out_b, _ = await asyncio.wait_for(proc.communicate(), 600)
        code, out = proc.returncode or 0, out_b.decode("utf-8", "replace")
    if code != 0:
        raise ToolError(f"git failed: {out[-800:]}")
    team_id = (await office.tickets.get(ctx.ticket_id)).team_id if ctx.ticket_id else None
    project = await office.projects.find(f"ref:{name}")
    if project is None:
        project = await office.projects.create(f"ref:{name}", path=str(target), access=ProjectAccess.READ_ONLY, description=f"Reference repo {url}")
    await office.projects.configure(project.id, team_id=team_id, kind="reference", repo_url=url)
    gate = office.gates.get(ctx.run_id)
    if gate is not None and target.resolve() not in gate.read_dirs:
        gate.read_dirs.append(target.resolve())  # readable right away in this run
    return f"{url} is available read-only at {target} (Read/Grep/Glob with absolute paths). Future runs of this team see it too."


async def repos_list(_args: dict[str, Any], ctx: ToolCtx) -> str:
    rows = [p for p in await ctx.office.projects.list() if p.kind == "reference" and p.path]
    if not rows:
        return "No reference repositories yet (repo_fetch adds one)."
    return "\n".join(f"- {p.name}: {p.path} ({p.repo_url or ''})" for p in rows)


# `needs`: a run gets the tool only when a connection of the ticket's scope has the capability. `effect="external"`: acts outside the machine, so the position's "external" rule decides.
ATLASSIAN_TOOLS: list[ToolDef] = [
    ToolDef(
        "jira_issue",
        "Read a Jira issue: summary, status, description and recent comments. Being replaced by source_get (same issue key): prefer source_get.",
        schema({"key": {"type": "string"}}),
        jira_issue,
        read_only=True,
        activity="read",
        reads_source=source_ref("jira", "key", "path"),
        needs=TRACKER,
    ),
    ToolDef(
        "jira_search",
        "Search Jira with JQL (e.g. related issues, earlier bugs in the same component).",
        schema({"jql": {"type": "string"}, "limit": {"type": "integer"}}, required=["jql"]),
        jira_search,
        read_only=True,
        needs=TRACKER,
    ),
    ToolDef(
        "jira_comment",
        "Comment on a Jira issue (default: this ticket's issue). A person approves the comment first.",
        schema({"key": {"type": "string"}, "body": {"type": "string"}}, required=["body"]),
        jira_comment,
        effect="external",
        needs=TRACKER,
    ),
    ToolDef(
        "wiki_search",
        "Search Confluence pages by text (optionally in one space). Returns ids, titles, last update. Being replaced by source_search: prefer source_search.",
        schema({"text": {"type": "string"}, "space": {"type": "string"}, "limit": {"type": "integer"}}, required=["text"]),
        wiki_search,
        read_only=True,
        needs=WIKI,
    ),
    ToolDef(
        "wiki_page",
        "Read a Confluence page as text, with its version and last update date. Being replaced by source_get (page id or URL): prefer source_get.",
        schema({"id": {"type": "string"}}),
        wiki_page,
        read_only=True,
        activity="read",
        reads_source=source_ref("page", "id"),
        needs=WIKI,
    ),
    ToolDef(
        "pr_create",
        "Push the ticket branch and open a pull request (default repo: the project's repository on its code host, "
        "destination: its main branch). A person approves first. The PR is linked to the ticket.",
        schema(
            {"title": {"type": "string"}, "description": {"type": "string"}, "repo": {"type": "string"}, "destination": {"type": "string"}},
            required=["title", "description"],
        ),
        pr_create,
        effect="external",
        needs=CODE_PR,
    ),
    ToolDef(
        "pr_comments",
        "Read the review comments and approvals of the ticket's pull request (or one given by url).",
        schema({"url": {"type": "string"}}, required=[]),
        pr_comments,
        read_only=True,
        needs=CODE_PR,
    ),
    ToolDef(
        "pr_reply",
        "Reply on the pull request (to a comment id, or a general comment). A person approves first.",
        schema({"text": {"type": "string"}, "comment_id": {"type": "integer"}, "url": {"type": "string"}}, required=["text"]),
        pr_reply,
        effect="external",
        needs=CODE_PR,
    ),
    ToolDef(
        "repo_fetch",
        "Get another repository read-only (callers, contracts, event producers/consumers). A person approves the first fetch.",
        schema({"url": {"type": "string"}, "name": {"type": "string"}}, required=["url"]),
        repo_fetch,
        effect="external",
    ),
    ToolDef("repos_list", "List reference repositories already available to read.", schema({}, required=[]), repos_list, read_only=True),
]
