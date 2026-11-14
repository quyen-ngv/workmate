"""The AI cross-check of documents (check `crosscheck_clean`, Doc Core §5.3): a second session with a fresh context
compares the pairs the pack names for the stage's gate (spec ↔ analysis, design ↔ spec, design ↔ the code it cites)
and reports where they do not agree. What a machine rule can check is not its job: it looks at meaning.

A finding ends in one of two ways: the author fixes the document and the next round no longer reports it, or somebody
who approves the gate accepts it with a reason. The author and the AI cannot accept. After MAX_ROUNDS rounds in one
stage run the cross-checker no longer runs; the findings still open go to the approvers on the gate card, and the gate
cannot be approved while one of them is neither fixed nor accepted.

A round is keyed by the content it read: the same documents and sources are never cross-checked twice.
"""

from __future__ import annotations

import hashlib
import json
import logging
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.doccore import Finding
from actoroffice.doccore.model import make_finding
from actoroffice.domain.ids import new_id, utcnow
from actoroffice.domain.run import Run, RunKind, RunStatus, TurnStatus
from actoroffice.domain.stage import StageDef
from actoroffice.domain.work import Ticket
from actoroffice.runtimes.base import GateDecision, GateResult, TurnSpec
from actoroffice.tools.code_tools import CODE_TOOLS
from actoroffice.tools.registry import BoundTool, ToolCtx

if TYPE_CHECKING:
    from actoroffice.app import Office
    from actoroffice.services.checks import CheckContext, CheckResult
    from actoroffice.services.docs import DocSet

log = logging.getLogger(__name__)

MAX_ROUNDS = 3
MAX_DOC = 14_000
MAX_EXCERPT = 1_200
READ_ONLY = {"Read", "Grep", "Glob", "StructuredOutput", "Tool Load"}

CROSSCHECKER = """\
You are a strict, independent reviewer of project documents. Another engineer (an AI) wrote them. Each section of
the prompt is one cross-check: two things that must agree. Find where they do not. Do not praise, do not restate.

Report only real differences in meaning:
- the right side requires something the left side does not deliver, or delivers differently (a missing case, a
  different value, a different condition, an error path that is ignored);
- the left side states something about the existing code that the cited code does not show;
- the left side changes behaviour the right side describes, without saying so;
- two items contradict each other.
Do not report: formatting, wording, missing headings, missing trace lines, anything a template check would find.
Do not report something only because you would have designed it differently.

You may read code with the office tools code_search, code_outline, code_symbol, code_refs, code_read, code_impact
(and Read/Grep/Glob) to confirm a finding. You cannot change anything.

Your answer is this JSON object:
{"findings": [{"check": "<the cross-check id of the section>", "doc": "<document key of the LEFT side, as in its
heading>", "item": "<id of the item of the left document the finding is about, e.g. C-2, AC-1.1>",
"severity": "high|medium|low", "claim": "what does not agree", "evidence": "the two places that show it"}]}
If your tools include StructuredOutput, give the object to it once, at the end. Otherwise write ONLY the JSON object
as your final text, no prose. "high" and "medium" block the approval until fixed or accepted by the approver; use
"low" for a remark. An empty list is a valid answer and the right one when the documents agree.
"""

SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "check": {"type": "string"},
                    "doc": {"type": "string"},
                    "item": {"type": ["string", "null"]},
                    "severity": {"type": "string", "enum": ["high", "medium", "low"]},
                    "claim": {"type": "string"},
                    "evidence": {"type": "string"},
                },
                "required": ["check", "doc", "severity", "claim"],
            },
        }
    },
    "required": ["findings"],
}


class _ReadOnlyGate:
    async def before_tool(self, tool: str, args: dict[str, Any], tool_use_id: str | None) -> GateResult:
        name = tool.removeprefix("mcp__office__").removeprefix("@office/")
        if tool in READ_ONLY or (name != tool and name in {t.name for t in CODE_TOOLS}):
            return GateResult(GateDecision.ALLOW)
        return GateResult(GateDecision.DENY, "The cross-checker can only read (code_* tools, Read, Grep, Glob).")

    async def after_tool(self, tool: str, args: dict[str, Any], result: Any) -> None:
        return None


