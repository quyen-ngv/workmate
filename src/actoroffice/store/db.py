"""Single-connection SQLite store (WAL).

One connection guarded by one asyncio lock keeps things simple and correct for a
single-process app: every statement is serialized, and `tx()` gives an atomic
BEGIN IMMEDIATE … COMMIT block. Never call `Database` methods while holding a
`Tx` (the lock is not re-entrant) — use the `Tx` methods instead.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from importlib import resources
from pathlib import Path
from typing import Any

import aiosqlite

Params = Sequence[Any] | dict[str, Any]


class Tx:
    def __init__(self, conn: aiosqlite.Connection) -> None:
        self._conn = conn

    async def execute(self, sql: str, params: Params = ()) -> aiosqlite.Cursor:
        return await self._conn.execute(sql, params)

    async def fetchone(self, sql: str, params: Params = ()) -> dict[str, Any] | None:
        async with self._conn.execute(sql, params) as cur:
            row = await cur.fetchone()
        return dict(row) if row is not None else None

    async def fetchall(self, sql: str, params: Params = ()) -> list[dict[str, Any]]:
        async with self._conn.execute(sql, params) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]


class Database:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        self._conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        if self._conn is not None:
            return
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = await aiosqlite.connect(self.path, isolation_level=None)
        self._conn.row_factory = aiosqlite.Row
        for pragma in ("journal_mode=WAL", "foreign_keys=ON", "busy_timeout=5000", "synchronous=NORMAL"):
            await self._conn.execute(f"PRAGMA {pragma}")
        await self._migrate()

    async def close(self) -> None:
        if self._conn is not None:
            await self._conn.close()
            self._conn = None

    @property
    def conn(self) -> aiosqlite.Connection:
        if self._conn is None:
            raise RuntimeError("Database is not open")
        return self._conn

    @asynccontextmanager
    async def tx(self) -> AsyncIterator[Tx]:
        async with self._lock:
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                yield Tx(self.conn)
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            await self.conn.execute("COMMIT")

    async def execute(self, sql: str, params: Params = ()) -> aiosqlite.Cursor:
        async with self._lock:
            return await self.conn.execute(sql, params)

    async def fetchone(self, sql: str, params: Params = ()) -> dict[str, Any] | None:
        async with self._lock:
            return await Tx(self.conn).fetchone(sql, params)

    async def fetchall(self, sql: str, params: Params = ()) -> list[dict[str, Any]]:
        async with self._lock:
            return await Tx(self.conn).fetchall(sql, params)

    async def _migrate(self) -> None:
        await self.conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_migrations (version TEXT PRIMARY KEY, applied_at TEXT NOT NULL)"
        )
        async with self.conn.execute("SELECT version FROM schema_migrations") as cur:
            applied = {row[0] for row in await cur.fetchall()}
        files = sorted(
            (f for f in resources.files("actoroffice.store.migrations").iterdir() if f.name.endswith(".sql")),
            key=lambda f: f.name,
        )
        for f in files:
            version = f.name.removesuffix(".sql")
            if version in applied:
                continue
            await self.conn.execute("BEGIN IMMEDIATE")
            try:
                for statement in _split_sql(f.read_text(encoding="utf-8")):
                    await self.conn.execute(statement)
                await self.conn.execute(
                    "INSERT INTO schema_migrations(version, applied_at) VALUES (?, datetime('now'))", (version,)
                )
            except BaseException:
                await self.conn.execute("ROLLBACK")
                raise
            await self.conn.execute("COMMIT")


def _split_sql(script: str) -> list[str]:
    """Split a migration file on ';' at line ends. Migrations must not use ';' inside strings or triggers."""
    statements: list[str] = []
    buf: list[str] = []
    for line in script.splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("--"):
            continue
        buf.append(line)
        if stripped.endswith(";"):
            statements.append("\n".join(buf))
            buf = []
    if buf:
        statements.append("\n".join(buf))
    return statements
