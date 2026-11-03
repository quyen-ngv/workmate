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
import logging
import os
import re
import sys
import time
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import TYPE_CHECKING, Any

import psutil
from pydantic import BaseModel

from actoroffice.domain.dossier import SYNC_SECTION, parse_dossier, render_dossier, set_sync_line, sync_line
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.stage import StageDef, StageRun
from actoroffice.domain.work import Project, Ticket

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)

OUTPUT_TAIL = 4000
# Used only when the repo has no identity of its own (as for a fresh project, services/projects.py)
IDENTITY = ["-c", "user.name=actor-office", "-c", "user.email=office@localhost", "-c", "commit.gpgsign=false"]
NOT_SYNCED = "chưa cập nhật theo nhánh chính"
DEFAULT_TEST_GLOBS = ["**/test/**", "**/tests/**", "**/__tests__/**", "**/*_test.*", "**/*.test.*", "**/*.spec.*", "**/test_*", "**/*Test.*", "**/*Tests.*", "**/spec/**"]
AC_RE = re.compile(r"\bAC[-\s]?(\d+)\b", re.IGNORECASE)
GIVEN = re.compile(r"\b(given|cho|giả sử)\b", re.IGNORECASE)
WHEN = re.compile(r"\b(when|khi)\b", re.IGNORECASE)
THEN = re.compile(r"\b(then|thì)\b", re.IGNORECASE)
OPTION = re.compile(r"(?im)^\s*(?:#+\s*|[-*]\s*|\d+[.)]\s*)?(?:\*\*)?(?:phương án|option|approach|cách)\s*[A-Z0-9]")
# "AC-5 [review]: …": checked by a person at the gate instead of a test; only spaces may sit between id and tag
REVIEW_TAG = re.compile(r"\bAC[-\s]?(\d+)(?!\d)[ \t]*\[review\]", re.IGNORECASE)
# Same line only: an empty "Cách kiểm:" must not take the text of the next line
HOW_TO_CHECK = re.compile(r"(?im)\bcách kiểm[ \t]*(?:\*\*)?[ \t]*:[ \t]*(?:\*\*)?[ \t]*(.*)$")
NO_HANDOFF_LINE = "chưa ghi cách kiểm"


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
        not_synced = False
        for check_id in only if only is not None else stage.checks:
            if not_synced and check_id.startswith("repo:"):
                # The branch lacks main's new commits: building or testing it now would test old code (SI-21)
                result = CheckResult(id=check_id, status="skipped", summary=f"{NOT_SYNCED} (xem check main_synced)")
            else:
                try:
                    result = await self._one(check_id, ctx)
                except Exception as exc:  # a broken check must never pass silently
                    result = CheckResult(id=check_id, status="failed", summary=f"check crashed: {type(exc).__name__}: {exc}")
            not_synced = not_synced or (check_id == "main_synced" and result.failed)
            results.append(result)
            if run_id:
                await office.runs.event(run_id, "check", result.model_dump())
        return results

    async def _one(self, check_id: str, ctx: CheckContext) -> CheckResult:
        if check_id.startswith("repo:"):
            return await self._repo(check_id, ctx)
        if check_id.startswith("rule:"):
            return await self._rule(check_id, ctx)
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
        # The commit that was tested, taken before the command runs: flaky_rerun reuses this result when it is unchanged.
        state: dict[str, Any] = {}
        if key == "test":
            head, clean = await git_state(ctx.workspace)
            state = {"head": head, "clean": clean} if head else {}
        for argv in argv_list:
            code, out, secs = await run_argv(argv, ctx.workspace, timeout=timeout, env=self._env(ctx))
            outputs.append(f"$ {' '.join(argv)}\n{out}\n(exit {code}, {secs:.0f}s)")
            if code != 0:
                ref = self._save(ctx, check_id, "\n\n".join(outputs))
                return CheckResult(
                    id=check_id, status="failed", summary=f"`{' '.join(argv)[:80]}` → exit {code}", output_ref=ref, data={"exit": code, **state}
                )
        ref = self._save(ctx, check_id, "\n\n".join(outputs))
        return CheckResult(id=check_id, status="passed", summary=f"{len(argv_list)} lệnh chạy thành công", output_ref=ref, data=state)

    async def _rule(self, check_id: str, ctx: CheckContext) -> CheckResult:
        rule = await self.office.rules.get(check_id.split(":", 1)[1])
        builtin = rule.spec.get("builtin")
        if builtin:
            fn = BUILTIN.get(str(builtin))
            if fn is None:
                return CheckResult(id=check_id, status="skipped", summary=f"{rule.id}: chưa hỗ trợ check {builtin}")
            result = await fn(ctx)
            return result.model_copy(update={"id": check_id, "summary": f"{rule.id} {rule.title}: {result.summary}"})
        if ctx.workspace is None:
            return CheckResult(id=check_id, status="skipped", summary="không có workspace")
        argv_list = command_list({"cmd": rule.spec.get("cmd")}, "cmd")
        outputs = []
        for argv in argv_list:
            code, out, secs = await run_argv(argv, ctx.workspace, timeout=float(rule.spec.get("timeout_s") or 900), env=self._env(ctx))
            outputs.append(f"$ {' '.join(argv)}\n{out}\n(exit {code}, {secs:.0f}s)")
            if code != 0:
                ref = self._save(ctx, check_id, "\n\n".join(outputs))
                return CheckResult(id=check_id, status="failed", summary=f"{rule.id} {rule.title}: exit {code}", output_ref=ref)
        ref = self._save(ctx, check_id, "\n\n".join(outputs))
        return CheckResult(id=check_id, status="passed", summary=f"{rule.id} {rule.title}", output_ref=ref)

    def _env(self, ctx: CheckContext) -> dict[str, str]:
        return test_env(self.office)

    def _save(self, ctx: CheckContext, check_id: str, text: str) -> str | None:
        if not ctx.run_id:
            return None
        folder = self.office.settings.runs_dir / ctx.run_id
        folder.mkdir(parents=True, exist_ok=True)
        name = f"check-{re.sub(r'[^a-z0-9]+', '-', check_id.lower())}.txt"
        (folder / name).write_text(self.office.redact(text[-200_000:]), encoding="utf-8")
        return name


