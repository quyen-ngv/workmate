"""Projects, tickets and what hangs off them (comments, documents, interactions)."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from .org import WorkspaceKind


class ProjectAccess(StrEnum):
    READ_ONLY = "read_only"  # tickets work in their own folder and may read the project folder
    WORKTREE = "worktree"  # git repo: one worktree + branch per ticket tree (safe, reviewable)
    DIRECT = "direct"  # work directly in the folder; one ticket run at a time per project


class Project(BaseModel):
    id: str
    name: str
    description: str = ""
    path: str | None = None  # the project's folder (read access in chat; work access per `access`)
    access: ProjectAccess = ProjectAccess.READ_ONLY
    main_branch: str = "main"
    test_command: str | None = None
    is_default: bool = False
    team_id: str | None = None
    kind: str = "code"  # code | reference | automation
    repo_url: str | None = None
    # How to set up, build, test, lint and run the project (argv lists, see doc/plans design §9.2).
    profile: dict[str, Any] = Field(default_factory=dict)
    # How much the AI may do without a click (security/policy.py): read_only | workspace | full.
    permission_mode: str = "workspace"
    allow_rules: list[str] = Field(default_factory=list)  # "always allow" rules chosen on approval cards

    @property
    def workspace_kind(self) -> WorkspaceKind:
        if self.access == ProjectAccess.WORKTREE and self.path:
            return WorkspaceKind.GIT_WORKTREE
        return WorkspaceKind.FOLDER


class TicketStatus(StrEnum):
    BACKLOG = "backlog"
    TODO = "todo"
    IN_PROGRESS = "in_progress"
    IN_REVIEW = "in_review"
    BLOCKED = "blocked"
    DONE = "done"
    CANCELLED = "cancelled"


S = TicketStatus

# Manual transitions. Entering IN_PROGRESS only happens through checkout.
TRANSITIONS: dict[TicketStatus, set[TicketStatus]] = {
    S.BACKLOG: {S.TODO, S.CANCELLED},
    S.TODO: {S.BACKLOG, S.BLOCKED, S.CANCELLED},
    S.IN_PROGRESS: {S.TODO, S.IN_REVIEW, S.BLOCKED, S.DONE, S.CANCELLED},
    S.IN_REVIEW: {S.TODO, S.BLOCKED, S.DONE, S.CANCELLED},
    S.BLOCKED: {S.TODO, S.CANCELLED},
    S.DONE: {S.TODO},
    S.CANCELLED: {S.TODO},
}

CHECKOUT_FROM: set[TicketStatus] = {S.BACKLOG, S.TODO, S.BLOCKED, S.IN_REVIEW}


class Priority(StrEnum):
    CRITICAL = "critical"
    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class Ticket(BaseModel):
    id: int
    key: str
    project_id: str | None = None
    type: str = "task"
    title: str
    description: str = ""
    status: TicketStatus = TicketStatus.TODO
    priority: Priority = Priority.MEDIUM
    assignee_actor_id: str | None = None
    assignee_user: bool = False
    parent_id: int | None = None
    created_by: str = "user"
    checkout_run_id: str | None = None
    data_class: str = "internal"
    origin_conversation_id: str | None = None
    due_at: str | None = None  # YYYY-MM-DD, set by whoever plans the work
    created_at: str = ""
    updated_at: str = ""
    # Team copilot: stages, people and links (see domain/stage.py)
    team_id: str | None = None
    stage: str | None = None
    stage_template: str | None = None
    owner_user_id: str | None = None
    mode: str = "wait"  # wait | assume: when people do not answer, wait or go on with stated assumptions
    paused: bool = False
    jira_key: str | None = None
    jira_url: str | None = None
    pr_url: str | None = None
    confluence_url: str | None = None


class Comment(BaseModel):
    id: int
    ticket_id: int
    author_kind: str  # "user" | "actor" | "system" | "change" (a requirement change notice)
    author_id: str | None = None
    body: str
    created_at: str = ""


class Document(BaseModel):
    ticket_id: int
    key: str  # "spec", "plan", "handoff", "test-report", …
    revision: int
    body: str
    updated_by: str
    updated_at: str = ""
    approved_revision: int | None = None  # the revision a gate approved; a newer one reopens the gate


class InteractionKind(StrEnum):
    APPROVE_TOOL = "approve_tool"
    QUESTION = "question"
    CONFIRM_TICKET = "confirm_ticket"  # an actor proposed a ticket from chat; the user confirms
    ASK_COLLEAGUE = "ask_colleague"  # a ticket run asked a colleague in the ticket's meeting group
    APPROVE_AUTOMATION = "approve_automation"  # an actor wants a script to run on its own; the user reviews it
    APPROVE_ACCESS = "approve_access"  # an actor needs to change files of a read-only project; the user decides
    ENV_ACCESS = "env_access"  # the AI asks for time-limited access to a shared environment; a Dev/Lead decides
    APPROVE_STAGE = "approve_stage"  # a stage gate: people with the right role approve the stage's documents


class InteractionStatus(StrEnum):
    PENDING = "pending"
    ANSWERED = "answered"
    EXPIRED = "expired"
    CANCELLED = "cancelled"


class Interaction(BaseModel):
    id: str
    kind: InteractionKind
    actor_id: str | None = None
    ticket_id: int | None = None
    run_id: str | None = None
    payload: dict[str, Any] = Field(default_factory=dict)
    status: InteractionStatus = InteractionStatus.PENDING
    response: dict[str, Any] | None = None
    resolved_by: str | None = None
    created_at: str = ""
    resolved_at: str | None = None
