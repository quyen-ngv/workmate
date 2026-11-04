"""A long system prompt reaches the Claude CLI through a file, not the command line (WinError 206 on Windows)."""

from pathlib import Path

from actoroffice.runtimes.base import TurnSpec
from actoroffice.runtimes.claude_sdk import PROMPT_ARG_LIMIT, ClaudeSdkRuntime, _prompt_file
from actoroffice.services.review import _ReadOnlyGate


def _spec(system_prompt: str, *, coding_preset: bool) -> TurnSpec:
    return TurnSpec(run_id="r1", prompt="p", cwd=Path.cwd(), system_prompt=system_prompt, model="m", tools=["Read"], coding_preset=coding_preset)


def test_short_prompt_stays_on_the_command_line() -> None:
    assert _prompt_file("x" * PROMPT_ARG_LIMIT) is None
    options = ClaudeSdkRuntime()._options(_spec("short", coding_preset=True), _ReadOnlyGate(Path.cwd()))
    assert options.system_prompt == {"type": "preset", "preset": "claude_code", "append": "short"}
    assert options.extra_args == {}


def test_long_prompt_goes_through_a_file() -> None:
    text = "Tìm hiểu " * PROMPT_ARG_LIMIT
    path = _prompt_file(text)
    assert path is not None
    try:
        assert path.read_text(encoding="utf-8") == text
        gate = _ReadOnlyGate(Path.cwd())
        coding = ClaudeSdkRuntime()._options(_spec(text, coding_preset=True), gate, path)
        assert coding.system_prompt == {"type": "preset", "preset": "claude_code"}
        assert coding.extra_args == {"append-system-prompt-file": str(path)}
        plain = ClaudeSdkRuntime()._options(_spec(text, coding_preset=False), gate, path)
        assert plain.system_prompt == {"type": "file", "path": str(path)}
        assert plain.extra_args == {}
    finally:
        path.unlink(missing_ok=True)