def test_env(office: Office) -> dict[str, str]:
    """Environment of the project's own commands (repo checks, the test of a merge): a short allow-list, no secrets."""
    keep = ("PATH", "HOME", "USERPROFILE", "SYSTEMROOT", "TEMP", "TMP", "LANG", "JAVA_HOME", "COMSPEC", "PATHEXT", "APPDATA", "LOCALAPPDATA")
    env = {k: v for k, v in os.environ.items() if k in keep}
    env["GIT_CEILING_DIRECTORIES"] = str(office.settings.workspaces_dir.resolve())
    env["CI"] = "1"
    env["PYTHONUTF8"] = "1"  # Windows pipes default to the ANSI code page; tools printing Vietnamese would crash
    env["PYTHONIOENCODING"] = "utf-8"
    return env


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


async def git_state(workspace: Path | None) -> tuple[str | None, bool | None]:
    """(HEAD sha, tree clean: no uncommitted change and no untracked file), or (None, None) outside a git workspace."""
    if workspace is None or not (workspace / ".git").exists():
        return None, None
    code, head, _ = await run_argv(["git", "rev-parse", "HEAD"], workspace, timeout=30)
    if code != 0 or not re.fullmatch(r"[0-9a-f]{40,64}", head.strip()):
        return None, None
    code, status, _ = await run_argv(["git", "status", "--porcelain"], workspace, timeout=30)
    return head.strip(), code == 0 and not status.strip()


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


def review_ids(text: str) -> set[str]:
    """AC ids tagged [review] right after the id, anywhere in the text."""
    return {f"AC-{m.group(1)}" for m in REVIEW_TAG.finditer(text)}


def how_to_check(body: str) -> str | None:
    """The text after "Cách kiểm:" in an AC body, or None when the line is missing or empty."""
    m = HOW_TO_CHECK.search(body)
    return (m.group(1).strip().strip("*").strip() or None) if m else None


