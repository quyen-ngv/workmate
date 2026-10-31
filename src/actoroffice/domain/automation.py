"""Automations: flows an actor designs from a few step types and the office runs on a schedule.

Step types (an automation is a list of them, run in order):
- notify: post a message in the conversation (a reminder, a nudge, a summary of the previous step's output)
- script: run a reviewed Python script (fetch, transform, compute, report) — deterministic, no LLM
- agent:  hand a task to an employee as a ticket (judgment needed: analyse, write, decide) — uses the LLM

A reminder is simply a one-step notify automation; a weekly report can be script → notify, or script → agent.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, Field, model_validator


class StepType(StrEnum):
    NOTIFY = "notify"
    SCRIPT = "script"
    AGENT = "agent"


class Step(BaseModel):
    type: StepType
    text: str = ""  # notify: the message; may use {output}, {date}, {time}
    file: str = ""  # script: file name inside the automation folder
    timeout_s: int = 300  # script
    actor: str = ""  # agent: who gets the ticket
    title: str = ""  # agent: ticket title (default: automation name and date)
    prompt: str = ""  # agent: what to do; the previous step's output is appended

    @model_validator(mode="after")
    def _complete(self) -> Step:
        if self.type == StepType.NOTIFY and not self.text.strip():
            raise ValueError("a notify step needs `text`")
        if self.type == StepType.SCRIPT and not self.file.strip():
            raise ValueError("a script step needs `file` (a .py file in your working directory)")
        if self.type == StepType.AGENT and not (self.actor.strip() and self.prompt.strip()):
            raise ValueError("an agent step needs `actor` and `prompt`")
        return self


class AutomationStatus(StrEnum):
    PENDING_REVIEW = "pending_review"
    ENABLED = "enabled"
    DISABLED = "disabled"
    FINISHED = "finished"  # a one-time automation that already ran


class Automation(BaseModel):
    id: str
    name: str
    description: str = ""
    owner_actor_id: str | None = None
    project_id: str | None = None
    conversation_id: str | None = None
    ticket_id: int | None = None
    steps: list[Step] = Field(default_factory=list)
    schedule: str | None = None
    next_at: str | None = None
    status: AutomationStatus = AutomationStatus.PENDING_REVIEW
    failures: int = 0
    approved_sha: str | None = None  # fingerprint of steps + script code the user approved; nothing else runs
    created_at: str = ""
    updated_at: str = ""

    @property
    def needs_review(self) -> bool:
        """Running code or spending an employee's time needs the user's OK; posting a message does not."""
        return any(s.type in (StepType.SCRIPT, StepType.AGENT) for s in self.steps)

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> Automation:
        import json

        return cls.model_validate({**row, "steps": json.loads(row.get("steps_json") or "[]")})


class AutomationRun(BaseModel):
    id: int
    automation_id: str
    trigger: str
    status: Literal["running", "succeeded", "failed"] | str
    exit_code: int | None = None
    output: str | None = None
    error: str | None = None
    started_at: str
    finished_at: str | None = None
    duration_ms: int | None = None
