"""Permission modes like Claude Code / Codex / Kiro, checked against commands real runs asked about (AO-2..AO-7):
read-only chains and workspace changes run without a click; the network, other folders and unknown programs ask;
"always allow" is per project and by command prefix."""

from __future__ import annotations

from importlib import resources
from pathlib import Path

import pytest
import yaml

from actoroffice.domain.org import Position
from actoroffice.security import shell
from actoroffice.security.policy import WAIT_LOOP_REASON, Decision, PolicyEngine, ToolCallContext, office_tool


@pytest.fixture
def ws(tmp_path) -> Path:
    path = tmp_path / "ws" / "AO-3"
    (path / "spikes").mkdir(parents=True)
    return path.resolve()


@pytest.fixture
def engine(tmp_path) -> PolicyEngine:
    return PolicyEngine(protected_dirs=[tmp_path / "home"])


def engineer() -> Position:
    data = yaml.safe_load((resources.files("actoroffice.templates.positions") / "engineer.yaml").read_text(encoding="utf-8"))
    return Position(id="pos", **data)


def ctx(ws: Path, mode: str = "workspace", **extra) -> ToolCallContext:
    return ToolCallContext(position=engineer(), workspace=ws, mode=mode, **extra)


def decide(engine: PolicyEngine, c: ToolCallContext, command: str) -> Decision:
    return engine.decide("Bash", {"command": command}, c).decision


@pytest.mark.parametrize(
    "command",
    [
        # read-only exploration that needed a click before (chains, pipes, 2>/dev/null)
        'cat pnpm-workspace.yaml; ls packages packages/adapters; grep -n -i -E "layer|depend|import" AGENTS.md CONTRIBUTING.md | head -30',
        'head -60 scripts/check-module-boundaries.mjs; grep -n "module-boundaries" package.json .github -r | head; ls .dependency-cruiser* 2>/dev/null; ls doc | head -40',
        "cat scripts/check-module-boundaries.mjs | head -80; ls",
        'git log --oneline -- src/actoroffice/runtimes/kiro_acp.py tests/test_kiro.py | head; grep -n -i kiro README.md docs/DESIGN.md | head -40',
        "ls; git ls-files | head -100",
        'sed -n 195,260p src/actoroffice/web/team_api.py; grep -n "ControlBody" -A8 src/actoroffice/web/team_api.py | head -20',
        "cat tests/test_kiro.py",
        "find . -name '*.py' -newer pyproject.toml | wc -l",
        "git status --short && git diff --stat",
        # workspace changes
        "rm cap1.json",
        "mkdir -p spikes/out && touch spikes/out/.keep",
        'git commit -m "fix(kiro): read file and command from permission _meta" -m "Co-Authored-By: Claude <noreply@anthropic.com>"',
        "git add -A && git commit -q -m 'x'",
        "py -m pytest -q 2>&1 | tail -5",
        "timeout 150 uv run python spikes/p7_kiro_live.py 2>&1 | tail -30",
        "uv run pytest -q > spikes/out.txt 2>&1",
    ],
)
def test_reads_and_workspace_changes_run_without_a_click(engine, ws, command):
    assert decide(engine, ctx(ws), command) == Decision.ALLOW, command


def test_cd_into_the_workspace_then_read(engine, ws):
    command = f"cd {ws.as_posix()} && git status --short && ls spikes tests 2>&1 | head -50 && cat spikes/p7_kiro_live.py | head -120"
    assert decide(engine, ctx(ws), command) == Decision.ALLOW


@pytest.mark.parametrize(
    "command",
    [
        "git push -u origin t/ao-4",  # network
        "git remote add origin https://github.com/acme/x.git",
        "curl https://example.com/api",
        "uvx ruff check src tests spikes",  # a program we do not know
        "which kiro-cli kiro; kiro-cli --version; kiro-cli --help",
        'K="$LOCALAPPDATA/Kiro-Cli/kiro-cli.exe"; "$K" --version',
        "cat ../../outside.txt",  # another folder
        "rm -r build",  # recursive delete
        "git reset --hard HEAD~1",  # drops work
        "ls $(pwd)",  # substitution
        "find . -name '*.tmp' -delete",
    ],
)
def test_network_other_folders_and_unknown_programs_ask(engine, ws, command):
    assert decide(engine, ctx(ws), command) == Decision.ASK, command


