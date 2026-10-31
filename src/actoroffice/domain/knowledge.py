"""Memory (what an actor learned) and skills (how to do things)."""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, Field


class MemoryScope(StrEnum):
    ACTOR = "actor"
    POSITION = "position"  # shared by every actor of the same position
    SHARED = "shared"  # the whole office


class MemoryKind(StrEnum):
    LESSON = "lesson"
    FACT = "fact"
    PREFERENCE = "preference"
    DECISION = "decision"


class Memory(BaseModel):
    id: int
    scope: MemoryScope
    kind: MemoryKind
    content: str
    actor_id: str | None = None
    position_id: str | None = None
    pinned: bool = False
    created_by: str
    source_ticket_id: int | None = None
    use_count: int = 0
    created_at: str = ""
    updated_at: str = ""


class SkillScope(StrEnum):
    SHARED = "shared"
    POSITION = "position"
    ACTOR = "actor"


class SkillRef(BaseModel):
    scope: SkillScope
    owner: str = ""  # position name / actor name; "" for shared
    name: str  # folder name, kebab-case


class Skill(BaseModel):
    ref: SkillRef
    description: str
    body: str = ""  # SKILL.md without front matter
    files: list[str] = Field(default_factory=list)  # sub-skills / references inside the skill folder
    created_by: str = ""
    updated_at: str = ""
