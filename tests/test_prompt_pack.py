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


@pytest.mark.parametrize("template_name", ["code", "quick"])
def test_ac1_pr_guide_does_not_rerun_tests(template_name: str) -> None:
    stage = _prompt(template_name, office_runs_tests=True, stage_id="pr").parts["stage"]
    assert NO_RERUN in stage
    assert "handoff" in stage and "build stage" in stage
    # apart from the warning itself, nothing tells the AI to run tests
    assert not re.search(r"(?i)\brun\b[^.\n]*\btests?\b", stage.replace(NO_RERUN, ""))
