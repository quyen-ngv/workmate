"""Independent review before a stage finishes (check `review_clean`): a second session with a fresh context and
an adversarial prompt reads the diff against the requirements and the plan and returns findings as JSON. The
reviewer can only read (Read/Grep/Glob inside the workspace).

Each finding gets a stable id (F1, F2… per stage run, given by the office, kept in `stage_runs.review_json`). The AI
answers every medium/high finding with `review_reply` (fixed, or wontfix with a reason); the check fails while one
is unanswered. The next round re-checks the findings replied `fixed` (still there → reopened) and leaves `wontfix`
to the people. After two reviewer rounds the reviewer no longer runs, but the check still fails until every
medium/high finding has a reply; the findings and replies are listed on the gate card.
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import OfficeError
from actoroffice.domain.ids import new_id
from actoroffice.domain.run import Run, RunKind, RunStatus, TurnStatus
from actoroffice.runtimes.base import GateDecision, GateResult, TurnSpec

if TYPE_CHECKING:
    from actoroffice.app import Office
    from actoroffice.services.checks import CheckContext, CheckResult

MAX_DIFF = 60_000
MAX_ROUNDS = 2
MAX_DATA = 40  # findings kept in the check's data
MAX_LISTED = 10  # open findings listed in the summary
READ_ONLY = {"Read", "Grep", "Glob"}
SERIOUS = ("high", "medium")
ACTIONS = ("fixed", "wontfix")

REVIEWER = """\
You are a strict, independent code reviewer. Another engineer (an AI) wrote the change below. Your job is to find
real problems before people review it, not to praise it.

Check, in this order:
1. Does the change implement every acceptance criterion (AC-n) of the requirements? Which AC has no test?
2. Bugs: wrong logic, missing edge cases (null/empty, boundaries, time zones, concurrency, retries, idempotency).
3. Tests that do not really test (no assertion, asserting mocks only, skipped, weakened to pass).
4. Violations of the team rules listed below, security problems (injection, secrets, unsafe deserialization),
   and log lines that are noise or leak data.
5. Changes outside the approved plan.

You may read files of the workspace with Read/Grep/Glob to confirm a finding. Do not suggest style nits.

When the prompt has "# Earlier findings", they were reported in an earlier round and have ids. Never report them
again as new findings. For each one marked RE-CHECK (the engineer says it is fixed), say in "previous" whether it
is "resolved" or "still" there, with a short note. Do not judge the others.

