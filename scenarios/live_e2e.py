"""Live end-to-end run with the real Claude runtime (uses tokens): one code ticket goes through every stage on a
tiny sample repo while this script plays the people (BA Lan, Dev Minh): it answers questions with the AI's
recommended option, approves gates, gives a PR link when asked, and reports the merge.

    uv run py scenarios/live_e2e.py --home F:/_ao_live [--model sonnet] [--minutes 45]
"""

from __future__ import annotations

import argparse
import asyncio
import json
import shutil
import subprocess
import sys
import time
from pathlib import Path

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.work import InteractionKind, ProjectAccess, TicketStatus

REFUND = '''"""Refund rules of the shop."""


def refund_allowed(days_since_delivery: int) -> bool:
    """A customer may ask for a refund up to 30 days after delivery."""
    return 0 <= days_since_delivery <= 30
'''
TEST = '''from shop.refund import refund_allowed


def test_within_30_days():
    assert refund_allowed(10)


def test_after_30_days():
    assert not refund_allowed(31)
'''
DESCRIPTION = (
    "Khách VIP phải được hoàn tiền trong 60 ngày kể từ ngày giao hàng (khách thường vẫn 30 ngày). "
    "Hàm hiện tại là shop.refund.refund_allowed. BA: Lan. Dev: Minh."
)


def log(text: str) -> None:
    print(f"[{time.strftime('%H:%M:%S')}] {text}", flush=True)


def git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


def make_repo(root: Path) -> Path:
    repo = root / "shop"
    if repo.exists():
        shutil.rmtree(repo)
    (repo / "src" / "shop").mkdir(parents=True)
    (repo / "tests").mkdir()
    (repo / "src" / "shop" / "__init__.py").write_text("", encoding="utf-8")
    (repo / "src" / "shop" / "refund.py").write_text(REFUND, encoding="utf-8")
    (repo / "tests" / "test_refund.py").write_text(TEST, encoding="utf-8")
    (repo / "pytest.ini").write_text("[pytest]\npythonpath = src\n", encoding="utf-8")
    (repo / "AGENTS.md").write_text("Python 3.12. Run tests with `py -m pytest -q`. Keep functions small and typed.\n", encoding="utf-8")
    git(repo, "init", "-q", "-b", "main")
    git(repo, "add", "-A")
    git(repo, "-c", "user.name=seed", "-c", "user.email=seed@local", "commit", "-q", "-m", "seed")
    return repo


async def play_people(office: Office, ticket_id: int, lan: str, minh: str) -> str | None:
    """Answer whatever waits for a person. Returns a short note of what was done (None when nothing waited)."""
    done = []
    for item in await office.interactions.pending(ticket_id=ticket_id):
        p = item.payload
        if item.kind == InteractionKind.QUESTION:
            roles = p.get("roles") or ["ba"]
            who = minh if set(roles) & {"dev", "lead", "qe"} and "ba" not in roles else lan
            ticket = await office.tickets.get(ticket_id)
            if ticket.stage == "pr":
                answer = "Mình đã push và mở PR: https://bitbucket.org/acme/shop/pull-requests/1 — bạn link vào ticket nhé."
            else:
                answer = p.get("recommended") or (p.get("options") or ["Đồng ý với đề xuất của bạn"])[0]
            await office.answer(item.id, {"answer": answer}, by=who)
            done.append(f"answered '{p.get('question', '')[:80]}' → {answer[:60]}")
        elif item.kind == InteractionKind.APPROVE_STAGE:
            who = lan if p.get("gate") in ("requirement", "knowledge") else minh
            await office.answer(item.id, {"approved": True}, by=who)
            done.append(f"approved gate {p.get('gate')} ({len(p.get('checks') or [])} checks)")
        elif item.kind == InteractionKind.APPROVE_TOOL:
            await office.answer(item.id, {"approved": True}, by="user")
            done.append(f"allowed tool {p.get('tool')}: {str(p.get('target'))[:80]}")
        elif item.kind == InteractionKind.ENV_ACCESS:
            await office.answer(item.id, {"approved": False}, by=minh)
            done.append("refused env access (none needed)")
    ticket = await office.tickets.get(ticket_id)
    status = await office.stages.status(ticket)
    if ticket.stage == "pr" and status["status"] == "waiting_external":
        await office.stages.external_passed(ticket_id, "merge", by=minh, note="PR merged (live test)")
        done.append("reported the PR merged")
    office.scheduler.kick()
    return "; ".join(done) or None


