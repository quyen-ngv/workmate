"""A CLI that is not signed in is told apart from other failures, and Kiro's stderr is shown without what it echoes."""

from __future__ import annotations

import pytest

from actoroffice.engine import failures
from actoroffice.engine.failures import FailureKind
from actoroffice.runtimes.kiro_acp import stderr_summary

KIRO = (
    'Kiro: Kiro could not load the available models because you are not signed in. Please sign in and retry.\n'
    '{"code":-32000,"message":"…","data":{"errorType":"ModelRegistryUnauthenticatedError","detail":"TokenExpiredError"}}'
)


@pytest.mark.parametrize(
    "error",
    ["Not logged in · Please run /login", KIRO, "TokenExpiredError", "Couldn't fetch your cloud config: fetch failed. You are not signed in"],
)
def test_a_cli_that_is_not_signed_in_is_its_own_kind_and_is_not_retried(error: str) -> None:
    for progressed in (False, True):
        assert failures.classify(error, progressed=progressed) == FailureKind.AUTH
    assert FailureKind.AUTH not in failures.RETRIED


def test_people_are_told_which_command_signs_in() -> None:
    kiro = failures.run_error("auth", "Kiro", 0, KIRO)
    assert kiro.startswith("Kiro · chưa đăng nhập:") and "`kiro-cli login`" in kiro and "bấm Thử lại" in kiro
    assert "/login" in failures.run_error("auth", "Claude", 0, "Not logged in")


@pytest.mark.parametrize("error", ["test_login.py failed: user is not allowed", "the file token.json was not found", None])
def test_other_failures_keep_their_kind(error: str | None) -> None:
    assert failures.classify(error, progressed=True) == FailureKind.AI


def test_kiro_stderr_is_shown_without_the_request_it_echoes() -> None:
    stderr = (
        '{"endpoint":"https://app.kiro.dev"}\n'
        "[INFO] [KiroAgent] MCP subsystem initialized\n"
        "[ERROR] (node:6460) ExperimentalWarning: SQLite is an experimental feature and might change at any time\n"
        "(Use `node --trace-warnings ...` to show where the warning was created)\n"
        '[ERROR] Error handling request {"jsonrpc":"2.0","id":2,"method":"session/new","params":{"cwd":"C:\\\\ws\\\\AO-1","mcpServers":[{"name":"office",'
        '"env":[{"name":"ACTOROFFICE_MCP_TOKEN","value":"ba1472f56518f4055a52cd0f050617d2"}]}],"prompt":"bí mật của ticket"}} '
        '{"code":-32601,"message":"_kiro/terminal/shell_type is not supported by this client"}\n'
    )
    shown = stderr_summary(stderr)
    assert "ba1472f5" not in shown and "bí mật" not in shown and "ExperimentalWarning" not in shown
    assert 'Error handling request session/new: {"code":-32601,"message":"_kiro/terminal/shell_type is not supported by this client"}' in shown
    assert "MCP subsystem initialized" in shown
    assert stderr_summary("") == ""
