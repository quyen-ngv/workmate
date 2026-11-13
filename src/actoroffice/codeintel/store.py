"""The index file (<data_dir>/index.db): repositories, files, text chunks (FTS5 trigram), graph nodes and edges.

It is rebuilt from the sources at any time, so there are no migrations: a file with another SCHEMA_VERSION is
dropped and built again. Plain sqlite3 (blocking); callers run it in a worker thread.
"""

from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

SCHEMA_VERSION = 1

SCHEMA = """
CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE repos (
  id INTEGER PRIMARY KEY, slug TEXT NOT NULL UNIQUE, connection_id TEXT, project_id TEXT, team_id TEXT, root TEXT NOT NULL DEFAULT '',
  default_branch TEXT NOT NULL DEFAULT 'main', indexed_commit TEXT, head_commit TEXT,
  state TEXT NOT NULL DEFAULT 'none', level TEXT NOT NULL DEFAULT 'text', stats_json TEXT NOT NULL DEFAULT '{}', error TEXT, updated_at TEXT);
CREATE TABLE files (
  id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL, path TEXT NOT NULL, blob_sha TEXT NOT NULL,
  lang TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, size INTEGER NOT NULL DEFAULT 0, lines INTEGER NOT NULL DEFAULT 0,
  UNIQUE (repo_id, path));
CREATE VIRTUAL TABLE chunks USING fts5(text, repo_id UNINDEXED, file_id UNINDEXED, path UNINDEXED,
  start_line UNINDEXED, end_line UNINDEXED, symbol UNINDEXED, tokenize = 'trigram');
CREATE TABLE nodes (
  id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL, kind TEXT NOT NULL, sub_kind TEXT NOT NULL DEFAULT '', name TEXT NOT NULL,
  qualified TEXT NOT NULL, file_id INTEGER, start_line INTEGER, end_line INTEGER, signature TEXT NOT NULL DEFAULT '',
  doc TEXT NOT NULL DEFAULT '', parent INTEGER, meta_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX idx_nodes_kind_name ON nodes (kind, name);
CREATE INDEX idx_nodes_file ON nodes (file_id);
CREATE INDEX idx_nodes_qualified ON nodes (qualified);
CREATE INDEX idx_nodes_repo_kind ON nodes (repo_id, kind);
CREATE TABLE edges (
  id INTEGER PRIMARY KEY, repo_id INTEGER NOT NULL, src INTEGER NOT NULL, dst INTEGER NOT NULL, kind TEXT NOT NULL,
  confidence TEXT NOT NULL, file_id INTEGER, line INTEGER, source TEXT NOT NULL, meta_json TEXT NOT NULL DEFAULT '{}');
CREATE INDEX idx_edges_src ON edges (src, kind);
CREATE INDEX idx_edges_dst ON edges (dst, kind);
CREATE INDEX idx_edges_file ON edges (file_id);
CREATE INDEX idx_edges_repo_source ON edges (repo_id, source);
-- What parsing found, kept per file so that only changed files are parsed again; edges are resolved from these.
CREATE TABLE raw_calls (file_id INTEGER NOT NULL, repo_id INTEGER NOT NULL, caller TEXT NOT NULL, name TEXT NOT NULL, line INTEGER NOT NULL);
CREATE INDEX idx_raw_calls_file ON raw_calls (file_id);
CREATE TABLE raw_imports (file_id INTEGER NOT NULL, repo_id INTEGER NOT NULL, module TEXT NOT NULL, names_json TEXT NOT NULL, alias_json TEXT NOT NULL, line INTEGER NOT NULL);
CREATE INDEX idx_raw_imports_file ON raw_imports (file_id);
CREATE TABLE raw_bases (file_id INTEGER NOT NULL, repo_id INTEGER NOT NULL, symbol TEXT NOT NULL, base TEXT NOT NULL, line INTEGER NOT NULL);
CREATE INDEX idx_raw_bases_file ON raw_bases (file_id);
CREATE TABLE raw_facts (file_id INTEGER NOT NULL, repo_id INTEGER NOT NULL, extractor TEXT NOT NULL, fact_json TEXT NOT NULL);
CREATE INDEX idx_raw_facts_file ON raw_facts (file_id);
"""


