"""The five things the core knows (item, link, evidence, baseline, finding) and the shapes a pack is written in.

A document key is `"<name>"` or `"<ticket>/<name>"` (`"spec"`, `"EP-4/analysis"`). An item id is unique inside its
document; a reference to an item of another ticket carries that ticket's key (`EP-4/REQ-2`).
"""

from __future__ import annotations

import hashlib
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

Severity = Literal["error", "warn"]
RULE_TYPES = ("template", "cover", "traces_to", "evidence", "fresh", "baseline", "answered", "declared_change")
# What a pack may ask for when something changes after approval (spec 6.3). The core only classifies the event.
REACTIONS = ("ignore", "notify_author", "revise", "reopen_gate", "block")


class _Strict(BaseModel):
    """Pack shapes reject unknown keys: a typo in a pack must fail at load, not be ignored at run time."""

    model_config = ConfigDict(extra="forbid")


def _as_list(value: Any) -> Any:
    """`each: spec.AC` and `each: [spec.AC, spec.BR]` mean the same kind of thing."""
    if value is None:
        return []
    return [value] if isinstance(value, str) else value


# --- pack shapes -----------------------------------------------------------------------------------------------


class ItemKind(_Strict):
    title: str = ""
    pattern: str | None = None  # regex the whole id must match; default `<KIND>-<number>`
    needs_evidence: bool = False
    blocks_gate: bool = False
    confirm_at_gate: bool = False


class DocType(_Strict):
    template: str | None = None  # path relative to the pack folder
    sections: list[str] = Field(default_factory=list)  # headings that must exist (numbering in front is ignored)
    items: list[str] = Field(default_factory=list)  # item kinds this document may hold
    based_on: list[str] = Field(default_factory=list)  # document types it is written from


class Syntax(_Strict):
    trace: list[str] = Field(default_factory=lambda: ["Truy vết"])
    source: list[str] = Field(default_factory=lambda: ["Nguồn"])
    changes: list[str] = Field(default_factory=lambda: ["Thay đổi"])
    answer: list[str] = Field(default_factory=lambda: ["Trả lời"])
    tags: dict[str, str] = Field(default_factory=dict)  # tag name used by rules → the word written in brackets
    version_line: str = r"Phiên bản: \d+"
    changelog: str = "Change log"  # heading of the change table


