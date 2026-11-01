"""Team rules: proposal/approval by scope owners, isolation between teams, compile per stage, enforcement."""

from __future__ import annotations

import sys
from collections.abc import AsyncIterator

import pytest

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.errors import PolicyDenied
from actoroffice.domain.run import TurnResult, TurnStatus
from actoroffice.domain.work import ProjectAccess
from actoroffice.runtimes.base import GateDecision
from actoroffice.services.rules import gate_rules_hit, policy_denial

from .fakes import FakeRuntime, TurnCtx


@pytest.fixture
def runtime() -> FakeRuntime:
    return FakeRuntime(scripts=[])


@pytest.fixture
async def office(tmp_path, runtime) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05), runtime=runtime)
    await o.open()
    await o.start_notifier()
    yield o
    await o.close()


@pytest.fixture
async def setup(office: Office):
    a = await office.teams.create_team("Billing")
    b = await office.teams.create_team("Search")
    lead = await office.teams.create_user("Tuấn")
    ba = await office.teams.create_user("Lan")
    other_lead = await office.teams.create_user("Khoa")
    await office.teams.add_member(a.id, lead.id, ["lead", "dev"])
    await office.teams.add_member(a.id, ba.id, ["ba"])
    await office.teams.add_member(b.id, other_lead.id, ["lead"])
    return a, b, lead, ba, other_lead


async def ticket_in(office: Office, team_id: str, title: str = "x"):
    t = await office.tickets.create(title, description="x")
    await office.db.execute("UPDATE tickets SET team_id = ?, stage = 'build', stage_template = 'code' WHERE id = ?", (team_id, t.id))
    return await office.tickets.get(t.id)


async def test_scope_owner_approves_and_rules_stay_inside_their_team(office: Office, setup) -> None:
    a, b, lead, ba, other_lead = setup
    draft = await office.rules.propose(
        title="Test Java", body="Dùng JUnit 5 + AssertJ", kind="guideline", scope=f"team:{a.id}", stage="build", by=ba.id, activate=True
    )
    assert draft.status.value == "draft"  # a BA cannot activate a team rule
    with pytest.raises(PolicyDenied):
        await office.rules.approve(draft.id, by=other_lead.id)  # the lead of another team cannot either
    active = await office.rules.approve(draft.id, by=lead.id)
    assert active.status.value == "active" and active.version == 2

    ta, tb = await ticket_in(office, a.id), await ticket_in(office, b.id)
    in_a = await office.rules.compile(ticket=ta, project=None, stage="build")
    in_b = await office.rules.compile(ticket=tb, project=None, stage="build")
    assert [r.id for r in in_a.guidelines] == [draft.id] and in_a.refs == [f"{draft.id}@2"]
    assert in_b.guidelines == []
    assert (await office.rules.compile(ticket=ta, project=None, stage="plan")).guidelines == []  # stage-specific

    # the file is the source of truth, versioned
    files = list((office.settings.data_dir / "rules" / f"team-{a.id}").glob("*.md"))
    assert files and "JUnit 5" in files[0].read_text(encoding="utf-8") and "status: active" in files[0].read_text(encoding="utf-8")
    assert [v["version"] for v in await office.rules.versions(draft.id)] == [1, 2]

    # an edit by someone who cannot approve sends it back to draft
    edited = await office.rules.update(draft.id, by=ba.id, body="Dùng JUnit 5")
    assert edited.status.value == "draft"


async def test_narrow_scope_wins_and_ai_proposals_are_drafts(office: Office, setup) -> None:
    a, _b, *_ = setup
    await office.rules.propose(title="Test framework", body="pytest", kind="guideline", scope="global", by="user", activate=True)
    team_rule = await office.rules.propose(title="Test framework", body="JUnit 5", kind="guideline", scope=f"team:{a.id}", by="user", activate=True)
    compiled = await office.rules.compile(ticket=await ticket_in(office, a.id), project=None, stage="build")
    assert [r.id for r in compiled.guidelines] == [team_rule.id]
    ai = await office.rules.propose(title="Log JSON", body="log có traceId", kind="guideline", scope="global", by="Copilot", activate=True, ai=True)
    assert ai.status.value == "draft"
    assert [r.id for r in await office.rules.search("traceId", active_only=False)] == [ai.id]