def handoff_line(handoff: str, ac_id: str) -> str | None:
    """The first non-heading handoff line that names the AC (AC-1 does not match AC-11), cut to 300 chars."""
    pattern = re.compile(rf"(?i)(?<![a-z])AC[-_\s]?{ac_id.split('-')[1]}(?!\d)")
    for line in handoff.splitlines():
        if not line.lstrip().startswith("#") and pattern.search(line):
            return line.strip()[:300]
    return None


def review_acs(requirements: str, handoff: str) -> list[dict[str, str]]:
    """Each AC [review]: id, first line, how it is to be checked (requirements) and how it was (handoff)."""
    tagged = review_ids(requirements)
    items = []
    for key, body in ac_blocks(requirements).items():
        if key in tagged:
            first = body.strip().splitlines()[0].strip()[:200] if body.strip() else key
            items.append({"id": key, "title": first, "how": how_to_check(body) or "", "done": handoff_line(handoff, key) or NO_HANDOFF_LINE})
    return items


async def check_ac_testable(ctx: CheckContext) -> CheckResult:
    text = ctx.docs.get("requirements", "")
    blocks = ac_blocks(text)
    if not blocks:
        return CheckResult(id="ac_testable", status="failed", summary="requirements chưa có acceptance criteria đánh số AC-1, AC-2…")
    tagged = review_ids(text)
    review = [k for k in blocks if k in tagged]
    weak = [k for k, body in blocks.items() if not (GIVEN.search(body) and WHEN.search(body) and THEN.search(body))]
    no_how = [k for k in review if how_to_check(blocks[k]) is None]
    problems = []
    if weak:
        problems.append(f"AC chưa ở dạng Given/When/Then: {', '.join(weak)}")
    if no_how:
        problems.append(f"AC [review] thiếu dòng 'Cách kiểm:': {', '.join(no_how)}")
    if 2 * len(review) > len(blocks):
        problems.append(f"quá nhiều AC [review]: {len(review)}/{len(blocks)} AC, tối đa {len(blocks) // 2} (không quá một nửa)")
    if problems:
        return CheckResult(id="ac_testable", status="failed", summary="; ".join(problems), data={"weak": weak, "no_how": no_how, "review": review})
    summary = f"{len(blocks)} AC đều kiểm thử được"
    if review:
        summary += f", {len(review)} AC kiểm bằng review ({', '.join(review)})"
    return CheckResult(id="ac_testable", status="passed", summary=summary, data={"acs": list(blocks), "review": review})


async def check_plan_ac(ctx: CheckContext) -> CheckResult:
    """Quick workflow: the short plan states at least one acceptance criterion (AC-1 …)."""
    acs = list(ac_blocks(ctx.docs.get("plan", "")))
    if not acs:
        return CheckResult(id="plan_ac", status="failed", summary="plan chưa có tiêu chí chấp nhận AC-1 (1–3 dòng là đủ)")
    return CheckResult(id="plan_ac", status="passed", summary=f"{len(acs)} AC", data={"acs": acs})


async def check_plan_has_ac_map(ctx: CheckContext) -> CheckResult:
    requirements = ctx.docs.get("requirements", "")
    acs = list(ac_blocks(requirements))
    if not acs:
        return CheckResult(id="plan_has_ac_map", status="skipped", summary="requirements không có AC đánh số")
    plan = ctx.docs.get("plan", "")
    tagged = review_ids(requirements)
    review = [a for a in acs if a in tagged]
    missing = [a for a in acs if not re.search(rf"\bAC[-\s]?{a.split('-')[1]}\b", plan, re.IGNORECASE)]
    if missing:
        return CheckResult(id="plan_has_ac_map", status="failed", summary=f"plan chưa map test cho: {', '.join(missing)}", data={"missing": missing, "review": review})
    summary = f"plan map đủ {len(acs) - len(review)} AC test, {len(review)} AC review"
    if review:
        summary += f" ({', '.join(review)})"
    return CheckResult(id="plan_has_ac_map", status="passed", summary=summary, data={"review": review})


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
    if ctx.office.atlassian.code_host() is None and ctx.workspace is not None and (ctx.workspace / ".git").exists():
        # A repo without a code host is reviewed on this machine: the ticket's branch is what people merge.
        base = ctx.project.main_branch if ctx.project else "main"
        _code, branch, _ = await run_argv(["git", "rev-parse", "--abbrev-ref", "HEAD"], ctx.workspace, timeout=30)
        code, count, _ = await run_argv(["git", "rev-list", "--count", f"{base}..HEAD"], ctx.workspace, timeout=30)
        _code, dirty, _ = await run_argv(["git", "status", "--porcelain"], ctx.workspace, timeout=30)
        if code == 0 and count.strip().isdigit() and int(count.strip()) > 0 and not dirty.strip():
            return CheckResult(
                id="pr_linked", status="passed", summary=f"nhánh local {branch.strip()} ({count.strip()} commit) chờ người review và merge",
                data={"branch": branch.strip(), "base": base},
            )
        reason = "còn thay đổi chưa commit" if dirty.strip() else f"nhánh chưa có commit nào so với {base}"
        return CheckResult(id="pr_linked", status="failed", summary=f"chưa có PR và {reason}")
    return CheckResult(id="pr_linked", status="failed", summary="chưa có link PR (pr_link)")