async def main(home: Path, model: str, minutes: float) -> int:
    if home.exists():
        shutil.rmtree(home)
    home.mkdir(parents=True)
    repo = make_repo(home)
    office = Office(Settings(data_dir=home / "office", default_model=model, approval_wait_s=5, max_concurrent_runs=1))
    await office.start()
    try:
        team = await office.teams.create_team("Shop")
        lan = await office.teams.create_user("Lan")
        minh = await office.teams.create_user("Minh")
        await office.teams.add_member(team.id, lan.id, ["ba"])
        await office.teams.add_member(team.id, minh.id, ["dev", "lead"])
        project = await office.projects.create("shop", path=str(repo), access=ProjectAccess.WORKTREE)
        await office.projects.configure(project.id, team_id=team.id, profile={"test": [sys.executable, "-m", "pytest", "-q"]})
        await office.rules.import_conventions(project, by="user")
        ticket = await office.tickets.create("VIP được hoàn tiền trong 60 ngày", description=DESCRIPTION, project_id=project.id)
        ticket = await office.stages.start(ticket.id, template="code", by="user", team_id=team.id, owner_user_id=lan.id)
        log(f"started {ticket.key} on stage {ticket.stage}")
        deadline = time.monotonic() + minutes * 60
        last_stage = None
        while time.monotonic() < deadline:
            await asyncio.sleep(5)
            ticket = await office.tickets.get(ticket.id)
            status = await office.stages.status(ticket)
            if (ticket.stage, status["status"]) != last_stage:
                last_stage = (ticket.stage, status["status"])
                log(f"stage {ticket.stage}: {status['status']}" + (f" (waits for {', '.join(status['who'])})" if status["who"] else ""))
            if ticket.status == TicketStatus.DONE:
                log("ticket DONE")
                break
            if status["status"] in ("needs_you", "waiting_external"):
                note = await play_people(office, ticket.id, lan.id, minh.id)
                if note:
                    log(f"people: {note}")
            if status["status"] == "failed":
                log("last run failed; retrying the stage once")
                await office.stages.retry(ticket.id, by="user")
        await office.scheduler.wait_idle(timeout=600)
        return await report(office, ticket.id)
    finally:
        await office.close()


async def report(office: Office, ticket_id: int) -> int:
    ticket = await office.tickets.get(ticket_id)
    print("\n================ REPORT ================")
    print(f"{ticket.key} status={ticket.status.value} stage={ticket.stage} pr={ticket.pr_url}")
    total = 0
    for r in await office.stages.runs(ticket.id):
        tok = sum(int(r.usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        total += tok
        failed = [c["id"] for c in r.checks if c.get("status") == "failed"]
        print(f"- {r.stage:10} #{r.attempt} {r.outcome or 'open':10} {tok / 1000:7.1f}k tok ${r.cost_usd:.3f} steps={sum(s.done for s in r.steps)}/{len(r.steps)} failed_checks={failed}")
    print(f"total ≈ {total / 1000:.1f}k tokens")
    for d in await office.tickets.list_documents(ticket.id):
        print(f"\n--- document {d.key} rev {d.revision} (approved {d.approved_revision}) ---\n{d.body[:1500]}")
    print("\n--- knowledge ---")
    for item in await office.knowledge.list(ticket_id=ticket.id):
        print(f"[{item.kind} {item.status}] {item.title}: {item.content[:200]}")
    print("\n--- comments ---")
    for c in await office.tickets.comments(ticket.id, limit=200):
        print(f"[{c.author_id or c.author_kind}] {c.body[:300]}")
    runs = await office.runs.for_ticket(ticket.id, limit=50)
    kinds: dict[str, int] = {}
    for run in runs:
        for a in await office.runs.activities(run.id):
            kinds[a["kind"]] = kinds.get(a["kind"], 0) + 1
    print("\nactivity kinds:", json.dumps(kinds))
    print("runs:", [(r.status.value, r.error) for r in runs])
    return 0 if ticket.status == TicketStatus.DONE else 1


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--home", required=True)
    parser.add_argument("--model", default="sonnet")
    parser.add_argument("--minutes", type=float, default=45)
    args = parser.parse_args()
    sys.exit(asyncio.run(main(Path(args.home), args.model, args.minutes)))
