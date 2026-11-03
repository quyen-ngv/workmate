"""Merging a finished ticket tree's branch into the project's main branch: an explicit, user-approved step.

Checks before merging: no run is working on the tree, the tree's worktree has no uncommitted changes, the project
folder is clean and on its main branch. The merge itself is made and tested in a scratch worktree detached at main
(`workspaces_dir/.merge-<KEY>`); main only moves by a fast-forward to the tested merge commit, and only if it did
not move while the test ran. A conflict, a failing test or a moved main leaves main where it was.

Only people merge: the web API calls this service, no AI tool does.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import shutil
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import Conflict, NotFound, OfficeError
from actoroffice.domain.stage import StageOutcome
from actoroffice.domain.work import Project, ProjectAccess, Ticket
from actoroffice.engine.workspace import branch_name
from actoroffice.services.automations import kill_tree
from actoroffice.services.checks import IDENTITY, command_list, merge_failure, run_argv, test_env

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)

LOG_TAIL = 1500


class MergeError(OfficeError):
    code = 409


class MergeRecordError(MergeError):
    """Main already holds the merge, but writing it to the ticket failed: never report this as "main unchanged"."""

    def __init__(self, message: str, result: dict[str, Any]) -> None:
        super().__init__(message)
        self.result = result


class MergeService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self._busy: set[str] = set()  # repos with a merge reserved: from the accepted click (or merge()) to its end
        self._jobs: dict[str, asyncio.Task[None]] = {}  # root key → background merge started from the button

    async def status(self, key: str) -> dict[str, Any]:
        """What a merge of this ticket's tree would do (shown on the ticket page of the classic UI)."""
        office = self.office
        root = await office.tickets.root_of(key)
        project = await office.projects.get(root.project_id) if root.project_id else None
        if not project or project.access != ProjectAccess.WORKTREE or not project.path:
            return {"available": False}
        branch = branch_name(root)
        repo = Path(project.path)
        exists = (await _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}"))[0] == 0
        if not exists:
            return {"available": False}
        ahead = (await _git(repo, "rev-list", "--count", f"{project.main_branch}..{branch}"))[1].strip()
        merged = (await _git(repo, "merge-base", "--is-ancestor", branch, project.main_branch))[0] == 0
        return {"available": True, "branch": branch, "main": project.main_branch, "ahead": int(ahead or 0), "merged": merged,
                "root": root.key}

    async def panel(self, key: str) -> dict[str, Any]:
        """The merge button of the team UI's PR pane: shown only for a root ticket at stage pr of a worktree project
        when the office has no code host and the ticket no PR; greyed out (ready false, with a reason) until the AI
        finished the stage, and while a merge runs."""
        office = self.office
        ticket = await office.tickets.get(key)
        root = await office.tickets.root_of(ticket.id)
        project = await office.projects.get(root.project_id) if root.project_id else None
        if not project or project.access != ProjectAccess.WORKTREE or not project.path:
            return {"available": False}
        if office.atlassian.code_host() is not None or ticket.id != root.id or ticket.pr_url or ticket.stage != "pr":
            return {"available": False}
        if (await office.stages.stage_def(ticket)).gate != "merge":
            return {"available": False}
        running = root.key in self._jobs
        reason = ""
        srun = await office.stages.current_run(ticket)
        if running:
            reason = "Đang merge"
        elif srun is None or srun.outcome != StageOutcome.COMPLETED.value:
            reason = "AI chưa xong bước PR"
        return {"available": True, "ready": not reason, "running": running, "reason": reason, "branch": branch_name(root),
                "main": project.main_branch, "root": root.key}

    async def start(self, key: str, *, by: str) -> dict[str, Any]:
        """The button: check now (a refusal comes back to the person at once), then merge in the background."""
        office = self.office
        panel = await self.panel(key)
        if not panel["available"]:
            raise MergeError("Ticket này không merge bằng nút được: chỉ ticket gốc ở bước PR, dự án git worktree, chưa có PR và code host")
        root = await office.tickets.root_of(key)
        if root.key in self._jobs:
            raise Conflict(f"Đang merge {root.key}")
        if not panel["ready"]:
            raise MergeError(panel["reason"])
        project = await office.projects.get(root.project_id or "")
        self._reserve(project)  # no await between the check and the reservation: a second click is refused here
        try:
            await self._check(root, project)
        except BaseException as exc:
            self._release(project)
            if isinstance(exc, MergeError):
                await self._refused(root, project, by, exc)
            raise
        self._jobs[root.key] = asyncio.get_running_loop().create_task(self._run_job(root, project, by))
        office.bus.publish("ticket.updated", ticket=root.model_dump())
        return {"started": True, "root": root.key}

    async def wait(self, key: str) -> None:
        """Until the background merge of this tree (if any) is over."""
        root = await self.office.tickets.root_of(key)
        task = self._jobs.get(root.key)
        if task is not None:
            await asyncio.shield(task)

    async def close(self) -> None:
        for task in list(self._jobs.values()):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
        self._jobs.clear()
        self._busy.clear()  # a job cancelled before it ran never reached its finally

    async def _run_job(self, root: Ticket, project: Project, by: str) -> None:
        """The button's merge; `start` reserved the repo, this job gives it back when it ends."""
        office = self.office
        try:
            try:
                result = await self._merge_reserved(root, project, by)
            except MergeRecordError as exc:
                result = exc.result  # main holds the merge: still pass the gate
                await self._say(root, f"⚠️ {exc}")
            except Exception as exc:
                if not isinstance(exc, OfficeError):
                    log.exception("merge of %s crashed", root.key)
                reason = str(exc) if isinstance(exc, OfficeError) else f"lỗi không lường trước: {type(exc).__name__}: {exc}"
                await self._say(root, f"❌ Chưa merge được, nhánh chính giữ nguyên. {reason}")
                return
            try:
                await office.stages.external_passed(root.id, "merge", by=by, note=f"đã merge {result['commit']} vào {result['main']}")
            except Exception as exc:
                log.exception("merge gate of %s", root.key)
                await self._say(root, f"⚠️ Đã merge {result['commit']} vào {result['main']} nhưng chưa qua được cổng merge ({type(exc).__name__}: {exc}). "
                                      "Bấm \"PR đã merge\" để đi tiếp.")
        finally:
            self._jobs.pop(root.key, None)
            self._release(project)
            with contextlib.suppress(Exception):
                office.bus.publish("ticket.updated", ticket=(await office.tickets.get(root.id)).model_dump())

    async def _say(self, root: Ticket, text: str) -> None:
        try:
            await self.office.tickets.add_comment(root.id, text, author_kind="system", author_id=None)
        except Exception:
            log.exception("comment on %s: %s", root.key, text)

    async def _refused(self, root: Ticket, project: Project, by: str, exc: MergeError) -> None:
        """A check before merging failed: the reason goes to the timeline and the audit log, main is untouched."""
        await self.office.audit.record("git:merge", "failed", ticket_id=root.id, target=f"{project.name}:{branch_name(root)}",
                                       detail={"by": by, "reason": str(exc)[:1000]})
        await self._say(root, f"❌ Chưa merge được, nhánh chính giữ nguyên. {exc}")

    def _reserve(self, project: Project) -> None:
        """Synchronous on purpose: checking and taking the repo cannot be split by another task."""
        key = str(Path(project.path or ""))
        if key in self._busy:
            raise Conflict("Đang có một merge khác trên dự án này")
        self._busy.add(key)

    def _release(self, project: Project) -> None:
        self._busy.discard(str(Path(project.path or "")))

    async def _target(self, key: str) -> tuple[Ticket, Project]:
        office = self.office
        root = await office.tickets.root_of(key)
        project = await office.projects.get(root.project_id) if root.project_id else None
        if not project or project.access != ProjectAccess.WORKTREE or not project.path:
            raise MergeError("Chỉ dự án chế độ git worktree mới có bước merge")
        return root, project

    async def _check(self, root: Ticket, project: Project) -> str:
        """The checks before merging; returns the branch tip to merge, pinned now: what was checked is what gets merged."""
        office = self.office
        repo = Path(project.path or "")
        branch = branch_name(root)
        worktree = office.settings.workspaces_dir / root.key
        if await office.runs.for_tree_running(root.id):
            raise MergeError("Còn nhân viên đang làm trên nhánh này; đợi họ xong rồi merge")
        if worktree.exists():
            dirty = (await _git(worktree, "status", "--porcelain"))[1].strip()
            if dirty:
                raise MergeError(f"Nhánh {branch} còn thay đổi chưa commit:\n{dirty[:500]}")
        await self._check_main(project)
        code, tip = await _git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}")
        if code != 0:
            raise MergeError(f"Không có nhánh {branch}")
        tip = tip.strip()
        ahead = (await _git(repo, "rev-list", "--count", f"{project.main_branch}..{tip}"))[1].strip()
        if not ahead.isdigit() or int(ahead) == 0:
            raise MergeError(f"Nhánh {branch} không có gì mới so với {project.main_branch}")
        return tip

    async def _check_main(self, project: Project) -> None:
        repo = Path(project.path or "")
        head = (await _git(repo, "rev-parse", "--abbrev-ref", "HEAD"))[1].strip()
        if head != project.main_branch:
            raise MergeError(f"Thư mục dự án đang ở nhánh {head}, không phải {project.main_branch}")
        dirty_main = (await _git(repo, "status", "--porcelain", "--untracked-files=no"))[1].strip()
        if dirty_main:
            raise MergeError(f"Thư mục dự án có thay đổi chưa commit:\n{dirty_main[:500]}")

    async def merge(self, key: str, *, by: str = "user") -> dict[str, Any]:
        """Merge the tree's branch into main (synchronous: the classic UI's API; the button runs it in the background)."""
        root, project = await self._target(key)
        self._reserve(project)
        try:
            return await self._merge_reserved(root, project, by)
        finally:
            self._release(project)

    async def _merge_reserved(self, root: Ticket, project: Project, by: str) -> dict[str, Any]:
        """merge() once the repo is reserved; every failure before main moved is in the audit log, unexpected ones too."""
        office = self.office
        try:
            result = await self._merge_locked(root, project, by)
        except Exception as exc:
            reason = str(exc) if isinstance(exc, OfficeError) else f"{type(exc).__name__}: {exc}"
            await office.audit.record("git:merge", "failed", ticket_id=root.id, target=f"{project.name}:{branch_name(root)}",
                                      detail={"by": by, "reason": reason[:1000]})
            raise
        branch, main, commit = result["branch"], result["main"], result["commit"]
        tested = f"test `{result['tested']}` pass trên kết quả merge" if result["tested"] else "không có lệnh test, kết quả merge chưa được test"
        try:
            await office.tickets.add_comment(root.id, f"Đã merge {branch} vào {main} ({commit}) bởi {await self._name(by)}; {tested}.",
                                             author_kind="system", author_id=None)
            await office.tickets.add_work_product(root.id, "merge", commit, {"note": f"{branch} → {main}", "sha": result["sha"]})
            await office.audit.record("git:merge", "merged", ticket_id=root.id, target=f"{project.name}:{branch}",
                                      detail={"commit": commit, "by": by, "tested": result["tested"]})
        except Exception as exc:
            log.exception("recording the merge of %s", root.key)
            raise MergeRecordError(f"Đã merge {branch} vào {main} ({commit}) nhưng chưa ghi được vào ticket: {type(exc).__name__}: {exc}", result) from exc
        office.bus.publish("ticket.document", ticket_id=root.id, key="merge", revision=0)
        return result

    async def _merge_locked(self, root: Ticket, project: Project, by: str) -> dict[str, Any]:
        office = self.office
        repo = Path(project.path or "")
        branch, main = branch_name(root), project.main_branch
        tip = await self._check(root, project)
        m0 = (await _git(repo, "rev-parse", f"refs/heads/{main}"))[1].strip()
        scratch = office.settings.workspaces_dir / f".merge-{root.key}"
        await _drop_worktree(repo, scratch)  # left over by an office that stopped mid-merge
        scratch.parent.mkdir(parents=True, exist_ok=True)
        code, out = await _git(repo, "worktree", "add", "--detach", str(scratch), m0)
        if code != 0:
            raise MergeError(f"Không tạo được worktree tạm để merge:\n{out[-500:]}")
        try:
            identity = [] if (await _git(repo, "config", "user.email"))[0] == 0 else IDENTITY
            code, out = await _git(scratch, *identity, "merge", "--no-ff", "-m", f"Merge {root.key}: {root.title}", tip)
            if code != 0:
                files = (await _git(scratch, "diff", "--name-only", "--diff-filter=U"))[1].split()
                await _git(scratch, "merge", "--abort")
                why = merge_failure(files, out)
                if files:
                    raise MergeError(f"Merge bị {why}. Cần xử lý trên nhánh {branch} rồi merge lại.")
                raise MergeError(f"Merge {branch} vào {main} {why}.")
            sha = (await _git(scratch, "rev-parse", "HEAD"))[1].strip()
            tested = await self._test(project, scratch)
            if await office.runs.for_tree_running(root.id):
                raise MergeError("Có nhân viên vừa bắt đầu làm trên nhánh này; merge lại sau")
            if (await _git(repo, "rev-parse", f"refs/heads/{main}"))[1].strip() != m0:
                raise MergeError(f"Nhánh chính đã đổi trong lúc test, hãy merge lại ({main} không còn ở {m0[:8]}).")
            await self._check_main(project)
            code, out = await _git(repo, "merge", "--ff-only", sha)
            if code != 0:
                raise MergeError(f"Không fast-forward được {main} tới commit merge:\n{out[-500:]}")
        finally:
            await _drop_worktree(repo, scratch)
        return {"merged": True, "commit": sha[:7], "sha": sha, "branch": branch, "main": main, "tested": tested}

    async def _test(self, project: Project, cwd: Path) -> str | None:
        """Run the project's test command on the merge result: profile "test" first, then test_command.
        Returns the command that passed, None when the project has none; a failure raises with the log's tail."""
        profile = project.profile or {}
        timeouts = profile.get("timeouts")
        timeout = float(timeouts.get("test", 900)) if isinstance(timeouts, dict) else 900.0
        env = test_env(self.office)
        argv_list = command_list(profile, "test")
        for argv in argv_list:
            code, out, _secs = await run_argv(argv, cwd, timeout=timeout, env=env)
            if code != 0:
                raise MergeError(f"Test lỗi trên kết quả merge (`{' '.join(argv)}` → exit {code}):\n{out[-LOG_TAIL:]}")
        if argv_list:
            return " && ".join(" ".join(argv) for argv in argv_list)
        if project.test_command:
            code, out = await _shell(project.test_command, cwd, timeout=timeout, env=env)
            if code != 0:
                raise MergeError(f"Test lỗi trên kết quả merge (`{project.test_command}` → exit {code}):\n{out[-LOG_TAIL:]}")
            return project.test_command
        return None

    async def _name(self, user_id: str) -> str:
        try:
            return (await self.office.teams.get_user(user_id)).name
        except NotFound:
            return user_id


async def _drop_worktree(repo: Path, path: Path) -> None:
    if path.exists():
        await _git(repo, "worktree", "remove", "--force", str(path))
    if path.exists():
        await asyncio.to_thread(shutil.rmtree, path, True)
    await _git(repo, "worktree", "prune")


def _env(cwd: Path) -> dict[str, str]:
    return {**os.environ, "GIT_CEILING_DIRECTORIES": str(cwd.parent)}


async def _git(cwd: Path, *args: str, timeout: float = 120) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(cwd), *args, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=_env(cwd)
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except (TimeoutError, asyncio.CancelledError):
        kill_tree(proc.pid)
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace")


async def _shell(command: str, cwd: Path, *, timeout: float = 900, env: dict[str, str] | None = None) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_shell(
        command, cwd=str(cwd), stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT, env=env or _env(cwd)
    )
    try:
        out, _ = await asyncio.wait_for(proc.communicate(), timeout)
    except TimeoutError:
        kill_tree(proc.pid)  # the shell and the test runner it started
        return 124, "timeout"
    except asyncio.CancelledError:
        kill_tree(proc.pid)
        raise
    return proc.returncode or 0, out.decode("utf-8", "replace")