async def check_main_synced(ctx: CheckContext) -> CheckResult:
    """Bring the ticket's branch up to date with the local main branch before the build stage's other checks (SI-21).

    The one check with a side effect: when main has commits the branch lacks and the worktree is clean, it merges
    main into the branch (a merge commit in the ticket's worktree; main itself is only read, nothing is fetched).
    A conflict is aborted, so on failure HEAD and the worktree are as they were."""
    ws = ctx.workspace
    if ws is None or not (ws / ".git").exists():
        return CheckResult(id="main_synced", status="skipped", summary="không phải git worktree")
    main = ctx.project.main_branch if ctx.project else "main"
    code, out, _ = await run_argv(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{main}"], ws, timeout=30)
    if code != 0:
        return CheckResult(id="main_synced", status="skipped", summary=f"repo không có nhánh chính {main}")
    main_sha = out.strip()
    branch = (await run_argv(["git", "rev-parse", "--abbrev-ref", "HEAD"], ws, timeout=30))[1].strip()
    if branch == main:
        return CheckResult(id="main_synced", status="skipped", summary=f"đang làm trên chính nhánh {main}")
    code, out, _ = await run_argv(["git", "merge-base", "--is-ancestor", main_sha, "HEAD"], ws, timeout=60)
    if code == 0:
        await _record_sync(ctx, main, main_sha)
        return CheckResult(id="main_synced", status="passed", summary=f"nhánh đã có mọi commit của {main} ({main_sha[:7]})",
                           data={"main": main, "main_sha": main_sha})
    if code != 1:
        return CheckResult(id="main_synced", status="failed", summary=f"không so được nhánh với {main}: {out.strip()[-300:]}")
    dirty = (await run_argv(["git", "status", "--porcelain"], ws, timeout=30))[1].strip()
    if dirty:
        return CheckResult(
            id="main_synced", status="failed",
            summary=f"{main} có commit mới nhưng worktree còn thay đổi chưa commit: hãy commit trước rồi gọi stage_complete lại "
                    f"(office sẽ merge {main} vào nhánh). Chưa commit: {' '.join(dirty.split())[:200]}",
        )
    identity = [] if (await run_argv(["git", "config", "user.email"], ws, timeout=30))[0] == 0 else IDENTITY
    message = f"Merge {main} into {branch} ({ctx.ticket.key})"
    code, out, _ = await run_argv(["git", *identity, "merge", "--no-ff", "--no-edit", "-m", message, main_sha], ws, timeout=300)
    if code != 0:
        files = (await run_argv(["git", "diff", "--name-only", "--diff-filter=U"], ws, timeout=30))[1].split()
        await run_argv(["git", "merge", "--abort"], ws, timeout=60)
        listed = ", ".join(files) if files else out.strip()[-300:]
        return CheckResult(
            id="main_synced", status="failed",
            summary=f"merge {main} ({main_sha[:7]}) vào nhánh bị xung đột ở: {listed}. Office đã huỷ merge, nhánh giữ nguyên. "
                    f"Hãy chạy `git merge {main}`, giải quyết conflict, commit, rồi gọi stage_complete lại.",
            data={"main": main, "main_sha": main_sha, "conflicts": files[:50]},
        )
    merge_sha = (await run_argv(["git", "rev-parse", "HEAD"], ws, timeout=30))[1].strip()
    try:
        await ctx.office.tickets.add_comment(
            ctx.ticket.id, f"🔄 Đã cập nhật nhánh {branch} theo {main} ({main_sha[:7]}), commit merge {merge_sha[:7]}.",
            author_kind="system", author_id=None,
        )
    except Exception:  # the merge is made: a failed note must not fail the check
        log.exception("comment on the main sync of %s", ctx.ticket.key)
    await _record_sync(ctx, main, main_sha)
    return CheckResult(id="main_synced", status="passed", summary=f"đã merge {main} ({main_sha[:7]}) vào nhánh, commit {merge_sha[:7]}",
                       data={"main": main, "main_sha": main_sha, "merge_sha": merge_sha})


async def _record_sync(ctx: CheckContext, main: str, main_sha: str) -> None:
    """Keep the office's one line in the dossier's tests section; written only when it changes. Never raises."""
    tickets = ctx.office.tickets
    try:
        for _ in range(3):  # optimistic revisions, as dossier_update
            doc = await tickets.get_document(ctx.ticket.id, "dossier")
            sections = parse_dossier(doc.body if doc else "")
            current = sections.get(SYNC_SECTION, "")
            updated = set_sync_line(current, sync_line(main, main_sha, date.today().isoformat()))
            if updated == current:
                return
            sections[SYNC_SECTION] = updated
            try:
                await tickets.put_document(ctx.ticket.id, "dossier", render_dossier(sections), author="office",
                                           base_revision=doc.revision if doc else None)
            except OfficeError:
                continue
            return
        log.warning("dossier of %s kept changing: main sync line not written", ctx.ticket.key)
    except Exception:
        log.exception("dossier line of the main sync of %s", ctx.ticket.key)


async def check_automation_saved(ctx: CheckContext) -> CheckResult:
    row = await ctx.office.db.fetchone("SELECT name FROM automations WHERE ticket_id = ? ORDER BY created_at DESC LIMIT 1", (ctx.ticket.id,))
    if not row:
        return CheckResult(id="automation_saved", status="failed", summary="chưa lưu automation (automation_save)")
    return CheckResult(id="automation_saved", status="passed", summary=f"automation {row['name']}")


BUILTIN = {
    "outputs": check_outputs,
    "ac_testable": check_ac_testable,
    "plan_has_ac_map": check_plan_has_ac_map,
    "plan_ac": check_plan_ac,
    "plan_has_options": check_plan_has_options,
    "steps_done": check_steps_done,
    "tests_changed": check_tests_changed,
    "pr_linked": check_pr_linked,
    "main_synced": check_main_synced,
    "automation_saved": check_automation_saved,
}


# ---- diff-based checks -------------------------------------------------------------------------------------

SECRET_PATTERNS = [
    (re.compile(r"-----BEGIN (RSA |EC |OPENSSH |DSA )?PRIVATE KEY-----"), "private key"),
    (re.compile(r"\bAKIA[0-9A-Z]{16}\b"), "AWS access key"),
    (re.compile(r"\bgh[pousr]_[A-Za-z0-9]{30,}\b"), "GitHub token"),
    (re.compile(r"\bxox[abpr]-[A-Za-z0-9-]{10,}\b"), "Slack token"),
    (re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}\b"), "Google API key"),
    (re.compile(r"\bATATT[0-9A-Za-z_\-=]{20,}\b"), "Atlassian API token"),
    (re.compile(r"\bsk-(ant-)?[A-Za-z0-9_\-]{20,}\b"), "API key"),
    (re.compile(r"(?i)\b(password|passwd|secret|api[_-]?key|token)\b\s*[:=]\s*[\"'][^\"'\s]{6,}[\"']"), "hard-coded credential"),
]
LOG_SMELLS = [
    (re.compile(r"\bSystem\.(out|err)\.print"), "System.out/err thay vì logger"),
    (re.compile(r"\bconsole\.log\("), "console.log còn sót"),
    (re.compile(r"\b(log|logger|LOG|LOGGER)\.(trace|debug|info|warn|warning|error)\(\s*f?[\"'][^\"']*[\"']\s*\+"), "nối chuỗi trong log (dùng tham số)"),
    (re.compile(r"(?i)\b(log|logger)\.\w+\(.*\b(password|passwd|token|secret)\b"), "log có thể lộ thông tin nhạy cảm"),
    (re.compile(r"\be\.printStackTrace\(\)"), "printStackTrace thay vì log có ngữ cảnh"),
]
PY_PRINT = re.compile(r"^\s*print\(")


async def merge_base(workspace: Path, base: str) -> str | None:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(workspace), "merge-base", "HEAD", base, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    return out.decode().strip() or None if proc.returncode == 0 else None


async def added_lines(workspace: Path, base: str) -> dict[str, list[tuple[int, str]]]:
    """file → [(line number, text)] added since the merge base (committed, uncommitted and untracked)."""
    start = await merge_base(workspace, base) or "HEAD"
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(workspace), "diff", "-U0", "--no-color", "--no-ext-diff", start,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
    )
    out, _ = await proc.communicate()
    result: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    line_no = 0
    for raw in out.decode("utf-8", "replace").splitlines():
        if raw.startswith("+++ "):
            path = raw[4:].strip()
            current = None if path == "/dev/null" else path.removeprefix("b/")
            continue
        if raw.startswith("@@"):
            m = re.search(r"\+(\d+)(?:,(\d+))?", raw)
            line_no = int(m.group(1)) if m else 0
            continue
        if current and raw.startswith("+") and not raw.startswith("+++"):
            result.setdefault(current, []).append((line_no, raw[1:]))
            line_no += 1
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(workspace), "ls-files", "--others", "--exclude-standard", stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL
    )
    out, _ = await proc.communicate()
    for path in out.decode("utf-8", "replace").splitlines():
        file = workspace / path
        if file.is_file() and file.stat().st_size < 2_000_000:
            text = file.read_text(encoding="utf-8", errors="replace")
            result[path] = list(enumerate(text.splitlines(), 1))
    return result


