"""Wake requests, runs and turn results."""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class WakeSource(StrEnum):
    ASSIGNMENT = "assignment"
    COMMENT = "comment"
    INTERACTION = "interaction"
    BLOCKERS_RESOLVED = "blockers_resolved"
    MENTION = "mention"
    MANUAL = "manual"


class RunKind(StrEnum):
    TICKET = "ticket"
    CHAT = "chat"
    SYSTEM = "system"  # office maintenance, e.g. memory compaction


class RunStatus(StrEnum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    PARKED = "parked"  # waiting for a human (deferred tool or question)
    FAILED = "failed"
    CANCELLED = "cancelled"


class WakeRequest(BaseModel):
    id: int
    actor_id: str
    ticket_id: int | None = None
    conversation_id: str | None = None
    source: WakeSource
    payload: dict[str, Any] = Field(default_factory=dict)
    status: str = "pending"  # pending | claimed | done | dropped
    coalesced: int = 0
    created_at: str = ""


class Usage(BaseModel):
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    model: str = ""  # the model that answered (reported by the runtime, else the one asked for)

    @classmethod
    def from_sdk(cls, raw: dict[str, Any] | None) -> Usage:
        raw = raw or {}
        return cls(
            input_tokens=int(raw.get("input_tokens") or 0),
            output_tokens=int(raw.get("output_tokens") or 0),
            cache_read_tokens=int(raw.get("cache_read_input_tokens") or 0),
            cache_write_tokens=int(raw.get("cache_creation_input_tokens") or 0),
        )


class Run(BaseModel):
    id: str
    actor_id: str
    kind: RunKind
    ticket_id: int | None = None
    conversation_id: str | None = None
    status: RunStatus = RunStatus.QUEUED
    session_before: str | None = None
    session_after: str | None = None
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float = 0.0
    tainted: bool = False
    error: str | None = None
    error_kind: str | None = None  # engine/failures.py FailureKind, set when the run failed
    started_at: str | None = None
    finished_at: str | None = None
    stats: dict[str, Any] = Field(default_factory=dict)  # what the run did: files, lines, commands (engine/stats.py)


class TurnStatus(StrEnum):
    COMPLETED = "completed"
    DEFERRED = "deferred"  # a tool call is parked until a human decides
    RATE_LIMITED = "rate_limited"
    MAX_TURNS = "max_turns"
    CANCELLED = "cancelled"
    ERROR = "error"


class DeferredTool(BaseModel):
    tool_use_id: str
    name: str
    input: dict[str, Any]


class RateLimit(BaseModel):
    status: str
    kind: str | None = None
    utilization: float | None = None
    resets_at: int | None = None


class TurnResult(BaseModel):
    status: TurnStatus
    session_id: str | None = None
    text: str = ""
    usage: Usage = Field(default_factory=Usage)
    cost_usd: float = 0.0
    deferred: DeferredTool | None = None
    rate_limit: RateLimit | None = None
    error: str | None = None
    failure_kind: str | None = None  # engine/failures.py FailureKind, set by the executor on an ERROR turn
    retries: int = 0  # automatic retries the executor made before this result
    structured: dict[str, Any] | None = None  # the answer matching TurnSpec.output_schema, when the runtime supports it