class RuleDef(_Strict):
    """One rule of a pack: a primitive type plus its parameters. Which parameters matter depends on the type."""

    type: str
    severity: Severity = "error"
    docs: list[str] = Field(default_factory=list)  # document types, or ["*"]
    each: list[str] = Field(default_factory=list)  # selectors `doctype.KIND`
    by: list[str] = Field(default_factory=list)
    to: list[str] = Field(default_factory=list)
    also: list[str] = Field(default_factory=list)
    against: list[str] = Field(default_factory=list)
    kinds: list[str] = Field(default_factory=list)
    providers: list[str] = Field(default_factory=list)
    unless_section: str | None = None
    unless_tag: str | None = None

    @field_validator("docs", "each", "by", "to", "also", "against", "kinds", "providers", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> Any:
        return _as_list(value)


class Crosscheck(_Strict):
    left: str  # doctype or doctype.KIND
    right: str  # same, or "evidence"
    at: list[str] = Field(default_factory=list)  # gates where this pair is compared
    prompt: str = ""
    severity: Severity = "error"

    @field_validator("at", mode="before")
    @classmethod
    def _lists(cls, value: Any) -> Any:
        return _as_list(value)


class OnChange(_Strict):
    upstream_changed: dict[str, str] = Field(default_factory=lambda: {"when_item_affected": "revise", "otherwise": "ignore"})
    evidence_stale: dict[str, str] = Field(default_factory=lambda: {"before_approval": "block", "after_approval": "notify_author"})


class Spawn(_Strict):
    flows: list[str] = Field(default_factory=list)  # flows a child ticket may follow; the first is the default
    copy_docs: list[str] = Field(default_factory=list, alias="copy")  # documents copied to the child, marked approved
    assign: list[str] = Field(default_factory=list)  # roles the approver must pick a person for


class StageConfig(_Strict):
    """Same keys as domain.stage.StageDef plus `spawn`; the integration step converts it."""

    id: str
    title: str = ""
    outputs: list[str] = Field(default_factory=list)
    checks: list[str] = Field(default_factory=list)
    gate: str | None = None
    requires_steps: bool = False
    instructions: str = ""
    spawn: Spawn | None = None


class GateConfig(_Strict):
    """Same keys as domain.stage.GatePolicy plus who submits, who may accept findings and what the card shows."""

    roles: list[str] = Field(default_factory=list)
    min: int = 1
    fallback: str | None = "owner"
    external: bool = False
    submit_by: list[str] = Field(default_factory=list)
    accept_findings: list[str] = Field(default_factory=list)  # empty = the same people as `roles`
    show: list[str] = Field(default_factory=list)


# --- documents -------------------------------------------------------------------------------------------------


class SourceRef(BaseModel):
    """One entry of a `Nguồn:` line. `kind` is only what the text looks like; which adapter serves it is not the core's call."""

    raw: str  # as written, the key evidence is matched on
    kind: str  # "file" | "ticket" | "url" | "other"
    ref: str  # "src/a.py" | "PAY-120" | "https://…"
    locator: str = ""  # ":40-88" | "§2.1" | "#L10-L20"
    repo: str = ""  # "repo" in `repo:src/a.py`

    def lines(self) -> tuple[int, int] | None:
        return line_range(self.locator)


class Item(BaseModel):
    id: str  # "BR-3"
    kind: str  # "BR"
    title: str
    body: str  # the head text and every line under it
    hash: str  # sha256 of the body with whitespace normalised: tells which items really changed between two revisions
    section: str
    traces: list[str] = Field(default_factory=list)  # ["AC-1.2", "EP-4/REQ-2"]
    sources: list[SourceRef] = Field(default_factory=list)
    changes: list[str] = Field(default_factory=list)  # ["F-2"]
    answer: str = ""
    tags: list[str] = Field(default_factory=list)  # tag names from the pack's syntax ("technical"), not the written word


class Problem(BaseModel):
    """A structural fault found while parsing. The `template` rule turns it into a Finding (the parser knows neither
    the document key nor the rule's severity)."""

    code: str  # "missing_section" | "no_version" | "no_changelog" | "duplicate_id" | "bad_id"
    item: str | None = None
    detail: str = ""  # tells two problems of the same code apart (the section name)
    message: str


class ParsedDoc(BaseModel):
    doctype: str
    version_line: bool
    sections: list[str]
    items: list[Item]
    problems: list[Problem] = Field(default_factory=list)

    def item(self, item_id: str) -> Item | None:
        return next((i for i in self.items if i.id == item_id), None)


class Evidence(BaseModel):
    """A pinned `Nguồn:`. The office fetches and fills it; the core only reads it."""

    doc: str  # document key the source line is in
    item: str
    source: str  # SourceRef.raw
    provider: str = ""  # "local_git", "jira", "confluence"…
    category: str = ""  # which port served it: "code" | "docs"
    ref: str = ""
    locator: str = ""
    version: str = ""  # commit sha / page version / issue updated-at
    content_hash: str = ""  # sha256 of the excerpt when it was pinned
    excerpt: str = ""
    fetched_at: str = ""
    error: str | None = None  # why the source could not be resolved (file missing, lines past the end, 404)
    current_hash: str | None = None  # hash of the same place at the source now; None = not compared


class Baseline(BaseModel):
    """What a revision of a document was written from."""

    docs: dict[str, int] = Field(default_factory=dict)  # {"EP-4/analysis": 3, "T-12/spec": 2}
    code: dict[str, str] = Field(default_factory=dict)  # {"<project_id>": "<commit sha>"}


class Approved(BaseModel):
    """The latest approved revision of a document, and what changed on the way to it."""

    revision: int
    # pinned revision → item ids that differ between it and `revision` (trace.impact). A pinned revision missing
    # here means "unknown", which the core treats as "everything may have changed".
    changed_since: dict[int, list[str]] = Field(default_factory=dict)


class Finding(BaseModel):
    id: str  # stable across runs, so a reply or an acceptance is not lost
    rule: str  # "R-COVER-AC", or "X-DESIGN-SPEC" for an AI crosscheck
    severity: Severity
    doc: str  # "T-12/design"
    item: str | None = None
    message: str  # Vietnamese: where it is and how to fix it
    status: str = "open"  # open | fixed | accepted
    accepted_by: str | None = None
    reason: str | None = None


class MatchState(StrEnum):
    """Derived, never stored: see rules.match_state()."""

    CHUA_KIEM = "chua_kiem"
    KHOP = "khop"
    LECH = "lech"
    CU = "cu"
    LOI = "loi"


MATCH_STATE_VI = {
    MatchState.CHUA_KIEM: "chưa kiểm",
    MatchState.KHOP: "khớp",
    MatchState.LECH: "lệch với tài liệu nguồn",
    MatchState.CU: "bằng chứng đã cũ",
    MatchState.LOI: "có lỗi",
}


def finding_id(rule: str, doc: str, item: str | None, discriminator: str = "") -> str:
    return hashlib.sha256("\x1f".join([rule, doc, item or "", discriminator]).encode()).hexdigest()[:16]


def make_finding(rule: str, severity: str, doc: str, item: str | None, message: str, discriminator: str = "") -> Finding:
    return Finding(id=finding_id(rule, doc, item, discriminator), rule=rule, severity=severity, doc=doc, item=item, message=message)


def line_range(locator: str) -> tuple[int, int] | None:
    """`:120`, `:120-130`, `#L10-L20` → (first, last); None for a section or nothing."""
    text = locator or ""
    for prefix in (":", "#L"):
        if text.startswith(prefix):
            first, _, last = text[len(prefix) :].partition("-")
            last = last.removeprefix("L") or first
            if first.isdigit() and last.isdigit():
                return int(first), int(last)
    return None
