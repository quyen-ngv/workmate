"""Checks the office runs before a stage may finish. The AI never marks its own work as passed: these results
come from the machine (documents present, acceptance criteria testable, tests changed, the repo's own
build/test/lint commands from the project profile).

A check id is a builtin name or "repo:<profile key>". Unknown ids and checks that do not apply (no git
workspace, no profile entry) are reported as skipped, never as failed.
"""

from __future__ import annotations

import asyncio
import contextlib
import fnmatch
import os
import re
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from pydantic import BaseModel

from actoroffice.domain.errors import OfficeError
from actoroffice.domain.stage import StageDef, StageRun
from actoroffice.domain.work import Project, Ticket

if TYPE_CHECKING:
    from actoroffice.app import Office

OUTPUT_TAIL = 4000
DEFAULT_TEST_GLOBS = ["**/test/**", "**/tests/**", "**/__tests__/**", "**/*_test.*", "**/*.test.*", "**/*.spec.*", "**/test_*", "**/*Test.*", "**/*Tests.*", "**/spec/**"]
AC_RE = re.compile(r"\bAC[-\s]?(\d+)\b", re.IGNORECASE)
GIVEN = re.compile(r"\b(given|cho|giả sử)\b", re.IGNORECASE)
WHEN = re.compile(r"\b(when|khi)\b", re.IGNORECASE)
THEN = re.compile(r"\b(then|thì)\b", re.IGNORECASE)
OPTION = re.compile(r"(?im)^\s*(?:#+\s*|[-*]\s*|\d+[.)]\s*)?(?:\*\*)?(?:phương án|option|approach|cách)\s*[A-Z0-9]")


class CheckResult(BaseModel):
    id: str
    status: str  # passed | failed | skipped
    summary: str
    output_ref: str | None = None
    data: dict[str, Any] = {}

    @property
    def failed(self) -> bool:
        return self.status == "failed"


@dataclass
class CheckContext:
    office: Office
    ticket: Ticket
    stage: StageDef
    stage_run: StageRun
    workspace: Path | None
    project: Project | None
    run_id: str | None
    docs: dict[str, str] = field(default_factory=dict)


class CheckRunner:
    def __init__(self, office: Office) -> None:
        self.office = office

    async def run(
        self, ticket: Ticket, stage: StageDef, stage_run: StageRun, *, workspace: Path | None, run_id: str | None, only: list[str] | None = None
    ) -> list[CheckResult]:
        office = self.office
        project = None
        if ticket.project_id:
            with contextlib.suppress(OfficeError):
                project = await office.projects.get(ticket.project_id)
        docs = {d.key: d.body for d in await office.tickets.list_documents(ticket.id)}
        ctx = CheckContext(office, ticket, stage, stage_run, workspace, project, run_id, docs)
        results: list[CheckResult] = []
        for check_id in only if only is not None else stage.checks:
            try:
                result = await self._one(check_id, ctx)
            except Exception as exc:  # a broken check must never pass silently
                result = CheckResult(id=check_id, status="failed", summary=f"check crashed: {type(exc).__name__}: {exc}")
            results.append(result)
            if run_id:
                await office.runs.event(run_id, "check", result.model_dump())
        return results

    async def _one(self, check_id: str, ctx: CheckContext) -> CheckResult:
        if check_id.startswith("repo:"):
            return await self._repo(check_id, ctx)
        fn = BUILTIN.get(check_id)
        if fn is None:
            return CheckResult(id=check_id, status="skipped", summary="chưa hỗ trợ check này")
        return await fn(ctx)

    async def _repo(self, check_id: str, ctx: CheckContext) -> CheckResult:
        key = check_id.split(":", 1)[1]
        profile = ctx.project.profile if ctx.project else {}
        argv_list = command_list(profile, key)
        if not argv_list:
            return CheckResult(id=check_id, status="skipped", summary=f"project chưa khai báo '{key}' trong profile")
        if ctx.workspace is None:
            return CheckResult(id=check_id, status="skipped", summary="không có workspace")
        outputs: list[str] = []
        timeout = float(profile.get("timeouts", {}).get(key, 900)) if isinstance(profile.get("timeouts"), dict) else 900.0
        for argv in argv_list:
            code, out, secs = await run_argv(argv, ctx.workspace, timeout=timeout, env=self._env(ctx))
            outputs.append(f"$ {' '.join(argv)}\n{out}\n(exit {code}, {secs:.0f}s)")
            if code != 0:
                ref = self._save(ctx, check_id, "\n\n".join(outputs))
                return CheckResult(id=check_id, status="failed", summary=f"`{' '.join(argv)[:80]}` → exit {code}", output_ref=ref, data={"exit": code})
        ref = self._save(ctx, check_id, "\n\n".join(outputs))
        return CheckResult(id=check_id, status="passed", summary=f"{len(argv_list)} lệnh chạy thành công", output_ref=ref)

    def _env(self, ctx: CheckContext) -> dict[str, str]:
        keep = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "LANG", "JAVA_HOME", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA")
        env = {k: v for k, v in os.environ.items() if k in keep}
        env["GIT_CEILING_DIRECTORIES"] = str(self.office.settings.workspaces_dir.resolve())
        env["CI"] = "1"
        return env

    def _save(self, ctx: CheckContext, check_id: str, text: str) -> str | None:
        if not ctx.run_id:
            return None
        folder = self.office.settings.runs_dir / ctx.run_id
        folder.mkdir(parents=True, exist_ok=True)
        name = f"check-{re.sub(r'[^a-z0-9]+', '-', check_id.lower())}.txt"
        (folder / name).write_text(self.office.redact(text[-200_000:]), encoding="utf-8")
        return name


