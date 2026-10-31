"""Skills as files (portable SKILL.md format), versioned with git.

    <data>/skills/shared/<skill>/SKILL.md
    <data>/skills/positions/<position>/<skill>/SKILL.md
    <data>/skills/actors/<actor>/<skill>/SKILL.md   (+ sub-skill / reference files next to SKILL.md)

An actor sees shared + its position's + its own skills; a closer scope overrides a same-named skill.
Actors read skills through office tools (server side), so the files never need to be in a workspace.
"""

from __future__ import annotations

import asyncio
import logging
import re
import shutil
from pathlib import Path

import yaml

from actoroffice.core.events import EventBus
from actoroffice.domain.errors import NotFound, OfficeError
from actoroffice.domain.knowledge import Skill, SkillRef, SkillScope
from actoroffice.domain.org import Actor, Position

log = logging.getLogger(__name__)

NAME = re.compile(r"^[a-z0-9][a-z0-9-]{1,48}$")
FILE = re.compile(r"^[a-zA-Z0-9][a-zA-Z0-9_\-./]{0,80}\.(md|txt|py|json|ya?ml|csv)$")
MAX_BYTES = 100_000


class SkillError(OfficeError):
    code = 422


def slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")[:48]


class SkillService:
    def __init__(self, root: Path, bus: EventBus) -> None:
        self.root = root
        self.bus = bus
        self._git_ready: bool | None = None
        self._lock = asyncio.Lock()

    # ---- paths ----------------------------------------------------------------------------

    def _dir(self, ref: SkillRef) -> Path:
        if not NAME.match(ref.name):
            raise SkillError("Tên skill: chữ thường, số và dấu '-' (2–49 ký tự)")
        if ref.scope == SkillScope.SHARED:
            base = self.root / "shared"
        else:
            if not ref.owner or not re.match(r"^[\w\-]+$", ref.owner, re.UNICODE):
                raise SkillError("Thiếu chủ sở hữu skill")
            base = self.root / ("positions" if ref.scope == SkillScope.POSITION else "actors") / ref.owner
        return base / ref.name

    @staticmethod
    def refs_for(actor: Actor, position: Position) -> list[tuple[SkillScope, str]]:
        return [(SkillScope.SHARED, ""), (SkillScope.POSITION, position.name), (SkillScope.ACTOR, actor.name)]

    # ---- read -----------------------------------------------------------------------------

    def list(self, scope: SkillScope | None = None, owner: str | None = None) -> list[Skill]:
        out: list[Skill] = []
        for sc, base in (
            (SkillScope.SHARED, self.root / "shared"),
            (SkillScope.POSITION, self.root / "positions"),
            (SkillScope.ACTOR, self.root / "actors"),
        ):
            if scope and sc != scope:
                continue
            if not base.exists():
                continue
            owners = [("", base)] if sc == SkillScope.SHARED else [(d.name, d) for d in sorted(base.iterdir()) if d.is_dir()]
            for own, folder in owners:
                if owner is not None and sc != SkillScope.SHARED and own != owner:
                    continue
                for skill_dir in sorted(folder.iterdir()):
                    if (skill_dir / "SKILL.md").exists():
                        out.append(self._load(SkillRef(scope=sc, owner=own, name=skill_dir.name), body=False))
        return out

    def for_actor(self, actor: Actor, position: Position) -> list[Skill]:
        merged: dict[str, Skill] = {}
        for scope, owner in self.refs_for(actor, position):
            for skill in self.list(scope, owner):
                merged[skill.ref.name] = skill  # closer scope wins
        return sorted(merged.values(), key=lambda s: s.ref.name)

    def resolve(self, actor: Actor, position: Position, name: str) -> Skill:
        for scope, owner in reversed(self.refs_for(actor, position)):
            ref = SkillRef(scope=scope, owner=owner, name=name)
            if (self._dir(ref) / "SKILL.md").exists():
                return self._load(ref)
        raise NotFound(f"Skill '{name}' not found")

    def exists(self, ref: SkillRef) -> bool:
        return (self._dir(ref) / "SKILL.md").exists()

    def get(self, ref: SkillRef) -> Skill:
        if not (self._dir(ref) / "SKILL.md").exists():
            raise NotFound(f"Skill '{ref.name}' not found")
        return self._load(ref)

    def read_file(self, ref: SkillRef, path: str) -> str:
        target = self._file(ref, path)
        if not target.exists():
            raise NotFound(f"File '{path}' not found in skill '{ref.name}'")
        return target.read_text(encoding="utf-8")

    def _load(self, ref: SkillRef, *, body: bool = True) -> Skill:
        folder = self._dir(ref)
        meta, text = split_front_matter((folder / "SKILL.md").read_text(encoding="utf-8"))
        files = sorted(
            str(p.relative_to(folder)).replace("\\", "/") for p in folder.rglob("*") if p.is_file() and p.name != "SKILL.md"
        )
        stat = (folder / "SKILL.md").stat()
        from datetime import UTC, datetime

        return Skill(
            ref=ref,
            description=str(meta.get("description", "")),
            body=text if body else "",
            files=files,
            created_by=str((meta.get("metadata") or {}).get("created_by", "")),
            updated_at=datetime.fromtimestamp(stat.st_mtime, UTC).isoformat(timespec="seconds"),
        )

    def _file(self, ref: SkillRef, path: str) -> Path:
        if not FILE.match(path) or ".." in path.split("/") or path == "SKILL.md":
            raise SkillError("Tên file không hợp lệ (cho phép .md .txt .py .json .yaml .csv, không có '..')")
        folder = self._dir(ref).resolve()
        target = (folder / path).resolve()
        if folder not in target.parents:
            raise SkillError("File phải nằm trong thư mục skill")
        return target

    # ---- write ----------------------------------------------------------------------------

    async def save(
        self,
        ref: SkillRef,
        description: str,
        body: str,
        *,
        author: str,
        context: dict[str, object] | None = None,
        commit: bool = True,
    ) -> Skill:
        description = " ".join(description.split())[:300]
        if not description:
            raise SkillError("Cần mô tả: skill dùng khi nào")
        if len(body.encode()) > MAX_BYTES:
            raise SkillError("Skill quá dài")
        folder = self._dir(ref)
        existing = folder / "SKILL.md"
        created_by = author
        if existing.exists():
            meta, _ = split_front_matter(existing.read_text(encoding="utf-8"))
            created_by = str((meta.get("metadata") or {}).get("created_by") or author)
        front = {"name": ref.name, "description": description, "metadata": {"created_by": created_by, "updated_by": author}}
        async with self._lock:
            folder.mkdir(parents=True, exist_ok=True)
            existing.write_text(
                "---\n" + yaml.safe_dump(front, allow_unicode=True, sort_keys=False) + "---\n\n" + body.strip() + "\n",
                encoding="utf-8",
            )
            if commit:
                await self._commit(f"{author}: save skill {ref.scope.value}/{ref.owner or '-'}/{ref.name}", author)
        skill = self._load(ref)
        # context: where it happened (conversation_id / ticket_id), so the notice shows up there
        self.bus.publish("skill.saved", skill=skill.model_dump(), author=author, **(context or {}))
        return skill

    async def save_file(self, ref: SkillRef, path: str, content: str, *, author: str, commit: bool = True) -> Skill:
        if not (self._dir(ref) / "SKILL.md").exists():
            raise NotFound(f"Skill '{ref.name}' not found")
        if len(content.encode()) > MAX_BYTES:
            raise SkillError("File quá lớn")
        target = self._file(ref, path)
        async with self._lock:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding="utf-8")
            if commit:
                await self._commit(f"{author}: save {ref.name}/{path}", author)
        return self._load(ref)

    async def delete(self, ref: SkillRef, *, author: str, path: str | None = None) -> None:
        async with self._lock:
            if path:
                self._file(ref, path).unlink(missing_ok=True)
            else:
                folder = self._dir(ref)
                if not folder.exists():
                    raise NotFound(f"Skill '{ref.name}' not found")
                shutil.rmtree(folder)
            await self._commit(f"{author}: delete {ref.name}{'/' + path if path else ''}", author)
        self.bus.publish("skill.deleted", ref=ref.model_dump(), author=author)

    async def rename_owner(self, scope: SkillScope, old: str, new: str, *, author: str) -> bool:
        """An actor (or position) was renamed: its own skills move to the folder of the new name."""
        base = self.root / ("positions" if scope == SkillScope.POSITION else "actors")
        src, dst = base / old, base / new
        if not src.is_dir():
            return False
        async with self._lock:
            if dst.exists() and not src.samefile(dst):  # same folder when only the letter case changes
                raise SkillError(f"Đã có thư mục skill của '{new}'")
            src.rename(dst)
            await self._commit(f"{author}: rename {old} → {new}", author)
        return True

    async def commit(self, message: str, author: str) -> None:
        async with self._lock:
            await self._commit(message, author)

    async def _commit(self, message: str, author: str) -> None:
        """Best effort: every change is a git commit so the user can diff and revert."""
        if self._git_ready is None:
            self._git_ready = shutil.which("git") is not None
            if self._git_ready and not (self.root / ".git").exists():
                self.root.mkdir(parents=True, exist_ok=True)
                self._git_ready = (await _git(self.root, "init", "-q"))[0] == 0
        if not self._git_ready:
            return
        await _git(self.root, "add", "-A")
        name = re.sub(r"[^\w .-]", "", author) or "office"
        await _git(
            self.root, "-c", f"user.name={name}", "-c", "user.email=office@actoroffice.local", "commit", "-q", "-m", message
        )


def split_front_matter(text: str) -> tuple[dict, str]:
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) == 3:
            try:
                meta = yaml.safe_load(parts[1]) or {}
            except yaml.YAMLError:
                meta = {}
            return (meta if isinstance(meta, dict) else {}), parts[2].strip()
    return {}, text.strip()


async def _git(cwd: Path, *args: str) -> tuple[int, str]:
    try:
        proc = await asyncio.create_subprocess_exec(
            "git", *args, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
        out, _ = await proc.communicate()
    except OSError as exc:
        log.warning("git unavailable for skills: %s", exc)
        return 1, str(exc)
    return proc.returncode or 0, out.decode("utf-8", "replace")
