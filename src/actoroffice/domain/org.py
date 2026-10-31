"""Positions (job templates) and actors (employees)."""

from __future__ import annotations

from enum import StrEnum
from typing import Literal

from pydantic import BaseModel, Field

Rule = Literal["allow", "ask", "deny"]


class WorkspaceKind(StrEnum):
    GIT_WORKTREE = "git_worktree"  # code: one worktree + branch per ticket
    FOLDER = "folder"  # documents, datasets, drafts
    REMOTE = "remote"  # admin work against hosts; no local files of value
    NONE = "none"  # coordination only (PM)


class PositionPolicy(BaseModel):
    """What actors of a position may do without asking. Hard safety denies always win."""

    exec_default: Rule = "ask"
    bash_allow: list[str] = Field(default_factory=list)  # glob patterns, e.g. "git status*"
    bash_deny: list[str] = Field(default_factory=list)
    external: Rule = "ask"  # tools that act outside the machine (post, email, API writes)
    web: Rule = "deny"  # WebFetch / WebSearch (untrusted content → session taint)
    read_only_dirs: list[str] = Field(default_factory=list)
    max_turns: int = 40


class Position(BaseModel):
    id: str
    name: str  # short handle, e.g. "dev"
    title: str  # display, e.g. "Developer"
    instructions: str = ""
    workspace_kind: WorkspaceKind = WorkspaceKind.FOLDER
    model: str = "sonnet"
    prompt_preset: Literal["claude_code", "custom"] = "custom"
    tools: list[str] = Field(default_factory=lambda: ["Read", "Write", "Edit", "Glob", "Grep"])
    packs: list[str] = Field(default_factory=list)  # capability packs (MCP connectors)
    policy: PositionPolicy = Field(default_factory=PositionPolicy)
    # Definition of done: documents that must exist (on the ticket, its parent or the tree's root) before this
    # position may set a ticket to done. "report|memo" means either.
    done_docs: list[str] = Field(default_factory=list)


class ActorStatus(StrEnum):
    ACTIVE = "active"
    PAUSED = "paused"


class PermissionMode(StrEnum):
    DEFAULT = "default"  # ask whenever the policy says "ask"
    TRUST_ALL = "trust_all"  # never ask (like Claude's bypass / Kiro's trust-all); hard safety denies still apply


class Actor(BaseModel):
    id: str
    position_id: str
    name: str  # display + @mention handle, e.g. "Minh"
    emoji: str = ""
    specialty: str = ""  # e.g. "Backend Java"
    status: ActorStatus = ActorStatus.ACTIVE
    model: str | None = None  # overrides the position model
    permission_mode: PermissionMode = PermissionMode.DEFAULT
    # "Always allow" rules, Claude Code style: "Bash", "Bash(npm install *)", "WebFetch", "mcp__x__post".
    allow_rules: list[str] = Field(default_factory=list)

    @property
    def handle(self) -> str:
        return self.name.lower().replace(" ", "")
