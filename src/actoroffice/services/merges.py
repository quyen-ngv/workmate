"""Merging a finished ticket tree's branch into the project's main branch: an explicit, user-approved step.

Checks before merging: the tree's worktree has no uncommitted changes, the project folder is clean and on its
main branch, and (optionally) the project's test command passes in the worktree. A conflicting merge is aborted.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import Conflict, OfficeError
from actoroffice.domain.work import ProjectAccess
from actoroffice.engine.workspace import branch_name
from actoroffice.services.automations import kill_tree

if TYPE_CHECKING:
    from actoroffice.app import Office


class MergeError(OfficeError):
    code = 409


class MergeService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self._locks: dict[str, asyncio.Lock] = {}

    async def status(self, key: str) -> dict[str, Any]:
        """What a merge of this ticket's tree would do (shown on the ticket page)."""
        office = self.office
        root = await office.tickets.root_of(key)
        project = await office.projects.get(root.project_id) if root.project_id else None
        if not project or project.access != ProjectAccess.WORKTREE or not project.path:
            return {"available": False}
        branch = branch_name(root)
        repo = Path(project.path)
        exists = (await _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"))[0] == 0
        if not exists:
            return {"available": False}
        ahead = (await _git(repo, "rev-list", "--count", f"{project.main_branch}..{branch}"))[1].strip()
        merged = (await _git(repo, "merge-base", "--is-ancestor", branch, project.main_branch))[0] == 0
        return {"available": True, "branch": branch, "main": project.main_branch, "ahead": int(ahead or 0), "merged": merged,
                "root": root.key}

    async def merge(self, key: str, *, by: str = "user") -> dict[str, Any]:
        office = self.office
        root = await office.tickets.root_of(key)
        project = await office.projects.get(root.project_id) if root.project_id else None
        if not project or project.access != ProjectAccess.WORKTREE or not project.path:
            raise MergeError("Chỉ dự án chế độ git worktree mới có bước merge")
        repo = Path(project.path)
        branch = branch_name(root)
        worktree = office.settings.workspaces_dir / root.key
        lock = self._locks.setdefault(str(repo), asyncio.Lock())
        if lock.locked():
            raise Conflict("Đang có một merge khác trên dự án này")
        async with lock:
            if any(r for r in await office.runs.for_tree_running(root.id)):
                raise MergeError("Còn nhân viên đang làm trên nhánh này; đợi họ xong rồi merge")
            if worktree.exists():
                dirty = (await _git(worktree, "status", "--porcelain"))[1].strip()
                if dirty:
                    raise MergeError(f"Nhánh {branch} còn thay đổi chưa commit:\n{dirty[:500]}")
            head = (await _git(repo, "rev-parse", "--abbrev-ref", "HEAD"))[1].strip()
            if head != project.main_branch:
                raise MergeError(f"Thư mục dự án đang ở nhánh {head}, không phải {project.main_branch}")
            dirty_main = (await _git(repo, "status", "--porcelain", "--untracked-files=no"))[1].strip()
            if dirty_main:
                raise MergeError(f"Thư mục dự án có thay đổi chưa commit:\n{dirty_main[:500]}")
            # Pin the commit now: what was checked (and tested) is exactly what gets merged.
            tip = (await _git(repo, "rev-parse", f"refs/heads/{branch}"))[1].strip()
            ahead = (await _git(repo, "rev-list", "--count", f"{project.main_branch}..{tip}"))[1].strip()
            if not ahead.isdigit() or int(ahead) == 0:
                raise MergeError(f"Nhánh {branch} không có gì mới so với {project.main_branch}")
            if project.test_command and worktree.exists():
                code, out = await _shell(project.test_command, worktree)
                if code != 0:
                    raise MergeError(f"Test chưa pass ({project.test_command}):\n{out[-1500:]}")
            if await office.runs.for_tree_running(root.id):
                raise MergeError("Có nhân viên vừa bắt đầu làm trên nhánh này; merge lại sau")
            message = f"Merge {root.key}: {root.title}"
            code, out = await _git(repo, "merge", "--no-ff", "-m", message, tip)
            if code != 0:
                await _git(repo, "merge", "--abort")
                raise MergeError(f"Merge bị xung đột, đã huỷ merge. Cần xử lý trên nhánh {branch}:\n{out[-1500:]}")
            commit = (await _git(repo, "rev-parse", "--short", "HEAD"))[1].strip()
        await office.tickets.add_comment(root.id, f"Đã merge {branch} vào {project.main_branch} ({commit}) bởi {by}.",
                                         author_kind="system", author_id=None)
        await office.tickets.add_work_product(root.id, "merge", commit, {"note": f"{branch} → {project.main_branch}"})
        await office.audit.record("git:merge", "merged", target=f"{project.name}:{branch}", detail={"commit": commit, "by": by})
        office.bus.publish("ticket.document", ticket_id=root.id, key="merge", revision=0)
        return {"merged": True, "commit": commit, "branch": branch, "main": project.main_branch}


def _env(cwd: Path) -> dict[str, str]:
    return {**os.environ, "GIT_CEILING_DIRECTORIES": str(cwd.parent)}


async def _git(cwd: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_env(cwd)
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        kill_tree(proc.pid)
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def _shell(command: str, cwd: Path, timeout: float = 900) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        command, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_env(cwd)
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        kill_tree(proc.pid)  # the shell and the test runner it started
        return 124, "timeout"
    except asyncio.CancelledError:
        kill_tree(proc.pid)
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace")
