"""Templates that make test setup cheap (SI-19).

Each test process builds a migrated SQLite file and committed git repos once, with the real code (`Database.open`,
`git init/add/commit`), and copies them into a test's tmp_path. A copy behaves like a freshly created one: the DB has
every migration recorded (so `_migrate` skips them) and the repo has the same tree, branch and a clean status.
"""

from __future__ import annotations

import asyncio
import shutil
import subprocess
import threading
from pathlib import Path

from actoroffice.store import Database

IDENT = ["-c", "user.name=t", "-c", "user.email=t@t", "-c", "commit.gpgsign=false"]

templates_dir: Path | None = None  # set once per process by the session fixture in conftest.py
_lock = threading.Lock()
_db: Path | None = None
_repos: dict[tuple, Path] = {}


def _root() -> Path:
    if templates_dir is None:
        raise RuntimeError("tests/conftest.py did not set seed.templates_dir")
    return templates_dir


def _run(coro_fn) -> None:
    """Run a coroutine to completion from sync code, also while a test's event loop is running."""
    errors: list[BaseException] = []

    def target() -> None:
        try:
            asyncio.run(coro_fn())
        except BaseException as e:  # re-raised in the caller's thread
            errors.append(e)

    thread = threading.Thread(target=target)
    thread.start()
    thread.join()
    if errors:
        raise errors[0]


def template_db() -> Path:
    """The migrated DB template of this process, built by `Database.open` and closed (WAL checkpointed)."""
    global _db
    with _lock:
        if _db is None:
            path = _root() / "template.db"

            async def build() -> None:
                db = Database(path)
                await db.open()
                await db.close()

            _run(build)
            _db = path
        return _db


def seed_db(path: Path | str) -> Path:
    """Put a copy of the migrated DB template at `path` (the file a `Database`/`Office` then opens). An existing DB
    is kept, like `Database.open` keeps it, so reopening an office (a second lifespan, a restart) sees its data."""
    target = Path(path)
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(template_db(), target)
    return target


def git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(cwd), *args], capture_output=True, text=True, check=True).stdout.strip()


def build_repo(path: Path, files: dict[str, str] | None = None, message: str = "init") -> Path:
    """A fresh repo on `main` with `files` committed in one commit (an empty commit when there are none)."""
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    for name, text in (files or {}).items():
        (path / name).parent.mkdir(parents=True, exist_ok=True)
        (path / name).write_text(text, encoding="utf-8")
    if files:
        git(path, "add", "-A")
    git(path, *IDENT, "commit", "-q", "--allow-empty", "-m", message)
    return path


def template_repo(files: dict[str, str] | None = None, message: str = "init") -> Path:
    key = (tuple(sorted((files or {}).items())), message)
    with _lock:
        if key not in _repos:
            _repos[key] = build_repo(_root() / f"repo{len(_repos)}", files, message)
        return _repos[key]


def copy_repo(dest: Path, files: dict[str, str] | None = None, message: str = "init") -> Path:
    """Copy of the template repo with `files` committed: same as `build_repo(dest, files, message)`, without git calls."""
    shutil.copytree(template_repo(files, message), dest)
    return dest