def test_hard_denies_and_protected_places_stay_denied_in_every_mode(engine, ws, tmp_path):
    for mode in ("read_only", "workspace", "full"):
        c = ctx(ws, mode)
        assert decide(engine, c, "git push origin main --force") == Decision.DENY
        assert decide(engine, c, "cat ~/.ssh/id_rsa") == Decision.DENY
        assert decide(engine, c, f"cat {(tmp_path / 'home' / 'office.db').as_posix()}") == Decision.DENY


def test_read_only_mode_asks_for_every_change(engine, ws):
    c = ctx(ws, "read_only")
    assert decide(engine, c, "cat a.py | head; git log --oneline | head") == Decision.ALLOW
    assert decide(engine, c, "git commit -m x") == Decision.ASK
    assert decide(engine, c, "rm a.txt") == Decision.ASK
    assert engine.decide("Write", {"file_path": str(ws / "a.py")}, c).decision == Decision.ASK
    assert engine.decide("Read", {"file_path": str(ws / "a.py")}, c).decision == Decision.ALLOW


def test_full_mode_runs_everything_but_the_hard_denies(engine, ws):
    c = ctx(ws, "full")
    assert decide(engine, c, "uvx ruff check src") == Decision.ALLOW
    assert decide(engine, c, "git push -u origin t/x") == Decision.ALLOW


def test_always_allow_by_prefix_matches_inside_chains(engine, ws):
    c = ctx(ws)
    verdict = engine.decide("Bash", {"command": "uvx ruff check src && git push origin t/x"}, c)
    assert verdict.decision == Decision.ASK
    assert [s["rule"] for s in verdict.suggestions] == ["Bash(uvx ruff*)", "Bash(git push*)"]
    c.allow_rules = ["Bash(uvx ruff*)"]
    assert decide(engine, c, "uvx ruff check src tests --output-format concise") == Decision.ALLOW
    assert decide(engine, c, "uvx ruff check src && git push origin t/x") == Decision.ASK  # push still asks
    c.allow_rules.append("Bash(git push*)")
    assert decide(engine, c, "uvx ruff check src && git push origin t/x") == Decision.ALLOW


def test_the_projects_own_commands_run(engine, ws):
    c = ctx(ws, project_commands=["mvn -q test"])
    assert decide(engine, c, "mvn -q test 2>&1 | tail -20") == Decision.ALLOW
    assert decide(engine, ctx(ws, position=None) if False else c, "make deploy") == Decision.ASK


def test_office_tools_in_every_runtime_naming_and_ticket_scoped_external_tools(engine, ws):
    c = ctx(ws)
    assert office_tool("@office/ticket_get") == "ticket_get" and office_tool("mcp__office__pr_create") == "pr_create"
    for name in ("@office/ticket_get", "@office/dossier_update", "mcp__office__step_done"):
        assert engine.decide(name, {}, c).decision == Decision.ALLOW, name
    assert engine.decide("mcp__office__pr_create", {}, c, external=True).decision == Decision.ALLOW
    assert engine.decide("mcp__office__pr_create", {}, ctx(ws, "read_only"), external=True).decision == Decision.ASK
    tainted = ctx(ws)
    tainted.tainted = True
    assert engine.decide("mcp__office__jira_comment", {}, tainted, external=True).decision == Decision.ASK


def test_split_respects_quotes_and_redirects():
    parts = shell.split('git commit -m "a && b > c" -m \'<noreply@x>\' 2>&1 | tail -3')
    assert [p.argv[:2] for p in parts] == [["git", "commit"], ["tail", "-3"]]
    assert parts[0].argv[3] == "a && b > c" and parts[0].redirects == [("2>&", "1")]
    assert shell.split("echo $(whoami)") is None and shell.split("cat <<EOF") is None and shell.split("(cd x; ls)") is None