Answer with ONLY a JSON object, no prose:
{"findings": [{"severity": "high|medium|low", "file": "path", "line": 0, "claim": "what is wrong",
"evidence": "the code or AC that shows it"}],
"previous": [{"id": "F1", "status": "resolved|still", "note": "why"}]}
Use "high" only for problems that must be fixed before merging. Empty lists are valid answers.
"""


class ReviewError(OfficeError):
    code = 422


class _ReadOnlyGate:
    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()

    async def before_tool(self, tool: str, args: dict[str, Any], tool_use_id: str | None) -> GateResult:
        if tool not in READ_ONLY:
            return GateResult(GateDecision.DENY, "The reviewer can only read (Read, Grep, Glob).")
        raw = args.get("file_path") or args.get("path")
        if raw:
            target = Path(str(raw))
            target = (self.workspace / target).resolve() if not target.is_absolute() else target.resolve()
            if not target.is_relative_to(self.workspace):
                return GateResult(GateDecision.DENY, "Stay inside the workspace.")
        return GateResult(GateDecision.ALLOW)

    async def after_tool(self, tool: str, args: dict[str, Any], result: Any) -> None:
        return None


class ReviewService:
    def __init__(self, office: Office) -> None:
        self.office = office

    async def review(self, ctx: CheckContext) -> CheckResult:
        from actoroffice.services.checks import CheckResult, merge_base

        office = self.office
        if not office.settings.ai_review:
            return CheckResult(id="review_clean", status="skipped", summary="review độc lập đang tắt (ai_review)")
        if ctx.workspace is None or not (ctx.workspace / ".git").exists():
            return CheckResult(id="review_clean", status="skipped", summary="không phải git worktree")
        srun_id = ctx.stage_run.id
        state = (await office.stages.get_run(srun_id)).review
        rounds = await self._rounds(srun_id)
        if rounds >= MAX_ROUNDS:
            if not state.get("parsed"):
                return CheckResult(id="review_clean", status="skipped", summary=f"đã review {rounds} vòng; reviewer không trả kết quả có cấu trúc")
            return self._result(ctx, state, rounds)
        base = ctx.project.main_branch if ctx.project else "main"
        start = await merge_base(ctx.workspace, base) or "HEAD"
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(ctx.workspace), "diff", "--no-color", start, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        diff = out.decode("utf-8", "replace")
        if not diff.strip():
            if state.get("findings"):
                return self._result(ctx, state, rounds)
            return CheckResult(id="review_clean", status="skipped", summary="không có thay đổi để review")
        compiled = await office.rules.compile(ticket=ctx.ticket, project=ctx.project, stage=ctx.stage.id)
        rules = "\n".join(f"- {line}" for line in compiled.guideline_lines()) or "(none)"
        sections = [
            "# Requirements (approved)",
            ctx.docs.get("requirements", "(none)")[:12_000],
            "# Plan (approved)",
            ctx.docs.get("plan", "(none)")[:8_000],
            "# Team rules",
            rules,
        ]
        if state.get("findings"):
            sections += ["# Earlier findings (do not report them again)", earlier_findings(state["findings"])]
        sections += [
            "# The change (git diff against the base branch)",
            diff[:MAX_DIFF] + ("\n…(diff truncated: read the files for the rest)" if len(diff) > MAX_DIFF else ""),
        ]
        ai = await office.ensure_ai()
        run_id = new_id("run")
        await office.runs.start(Run(id=run_id, actor_id=ai.id, kind=RunKind.SYSTEM, ticket_id=ctx.ticket.id))
        await office.db.execute("UPDATE runs SET purpose = 'reviewer', stage_run_id = ? WHERE id = ?", (srun_id, run_id))
        spec = TurnSpec(
            run_id=run_id,
            prompt="\n\n".join(sections),
            cwd=ctx.workspace,
            system_prompt=REVIEWER + f"\nWrite claims and notes in {office.settings.language}.",
            model=office.settings.ai_model or office.settings.default_model,
            tools=sorted(READ_ONLY),
            max_turns=20,
            show_thinking=False,
        )

        async def sink(kind: str, payload: dict[str, Any]) -> None:
            await office.runs.event(run_id, kind, payload)

        result = None
        try:
            result = await office.runtime.run_turn(spec, _ReadOnlyGate(ctx.workspace), sink)
            result.usage.model = result.usage.model or spec.model or ""
        finally:
            ok = result is not None and result.status == TurnStatus.COMPLETED
            await office.runs.finish(
                run_id,
                RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
                usage=result.usage if result is not None else None,
                cost_usd=result.cost_usd if result is not None else 0.0,
            )
        parsed = parse_review(result.text) if result is not None else None
        # Read the findings again after the reviewer's turn before writing them back.
        state = (await office.stages.get_run(srun_id)).review
        rounds = await self._rounds(srun_id)
        if parsed is None:
            if state.get("findings"):
                return self._result(ctx, state, rounds, note="reviewer vòng này không trả kết quả có cấu trúc")
            return CheckResult(id="review_clean", status="skipped", summary="reviewer không trả kết quả có cấu trúc")
        apply_previous(state, parsed["previous"])
        assign_ids(state, parsed["findings"], rounds)
        state["parsed"] = int(state.get("parsed") or 0) + 1
        await office.stages.save_review(srun_id, state)
        return self._result(ctx, state, rounds)

    def _result(self, ctx: CheckContext, state: dict[str, Any], rounds: int, note: str = "") -> CheckResult:
        result = evaluate(state, rounds, note=note)
        if ctx.run_id:
            folder = self.office.settings.runs_dir / ctx.run_id
            folder.mkdir(parents=True, exist_ok=True)
            lines = [finding_line(f) + (f" — {reply_text(f)}" if f.get("reply") else "") for f in state.get("findings") or []]
            (folder / "check-review-clean.txt").write_text("\n".join(lines) or "no findings", encoding="utf-8")
            result.output_ref = "check-review-clean.txt"
        return result

    async def reply(self, ticket_id: int, fid: str, action: str, note: str) -> dict[str, Any]:
        """The AI answers one finding of the ticket's current stage run. Nothing is saved when the input is wrong."""
        office = self.office
        action = action.strip().lower()
        note = note.strip()
        if action not in ACTIONS:
            raise ReviewError(f"action must be one of: {', '.join(ACTIONS)}")
        if action == "wontfix" and not note:
            raise ReviewError("wontfix needs a reason in note: why the finding is not fixed")
        ticket = await office.tickets.get(ticket_id)
        srun = await office.stages.current_run(ticket)
        if srun is not None and srun.finished_at is not None:
            # The gate card and /stage already show this run's answers; they must not change behind them.
            raise ReviewError("this stage is already finished: review findings can only be answered before stage_complete passes")
        findings = (srun.review.get("findings") or []) if srun else []
        finding = next((f for f in findings if str(f.get("id")).upper() == fid.strip().upper()), None)
        if srun is None or finding is None:
            known = ", ".join(str(f.get("id")) for f in findings) or "none"
            raise ReviewError(f"no review finding '{fid}' in this stage (known ids: {known})")
        finding["reply"] = {"action": action, "note": note[:1000], "after_round": await self._rounds(srun.id)}
        finding["verified"] = False
        await office.stages.save_review(srun.id, srun.review)
        return finding

    async def _rounds(self, stage_run_id: int) -> int:
        row = await self.office.db.fetchone(
            "SELECT COUNT(*) AS n FROM runs WHERE purpose = 'reviewer' AND stage_run_id = ?", (stage_run_id,)
        )
        return int(row["n"]) if row else 0


