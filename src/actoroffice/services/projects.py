"""Projects: a folder plus its own chats, groups and tickets. Staff is shared across projects."""

from __future__ import annotations

import asyncio
import os
import subprocess
from pathlib import Path
from typing import Any

from actoroffice.core.events import EventBus
from actoroffice.domain.errors import Conflict, NotFound, OfficeError
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.work import Project, ProjectAccess
from actoroffice.store import Database

DEFAULT_NAME = "Chung"


class ProjectError(OfficeError):
    code = 422


class ProjectService:
    def __init__(self, db: Database, bus: EventBus, *, forbidden: list[Path]) -> None:
        self.db = db
        self.bus = bus
        self.forbidden = [p.resolve() for p in forbidden]

    async def ensure_default(self) -> Project:
        row = await self.db.fetchone("SELECT config_json FROM projects WHERE json_extract(config_json, '$.is_default') = 1")
        if row:
            project = Project.model_validate_json(row["config_json"])
        else:
            project = Project(id=new_id("prj"), name=DEFAULT_NAME, description="Việc chung, không gắn thư mục", is_default=True)
            await self._insert(project)
        # Data created before projects existed belongs to the default project.
        await self.db.execute("UPDATE conversations SET project_id = ? WHERE project_id IS NULL", (project.id,))
        await self.db.execute("UPDATE tickets SET project_id = ? WHERE project_id IS NULL", (project.id,))
        return project

    async def create(self, name: str, *, path: str | None, access: ProjectAccess, description: str = "") -> Project:
        name = name.strip()
        if not name:
            raise ProjectError("Cần tên dự án")
        if await self.db.fetchone("SELECT 1 FROM projects WHERE name = ?", (name,)):
            raise Conflict(f"Dự án '{name}' đã tồn tại")
        checked = await asyncio.to_thread(self._validated, path, access)  # may run git: keep the event loop free
        project = Project(id=new_id("prj"), name=name, description=description.strip(), **checked)
        await self._insert(project)
        self.bus.publish("project.changed", project=project.model_dump())
        return project

    async def update(self, project_id: str, *, name: str, path: str | None, access: ProjectAccess, description: str = "") -> Project:
        current = await self.get(project_id)
        checked = await asyncio.to_thread(self._validated, path, access)
        updated = current.model_copy(update={"name": name.strip() or current.name, "description": description.strip(), **checked})
        await self.db.execute(
            "UPDATE projects SET name = ?, config_json = ? WHERE id = ?", (updated.name, updated.model_dump_json(), project_id)
        )
        self.bus.publish("project.changed", project=updated.model_dump())
        return updated

    async def configure(
        self,
        project_id: str,
        *,
        team_id: str | None = None,
        kind: str | None = None,
        repo_url: str | None = None,
        profile: dict | None = None,
        test_command: str | None = None,
    ) -> Project:
        """Team copilot fields: which team owns the project, code or reference repo, its build/test profile."""
        current = await self.get(project_id)
        if kind is not None and kind not in ("code", "reference", "automation"):
            raise ProjectError("kind: code | reference | automation")
        updates = {k: v for k, v in {"team_id": team_id, "kind": kind, "repo_url": repo_url, "profile": profile, "test_command": test_command}.items() if v is not None}
        updated = current.model_copy(update=updates)
        await self.db.execute("UPDATE projects SET config_json = ? WHERE id = ?", (updated.model_dump_json(), project_id))
        self.bus.publish("project.changed", project=updated.model_dump())
        return updated

    async def set_permissions(self, project_id: str, *, mode: str | None = None, allow_rules: list[str] | None = None) -> Project:
        """The project's permission mode and its "always allow" rules."""
        from actoroffice.security.policy import MODES

        current = await self.get(project_id)
        updates: dict[str, Any] = {}
        if mode is not None:
            if mode not in MODES:
                raise ProjectError(f"permission mode: {' | '.join(MODES)}")
            updates["permission_mode"] = mode
        if allow_rules is not None:
            updates["allow_rules"] = list(dict.fromkeys(r.strip() for r in allow_rules if r.strip()))
        updated = current.model_copy(update=updates)
        await self.db.execute("UPDATE projects SET config_json = ? WHERE id = ?", (updated.model_dump_json(), project_id))
        self.bus.publish("project.changed", project=updated.model_dump())
        return updated

    async def set_one_at_a_time(self, project_id: str, enabled: bool) -> Project:
        """The project's "Chạy từng ticket một" option (SI-31). Only stores it: the caller releases the queue."""
        updated = (await self.get(project_id)).model_copy(update={"one_at_a_time": enabled})
        await self.db.execute("UPDATE projects SET config_json = ? WHERE id = ?", (updated.model_dump_json(), project_id))
        self.bus.publish("project.changed", project=updated.model_dump())
        return updated

    async def find(self, ref: str) -> Project | None:
        for project in await self.list():
            if ref in (project.id, project.name):
                return project
        return None

    async def get(self, project_id: str) -> Project:
        row = await self.db.fetchone("SELECT config_json FROM projects WHERE id = ?", (project_id,))
        if not row:
            raise NotFound(f"Project {project_id} not found")
        return Project.model_validate_json(row["config_json"])

    async def list(self) -> list[Project]:
        rows = await self.db.fetchall("SELECT config_json FROM projects ORDER BY created_at")
        return [Project.model_validate_json(r["config_json"]) for r in rows]

    async def _insert(self, project: Project) -> None:
        await self.db.execute(
            "INSERT INTO projects(id, name, config_json, created_at) VALUES (?, ?, ?, ?)",
            (project.id, project.name, project.model_dump_json(), utcnow()),
        )

    def _validated(self, path: str | None, access: ProjectAccess) -> dict:
        if not path or not path.strip():
            if access != ProjectAccess.READ_ONLY:
                raise ProjectError("Chế độ worktree/direct cần một thư mục")
            return {"path": None, "access": access}
        folder = Path(path.strip().strip('"')).expanduser().resolve()
        if folder.parent == folder or folder == Path.home().resolve():
            raise ProjectError("Không dùng gốc ổ đĩa hay thư mục home: chọn đúng thư mục dự án")
        for bad in self.forbidden:
            if folder == bad or bad in folder.parents or folder in bad.parents:
                raise ProjectError("Thư mục này chứa dữ liệu nhạy cảm hoặc dữ liệu của văn phòng")
        if not folder.exists() and access != ProjectAccess.READ_ONLY and Path(folder.anchor).is_dir():
            folder.mkdir(parents=True)  # a new project: the user named a folder that does not exist yet
        if not folder.is_dir():
            raise ProjectError(f"Không tìm thấy thư mục {folder}")
        result = {"path": str(folder), "access": access}
        if access == ProjectAccess.WORKTREE:
            branch = git_branch(folder)
            if branch is None and not any(folder.iterdir()):
                _git_init(folder)  # an empty folder becomes a fresh repo with one empty commit on main
                branch = git_branch(folder)
            if branch is None:
                raise ProjectError("Chế độ worktree cần một git repository (hoặc chọn 'direct' / 'read_only')")
            result["main_branch"] = branch
        return result