def test_remembered_prefixes_never_cover_a_whole_interpreter():
    from actoroffice.security.policy import command_prefix

    assert command_prefix(["py", "-m", "ruff", "check", "src"]) == "py -m ruff"
    assert command_prefix(["uvx", "ruff", "check", "src"]) == "uvx ruff"
    assert command_prefix(["uv", "run", "pytest", "-q"]) == "uv run pytest"
    assert command_prefix(["node", "build.js", "--prod"]) == "node build.js"
    assert command_prefix(["kiro-cli", "whoami"]) == "kiro-cli whoami"
    assert command_prefix(["py"]) is None and command_prefix(["node"]) is None


@pytest.mark.parametrize(
    "command",
    [
        "uvx ruff check src tests spikes",
        "py -m ruff check src",
        "node build.js --prod",
        'py -c "print(1)"',
        "kiro-cli --version",
        "rm -r build",
        "git reset --hard HEAD~1",
        "git clean -fdx",
        "find . -name '*.tmp' -delete",
        "grep -l TODO -r src | xargs sed -i 's/TODO/DONE/'",
        "./gradlew test --offline",
        "make build",
    ],
)
def test_folder_mode_runs_any_program_that_stays_in_the_folder(engine, ws, command):
    verdict = engine.decide("Bash", {"command": command}, ctx(ws, "folder"))
    # sed -i through the shell is steered to the Edit tool in every mode (a deny with guidance, not a question)
    expected = Decision.DENY if "sed -i" in command else Decision.ALLOW
    assert verdict.decision == expected, (command, verdict.reason)


@pytest.mark.parametrize(
    "command",
    [
        "git push origin t/x",
        "curl https://example.com",
        "npm install left-pad",
        "py -m pip install requests",
        "node ../other/build.js",
        "rm -r ../shared",
        "cat ../../outside.txt",
        '"$K" --version',
        "ls $(pwd)",
        "find / -name x -delete",
        "uvx ruff check --config=/etc/ruff.toml src",
        "docker run alpine",
    ],
)
def test_folder_mode_still_asks_for_the_network_and_other_folders(engine, ws, command):
    assert engine.decide("Bash", {"command": command}, ctx(ws, "folder")).decision == Decision.ASK, command


def test_workspace_mode_offers_to_switch_to_folder_mode_when_only_local_programs_asked(engine, ws):
    local = engine.decide("Bash", {"command": "uvx ruff check src && rm -r build"}, ctx(ws))
    assert local.decision == Decision.ASK and local.suggestions[0] == {"scope": "mode", "rule": "mode:folder"}
    network = engine.decide("Bash", {"command": "uvx ruff check src && git push origin t/x"}, ctx(ws))
    assert all(s["scope"] != "mode" for s in network.suggestions)  # the folder mode would not cover the push


def test_newlines_inside_quotes_are_text_and_outside_they_split(engine, ws):
    commit = 'git add -A && git commit -q -m "fix(telegram): x\n\nCo-Authored-By: Claude <noreply@anthropic.com>" && git log --oneline -1'
    assert decide(engine, ctx(ws), commit) == Decision.ALLOW
    assert decide(engine, ctx(ws), "ls\ncurl https://x") == Decision.ASK  # two commands, the second asks
    parts = shell.split("ls\ncat a.txt")
    assert [p.argv[0] for p in parts] == ["ls", "cat"]
    assert shell.split("ls \\\nrm x") is None  # a line continuation is judged as a whole


# ---- SI-6: a wait loop without a bound is denied, with guidance for the AI ----------------------------------

def without_wait_check(monkeypatch, engine: PolicyEngine, tool: str, command: str, c: ToolCallContext):
    """The verdict the command got before SI-6: the same policy with the wait-loop check switched off."""
    monkeypatch.setattr(shell, "wait_loop", lambda _command, *_args, **_kwargs: False)
    try:
        return engine.decide(tool, {"command": command}, c)
    finally:
        monkeypatch.undo()


def test_ac2_wait_loop_is_denied_with_guidance(engine, ws):
    verdict = engine.decide("Bash", {"command": "until [ -s .pytest-report.xml ]; do sleep 15; done"}, ctx(ws))
    assert verdict.decision == Decision.DENY
    assert verdict.reason == WAIT_LOOP_REASON
    assert "foreground" in verdict.reason and "timeout" in verdict.reason