class IndexStore:
    def __init__(self, path: Path | str) -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = self._open()

    def _open(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=5000")
        has_meta = conn.execute("SELECT 1 FROM sqlite_master WHERE name = 'meta'").fetchone()
        version = conn.execute("SELECT value FROM meta WHERE key = 'schema'").fetchone() if has_meta else None
        if version is None or int(version[0]) != SCHEMA_VERSION:
            if has_meta and self.path != ":memory:":  # another layout: the index is only a cache, start over
                conn.close()
                for suffix in ("", "-wal", "-shm"):
                    Path(self.path + suffix).unlink(missing_ok=True)
                conn = sqlite3.connect(self.path, check_same_thread=False, isolation_level=None)
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA journal_mode=WAL")
            conn.executescript(SCHEMA)
            conn.execute("INSERT INTO meta(key, value) VALUES ('schema', ?)", (str(SCHEMA_VERSION),))
        return conn

    def close(self) -> None:
        self.conn.close()

    # ---- small helpers ---------------------------------------------------------------------------------------

    def one(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Row | None:
        return self.conn.execute(sql, params).fetchone()

    def all(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> list[sqlite3.Row]:
        return self.conn.execute(sql, params).fetchall()

    def run(self, sql: str, params: tuple[Any, ...] | list[Any] = ()) -> sqlite3.Cursor:
        return self.conn.execute(sql, params)

    def begin(self) -> None:
        self.conn.execute("BEGIN IMMEDIATE")

    def commit(self) -> None:
        self.conn.execute("COMMIT")

    def rollback(self) -> None:
        if self.conn.in_transaction:
            self.conn.execute("ROLLBACK")

    # ---- repositories ----------------------------------------------------------------------------------------

    def repo(self, slug: str) -> sqlite3.Row | None:
        return self.one("SELECT * FROM repos WHERE slug = ?", (slug,))

    def ensure_repo(self, slug: str, **fields: Any) -> int:
        row = self.repo(slug)
        if row is None:
            self.run("INSERT INTO repos(slug) VALUES (?)", (slug,))
            row = self.repo(slug)
        assert row is not None
        if fields:
            self.update_repo(int(row["id"]), **fields)
        return int(row["id"])

    def update_repo(self, repo_id: int, **fields: Any) -> None:
        if "stats" in fields:
            fields["stats_json"] = json.dumps(fields.pop("stats"), ensure_ascii=False)
        names = ", ".join(f"{k} = ?" for k in fields)
        self.run(f"UPDATE repos SET {names} WHERE id = ?", [*fields.values(), repo_id])

    def drop_repo(self, slug: str) -> bool:
        row = self.repo(slug)
        if row is None:
            return False
        repo_id = int(row["id"])
        self.begin()
        try:
            for table in ("chunks", "nodes", "edges", "raw_calls", "raw_imports", "raw_bases", "raw_facts", "files"):
                self.run(f"DELETE FROM {table} WHERE repo_id = ?", (repo_id,))
            self.run("DELETE FROM repos WHERE id = ?", (repo_id,))
            self.commit()
        except BaseException:
            self.rollback()
            raise
        return True

    def drop_file(self, file_id: int) -> None:
        """Everything derived from one file (inside the caller's transaction)."""
        self.run("DELETE FROM chunks WHERE file_id = ?", (file_id,))
        self.run("DELETE FROM edges WHERE file_id = ?", (file_id,))
        self.run("DELETE FROM nodes WHERE file_id = ?", (file_id,))
        for table in ("raw_calls", "raw_imports", "raw_bases", "raw_facts"):
            self.run(f"DELETE FROM {table} WHERE file_id = ?", (file_id,))