def _git_init(folder: Path) -> None:
    env = {**os.environ, "GIT_CEILING_DIRECTORIES": str(folder.parent)}
    identity = ["-c", "user.name=actor-office", "-c", "user.email=office@localhost", "-c", "commit.gpgsign=false"]
    for args in (["init", "-q", "-b", "main"], [*identity, "commit", "-q", "--allow-empty", "-m", "Khởi tạo dự án"]):
        done = subprocess.run(["git", "-C", str(folder), *args], capture_output=True, text=True, timeout=30, env=env)
        if done.returncode != 0:
            raise ProjectError(f"git init thất bại: {done.stderr.strip()}")


def git_branch(folder: Path) -> str | None:
    """Current branch, only if `folder` is the root of a git repo (not just somewhere inside one —
    e.g. a home directory that happens to be a repo must not make every sub-folder look like one)."""
    try:
        top = subprocess.run(["git", "-C", str(folder), "rev-parse", "--show-toplevel"], capture_output=True, text=True, timeout=10)
        if top.returncode != 0 or Path(top.stdout.strip()).resolve() != folder:
            return None
        out = subprocess.run(
            ["git", "-C", str(folder), "rev-parse", "--abbrev-ref", "HEAD"], capture_output=True, text=True, timeout=10
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    branch = out.stdout.strip()
    return branch if out.returncode == 0 and branch and branch != "HEAD" else None