def parse_answer(text: str, structured: Any = None) -> list[dict[str, Any]] | None:
    """The findings of the cross-checker's answer; None when there is no JSON object with a `findings` list."""
    data = structured if isinstance(structured, dict) else None
    if data is None:
        # The object may come after prose that has braces of its own ("returns {'ok': False}"), or inside a code
        # fence: take the object that holds "findings", wherever it starts.
        text = text or ""
        decoder = json.JSONDecoder()
        for m in re.finditer(r'\{\s*"findings"', text):
            try:
                data, _ = decoder.raw_decode(text, m.start())
            except json.JSONDecodeError:
                continue
            break
        if data is None:
            return None
    findings = data.get("findings") if isinstance(data, dict) else None
    return [f for f in findings if isinstance(f, dict)] if isinstance(findings, list) else None


class CrossCheckService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self.last_error = ""  # why the last round gave no answer (shown on the gate card)

    # ---- what a round reads ----------------------------------------------------------------------------------

    def _pairs(self, ticket: Ticket, stage: StageDef, ds: DocSet) -> list[tuple[str, str, list[str], list[str]]]:
        """(check id, right spec, left document keys, right document keys) for the stage's gate."""
        docs = self.office.docs
        own = docs.output_keys(ticket, stage, ds)
        out = []
        for check_id, cfg in docs.pack_of(ticket).crosschecks.items():
            if stage.gate not in cfg.at:
                continue
            left = sorted(k for k in own if ds.docs[k].doctype == cfg.left)
            right_type = cfg.right.split(".", 1)[0]
            right = sorted(k for k in ds.docs if ds.docs[k].doctype == right_type and k not in left) if cfg.right != "evidence" else []
            if left and (right or cfg.right == "evidence"):
                out.append((check_id, cfg.right, left, right))
        return out

    async def _prompt(self, ticket: Ticket, stage: StageDef, ds: DocSet) -> tuple[str, str, list[str]]:
        """The prompt of a round, the hash of everything it shows, and the cross-check ids in it."""
        office = self.office
        pairs = self._pairs(ticket, stage, ds)
        if not pairs:
            return "", "", []
        bodies: dict[str, str] = {}
        for key, (ticket_id, stored_key) in ds.stored.items():
            doc = await office.tickets.get_document(ticket_id, stored_key)
            if doc is None:
                continue
            body = doc.body
            if key not in ds.own and doc.approved_revision:
                body = await office.docs._body(ticket_id, stored_key, doc.approved_revision)
            bodies[key] = body
        evidence = await office.docs.evidence(ticket, ds)
        sections = []
        for check_id, right_spec, left, right in pairs:
            part = [f"# Cross-check {check_id}"]
            for key in left:
                part.append(f"## LEFT document `{key}`\n{bodies.get(key, '')[:MAX_DOC]}")
            if right_spec == "evidence":
                cited = [e for e in evidence if e.doc in left and e.category == "code" and not e.error]
                if not cited:
                    continue
                part.append("## RIGHT: the existing code the left document cites (as it was when it was written)")
                for e in cited:
                    part.append(f"### {e.item} — Nguồn: {e.source} (commit {e.version[:10]})\n```\n{e.excerpt[:MAX_EXCERPT]}\n```")
            else:
                for key in right:
                    text = bodies.get(key, "")
                    if "." in right_spec:  # only the items of one kind: analysis.F
                        kind = right_spec.split(".", 1)[1]
                        items = [i for i in ds.docs[key].items if i.kind == kind]
                        text = "\n\n".join(f"### {i.id}: {i.title}\n{i.body}" for i in items)
                        if not text:
                            continue
                    part.append(f"## RIGHT document `{key}`" + (f" (only its {right_spec.split('.', 1)[1]} items)" if "." in right_spec else "") + f"\n{text[:MAX_DOC]}")
            if len(part) > 2:
                sections.append("\n\n".join(part))
        prompt = "\n\n".join(sections)
        return prompt, hashlib.sha256(prompt.encode("utf-8")).hexdigest(), [p[0] for p in pairs]

    # ---- storage ---------------------------------------------------------------------------------------------

    async def _rounds(self, stage_run_id: int) -> int:
        row = await self.office.db.fetchone("SELECT COUNT(*) AS n FROM doc_crosschecks WHERE stage_run_id = ?", (stage_run_id,))
        return int(row["n"]) if row else 0

    async def _latest(self, stage_run_id: int) -> dict[str, Any] | None:
        return await self.office.db.fetchone("SELECT * FROM doc_crosschecks WHERE stage_run_id = ? ORDER BY id DESC LIMIT 1", (stage_run_id,))

    async def findings(self, ticket: Ticket) -> list[Finding]:
        """The findings of the last round of the ticket's current stage run, accepted ones marked."""
        office = self.office
        srun = await office.stages.current_run(ticket)
        row = await self._latest(srun.id) if srun is not None else None
        if row is None:
            return []
        findings = [Finding.model_validate(f) for f in json.loads(row["findings_json"])]
        accepted = {r["finding_id"]: r for r in await office.db.fetchall("SELECT * FROM doc_findings WHERE ticket_id = ?", (ticket.id,))}
        for f in findings:
            hit = accepted.get(f.id)
            if hit is not None:
                f.status, f.accepted_by, f.reason = "accepted", hit["accepted_by"], hit["reason"]
        return findings

    @staticmethod
    def _to_findings(ticket: Ticket, raw: list[dict[str, Any]], checks: list[str], left_keys: set[str]) -> list[Finding]:
        """One finding per (cross-check, document, item): its id does not depend on the reviewer's wording, so an
        acceptance survives the next round. Several claims about one item are joined."""
        grouped: dict[tuple[str, str, str | None], list[tuple[str, str]]] = {}
        for f in raw:
            check = str(f.get("check") or "").strip().upper()
            check = check if check in checks else (checks[0] if len(checks) == 1 else "X-CHECK")
            doc = str(f.get("doc") or "").strip().strip("`")
            doc = doc if doc in left_keys else next((k for k in sorted(left_keys) if k.endswith("/" + doc.split("/")[-1])), doc or sorted(left_keys)[0])
            item = str(f.get("item") or "").strip() or None
            claim = str(f.get("claim") or "").strip()
            if not claim:
                continue
            evidence = str(f.get("evidence") or "").strip()
            level = str(f.get("severity") or "medium").strip().lower()
            grouped.setdefault((check, doc, item), []).append(("warn" if level == "low" else "error", claim[:900] + (f" — {evidence[:500]}" if evidence else "")))
        out = []
        for (check, doc, item), claims in grouped.items():
            severity = "error" if any(s == "error" for s, _ in claims) else "warn"
            out.append(make_finding(check, severity, doc, item, " | ".join(c for _, c in claims)))
        return out

    # ---- the check -------------------------------------------------------------------------------------------

    async def check(self, ctx: CheckContext) -> CheckResult:
        from actoroffice.services.checks import CheckResult

        office = self.office
        cid = "crosscheck_clean"
        if not office.settings.ai_review:
            return CheckResult(id=cid, status="skipped", summary="đối chiếu bằng AI đang tắt (ai_review)")
        ticket, stage = ctx.ticket, ctx.stage
        ds = await office.docs.docset(ticket)
        prompt, digest, checks = await self._prompt(ticket, stage, ds)
        if not prompt:
            return CheckResult(id=cid, status="skipped", summary="giai đoạn này không có cặp tài liệu nào cần đối chiếu")
        srun_id = ctx.stage_run.id
        latest = await self._latest(srun_id)
        rounds = await self._rounds(srun_id)
        if latest is None or (latest["input_hash"] != digest and rounds < MAX_ROUNDS):
            raw = await self._run(ticket, prompt, ctx.workspace, srun_id)
            if raw is None:
                # What an earlier round found is about other content: it is not shown as the result of this one.
                why = f"lượt chạy lỗi: {office.redact(self.last_error)}" if self.last_error else "không trả kết quả có cấu trúc"
                return CheckResult(
                    id=cid, status="skipped", summary=f"⚠ CHƯA đối chiếu được bằng AI ({why}). Phần ngữ nghĩa của tài liệu chưa có ai kiểm: người duyệt cần tự đối chiếu."
                )
            else:
                left = {k for _, _, lefts, _ in self._pairs(ticket, stage, ds) for k in lefts}
                found = self._to_findings(ticket, raw, checks, left)
                await office.db.execute(
                    "INSERT INTO doc_crosschecks(ticket_id, stage_run_id, input_hash, findings_json, created_at) VALUES (?, ?, ?, ?, ?)",
                    (ticket.id, srun_id, digest, json.dumps([json.loads(f.model_dump_json()) for f in found], ensure_ascii=False), utcnow()),
                )
                rounds += 1
        findings = await self.findings(ticket)
        open_errors = [f for f in findings if f.status == "open" and f.severity == "error"]
        data = {"findings": office.docs.dump(findings), "rounds": rounds}
        if not open_errors:
            note = f"không còn điểm lệch (vòng {rounds})" if not findings else f"{len(findings)} ghi chú, không điểm nào chặn (vòng {rounds})"
            return CheckResult(id=cid, status="passed", summary=note, data=data)
        listed = "\n".join(f"- [{f.id[:8]}] {f.rule} · {f.doc}{' · ' + f.item if f.item else ''}: {f.message}" for f in open_errors[:10])
        latest = await self._latest(srun_id)
        if rounds >= MAX_ROUNDS and latest is not None:
            # No more rounds: what is still open goes to the approvers, who fix-or-accept each one on the card.
            return CheckResult(
                id=cid, status="passed", summary=f"đã đối chiếu {rounds} vòng; còn {len(open_errors)} điểm lệch chuyển cho người duyệt xử lý:\n{listed}", data=data
            )
        return CheckResult(
            id=cid, status="failed",
            summary=f"{len(open_errors)} điểm lệch giữa các tài liệu (vòng {rounds}/{MAX_ROUNDS}). Sửa tài liệu cho khớp rồi stage_complete lại; "
            f"điểm nào không sửa được thì nói rõ lý do trong tài liệu để người duyệt quyết:\n{listed}",
            data=data,
        )  # fmt: skip

    async def _run(self, ticket: Ticket, prompt: str, workspace: Path | None, stage_run_id: int) -> list[dict[str, Any]] | None:
        office = self.office
        ai = await office.ensure_ai()
        position = await office.org.get_position(ai.position_id)
        run_id = new_id("run")
        await office.runs.start(Run(id=run_id, actor_id=ai.id, kind=RunKind.SYSTEM, ticket_id=ticket.id))
        await office.db.execute("UPDATE runs SET purpose = 'crosscheck', stage_run_id = ? WHERE id = ?", (stage_run_id, run_id))
        cwd = workspace or (office.settings.workspaces_dir / (await office.tickets.root_of(ticket.id)).key)
        cwd.mkdir(parents=True, exist_ok=True)
        spec = TurnSpec(
            run_id=run_id,
            prompt=prompt,
            cwd=cwd,
            system_prompt=CROSSCHECKER + f"\nWrite claims in {office.settings.language}.",
            model=office.settings.ai_model or office.settings.default_model,
            tools=["Read", "Grep", "Glob"],
            internal_tools=[BoundTool(t, ToolCtx(office, ai, position, run_id, ticket_id=ticket.id, workspace=cwd)) for t in CODE_TOOLS],
            max_turns=25,
            show_thinking=False,
            output_schema=SCHEMA,
        )

        async def sink(kind: str, payload: dict[str, Any]) -> None:
            await office.runs.event(run_id, kind, payload)

        result = None
        try:
            result = await office.runtime.run_turn(spec, _ReadOnlyGate(), sink)
            result.usage.model = result.usage.model or spec.model or ""
        finally:
            ok = result is not None and result.status == TurnStatus.COMPLETED
            await office.runs.finish(
                run_id, RunStatus.SUCCEEDED if ok else RunStatus.FAILED, usage=result.usage if result is not None else None,
                cost_usd=result.cost_usd if result is not None else 0.0,
            )  # fmt: skip
        if result is None:
            self.last_error = "lượt chạy không bắt đầu được"
            return None
        found = parse_answer(result.text, result.structured)
        self.last_error = (result.error or "").strip()[:300] if found is None else ""
        if found is None:
            log.warning("crosscheck %s: no structured answer (status %s, error %s): %r", run_id, result.status, result.error, (result.text or "")[-600:])
        return found