def _git_ctx(ctx: CheckContext) -> tuple[Path, str] | None:
    if ctx.workspace is None or not (ctx.workspace / ".git").exists():
        return None
    return ctx.workspace, (ctx.project.main_branch if ctx.project else "main")


async def check_ac_coverage(ctx: CheckContext) -> CheckResult:
    requirements = ctx.docs.get("requirements", "")
    acs = list(ac_blocks(requirements))
    if not acs:
        return CheckResult(id="ac_coverage", status="skipped", summary="requirements không có AC đánh số")
    git = _git_ctx(ctx)
    if git is None:
        return CheckResult(id="ac_coverage", status="skipped", summary="không phải git worktree")
    tagged = review_ids(requirements)
    review = [a for a in acs if a in tagged]
    tested = [a for a in acs if a not in tagged]
    globs = (ctx.project.profile.get("test_glob") if ctx.project else None) or DEFAULT_TEST_GLOBS
    added = await added_lines(*git)
    text = "\n".join(line for f, lines in added.items() if is_test(f, globs) for _, line in lines)
    # "AC-1", "ac1" in test_ac1_total, @DisplayName("AC 1 …"): any non-letter may precede it
    missing = [a for a in tested if not re.search(rf"(?i)(?<![a-z])AC[-_\s]?{a.split('-')[1]}(?!\d)", text)]
    # an AC [review] needs no test, but the handoff must say how it was checked
    handoff = ctx.docs.get("handoff", "")
    no_handoff = [a for a in review if handoff_line(handoff, a) is None]
    data = {"missing": missing, "no_handoff": no_handoff, "review": review}
    problems = []
    if missing:
        problems.append(f"chưa có test gắn với: {', '.join(missing)} (đặt AC id trong tên/annotation test)")
    if no_handoff:
        problems.append(f"AC [review] chưa có dòng cách kiểm trong handoff: {', '.join(no_handoff)} (thêm dòng có AC id vào bảng AC của handoff)")
    if problems:
        return CheckResult(id="ac_coverage", status="failed", summary="; ".join(problems), data=data)
    summary = f"{len(tested)}/{len(tested)} AC có test"
    if review:
        summary += f", {len(review)} AC kiểm bằng review: {', '.join(review)}"
    return CheckResult(id="ac_coverage", status="passed", summary=summary, data=data)


