"""People and teams: the humans the AI engineer works with (BA, Dev, QE, lead, admin)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field

from .stage import GatePolicy, StageTemplate


class Role(StrEnum):
    BA = "ba"
    DEV = "dev"
    QE = "qe"
    LEAD = "lead"
    ADMIN = "admin"
    OWNER = "owner"  # relative to a ticket: the person who asked for it


ROLE_VI = {
    Role.BA: "BA",
    Role.DEV: "Dev",
    Role.QE: "QE",
    Role.LEAD: "Lead",
    Role.ADMIN: "Admin",
    Role.OWNER: "người yêu cầu",
}

# The person running the office locally (web login, CLI). Acts as admin everywhere.
LOCAL_OWNER_ID = "user"


class User(BaseModel):
    id: str
    name: str
    email: str | None = None
    telegram_id: int | None = None
    default_roles: list[str] = Field(default_factory=list)
    active: bool = True
    created_at: str = ""


class TeamSettings(BaseModel):
    """Per-team overrides. Anything left empty falls back to the office defaults (domain/stage.py)."""

    gates: dict[str, GatePolicy] = Field(default_factory=dict)
    stage_templates: dict[str, StageTemplate] = Field(default_factory=dict)
    model: str | None = None  # runtime for this team's tickets: "sonnet", "opus", "kiro", "kiro:<id>"
    jira_jql: str | None = None  # extra JQL for the Jira mirror (default: project = KEY AND labels = ai)
    default_template: str = "code"


class Team(BaseModel):
    id: str
    name: str
    jira_project_key: str | None = None
    confluence_space: str | None = None
    confluence_parent_id: str | None = None
    settings: TeamSettings = Field(default_factory=TeamSettings)
    created_at: str = ""


class Member(BaseModel):
    user: User
    roles: list[str] = Field(default_factory=list)