def severity(value: Any) -> str:
    text = str(value or "").strip().lower()
    if text in ("high", "medium", "low"):
        return text
    return "high" if text in ("critical", "blocker") else "medium"  # an unknown level is not silently low


def assign_ids(state: dict[str, Any], new: list[dict[str, Any]], round_no: int) -> None:
    """Give new findings the next ids (F1, F2…); ids already given never change."""
    findings = state.setdefault("findings", [])
    n = int(state.get("next") or len(findings) + 1)
    for f in new:
        findings.append(
            {
                "id": f"F{n}",
                "severity": severity(f.get("severity")),
                "file": str(f.get("file") or ""),
                "line": f.get("line"),
                "claim": str(f.get("claim") or "")[:1000],
                "evidence": str(f.get("evidence") or "")[:1000],
                "round": round_no,
                "reply": None,
                "verified": False,
                "reopened": False,
                "reviewer_note": "",
            }
        )
        n += 1
    state["next"] = n


def apply_previous(state: dict[str, Any], previous: list[dict[str, Any]]) -> None:
    """The reviewer's verdict on findings replied `fixed`: still there → open again; resolved → verified.
    Verdicts on wontfix or unanswered findings are ignored (wontfix goes to the people)."""
    by_id = {str(f.get("id")).upper(): f for f in state.get("findings") or []}
    for p in previous:
        f = by_id.get(str(p.get("id") or "").strip().upper())
        if f is None or not f.get("reply") or f["reply"].get("action") != "fixed":
            continue
        status = str(p.get("status") or "").strip().lower()
        if status == "still":
            f["reply"] = None
            f["reopened"] = True
            f["verified"] = False
            f["reviewer_note"] = str(p.get("note") or "")[:1000]
        elif status == "resolved":
            f["verified"] = True


def is_open(f: dict[str, Any]) -> bool:
    return f.get("severity") in SERIOUS and not f.get("reply")


def is_unverified(f: dict[str, Any], rounds: int) -> bool:
    reply = f.get("reply") or {}
    return reply.get("action") == "fixed" and not f.get("verified") and rounds >= MAX_ROUNDS


def finding_line(f: dict[str, Any]) -> str:
    return f"{f.get('id')} [{f.get('severity')}] {f.get('file')}:{f.get('line')} {f.get('claim')}"


def reply_text(f: dict[str, Any]) -> str:
    reply = f.get("reply") or {}
    label = "đã sửa" if reply.get("action") == "fixed" else "không sửa"
    return label + (f": {reply['note']}" if reply.get("note") else "")