async def check_no_secrets(ctx: CheckContext) -> CheckResult:
    git = _git_ctx(ctx)
    if git is None:
        return CheckResult(id="no_secrets", status="skipped", summary="không phải git worktree")
    found = []
    for path, lines in (await added_lines(*git)).items():
        for no, line in lines:
            for pattern, label in SECRET_PATTERNS:
                if pattern.search(line):
                    found.append(f"{path}:{no} {label}")
    if found:
        return CheckResult(id="no_secrets", status="failed", summary=f"{len(found)} chỗ giống secret: {found[0]}", data={"found": found[:30]})
    return CheckResult(id="no_secrets", status="passed", summary="không thấy secret trong phần thay đổi")


async def check_log_lint(ctx: CheckContext) -> CheckResult:
    git = _git_ctx(ctx)
    if git is None:
        return CheckResult(id="log_lint", status="skipped", summary="không phải git worktree")
    globs = (ctx.project.profile.get("test_glob") if ctx.project else None) or DEFAULT_TEST_GLOBS
    found = []
    for path, lines in (await added_lines(*git)).items():
        if is_test(path, globs):
            continue
        for no, line in lines:
            for pattern, label in LOG_SMELLS:
                if pattern.search(line):
                    found.append(f"{path}:{no} {label}")
            if path.endswith(".py") and PY_PRINT.match(line) and "/scripts/" not in f"/{path}":
                found.append(f"{path}:{no} print() thay vì logging")
    if found:
        return CheckResult(id="log_lint", status="failed", summary=f"{len(found)} chỗ log chưa chuẩn: {found[0]}", data={"found": found[:30]})
    return CheckResult(id="log_lint", status="passed", summary="log trong phần thay đổi đạt chuẩn")