def command_list(profile: dict[str, Any], key: str) -> list[list[str]]:
    """profile[key] is an argv list, a list of argv lists, or a string (split on spaces); `os.<platform>` overrides."""
    override = (profile.get("os") or {}).get(_platform_key(), {}) if isinstance(profile.get("os"), dict) else {}
    value = override.get(key, profile.get(key)) if isinstance(override, dict) else profile.get(key)
    if not value:
        return []
    if isinstance(value, str):
        return [value.split()]
    if isinstance(value, list) and value and all(isinstance(v, str) for v in value):
        return [list(value)]
    if isinstance(value, list):
        return [list(v) if isinstance(v, list) else str(v).split() for v in value if v]
    return []


def _platform_key() -> str:
    if sys.platform == "win32":
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


async def run_argv(argv: list[str], cwd: Path, *, timeout: float, env: dict[str, str] | None = None) -> tuple[int, str, float]:
    """Run one command (argv, no shell), kill its whole process tree on timeout. Returns (exit, output tail, seconds)."""
    started = time.monotonic()
    exe = argv[0]
    if sys.platform == "win32":
        import shutil

        exe = shutil.which(exe) or exe  # resolves mvn.cmd, gradlew.bat …
    try:
        proc = await asyncio.create_subprocess_exec(
            exe, *argv[1:], cwd=str(cwd), env=env, stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
        )
    except OSError as exc:
        return 127, f"cannot start {argv[0]}: {exc}", 0.0
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        _kill_tree(proc.pid)
        with contextlib.suppress(Exception):
            await proc.wait()
        return 124, f"timeout after {timeout:.0f}s", time.monotonic() - started
    text = out.decode("utf-8", "replace").replace("\r\n", "\n")
    return proc.returncode or 0, text[-OUTPUT_TAIL * 10 :], time.monotonic() - started


def _kill_tree(pid: int) -> None:
    with contextlib.suppress(psutil.Error):
        parent = psutil.Process(pid)
        for child in parent.children(recursive=True):
            with contextlib.suppress(psutil.Error):
                child.kill()
        parent.kill()


# ---- builtin checks ----------------------------------------------------------------------------------


async def check_outputs(ctx: CheckContext) -> CheckResult:
    missing = []
    for wanted in ctx.stage.outputs:
        options = wanted.split("|")
        if not any(ctx.docs.get(o, "").strip() for o in options):
            missing.append(wanted)
    if missing:
        return CheckResult(id="outputs", status="failed", summary=f"thiếu tài liệu: {', '.join(missing)} (dùng doc_put)")
    return CheckResult(id="outputs", status="passed", summary="đủ tài liệu: " + ", ".join(ctx.stage.outputs) if ctx.stage.outputs else "không yêu cầu tài liệu")


def ac_blocks(text: str) -> dict[str, str]:
    """AC-n → its text (until the next AC id)."""
    matches = list(AC_RE.finditer(text))
    blocks: dict[str, str] = {}
    for i, m in enumerate(matches):
        end = matches[i + 1].start() if i + 1 < len(matches) else len(text)
        key = f"AC-{m.group(1)}"
        blocks[key] = blocks.get(key, "") + text[m.start() : end]
    return blocks


async def check_ac_testable(ctx: CheckContext) -> CheckResult:
    text = ctx.docs.get("requirements", "")
    blocks = ac_blocks(text)
    if not blocks:
        return CheckResult(id="ac_testable", status="failed", summary="requirements chưa có acceptance criteria đánh số AC-1, AC-2…")
    weak = [k for k, body in blocks.items() if not (GIVEN.search(body) and WHEN.search(body) and THEN.search(body))]
    if weak:
        return CheckResult(id="ac_testable", status="failed", summary=f"AC chưa ở dạng Given/When/Then: {', '.join(weak)}", data={"weak": weak})
    return CheckResult(id="ac_testable", status="passed", summary=f"{len(blocks)} AC đều kiểm thử được", data={"acs": list(blocks)})


