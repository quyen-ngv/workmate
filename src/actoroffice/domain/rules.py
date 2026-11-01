"""Rules the AI engineer follows, owned by people, scoped to the whole office, a team, a project or a repo.

Four kinds, enforced where they can be enforced:
- guideline: text for the prompt of the matching stages ("tests use JUnit 5 + AssertJ")
- check:     a machine check that must pass before the stage finishes (a builtin id or a command)
- policy:    tool calls the gate denies (tool names, command patterns, hosts)
- gate:      extra approvals when a condition holds (e.g. changes under db/migrations/** → a lead approves)

A narrower scope wins over a wider one for guidelines and checks with the same title/id; policies only add up.
"""

from __future__ import annotations

import re
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class RuleKind(StrEnum):
    GUIDELINE = "guideline"
    CHECK = "check"
    POLICY = "policy"
    GATE = "gate"


class RuleStatus(StrEnum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


SCOPE_RE = re.compile(r"^(global|team:[\w\-]+|project:[\w\-]+|repo:[\w\-]+)$")
SPECIFICITY = {"repo": 3, "project": 2, "team": 1, "global": 0}


def scope_kind(scope: str) -> str:
    return scope.split(":", 1)[0]


def scope_dir(scope: str) -> str:
    return scope.replace(":", "-")


class Rule(BaseModel):
    id: str
    title: str
    kind: RuleKind
    scope: str = "global"
    stage: str = "any"
    status: RuleStatus = RuleStatus.DRAFT
    owner: str | None = None
    created_by: str | None = None
    approved_by: str | None = None
    version: int = 1
    spec: dict[str, Any] = Field(default_factory=dict)
    body: str = ""
    source: str | None = None  # "file:CLAUDE.md" when imported from a repo
    updated_at: str = ""

    @property
    def ref(self) -> str:
        return f"{self.id}@{self.version}"

    @property
    def specificity(self) -> int:
        return SPECIFICITY.get(scope_kind(self.scope), 0)

    def applies_to_stage(self, stage: str) -> bool:
        stages = [s.strip() for s in self.stage.split(",") if s.strip()]
        return "any" in stages or stage in stages


class CompiledRules(BaseModel):
    guidelines: list[Rule] = Field(default_factory=list)
    checks: list[Rule] = Field(default_factory=list)
    policies: list[Rule] = Field(default_factory=list)
    gates: list[Rule] = Field(default_factory=list)
    overflow: list[Rule] = Field(default_factory=list)  # guidelines that did not fit in the prompt (rule_search)

    @property
    def refs(self) -> list[str]:
        return [r.ref for r in (*self.guidelines, *self.checks, *self.policies, *self.gates)]

    def guideline_lines(self) -> list[str]:
        lines = [f"[{r.id}] {r.title}: {_one(r.body)}" for r in self.guidelines]
        if self.overflow:
            lines.append("More rules apply (read with rule_search): " + ", ".join(f"{r.id} {r.title}" for r in self.overflow))
        return lines


def _one(text: str, limit: int = 1200) -> str:
    text = text.strip()
    return text if len(text) <= limit else text[:limit] + "…"
