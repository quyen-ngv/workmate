"""Conversations (DM / group) and messages."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class ConversationKind(StrEnum):
    DM = "dm"
    GROUP = "group"


class MessageKind(StrEnum):
    TEXT = "text"
    SYSTEM = "system"
    INTERACTION = "interaction"  # card: approval / question / ticket proposal (meta.interaction_id)
    TICKET = "ticket"  # card: a ticket event (meta.ticket_key, meta.status)
    MEMORY = "memory"  # card: "📌 X remembered …" with undo (meta.memory_id)


class Member(BaseModel):
    kind: str  # "user" | "actor"
    id: str


class Conversation(BaseModel):
    id: str
    kind: ConversationKind
    title: str
    project_id: str | None = None
    ticket_id: int | None = None
    created_by: str = "user"
    archived: bool = False
    members: list[Member] = Field(default_factory=list)
    created_at: str = ""
    updated_at: str | None = None

    @property
    def actor_ids(self) -> list[str]:
        return [m.id for m in self.members if m.kind == "actor"]


class Message(BaseModel):
    id: int
    conversation_id: str
    author_kind: str  # "user" | "actor" | "system"
    author_id: str | None = None
    body: str
    kind: MessageKind = MessageKind.TEXT
    mentions: list[str] = Field(default_factory=list)  # actor ids
    meta: dict[str, Any] = Field(default_factory=dict)
    provenance: str = "trusted"
    created_at: str = ""