@pytest.mark.parametrize(
    "extra",
    [
        {"mode": "read_only"},
        {"mode": "workspace"},
        {"mode": "folder"},
        {"mode": "full"},
        {"mode": "full", "trust_all": True},
        {"mode": "folder", "allow_rules": ["Bash(*)", "Bash"]},
        {"mode": "folder", "project_commands": ["while true; do sleep 5; done"]},
        {"mode": "folder", "bash_allow": ["*"]},
    ],
)
def test_ac3_wait_loop_denied_in_every_mode_and_trust(engine, ws, extra):
    extra = dict(extra)
    position = engineer()
    position.policy.bash_allow = extra.pop("bash_allow", position.policy.bash_allow)
    c = ToolCallContext(position=position, workspace=ws, **extra)
    verdict = engine.decide("Bash", {"command": "while true; do sleep 5; done"}, c)
    assert verdict.decision == Decision.DENY and verdict.reason == WAIT_LOOP_REASON


@pytest.mark.parametrize(
    "command",
    [
        "pytest -q > out.txt & until [ -s out.txt ]; do sleep 15; done",
        "pytest -q > out.txt &\nuntil [ -s out.txt ]\ndo\n  sleep 15\ndone\ncat out.txt",
        "while ! test -f out.xml; do\n  echo waiting\n  sleep 10\ndone",
        "ls && while true; do sleep 1; done",
    ],
)
def test_ac4_background_then_wait_and_multiline(engine, ws, command):
    assert decide(engine, ctx(ws, "folder"), command) == Decision.DENY, command


def test_ac5_counter_loop_is_still_unbounded(engine, ws):
    command = "i=0; while [ $i -lt 10 ]; do sleep 1; i=$((i+1)); done"
    assert decide(engine, ctx(ws, "folder"), command) == Decision.DENY


@pytest.mark.parametrize(
    "command",
    [
        "bash -c 'until [ -s f ]; do sleep 5; done'",
        'sh -c "while true; do sleep 5; done"',
        "bash -lc 'until [ -s f ]; do sleep 5; done' && cat f",
        "timeout 5 ls; bash -c 'until [ -s f ]; do sleep 5; done'",  # the timeout bounds another command
    ],
)
def test_ac6_loop_inside_bash_c_is_denied(engine, ws, command):
    assert decide(engine, ctx(ws, "folder"), command) == Decision.DENY, command


@pytest.mark.parametrize(
    "command",
    [
        "timeout 300 bash -c 'until [ -s f ]; do sleep 5; done'",
        "timeout -k 5 300 sh -c 'until [ -s f ]; do sleep 5; done'",
        "FOO=1 timeout 60s bash -c \"while true; do sleep 5; done\"",
        "uv run pytest -q; timeout 300 bash -c 'until [ -s f ]; do sleep 5; done'",
    ],
)
def test_ac7_timeout_bounds_the_loop(engine, ws, command, monkeypatch):
    c = ctx(ws, "folder")
    assert shell.wait_loop(command) is False
    verdict = engine.decide("Bash", {"command": command}, c)
    assert verdict.reason != WAIT_LOOP_REASON
    assert verdict == without_wait_check(monkeypatch, engine, "Bash", command, c)


@pytest.mark.parametrize(
    ("tool", "command"),
    [
        ("PowerShell", "while (-not (Test-Path out.xml)) { Start-Sleep 5 }"),
        ("PowerShell", "do { Start-Sleep -Seconds 5 } until (Test-Path out.xml)"),
        ("PowerShell", "do {\n  sleep 2\n} while (-not (Test-Path out.xml))"),
        ("PowerShell", "Start-Job { pytest }; while ($true) { Start-Sleep -Milliseconds 500 }"),
        ("Bash", 'powershell -Command "while (-not (Test-Path out.xml)) { Start-Sleep 5 }"'),
        ("Bash", "pwsh -NoProfile -c 'do { Start-Sleep 1 } until (Test-Path x)'"),
    ],
)
def test_ac8_powershell_wait_loops(engine, ws, tool, command):
    verdict = engine.decide(tool, {"command": command}, ctx(ws, "folder"))
    assert verdict.decision == Decision.DENY and verdict.reason == WAIT_LOOP_REASON, command


