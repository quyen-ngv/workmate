"""Where the code to index comes from: a git repository at one commit (a local project folder or a mirror), or a
plain folder. The index always reads committed content, so a ticket's unfinished changes never reach it.
"""

from __future__ import annotations

import base64
import hashlib
import os
import subprocess
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path

from actoroffice.ports.code import CloneAuth

BATCH = 200
SKIP_DIRS = {".git", "node_modules", "__pycache__", ".venv", "venv", ".idea", ".gradle", ".mvn", ".ruff_cache", ".pytest_cache", ".tox"}


class SyncError(Exception):
    pass


@dataclass(frozen=True)
class SourceFile:
    path: str
    blob_sha: str
    size: int


def _git(repo: Path, *args: str, data: bytes | None = None, env: dict[str, str] | None = None, timeout: float = 600) -> bytes:
    try:
        done = subprocess.run(
            ["git", "-C", str(repo), *args], input=data, capture_output=True, timeout=timeout, env={**os.environ, **(env or {})}, check=False
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise SyncError(f"git {args[0]}: {exc}") from exc
    if done.returncode != 0:
        raise SyncError(f"git {args[0]}: {done.stderr.decode('utf-8', 'replace').strip()[:300]}")
    return done.stdout


class GitSource:
    """A git repository (bare or not) read at `ref`."""

    def __init__(self, repo: Path, ref: str = "HEAD") -> None:
        self.repo = repo
        self.ref = ref
        self._commit: str | None = None

    def commit(self) -> str:
        if self._commit is None:
            self._commit = _git(self.repo, "rev-parse", "--verify", f"{self.ref}^{{commit}}").decode().strip()
        return self._commit

    def files(self) -> list[SourceFile]:
        out = _git(self.repo, "ls-tree", "-r", "-l", "-z", self.commit())
        found: list[SourceFile] = []
        for entry in out.split(b"\0"):
            if not entry:
                continue
            head, _, path = entry.partition(b"\t")
            mode, kind, sha, size = head.split()
            if kind != b"blob" or mode == b"120000":  # no submodules, no symlinks
                continue
            found.append(SourceFile(path.decode("utf-8", "replace"), sha.decode(), int(size) if size != b"-" else 0))
        return found

    def read_many(self, files: list[SourceFile]) -> Iterator[tuple[SourceFile, bytes]]:
        for i in range(0, len(files), BATCH):
            batch = files[i : i + BATCH]
            out = _git(self.repo, "cat-file", "--batch", data="".join(f"{f.blob_sha}\n" for f in batch).encode())
            pos = 0
            for f in batch:
                end = out.index(b"\n", pos)
                size = int(out[pos:end].split()[2])
                yield f, out[end + 1 : end + 1 + size]
                pos = end + 1 + size + 1

    def read(self, path: str, at: str | None = None) -> bytes:
        return _git(self.repo, "show", f"{at or self.commit()}:{path}")

    def exists(self, path: str, at: str | None = None) -> bool:
        try:
            _git(self.repo, "cat-file", "-e", f"{at or self.commit()}:{path}")
        except SyncError:
            return False
        return True


class FolderSource:
    """A folder that is not a git repository: its "commit" is a hash of the file list, so any change re-indexes."""

    def __init__(self, root: Path) -> None:
        self.root = root.resolve()
        self._files: list[SourceFile] | None = None

    def files(self) -> list[SourceFile]:
        if self._files is None:
            found: list[SourceFile] = []
            for base, dirs, names in os.walk(self.root):
                dirs[:] = sorted(d for d in dirs if d not in SKIP_DIRS)
                for name in sorted(names):
                    full = Path(base) / name
                    try:
                        stat = full.stat()
                    except OSError:
                        continue
                    stamp = hashlib.sha1(f"{stat.st_size}:{stat.st_mtime_ns}".encode()).hexdigest()
                    found.append(SourceFile(full.relative_to(self.root).as_posix(), stamp, stat.st_size))
            self._files = found
        return self._files

    def commit(self) -> str:
        return "dir-" + hashlib.sha1("\n".join(f"{f.path}:{f.blob_sha}" for f in self.files()).encode()).hexdigest()[:16]

    def read_many(self, files: list[SourceFile]) -> Iterator[tuple[SourceFile, bytes]]:
        for f in files:
            try:
                yield f, (self.root / f.path).read_bytes()
            except OSError:
                yield f, b""

    def read(self, path: str, at: str | None = None) -> bytes:
        target = (self.root / path).resolve()
        if not target.is_relative_to(self.root) or not target.is_file():
            raise SyncError(f"không có file {path}")
        return target.read_bytes()

    def exists(self, path: str, at: str | None = None) -> bool:
        target = (self.root / path).resolve()
        return target.is_relative_to(self.root) and target.is_file()


Source = GitSource | FolderSource


def is_git(path: Path) -> bool:
    try:
        _git(path, "rev-parse", "--git-dir", timeout=20)
    except SyncError:
        return False
    return True


def local_source(path: Path, branch: str = "main") -> Source:
    """A project folder: its main branch when it is a git repository (HEAD when that branch does not exist)."""
    if not is_git(path):
        return FolderSource(path)
    for ref in (branch, "HEAD"):
        source = GitSource(path, ref)
        try:
            source.commit()
        except SyncError:
            continue
        return source
    return FolderSource(path)  # a repository without any commit yet


def auth_env(auth: CloneAuth | None) -> dict[str, str]:
    """Credentials for one git process, through its environment: never in argv, never in .git/config."""
    if auth is None or not auth.password:
        return {"GIT_TERMINAL_PROMPT": "0"}
    token = base64.b64encode(f"{auth.username}:{auth.password}".encode()).decode()
    return {
        "GIT_TERMINAL_PROMPT": "0",
        "GIT_CONFIG_COUNT": "1",
        "GIT_CONFIG_KEY_0": "http.extraHeader",
        "GIT_CONFIG_VALUE_0": f"Authorization: Basic {token}",
    }


def mirror(url: str, dest: Path, branch: str = "main", auth: CloneAuth | None = None) -> GitSource:
    """A bare mirror of a remote repository, cloned once and fetched afterwards. Read-only, apart from the worktrees
    tickets work in."""
    env = auth_env(auth)
    if (dest / "HEAD").is_file():
        _git(dest, "fetch", "--prune", "--quiet", "origin", "+refs/heads/*:refs/heads/*", env=env)
    else:
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            done = subprocess.run(
                ["git", "clone", "--mirror", "--quiet", url, str(dest)], capture_output=True, timeout=1800, env={**os.environ, **env}, check=False
            )
        except (OSError, subprocess.TimeoutExpired) as exc:
            raise SyncError(f"git clone: {exc}") from exc
        if done.returncode != 0:
            raise SyncError(f"git clone: {done.stderr.decode('utf-8', 'replace').strip()[:300]}")
    for ref in (branch, "HEAD"):
        source = GitSource(dest, ref)
        try:
            source.commit()
        except SyncError:
            continue
        return source
    raise SyncError("kho mã chưa có commit nào")
