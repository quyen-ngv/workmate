"""Independent review before a stage finishes (check `review_clean`): a second session with a fresh context and
an adversarial prompt reads the diff against the requirements and the plan and returns findings as JSON. High
severity findings fail the check (the AI fixes them); after two rounds the remaining findings go to the people
at the gate instead of looping. The reviewer can only read (Read/Grep/Glob inside the workspace).
"""

from __future__ import annotations

import asyncio
import json
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.domain.ids import new_id
from actoroffice.domain.run import Run, RunKind, RunStatus, TurnStatus
from actoroffice.runtimes.base import GateDecision, GateResult, TurnSpec

if TYPE_CHECKING:
    from actoroffice.app import Office
    from actoroffice.services.checks import CheckContext, CheckResult

MAX_DIFF = 60_000
MAX_ROUNDS = 2
READ_ONLY = {"Read", "Grep", "Glob"}

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

Answer with ONLY a JSON object, no prose:
{"findings": [{"severity": "high|medium|low", "file": "path", "line": 0, "claim": "what is wrong",
"evidence": "the code or AC that shows it"}]}
Use "high" only for problems that must be fixed before merging. An empty list is a valid answer.
"""


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
        rounds = await self._rounds(ctx.stage_run.id)
        if rounds >= MAX_ROUNDS:
            return CheckResult(
                id="review_clean",
                status="skipped",
                summary=f"đã review {rounds} vòng; các điểm còn lại chuyển cho người duyệt",
            )
        base = ctx.project.main_branch if ctx.project else "main"
        start = await merge_base(ctx.workspace, base) or "HEAD"
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(ctx.workspace), "diff", "--no-color", start, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        diff = out.decode("utf-8", "replace")
        if not diff.strip():
            return CheckResult(id="review_clean", status="skipped", summary="không có thay đổi để review")
        compiled = await office.rules.compile(ticket=ctx.ticket, project=ctx.project, stage=ctx.stage.id)
        rules = "\n".join(f"- {line}" for line in compiled.guideline_lines()) or "(none)"
        prompt = "\n\n".join(
            [
                "# Requirements (approved)",
                ctx.docs.get("requirements", "(none)")[:12_000],
                "# Plan (approved)",
                ctx.docs.get("plan", "(none)")[:8_000],
                "# Team rules",
                rules,
                "# The change (git diff against the base branch)",
                diff[:MAX_DIFF] + ("\n…(diff truncated: read the files for the rest)" if len(diff) > MAX_DIFF else ""),
            ]
        )
        ai = await office.ensure_ai()
        run_id = new_id("run")
        await office.runs.start(Run(id=run_id, actor_id=ai.id, kind=RunKind.SYSTEM, ticket_id=ctx.ticket.id))
        await office.db.execute("UPDATE runs SET purpose = 'reviewer', stage_run_id = ? WHERE id = ?", (ctx.stage_run.id, run_id))
        spec = TurnSpec(
            run_id=run_id,
            prompt=prompt,
            cwd=ctx.workspace,
            system_prompt=REVIEWER + f"\nWrite claims in {office.settings.language}.",
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
        finally:
            ok = result is not None and result.status == TurnStatus.COMPLETED
            await office.runs.finish(
                run_id,
                RunStatus.SUCCEEDED if ok else RunStatus.FAILED,
                usage=result.usage if result is not None else None,
                cost_usd=result.cost_usd if result is not None else 0.0,
            )
        findings = parse_findings(result.text) if result is not None else None
        if findings is None:
            return CheckResult(id="review_clean", status="skipped", summary="reviewer không trả kết quả có cấu trúc")
        high = [f for f in findings if str(f.get("severity")).lower() == "high"]
        lines = [f"[{f.get('severity')}] {f.get('file')}:{f.get('line')} {f.get('claim')}" for f in findings]
        summary = f"{len(findings)} điểm ({len(high)} nghiêm trọng)" + (": " + lines[0] if lines else "")
        if ctx.run_id:
            folder = office.settings.runs_dir / ctx.run_id
            folder.mkdir(parents=True, exist_ok=True)
            (folder / "check-review-clean.txt").write_text("\n".join(lines) or "no findings", encoding="utf-8")
        return CheckResult(
            id="review_clean",
            status="failed" if high else "passed",
            summary=summary,
            output_ref="check-review-clean.txt" if ctx.run_id else None,
            data={"findings": findings[:40], "round": await self._rounds(ctx.stage_run.id)},
        )

    async def _rounds(self, stage_run_id: int) -> int:
        row = await self.office.db.fetchone(
            "SELECT COUNT(*) AS n FROM runs WHERE purpose = 'reviewer' AND stage_run_id = ?", (stage_run_id,)
        )
        return int(row["n"]) if row else 0


def parse_findings(text: str) -> list[dict[str, Any]] | None:
    m = re.search(r"\{.*\}", text or "", re.DOTALL)
    if not m:
        return None
    try:
        data = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    findings = data.get("findings") if isinstance(data, dict) else None
    return [f for f in findings if isinstance(f, dict)] if isinstance(findings, list) else None
