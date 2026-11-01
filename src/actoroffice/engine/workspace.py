"""Where an actor works on a ticket. The project decides:

- worktree:  a git worktree + branch per ticket tree, created from the project's repo;
- direct:    the project folder itself (the scheduler runs one ticket at a time per such project);
- read_only: a scratch folder per ticket tree, with read access to the project folder (if any).
"""

from __future__ import annotations

import asyncio
import re
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path

from actoroffice.domain.org import Position, WorkspaceKind
from actoroffice.domain.work import Project, ProjectAccess, Ticket


@dataclass(frozen=True)
class Workspace:
    path: Path
    kind: WorkspaceKind
    branch: str | None = None
    read_dirs: list[Path] = field(default_factory=list)  # readable, not writable
    direct: bool = False  # editing the user's real folder


class WorkspaceError(Exception):
    pass


class WorkspaceManager:
    def __init__(self, root: Path) -> None:
        self.root = root

    async def realize(self, ticket: Ticket, project: Project | None, position: Position, root: Ticket | None = None) -> Workspace:
        project_dir = Path(project.path) if project and project.path else None
        # Workspaces belong to the ticket tree (not the actor) so BA, Lead, DEV and QA see the same files.
        root = root or ticket
        anchor = root.key
        path = (self.root / anchor).resolve()
        if project_dir and project and project.access == ProjectAccess.DIRECT:
            # Drafts written while the project was still read-only stay readable, so nothing is lost.
            drafts = [path] if path.is_dir() and any(path.iterdir()) else []
            return Workspace(project_dir, WorkspaceKind.FOLDER, direct=True, read_dirs=drafts)
        if project_dir and project and project.access == ProjectAccess.WORKTREE:
            drafts = []
            if path.is_dir() and not (path / ".git").exists() and any(path.iterdir()):
                # The tree started read-only (a plain scratch folder); move it aside for the worktree, keep it readable.
                draft = path.with_name(f"{anchor}-draft")
                if not draft.exists():
                    path.rename(draft)
                drafts = [draft]
            elif path.with_name(f"{anchor}-draft").is_dir():
                drafts = [path.with_name(f"{anchor}-draft")]
            branch = branch_name(root)
            await self._ensure_worktree(project_dir, path, branch, project.main_branch)
            return Workspace(path, WorkspaceKind.GIT_WORKTREE, branch, read_dirs=drafts)
        path.mkdir(parents=True, exist_ok=True)
        return Workspace(path, WorkspaceKind.FOLDER, read_dirs=[project_dir] if project_dir else [])

    async def _ensure_worktree(self, repo: Path, path: Path, branch: str, base: str) -> None:
        if (path / ".git").exists():
            return
        path.parent.mkdir(parents=True, exist_ok=True)
        exists = await _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False)
        args = ["worktree", "add", str(path), branch] if exists[0] == 0 else ["worktree", "add", "-b", branch, str(path), base]
        code, out = await _git(repo, *args, check=False)
        if code != 0:
            raise WorkspaceError(f"git worktree add failed: {out.strip()}")


PORT_BASE = 20000
PORTS_PER_TICKET = 10


def ticket_env(ticket: Ticket, project: Project | None) -> dict[str, str]:
    """Per-ticket names and ports so parallel tickets never collide: a compose project name and a block of ten
    ports (AO_PORT_0..9, plus the names the project profile asks for, e.g. APP_PORT, DB_PORT)."""
    base = PORT_BASE + (ticket.id % 2000) * PORTS_PER_TICKET
    env = {"COMPOSE_PROJECT_NAME": f"ao-{ticket.key.lower()}", "AO_TICKET": ticket.key, "AO_PORT_BASE": str(base)}
    for i in range(PORTS_PER_TICKET):
        env[f"AO_PORT_{i}"] = str(base + i)
    names = (project.profile.get("ports") if project else None) or []
    for i, name in enumerate(names[:PORTS_PER_TICKET]):
        if re.fullmatch(r"[A-Z][A-Z0-9_]{1,40}", str(name)):
            env[str(name)] = str(base + i)
    return env


async def cleanup_ticket(ticket: Ticket, workspace: Path) -> None:
    """Stop the ticket's compose services (best effort) when its work is over."""
    import shutil

    if shutil.which("docker") is None or not workspace.is_dir():
        return
    proc = await asyncio.create_subprocess_exec(
        "docker", "compose", "-p", f"ao-{ticket.key.lower()}", "down", "-v", "--remove-orphans",
        cwd=str(workspace), stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        await asyncio.wait_for(proc.wait(), 120)
    except TimeoutError:
        proc.kill()


async def changes(workspace: Path, base: str) -> dict:
    """What the ticket changed so far (Files / Changes panel): files with +/- counts and the diff."""
    if not (workspace / ".git").exists():
        return {"git": False, "files": [], "diff": ""}
    code, merge_base = await _git(workspace, "merge-base", "HEAD", base, check=False)
    start = merge_base.strip() if code == 0 and merge_base.strip() else "HEAD"
    _code, branch = await _git(workspace, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    _code, numstat = await _git(workspace, "diff", "--numstat", start, check=False)
    files = []
    for line in numstat.splitlines():
        parts = line.split("\t")
        if len(parts) == 3:
            added, removed, path = parts
            files.append({"path": path, "added": int(added) if added.isdigit() else 0, "removed": int(removed) if removed.isdigit() else 0})
    _code, untracked = await _git(workspace, "ls-files", "--others", "--exclude-standard", check=False)
    for path in untracked.splitlines():
        if path.strip():
            files.append({"path": path.strip(), "added": 0, "removed": 0, "untracked": True})
    _code, diff = await _git(workspace, "diff", "--no-color", start, check=False)
    return {"git": True, "branch": branch.strip(), "base": base, "files": files, "diff": diff[:200_000], "truncated": len(diff) > 200_000}


def branch_name(root: Ticket) -> str:
    return f"t/{root.key.lower()}-{_slug(root.title)}"


def _slug(text: str, limit: int = 24) -> str:
    ascii_text = unicodedata.normalize("NFD", text.lower().replace("đ", "d")).encode("ascii", "ignore").decode()
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_text).strip("-")
    return slug[:limit].rstrip("-") or "work"


async def _git(repo: Path, *args: str, check: bool = True) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(repo), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    out, _ = await proc.communicate()
    text = out.decode("utf-8", "replace")
    if check and proc.returncode != 0:
        raise WorkspaceError(text)
    return proc.returncode or 0, text