def evaluate(state: dict[str, Any], rounds: int, note: str = "") -> CheckResult:
    """The check's result from the stored findings and replies (the same with or without a reviewer round)."""
    from actoroffice.services.checks import CheckResult

    findings = list(state.get("findings") or [])
    serious = [f for f in findings if f.get("severity") in SERIOUS]
    counts = {
        "high": sum(f.get("severity") == "high" for f in findings),
        "medium": sum(f.get("severity") == "medium" for f in findings),
        "low": sum(f.get("severity") == "low" for f in findings),
        "fixed": sum((f.get("reply") or {}).get("action") == "fixed" for f in serious),
        "wontfix": sum((f.get("reply") or {}).get("action") == "wontfix" for f in serious),
        "open": sum(is_open(f) for f in serious),
    }
    replies = f"{counts['fixed']} đã sửa, {counts['wontfix']} không sửa" + (f", {counts['open']} chưa trả lời" if counts["open"] else "")
    summary = f"{counts['high']} high · {counts['medium']} medium ({replies}) · {counts['low']} low"
    if rounds >= MAX_ROUNDS:
        summary += f"; đã review {rounds} vòng"
    if note:
        summary += f"; {note}"
    opened = [f for f in serious if is_open(f)]
    if opened:
        lines = [finding_line(f) + (f" (mở lại: {f['reviewer_note']})" if f.get("reopened") and f.get("reviewer_note") else "") for f in opened]
        summary += "\nCòn điểm chưa trả lời (sửa rồi gọi review_reply fixed, hoặc review_reply wontfix kèm lý do):\n"
        summary += "\n".join(f"- {line}" for line in lines[:MAX_LISTED])
        if len(lines) > MAX_LISTED:
            summary += f"\n…và {len(lines) - MAX_LISTED} điểm nữa"
    ordered = serious + [f for f in findings if f.get("severity") not in SERIOUS]  # medium/high first: they reach the gate
    data = [{**f, "open": is_open(f), "unverified": is_unverified(f, rounds)} for f in ordered[:MAX_DATA]]
    return CheckResult(
        id="review_clean",
        status="failed" if opened else "passed",
        summary=summary,
        data={"findings": data, "round": rounds, "counts": counts},
    )


def gate_items(data: dict[str, Any] | None) -> list[dict[str, Any]]:
    """Medium/high findings with the AI's reply and a label, for the gate card and the pr pane."""
    items = []
    for f in (data or {}).get("findings") or []:
        if f.get("severity") not in SERIOUS:
            continue
        label = "chưa xác minh" if f.get("unverified") else "mở lại" if f.get("reopened") and not f.get("reply") else ""
        items.append(
            {
                "id": f.get("id"),
                "severity": f.get("severity"),
                "where": f"{f.get('file')}:{f.get('line')}",
                "claim": f.get("claim"),
                "reply": reply_text(f) if f.get("reply") else "chưa trả lời",
                "label": label,
                "reviewer_note": f.get("reviewer_note") or "",
            }
        )
    return items


def earlier_findings(findings: list[dict[str, Any]]) -> str:
    lines = []
    for f in findings:
        reply = f.get("reply") or {}
        if reply.get("action") == "fixed":
            tail = f"engineer says fixed: {reply.get('note') or '-'} → RE-CHECK"
        elif reply.get("action") == "wontfix":
            tail = f"engineer will not fix: {reply.get('note')} → do not re-check (people decide)"
        else:
            tail = "no reply yet → do not report again"
        lines.append(f"- {finding_line(f)} — {tail}")
    return "\n".join(lines)


def parse_review(text: str) -> dict[str, list[dict[str, Any]]] | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    findings = data.get("findings") if isinstance(data, dict) else None
    if not isinstance(findings, list):
        return None
    previous = data.get("previous")
    return {
        "findings": [f for f in findings if isinstance(f, dict)],
        "previous": [p for p in previous if isinstance(p, dict)] if isinstance(previous, list) else [],
    }


def parse_findings(text: str) -> list[dict[str, Any]] | None:
    parsed = parse_review(text)
    return parsed["findings"] if parsed is not None else None