async def test_policies_need_an_admin_and_the_gate_enforces_them(office: Office, setup, runtime: FakeRuntime) -> None:
    a, _b, lead, ba, _ = setup
    with pytest.raises(PolicyDenied):
        rule = await office.rules.propose(
            title="Không force push", body="Không bao giờ ghi đè lịch sử", kind="policy", scope=f"team:{a.id}",
            spec={"deny_patterns": [r"git push .*--force", r"--no-verify"], "deny_hosts": ["*.prod.acme.vn"]}, by=lead.id,
        )
        await office.rules.approve(rule.id, by=lead.id)
    policy = await office.rules.get("R-1")
    await office.rules.approve(policy.id, by="user")
    policies = [await office.rules.get(policy.id)]
    assert policy_denial(policies, "Bash", {"command": "git push origin t/x --force"}) is not None
    assert policy_denial(policies, "WebFetch", {"url": "https://api.prod.acme.vn/v1/orders"}) is not None
    assert policy_denial(policies, "Bash", {"command": "git push origin t/x"}) is None

    ticket = await office.tickets.create("Refactor", description="x")
    seen: list[str] = []

    async def work(ctx: TurnCtx) -> TurnResult:
        decision = await ctx.gate.before_tool("Bash", {"command": "git commit -m x --no-verify"}, None)
        seen.append(decision.decision.value)
        assert "R-1" in decision.reason
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts.append(work)
    await office.stages.start(ticket.id, template="code", by="user", team_id=a.id, owner_user_id=ba.id)
    await office.scheduler.wait_idle(timeout=20)
    assert seen == [GateDecision.DENY.value]
    srun = (await office.stages.runs(ticket.id))[0]
    assert srun.rules == [f"{policy.id}@2"]
    run = (await office.runs.for_ticket(ticket.id))[0]
    assert any(act["kind"] == "rule" and "chặn" in act["title"] for act in await office.runs.activities(run.id))


async def test_rule_checks_run_with_the_stage(office: Office, setup, runtime: FakeRuntime) -> None:
    a, _b, _lead, ba, _ = setup
    rule = await office.rules.propose(
        title="Mã nguồn phải qua lint nội bộ", body="", kind="check", scope=f"team:{a.id}", stage="understand",
        spec={"cmd": [sys.executable, "-c", "import sys; print('lint: 2 lỗi'); sys.exit(3)"]}, by="user", activate=True,
    )
    ticket = await office.tickets.create("Báo cáo", description="x")
    outcome: list[str] = []

    async def work(ctx: TurnCtx) -> TurnResult:
        await ctx.office("doc_put", {"key": "requirements", "body": "AC-1: Given a When b Then c", "base_revision": None})
        await ctx.office("dossier_update", {"section": "context", "body": "x"})
        outcome.append(await ctx.office("stage_complete", {"summary": "xong"}))
        return TurnResult(status=TurnStatus.COMPLETED, session_id=f"s-{ctx.spec.run_id}")

    runtime.scripts.append(work)
    await office.stages.start(ticket.id, template="code", by="user", team_id=a.id, owner_user_id=ba.id)
    await office.scheduler.wait_idle(timeout=30)
    assert f"rule:{rule.id}" in outcome[0] and "exit 3" in outcome[0]
    run = (await office.runs.for_ticket(ticket.id))[0]
    log = (office.settings.runs_dir / run.id / f"check-rule-{rule.id.lower()}.txt").read_text(encoding="utf-8")
    assert "lint: 2 lỗi" in log


async def test_repo_conventions_import_and_gate_rules(office: Office, setup, tmp_path) -> None:
    a, *_ = setup
    repo = tmp_path / "repo"
    (repo / ".kiro" / "steering").mkdir(parents=True)
    (repo / "CLAUDE.md").write_text("Luôn chạy `mvn -q verify` trước khi commit.", encoding="utf-8")
    (repo / ".kiro" / "steering" / "api.md").write_text("---\ntitle: API style\n---\nREST dùng kebab-case.", encoding="utf-8")
    project = await office.projects.create("billing", path=str(repo), access=ProjectAccess.READ_ONLY)
    imported = await office.rules.import_conventions(project, by="user")
    assert sorted(r.source for r in imported) == ["file:.kiro/steering/api.md", "file:CLAUDE.md"]
    assert {r.title for r in imported} >= {"API style"} and all(r.status.value == "active" for r in imported)
    again = await office.rules.import_conventions(project, by="user")
    assert [r.id for r in again] == [r.id for r in imported]  # re-import updates, never duplicates

    gate = await office.rules.propose(
        title="Migration cần lead", body="", kind="gate", scope=f"team:{a.id}", spec={"roles": ["lead"], "when": {"paths": ["db/migrations/**"]}}, by="user", activate=True
    )
    assert gate_rules_hit([gate], ["src/a.py", "db/migrations/V7__refund.sql"]) == [gate]
    assert gate_rules_hit([gate], ["src/a.py"]) == []


def test_chains_of_allowed_commands_need_no_approval(tmp_path) -> None:
    from actoroffice.domain.org import Position, PositionPolicy
    from actoroffice.security.policy import Decision, PolicyEngine, ToolCallContext

    position = Position(id="p", name="engineer", title="E", policy=PositionPolicy(bash_allow=["git add *", "git commit *", "py -m pytest*", "tail *"]))
    ctx = ToolCallContext(position=position, workspace=tmp_path)
    engine = PolicyEngine()
    assert engine.decide("Bash", {"command": "git add -A && git commit -q -m 'x'"}, ctx).decision == Decision.ALLOW
    assert engine.decide("Bash", {"command": "py -m pytest -q 2>&1 | tail -5"}, ctx).decision == Decision.ALLOW
    assert engine.decide("Bash", {"command": "git add -A && rm -r build"}, ctx).decision != Decision.ALLOW
    assert engine.decide("Bash", {"command": "git commit -m $(cat secret)"}, ctx).decision != Decision.ALLOW