@pytest.mark.parametrize(
    ("tool", "command"),
    [
        ("Bash", "uv run pytest -q"),
        ("Bash", "git status"),
        ("Bash", 'grep -rn "while true; do sleep" src'),
        ("Bash", 'git commit -m "fix: until loop with sleep"'),
        ("Bash", "git commit -m 'fix: while x; do sleep 1; done'"),
        ("Bash", "sleep 2"),
        ("Bash", "while read line; do echo $line; done < f.txt"),
        ("Bash", "pytest -q &"),
        ("Bash", "for f in a b; do sleep 1; done"),
        ("Bash", "echo until done; sleep 1"),
        ("PowerShell", "Get-ChildItem | Where-Object { $_.Name -like 'while*' }"),
        ("PowerShell", "Start-Sleep 2"),
        ("PowerShell", "Select-String -Pattern 'while (1) { Start-Sleep 1 }' a.ps1"),
    ],
)
def test_ac9_ordinary_commands_unchanged(engine, ws, tool, command, monkeypatch):
    c = ctx(ws)
    assert shell.wait_loop(command) is False, command
    verdict = engine.decide(tool, {"command": command}, c)
    assert verdict == without_wait_check(monkeypatch, engine, tool, command, c)


# ---- SI-12: nested loops, timeout 0, ( / $( before timeout, PowerShell quoting --------------------------------

@pytest.mark.parametrize(
    "command",
    [
        "while true; do for f in *; do echo $f; done; sleep 5; done",
        "until [ -s f ]; do for i in 1 2; do sleep 1; done; done",  # the sleep is in the inner loop
        "while true; do for a in x; do for b in y; do echo $a$b; done; done; sleep 5; done",  # three levels
        "while true; do while read l; do echo $l; done < f; sleep 2; done",
        "for f in a b; do while true; do sleep 1; done; done",  # the wait loop is the inner one
        "while true\ndo\n  for f in *\n  do\n    echo $f\n  done\n  sleep 5\ndone",
        "while true; do echo done; for f in *; do echo \"done\"; done; sleep 5; done",
        "bash -c 'while true; do for f in *; do echo $f; done; sleep 5; done'",
        'sh -c "until [ -s f ]; do for i in 1 2; do sleep 1; done; done"',
        # words that look like do/done are not the keywords
        "done=0; while [ $done = 0 ]; do if [ -s f ]; then done=1; fi; sleep 5; done",
        "while true; do ./done.sh; sleep 5; done",
        "while true; do done.sh; do-it; sleep 1; done",
        "until [ -s f ]; do dosomething; sleep 1; done",
        "while true; do echo then done; sleep 5; done",
        "while true; do echo do done; echo else done; sleep 5; done",
        "while true; do if [ -s f ]; then echo then done; fi; sleep 5; done",
        # never closed: judged on what is there
        "while true; do sleep 5",
        "while true; do for f in *; do echo $f; done; sleep 5",
    ],
)
def test_ac1_nested_loop_with_sleep_is_denied(engine, ws, command):
    assert shell.wait_loop(command) is True, command
    verdict = engine.decide("Bash", {"command": command}, ctx(ws, "folder"))
    assert verdict.decision == Decision.DENY and verdict.reason == WAIT_LOOP_REASON, command


@pytest.mark.parametrize(
    "command",
    [
        "while read line; do for w in $line; do echo $w; done; done < f.txt",
        "for f in *; do while read l; do echo $l; done < $f; done; sleep 1",  # the sleep runs after the loops
        "while read l; do echo done; done < f.txt; sleep 1",
        "while read l; do echo \"do\"; for w in $l; do echo $w; done; done < f.txt && sleep 2",
        "bash -c 'while read l; do for w in $l; do echo $w; done; done < f.txt'",
        "while read l; do ./done.sh $l; done=1; done < f.txt; sleep 1",
        "while read l; do echo $l; done < f.txt; ./do-sleep.sh",
        "while read l; do echo $l",  # never closed, no sleep
    ],
)
def test_ac1_nested_loop_without_sleep_is_unchanged(engine, ws, command, monkeypatch):
    c = ctx(ws)
    assert shell.wait_loop(command) is False, command
    verdict = engine.decide("Bash", {"command": command}, c)
    assert verdict == without_wait_check(monkeypatch, engine, "Bash", command, c)