def parse_coverage(kind: str, text: str, root: Path) -> dict[str, dict[int, bool]]:
    """file (relative, posix) → {line: covered} from an lcov, cobertura or jacoco report."""
    out: dict[str, dict[int, bool]] = {}
    if kind == "lcov":
        current = None
        for line in text.splitlines():
            if line.startswith("SF:"):
                path = Path(line[3:].strip())
                with contextlib.suppress(ValueError):
                    path = path.resolve().relative_to(root.resolve()) if path.is_absolute() else path
                current = path.as_posix()
                out.setdefault(current, {})
            elif line.startswith("DA:") and current:
                no, hits = line[3:].split(",")[:2]
                out[current][int(no)] = int(hits) > 0
        return out
    import xml.etree.ElementTree as ET

    tree = ET.fromstring(text)
    if kind == "jacoco":
        for package in tree.iter("package"):
            pkg = package.get("name", "")
            for source in package.iter("sourcefile"):
                name = f"{pkg}/{source.get('name')}".strip("/")
                out[name] = {int(ln.get("nr", 0)): int(ln.get("ci", 0)) > 0 for ln in source.iter("line")}
        return out
    for cls in tree.iter("class"):  # cobertura
        name = cls.get("filename", "")
        lines = out.setdefault(name, {})
        for ln in cls.iter("line"):
            lines[int(ln.get("number", 0))] = int(ln.get("hits", 0)) > 0
    return out


async def check_coverage_changed_lines(ctx: CheckContext) -> CheckResult:
    git = _git_ctx(ctx)
    spec = (ctx.project.profile.get("coverage") if ctx.project else None) or {}
    if git is None or not spec.get("path"):
        return CheckResult(id="coverage_changed_lines", status="skipped", summary="project chưa khai báo coverage report (profile.coverage)")
    report = git[0] / spec["path"]
    if not report.is_file():
        return CheckResult(id="coverage_changed_lines", status="failed", summary=f"không thấy báo cáo coverage {spec['path']} (chạy test có coverage trước)")
    coverage = parse_coverage(str(spec.get("kind") or "lcov"), report.read_text(encoding="utf-8", errors="replace"), git[0])
    minimum = float(spec.get("min") or 80)
    total = covered = 0
    for path, lines in (await added_lines(*git)).items():
        known = next((v for k, v in coverage.items() if path.endswith(k) or k.endswith(path)), None)
        if not known:
            continue
        for no, _ in lines:
            if no in known:
                total += 1
                covered += known[no]
    if total == 0:
        return CheckResult(id="coverage_changed_lines", status="skipped", summary="không có dòng code thay đổi nằm trong báo cáo coverage")
    pct = 100.0 * covered / total
    status = "passed" if pct >= minimum else "failed"
    return CheckResult(id="coverage_changed_lines", status=status, summary=f"{pct:.0f}% dòng thay đổi được test ({covered}/{total}, cần ≥ {minimum:.0f}%)", data={"pct": pct})


