"""Stages: the life of a ticket (understand → plan → build → pr → close) with human gates between them.

A stage template is chosen per ticket (by the kind of work). Each stage names the documents it must produce,
the checks the office runs before it can finish, and the gate (if any) people must pass before the next stage.
Gates are soft: an approver policy says which roles may approve; one person holding several roles can pass
several gates.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class GatePolicy(BaseModel):
    roles: list[str] = Field(default_factory=list)  # any of these roles may approve
    min: int = 1  # approvals needed
    fallback: str | None = "owner"  # who is asked when nobody holds the roles ("owner" = ticket owner)
    external: bool = False  # resolved by an outside event (PR merged, automation approved), not by a card


class StageDef(BaseModel):
    id: str
    title: str = ""
    outputs: list[str] = Field(default_factory=list)  # documents that must exist ("report|document" = either)
    checks: list[str] = Field(default_factory=list)  # builtin check ids, or "repo:<name>" from the project profile
    gate: str | None = None  # gate name (see GatePolicy) passed after this stage
    requires_steps: bool = False  # the AI must declare plan_steps before finishing
    instructions: str = ""  # extra instructions (overrides nothing; added after the defaults)


class StageTemplate(BaseModel):
    name: str
    stages: list[StageDef]

    def stage(self, stage_id: str | None) -> StageDef | None:
        return next((s for s in self.stages if s.id == stage_id), None)

    def next_after(self, stage_id: str) -> StageDef | None:
        ids = [s.id for s in self.stages]
        if stage_id not in ids:
            return None
        i = ids.index(stage_id)
        return self.stages[i + 1] if i + 1 < len(self.stages) else None


class StageStatus(StrEnum):
    """Derived, never stored: see StageService.status()."""

    NOT_STARTED = "not_started"
    QUEUED = "queued"
    WORKING = "working"
    NEEDS_YOU = "needs_you"
    WAITING_EXTERNAL = "waiting_external"
    PAUSED = "paused"
    BLOCKED = "blocked"
    FAILED = "failed"
    IDLE = "idle"
    DONE = "done"


STAGE_STATUS_VI = {
    StageStatus.NOT_STARTED: "chưa bắt đầu",
    StageStatus.QUEUED: "đang xếp hàng",
    StageStatus.WORKING: "đang làm",
    StageStatus.NEEDS_YOU: "cần người",
    StageStatus.WAITING_EXTERNAL: "chờ bên ngoài",
    StageStatus.PAUSED: "tạm dừng",
    StageStatus.BLOCKED: "bị chặn",
    StageStatus.FAILED: "lỗi",
    StageStatus.IDLE: "đang chờ",
    StageStatus.DONE: "xong",
}


class StageOutcome(StrEnum):
    COMPLETED = "completed"
    REJECTED = "rejected"  # a gate sent it back
    REOPENED = "reopened"  # moved back to an earlier stage
    SWITCHED = "switched"  # the ticket moved to another workflow (quick ↔ code)
    STOPPED = "stopped"


class Step(BaseModel):
    text: str
    done: bool = False
    note: str = ""


class StageRun(BaseModel):
    id: int
    ticket_id: int
    stage: str
    attempt: int
    session_id: str | None = None
    started_at: str
    finished_at: str | None = None
    outcome: str | None = None
    summary: str | None = None
    usage: dict[str, Any] = Field(default_factory=dict)
    cost_usd: float = 0.0
    rules: list[str] = Field(default_factory=list)
    checks: list[dict[str, Any]] = Field(default_factory=list)
    steps: list[Step] = Field(default_factory=list)
    review: dict[str, Any] = Field(default_factory=dict)  # review findings with ids and replies (services/review.py)


STAGE_TITLES = {
    "understand": "Tìm hiểu",
    "scope": "Chốt cách sửa",
    "plan": "Lên kế hoạch",
    "design": "Thiết kế",
    "build": "Làm",
    "pr": "Pull request",
    "produce": "Thực hiện",
    "close": "Đóng",
}


def _code() -> StageTemplate:
    return StageTemplate(
        name="code",
        stages=[
            StageDef(id="understand", outputs=["requirements", "dossier"], checks=["outputs", "ac_testable"], gate="requirement"),
            StageDef(id="plan", outputs=["plan"], checks=["outputs", "plan_has_ac_map", "plan_has_options"], gate="plan"),
            StageDef(
                id="build",
                outputs=["handoff"],
                checks=[
                    "outputs",
                    "steps_done",
                    "tests_changed",
                    "ac_coverage",
                    "no_secrets",
                    "log_lint",
                    "repo:build",
                    "repo:test",
                    "repo:lint",
                    "coverage_changed_lines",
                    "review_clean",
                ],
                requires_steps=True,
            ),
            StageDef(id="pr", checks=["pr_linked", "flaky_rerun"], gate="merge"),
            StageDef(id="close", outputs=["dossier"], checks=["outputs", "kb_noted"], gate="knowledge"),
        ],
    )


def _quick() -> StageTemplate:
    """A small, clear change (a layout fix, a typo, a config value, a bug whose cause is known): one short note and
    one approval, then the change and its pull request. No requirements document, options, handoff or KB stage."""
    return StageTemplate(
        name="quick",
        stages=[
            StageDef(id="scope", outputs=["plan"], checks=["outputs", "plan_ac"], gate="plan"),
            StageDef(id="build", checks=["no_secrets", "log_lint", "repo:build", "repo:test", "repo:lint"]),
            StageDef(id="pr", checks=["pr_linked"], gate="merge"),
        ],
    )


TEMPLATE_TITLES = {
    "code": "Code (đầy đủ)",
    "quick": "Sửa nhỏ (1 lần duyệt)",
    "automation": "Tự động hoá",
    "research": "Nghiên cứu",
    "doc": "Tài liệu",
}
SWITCHABLE = {"code": "quick", "quick": "code"}  # workflows a ticket can move between before its PR


def _automation() -> StageTemplate:
    return StageTemplate(
        name="automation",
        stages=[
            StageDef(id="understand", outputs=["requirements", "dossier"], checks=["outputs"], gate="requirement"),
            StageDef(id="design", outputs=["plan"], checks=["outputs"], gate="plan"),
            StageDef(id="build", checks=["steps_done", "automation_saved"], requires_steps=True, gate="script"),
            StageDef(id="close", outputs=["dossier"], checks=["outputs", "kb_noted"], gate="knowledge"),
        ],
    )


def _produce(name: str, output: str) -> StageTemplate:
    return StageTemplate(
        name=name,
        stages=[
            StageDef(id="understand", outputs=["requirements", "dossier"], checks=["outputs"], gate="requirement"),
            StageDef(id="produce", outputs=[output], checks=["outputs"], gate="accept"),
            StageDef(id="close", outputs=["dossier"], checks=["outputs", "kb_noted"], gate="knowledge"),
        ],
    )


def default_templates() -> dict[str, StageTemplate]:
    return {
        "code": _code(),
        "quick": _quick(),
        "automation": _automation(),
        "research": _produce("research", "report"),
        "doc": _produce("doc", "document"),
    }


def default_gates() -> dict[str, GatePolicy]:
    return {
        "requirement": GatePolicy(roles=["ba"], fallback="owner"),
        "plan": GatePolicy(roles=["dev", "lead"], fallback="owner"),
        "accept": GatePolicy(roles=["owner"], fallback="owner"),
        "script": GatePolicy(roles=["dev", "lead"], fallback="owner", external=True),
        "merge": GatePolicy(roles=["dev", "lead"], fallback="owner", external=True),
        "knowledge": GatePolicy(roles=["ba", "dev", "lead", "owner"], fallback="owner"),
    }