@pytest.mark.parametrize(
    "command",
    [
        'timeout 0 bash -c "while true; do sleep 5; done"',  # GNU timeout 0: no limit
        "timeout 0s sh -c 'until [ -s f ]; do sleep 5; done'",
        "timeout -k 5 0 bash -c 'until [ -s f ]; do sleep 5; done'",
        "timeout --kill-after 5 0 bash -c 'until [ -s f ]; do sleep 5; done'",
        "timeout --signal KILL 0 bash -c 'until [ -s f ]; do sleep 5; done'",
        "timeout -vk 5 0 bash -c 'until [ -s f ]; do sleep 5; done'",
        "timeout 5; while true; do sleep 1; done",  # timeout.exe just waits, it wraps nothing
        "timeout 5 && bash -c 'until [ -s f ]; do sleep 5; done'",
        "timeout /t 5 bash -c 'until [ -s f ]; do sleep 5; done'",
        "X=$(timeout 5 cat f) bash -c 'until [ -s f ]; do sleep 5; done'",  # the timeout bounds only cat
    ],
)
def test_ac2_timeout_without_a_bound_is_denied(engine, ws, command):
    assert shell.wait_loop(command) is True, command
    verdict = engine.decide("Bash", {"command": command}, ctx(ws, "folder"))
    assert verdict.decision == Decision.DENY and verdict.reason == WAIT_LOOP_REASON, command


@pytest.mark.parametrize(
    "command",
    [
        '(timeout 300 bash -c "while true; do sleep 5; done")',
        'x=$(timeout 300 bash -c "until [ -s f ]; do sleep 5; done")',
        "echo $( timeout 1.5m sh -c 'until [ -s f ]; do sleep 5; done' )",
        "timeout --kill-after=5 300 bash -c 'until [ -s f ]; do sleep 5; done'",
        "timeout --kill-after 5 300 bash -c 'until [ -s f ]; do sleep 5; done'",
        "timeout --preserve-status -s TERM 300 bash -c 'until [ -s f ]; do sleep 5; done'",
    ],
)
def test_ac2_timeout_inside_subshell_bounds_the_loop(engine, ws, command, monkeypatch):
    c = ctx(ws, "folder")
    assert shell.wait_loop(command) is False, command
    verdict = engine.decide("Bash", {"command": command}, c)
    assert verdict.reason != WAIT_LOOP_REASON
    assert verdict == without_wait_check(monkeypatch, engine, "Bash", command, c)


@pytest.mark.parametrize(
    ("tool", "command"),
    [
        ("PowerShell", 'Copy-Item out.xml "C:\\build\\"; while ($true) { Start-Sleep 5 }'),
        ("PowerShell", "Copy-Item out.xml 'C:\\build\\'; do { Start-Sleep 1 } until (Test-Path x)"),
        ("PowerShell", 'Write-Host "say ""hi"" C:\\tmp\\"; while ($true) { Start-Sleep 5 }'),
        ("Bash", "pwsh -c 'Copy-Item a \"C:\\build\\\"; while ($true) { Start-Sleep 5 }'"),
    ],
)
def test_ac3_powershell_backslash_is_no_escape(engine, ws, tool, command):
    verdict = engine.decide(tool, {"command": command}, ctx(ws, "folder"))
    assert verdict.decision == Decision.DENY and verdict.reason == WAIT_LOOP_REASON, command


@pytest.mark.parametrize(
    "command",
    [
        "Select-String -Pattern 'while (1) { Start-Sleep 1 }' a.ps1",
        "Write-Host \"it`\"s while (1) { Start-Sleep 1 }\"",  # `" is PowerShell's escaped quote
        "Write-Host 'it''s while (1) { Start-Sleep 1 }'",
        'Get-ChildItem "C:\\build\\" | Where-Object { $_.Name -like \'while*\' }',
    ],
)
def test_ac3_powershell_quoted_loops_are_not_denied(engine, ws, command, monkeypatch):
    c = ctx(ws)
    assert shell.wait_loop(command, dialect="ps") is False, command
    verdict = engine.decide("PowerShell", {"command": command}, c)
    assert verdict == without_wait_check(monkeypatch, engine, "PowerShell", command, c)
