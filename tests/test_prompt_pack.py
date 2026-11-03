"""The build and pr stage prompts: who runs the whole test suite (SI-3, SI-5)."""

from __future__ import annotations

import re

import pytest

from actoroffice.domain.org import Actor
from actoroffice.domain.run import WakeRequest, WakeSource
from actoroffice.domain.stage import StageRun, default_templates
from actoroffice.domain.work import Ticket
from actoroffice.engine import prompt_pack

OLD = "run the project's build/test/lint"
NO_RERUN = "Do not run the test suite again"
REVIEW_BULLET = '- When woken with review comments ("Review mới trên PR")'


def _prompt(template_name: str, office_runs_tests: bool, stage_id: str = "build") -> prompt_pack.StagePrompt:
    template = default_templates()[template_name]
    stage = template.stage(stage_id)
    assert stage is not None
    return prompt_pack.build(
        actor=Actor(id="copilot", position_id="ai-engineer", name="Copilot"),
        ticket=Ticket(id=1, key="SI-3", title="t"),
        template=template,
        stage=stage,
        stage_run=StageRun(id=1, ticket_id=1, stage=stage_id, attempt=1, started_at=""),
        language="Vietnamese",
        team_name=None,
        people=[],
        project_line="",
        dossier="",
        guidelines=[],
        memories=[],
        skills=[],
        wake=WakeRequest(id=1, actor_id="copilot", source=WakeSource.ASSIGNMENT),
        resumed=False,
        answered=[],
        new_comments=[],
        changes=[],
        office_runs_tests=office_runs_tests,
    )


def _system(template_name: str, office_runs_tests: bool) -> str:
    return _prompt(template_name, office_runs_tests).system


@pytest.mark.parametrize("template_name", ["code", "quick"])
def test_ac1_profile_with_test_runs_only_related_tests(template_name: str) -> None:
    system = _system(template_name, office_runs_tests=True)
    assert OLD not in system.lower()
    assert "only the tests for" in system
    assert "the office runs it at stage_complete" in system


@pytest.mark.parametrize("template_name", ["code", "quick"])
def test_ac2_profile_without_test_keeps_running_the_suite(template_name: str) -> None:
    system = _system(template_name, office_runs_tests=False)
    assert OLD in system.lower()
    assert "only the tests for" not in system


def _pr_stage(template_name: str) -> str:
    return _prompt(template_name, office_runs_tests=True, stage_id="pr").parts["stage"]


def _split_review_bullet(stage: str) -> tuple[str, str]:
    """Return (the review-comment bullet, the guide without it)."""
    start = stage.index(REVIEW_BULLET)
    end = stage.find("\n- ", start)
    end = len(stage) if end == -1 else end
    return stage[start:end], stage[:start] + stage[end:]


def _flat(text: str) -> str:
    return " ".join(text.split())


@pytest.mark.parametrize("template_name", ["code", "quick"])
def test_ac1_pr_guide_does_not_rerun_tests_while_code_unchanged(template_name: str) -> None:
    stage = _pr_stage(template_name)
    assert NO_RERUN in stage
    assert "handoff" in stage and "build stage" in stage
    assert f"{NO_RERUN} while the code has not changed since the build stage" in _flat(stage)


@pytest.mark.parametrize("template_name", ["code", "quick"])
def test_ac2_pr_review_fix_runs_related_tests(template_name: str) -> None:
    bullet, _ = _split_review_bullet(_pr_stage(template_name))
    flat = _flat(bullet)
    assert "run only the tests related to what you changed" in flat
    assert "the office runs the whole suite at stage_complete" in flat
    assert flat.index("run only the tests") < flat.index("commit")
    assert NO_RERUN not in bullet


@pytest.mark.parametrize("template_name", ["code", "quick"])
def test_ac3_pr_guide_outside_review_does_not_run_tests(template_name: str) -> None:
    _, rest = _split_review_bullet(_pr_stage(template_name))
    # apart from the warning itself and the review bullet, nothing tells the AI to run tests
    assert not re.search(r"(?i)\brun\b[^.\n]*\btests?\b", rest.replace(NO_RERUN, ""))


def test_ac1_base_says_run_long_commands_in_the_foreground() -> None:
    """SI-6: long commands run in the foreground, not in the background with a sleep loop waiting for them."""
    section = _system("code", office_runs_tests=True).split("# How you work", 1)[1].split("\n# ", 1)[0]
    how_you_work = " ".join(section.split())
    assert "Run long commands (tests, builds) in the foreground, one command per call" in how_you_work
    assert "never start them in the background and wait with a sleep/until/while loop" in how_you_work


def _stage(template_name: str, stage_id: str) -> str:
    return " ".join(_prompt(template_name, office_runs_tests=True, stage_id=stage_id).parts["stage"].split())


def test_ac11_stage_guides_mention_review_acs() -> None:
    """SI-16: each stage of the code workflow says what an AC tagged [review] needs there."""
    understand = _stage("code", "understand")
    assert 'Tag an AC "[review]" right after its id' in understand and "process, documents" in understand and "hard to automate" in understand
    assert '"Cách kiểm: …"' in understand and "At most half of the ACs may be [review]" in understand
    assert "Inside an AC never write another AC's id" in understand
    assert "An AC tagged [review] is mapped to how it will be checked by review instead of a test" in _stage("code", "plan")
    assert "An AC tagged [review] needs no test, but the AC table of the `handoff` must have a line with its id" in _stage("code", "build")
    for template_name in ("code", "quick"):  # the pr guide is shared: the bullet says "if any"
        assert "ACs tagged [review], if any: list them in the PR description with how each was checked" in _stage(template_name, "pr")
    # the quick workflow has no requirements: its scope and build guides do not mention [review]
    assert "[review]" not in _stage("quick", "scope") and "[review]" not in _stage("quick", "build")