async def check_plan_has_ac_map(ctx: CheckContext) -> CheckResult:
    acs = list(ac_blocks(ctx.docs.get("requirements", "")))
    if not acs:
        return CheckResult(id="plan_has_ac_map", status="skipped", summary="requirements không có AC đánh số")
    plan = ctx.docs.get("plan", "")
    missing = [a for a in acs if not re.search(rf"\bAC[-\s]?{a.split('-')[1]}\b", plan, re.IGNORECASE)]
    if missing:
        return CheckResult(id="plan_has_ac_map", status="failed", summary=f"plan chưa map test cho: {', '.join(missing)}", data={"missing": missing})
    return CheckResult(id="plan_has_ac_map", status="passed", summary=f"plan map đủ {len(acs)} AC")


async def check_plan_has_options(ctx: CheckContext) -> CheckResult:
    count = len(OPTION.findall(ctx.docs.get("plan", "")))
    if count < 2:
        return CheckResult(id="plan_has_options", status="failed", summary="plan cần ít nhất 2 phương án (Phương án A / B…) để Dev chọn")
    return CheckResult(id="plan_has_options", status="passed", summary=f"{count} phương án")


async def check_steps_done(ctx: CheckContext) -> CheckResult:
    steps = ctx.stage_run.steps
    if not steps:
        if ctx.stage.requires_steps:
            return CheckResult(id="steps_done", status="failed", summary="chưa khai các bước (plan_steps)")
        return CheckResult(id="steps_done", status="skipped", summary="không có bước")
    left = [s.text for s in steps if not s.done]
    if left:
        return CheckResult(id="steps_done", status="failed", summary=f"còn {len(left)} bước chưa xong: {left[0][:80]}")
    return CheckResult(id="steps_done", status="passed", summary=f"{len(steps)}/{len(steps)} bước xong")


async def check_tests_changed(ctx: CheckContext) -> CheckResult:
    if ctx.workspace is None or not (ctx.workspace / ".git").exists():
        return CheckResult(id="tests_changed", status="skipped", summary="không phải git worktree")
    base = ctx.project.main_branch if ctx.project else "main"
    files = await changed_files(ctx.workspace, base)
    if not files:
        return CheckResult(id="tests_changed", status="failed", summary="chưa có thay đổi nào so với " + base)
    globs = (ctx.project.profile.get("test_glob") if ctx.project else None) or DEFAULT_TEST_GLOBS
    tests = [f for f in files if is_test(f, globs)]
    if not tests:
        return CheckResult(id="tests_changed", status="failed", summary=f"{len(files)} file đổi nhưng không có file test nào", data={"files": files[:50]})
    return CheckResult(id="tests_changed", status="passed", summary=f"{len(tests)} file test trong {len(files)} file đổi", data={"tests": tests[:50]})


def is_test(path: str, globs: list[str]) -> bool:
    p = "/" + path.replace("\\", "/").lstrip("/")
    for glob in globs:
        pat = glob.replace("\\", "/")
        if pat.startswith("**/"):
            pat = "*/" + pat[3:]
        elif not pat.startswith(("*", "/")):
            pat = "*/" + pat
        if fnmatch.fnmatch(p, pat):
            return True
    return False


async def changed_files(workspace: Path, base: str) -> list[str]:
    files: set[str] = set()
    for args in (["diff", "--name-only", f"{base}...HEAD"], ["diff", "--name-only", "HEAD"], ["ls-files", "--others", "--exclude-standard"]):
        proc = await asyncio.create_subprocess_exec(
            "git", "-C", str(workspace), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
        )
        out, _ = await proc.communicate()
        if proc.returncode == 0:
            files |= {line.strip() for line in out.decode("utf-8", "replace").splitlines() if line.strip()}
    return sorted(files)


async def check_pr_linked(ctx: CheckContext) -> CheckResult:
    if ctx.ticket.pr_url:
        return CheckResult(id="pr_linked", status="passed", summary=ctx.ticket.pr_url)
    return CheckResult(id="pr_linked", status="failed", summary="chưa có link PR (pr_link)")


async def check_automation_saved(ctx: CheckContext) -> CheckResult:
    row = await ctx.office.db.fetchone("SELECT name FROM automations WHERE ticket_id = ? ORDER BY created_at DESC LIMIT 1", (ctx.ticket.id,))
    if not row:
        return CheckResult(id="automation_saved", status="failed", summary="chưa lưu automation (automation_save)")
    return CheckResult(id="automation_saved", status="passed", summary=f"automation {row['name']}")


BUILTIN = {
    "outputs": check_outputs,
    "ac_testable": check_ac_testable,
    "plan_has_ac_map": check_plan_has_ac_map,
    "plan_has_options": check_plan_has_options,
    "steps_done": check_steps_done,
    "tests_changed": check_tests_changed,
    "pr_linked": check_pr_linked,
    "automation_saved": check_automation_saved,
}