async def check_flaky_rerun(ctx: CheckContext) -> CheckResult:
    argv_list = command_list(ctx.project.profile if ctx.project else {}, "test")
    if not argv_list or ctx.workspace is None:
        return CheckResult(id="flaky_rerun", status="skipped", summary="project chưa khai báo lệnh test")
    runner = ctx.office.stages.checks
    codes = []
    note = ""
    data: dict[str, Any] = {"runs": 2}
    previous = await _build_test_result(ctx)
    if previous is not None:
        head, clean = await git_state(ctx.workspace)
        if head and clean and previous["data"].get("clean") is True and previous["data"]["head"] == head:
            # Same commit, clean tree both times: the build stage's run is the first of the two.
            codes.append(0 if previous["status"] == "passed" else int(previous["data"].get("exit", 1)))
            note = f" (lần 1 là kết quả repo:test của bước Làm, commit {head[:7]})"
            data = {"runs": 1, "reused_head": head}
    while len(codes) < 2:
        code = 0
        for argv in argv_list:
            code, _out, _secs = await run_argv(argv, ctx.workspace, timeout=1800, env=runner._env(ctx))
            if code:
                break
        codes.append(code)
    if codes[0] != codes[1]:
        return CheckResult(
            id="flaky_rerun", status="failed", summary=f"test cho kết quả khác nhau giữa 2 lần chạy (exit {codes[0]} rồi {codes[1]}): có test flaky{note}", data=data
        )
    if codes[0] != 0:
        return CheckResult(id="flaky_rerun", status="failed", summary=f"test lỗi cả 2 lần (exit {codes[0]}){note}", data=data)
    return CheckResult(id="flaky_rerun", status="passed", summary=f"test ổn định qua 2 lần chạy{note}", data=data)


async def _build_test_result(ctx: CheckContext) -> dict[str, Any] | None:
    """The repo:test result of the ticket's last finished build stage run, when it ran (passed/failed) and knows its commit."""
    runs = [r for r in await ctx.office.stages.runs(ctx.ticket.id) if r.stage == "build" and r.finished_at]
    if not runs:
        return None
    result = next((c for c in runs[-1].checks if c.get("id") == "repo:test"), None)
    if result is None or result.get("status") not in ("passed", "failed") or not (result.get("data") or {}).get("head"):
        return None
    return result


async def check_review_clean(ctx: CheckContext) -> CheckResult:
    return await ctx.office.review.review(ctx)


async def check_kb_noted(ctx: CheckContext) -> CheckResult:
    items = await ctx.office.knowledge.list(ticket_id=ctx.ticket.id)
    if items:
        kinds = sorted({i.kind for i in items})
        return CheckResult(id="kb_noted", status="passed", summary=f"{len(items)} mục kiến thức ({', '.join(kinds)})")
    if re.search(r"(?i)\b(none|không có)\b", ctx.docs.get("kb-draft", "")):
        return CheckResult(id="kb_noted", status="passed", summary="không có kiến thức dùng lại được (đã ghi rõ)")
    return CheckResult(id="kb_noted", status="failed", summary="chưa ghi kiến thức nào (kb_note), hoặc ghi 'none' trong kb-draft nếu thật sự không có")


BUILTIN.update(
    {
        "kb_noted": check_kb_noted,
        "ac_coverage": check_ac_coverage,
        "no_secrets": check_no_secrets,
        "log_lint": check_log_lint,
        "coverage_changed_lines": check_coverage_changed_lines,
        "flaky_rerun": check_flaky_rerun,
        "review_clean": check_review_clean,
    }
)
