"""Shell commands seen the way a person reviewing them would: split into the commands of a chain, with their
redirects, respecting quotes. Each command gets a class:

- read       only reads (ls, cat, grep, git log …) — never needs a click
- workspace  changes files, but only inside the ticket's workspace (rm a file, mkdir, git commit …)
- local      runs any program, or deletes / resets, with every path it names inside the workspace
             (uvx ruff check src, node build.js, rm -r build, git reset --hard)
- network    talks to another machine (git push, curl, installing packages …)
- unknown    a program we know nothing about (or one used in a risky way)

The policy turns the classes into allow / ask according to the project's permission mode (security/policy.py).
Commands with substitution ($(…), backticks), heredocs or subshells are not split: they are judged as a whole.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path


class Kind(StrEnum):
    READ = "read"
    WORKSPACE = "workspace"
    LOCAL = "local"
    NETWORK = "network"
    UNKNOWN = "unknown"


ORDER = {Kind.READ: 0, Kind.WORKSPACE: 1, Kind.LOCAL: 2, Kind.NETWORK: 3, Kind.UNKNOWN: 4}

# Programs whose job is to talk to other machines.
NETWORK_PROGRAMS = {
    "curl", "wget", "ssh", "scp", "sftp", "rsync", "ftp", "nc", "ncat", "telnet", "ping", "gh", "az", "aws",
    "gcloud", "kubectl", "helm", "terraform", "docker", "podman", "invoke-webrequest", "iwr", "irm",
    "invoke-restmethod", "pip", "pip3", "choco", "winget", "brew", "apt", "apt-get", "yum", "dnf", "scoop", "gem",
}
# Package managers: installing or publishing downloads (or uploads) and writes outside the folder.
INSTALLS = {
    "npm": {"install", "i", "add", "ci", "update", "upgrade", "publish", "login", "link"},
    "pnpm": {"install", "i", "add", "update", "up", "publish", "login", "link"},
    "yarn": {"add", "install", "upgrade", "publish", "login", "link"},
    "bun": {"install", "i", "add", "update", "publish", "link"},
    "cargo": {"install", "publish", "login"},
    "go": {"install", "get"},
    "composer": {"install", "require", "update"},
    "poetry": {"add", "install", "update", "publish"},
    "uv": {"add", "pip", "tool", "publish"},
    "dotnet": {"add", "tool", "nuget"},
    "python": {"-m pip"}, "py": {"-m pip"}, "python3": {"-m pip"},
}

# Programs that only read (when their path arguments stay readable). sed and find have their own checks.
READ_PROGRAMS = {
    "ls", "dir", "cat", "type", "head", "tail", "wc", "sort", "uniq", "cut", "tr", "nl", "column", "grep", "egrep",
    "fgrep", "rg", "tree", "file", "stat", "du", "df", "pwd", "echo", "printf", "which", "where", "whereis",
    "basename", "dirname", "realpath", "readlink", "date", "whoami", "hostname", "uname", "diff", "cmp", "comm",
    "jq", "true", "false", "test", "[", "less", "more", "md5sum", "sha1sum", "sha256sum", "xxd", "od", "strings",
}
NO_PATH_PROGRAMS = {"echo", "printf", "date", "whoami", "hostname", "uname", "pwd", "which", "where", "whereis", "true", "false", "tr"}
GREP_LIKE = {"grep", "egrep", "fgrep", "rg"}
# Commands that only change files, judged by where their path arguments point.
FILE_PROGRAMS = {"mkdir", "touch", "cp", "mv", "rm", "rmdir", "ln", "chmod"}
# Wrappers that run another command: judge the inner command.
WRAPPERS = {"timeout", "time", "nice", "nohup", "command", "xargs"}

GIT_READ = {
    "status", "log", "diff", "show", "ls-files", "rev-parse", "describe", "blame", "shortlog", "grep", "cat-file",
    "ls-tree", "merge-base", "reflog", "rev-list", "name-rev", "whatchanged", "version", "help", "count-objects",
    "for-each-ref", "check-ignore", "var",
}
GIT_LOCAL = {
    "add", "commit", "checkout", "switch", "stash", "merge", "rebase", "branch", "tag", "cherry-pick", "revert",
    "mv", "rm", "init", "notes", "apply", "am", "worktree", "restore", "reset", "config",
}
GIT_NETWORK = {"push", "pull", "fetch", "clone", "ls-remote", "submodule", "remote"}

SAFE_TARGETS = {"/dev/null", "nul", "NUL", "$null", "/dev/stdout", "/dev/stderr"}
NOT_SPLITTABLE = re.compile(r"`|\$\(|<\(|>\(|<<")
ASSIGNMENT = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*=")
OPERATORS = {"&&", "||", ";", "|", "&", "|&"}
RANGE = re.compile(r"[\d,;$p]+")  # numbers and sed-style ranges (195,260p)
GLOB = re.compile(r"[*?\[]")
GIT_BASH_DRIVE = re.compile(r"^/([a-zA-Z])(/|$)")


@dataclass
class Command:
    argv: list[str]
    redirects: list[tuple[str, str]] = field(default_factory=list)  # (operator, target)

    @property
    def text(self) -> str:
        return " ".join(self.argv)


@dataclass
class Place:
    """Where a command runs and what it may touch."""

    workspace: Path | None
    readable: list[Path]
    protected: list[Path]


def split(command: str) -> list[Command] | None:
    """The commands of a chain, or None when the line cannot be split safely (substitution, heredoc, subshell,
    unbalanced quotes)."""
    if NOT_SPLITTABLE.search(command):
        return None
    command = _newlines_as_separators(command)
    if command is None:
        return None
    lexer = shlex.shlex(command, posix=True, punctuation_chars=";&|<>()")
    lexer.whitespace_split = True
    lexer.commenters = ""
    try:
        tokens = list(lexer)
    except ValueError:
        return None
    commands: list[Command] = []
    current = Command([])
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok in ("(", ")"):
            return None
        if tok in OPERATORS:
            if not current.argv:
                return None  # `; ls` or `&& && x`
            commands.append(current)
            current = Command([])
            i += 1
            continue
        if tok and set(tok) <= set("<>&"):
            fd = ""
            last = current.argv[-1] if current.argv else ""
            if re.fullmatch(r"\d", last) and re.search(rf"(?<!\S){last}{re.escape(tok)}", command):
                fd = current.argv.pop()  # 2>, 1>> …
            if i + 1 >= len(tokens):
                return None
            current.redirects.append((fd + tok, tokens[i + 1].lstrip("&") if tok == ">" else tokens[i + 1]))
            i += 2
            continue
        current.argv.append(tok)
        i += 1
    if current.argv:
        commands.append(current)
    elif current.redirects:
        return None
    return commands or None


def _newlines_as_separators(command: str) -> str | None:
    """A newline outside quotes ends a command (like `;`); inside quotes it is text (a multi-line commit message).
    None when a backslash-newline continuation or an unbalanced quote makes it unclear."""
    out: list[str] = []
    quote = ""
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == "\\" and quote != "'" and i + 1 < len(command):
            if command[i + 1] in "\r\n" and not quote:
                return None
            out += [ch, command[i + 1]]
            i += 2
            continue
        if quote:
            if ch == quote:
                quote = ""
        elif ch in "'\"":
            quote = ch
        elif ch in "\r\n":
            out.append(" ; " if ch == "\n" else " ")
            i += 1
            continue
        out.append(ch)
        i += 1
    return None if quote else "".join(out)


def classify(cmd: Command, place: Place, cwd: Path | None) -> tuple[Kind, str]:
    """(class, why) for one command of a chain. `cwd` is where the chain has cd'ed to so far."""
    writes = False
    for op, target in cmd.redirects:
        if target.isdigit() or target in SAFE_TARGETS:
            continue  # 2>&1, >/dev/null
        where = _where(target, place, cwd)
        if "<" in op:
            if where not in ("workspace", "readable"):
                return Kind.UNKNOWN, f"reads {target}"
        elif where != "workspace":
            return Kind.UNKNOWN, f"writes {target} outside the workspace"
        else:
            writes = True
    argv = list(cmd.argv)
    while argv and ASSIGNMENT.match(argv[0]):
        argv.pop(0)  # FOO=bar cmd
    if not argv:
        return Kind.READ, "assignment"
    base: tuple[Kind, str] = (Kind.WORKSPACE, "writes a file in the workspace") if writes else (Kind.READ, "")
    program = program_name(argv[0])
    if program in WRAPPERS:
        rest = _after_wrapper(program, argv)
        if program == "xargs":
            return _worst(base, _local(rest or ["xargs"], place, cwd))
        return _worst(base, classify(Command(rest), place, cwd)) if rest else base
    if program == "git":
        return _worst(base, _git(argv[1:], place, cwd))
    if program == "sed":
        flags = [a for a in argv[1:] if a.startswith("-")]
        files = [a for a in argv[1:] if not a.startswith("-")][1:]
        if any(f == "--in-place" or (not f.startswith("--") and "i" in f) for f in flags):
            return _worst(base, _paths(files, place, cwd, Kind.LOCAL, why="sed -i edits files"))
        if not any(f.startswith("-n") for f in flags):
            return _worst(base, _paths(files, place, cwd, Kind.LOCAL, why="sed script may write (no -n)"))
        return _worst(base, _paths([a for a in argv[1:] if not a.startswith("-")][1:], place, cwd, Kind.READ))
    if program == "find":
        roots = [a for a in argv[1:] if not a.startswith(("-", "(", "!"))][:1] or ["."]
        if any(a in ("-exec", "-execdir", "-ok", "-okdir", "-delete", "-fprint", "-fprintf", "-fls") for a in argv[1:]):
            return _worst(base, _paths(roots, place, cwd, Kind.LOCAL, why="find runs or deletes"))
        return _worst(base, _paths(roots, place, cwd, Kind.READ))
    if program == "cd":
        return _paths(argv[1:2] or ["~"], place, cwd, Kind.READ)
    if program in READ_PROGRAMS:
        if program in NO_PATH_PROGRAMS:
            return base if base[0] != Kind.READ else (Kind.READ, program)
        positional = [a for a in argv[1:] if not a.startswith("-")]
        if program in GREP_LIKE and not any(a in ("-e", "-f", "--regexp", "--file") for a in argv[1:]):
            positional = positional[1:]  # the first one is the pattern
        if program == "jq":
            positional = positional[1:]  # the filter
        return _worst(base, _paths(positional, place, cwd, Kind.READ))
    if program in FILE_PROGRAMS:
        flags = [a for a in argv[1:] if a.startswith("-")]
        recursive = program in ("rm", "rmdir") and any(f == "--recursive" or (not f.startswith("--") and "r" in f.lower()) for f in flags)
        targets = [a for a in argv[1:] if not a.startswith("-") and not (program == "chmod" and re.fullmatch(r"[0-7]{3,4}|[ugoa]*[+=-][rwxX]+", a))]
        if not targets:
            return Kind.UNKNOWN, program
        if recursive:
            return _paths(targets, place, cwd, Kind.LOCAL, why="recursive delete")
        return _paths(targets, place, cwd, Kind.WORKSPACE)
    return _worst(base, _local(argv, place, cwd))


def _local(argv: list[str], place: Place, cwd: Path | None) -> tuple[Kind, str]:
    """Any other program: local when every path it names stays in the workspace; network when that is its job."""
    program = program_name(argv[0])
    if "$" in argv[0] or "%" in argv[0]:
        return Kind.UNKNOWN, f"the program name {argv[0]} is a variable"
    if program in NETWORK_PROGRAMS:
        return Kind.NETWORK, program
    subs = INSTALLS.get(program, set())
    if argv[1:2] and (argv[1] in subs or " ".join(argv[1:3]) in subs):
        return Kind.NETWORK, f"{program} {argv[1]} downloads packages"
    for arg in argv[1:]:
        value = arg.split("=", 1)[1] if arg.startswith("-") and "=" in arg else arg
        if "://" in value:
            return Kind.NETWORK, f"{program} {value[:60]}"
        if value.startswith("-") or not re.search(r"[/\\]|^~|^\.\.?$", value):
            continue  # flags and plain words
        where = _where(value, place, cwd)
        if where != "workspace":
            return Kind.UNKNOWN, f"{value} is outside the workspace"
    return Kind.LOCAL, f"{program} runs a program"


def unwrap(cmd: Command) -> Command:
    """The command a chain part really runs: without leading assignments and wrappers (timeout 60 x → x)."""
    argv = list(cmd.argv)
    while True:
        while argv and ASSIGNMENT.match(argv[0]):
            argv.pop(0)
        if argv and program_name(argv[0]) in WRAPPERS - {"xargs"}:
            argv = _after_wrapper(program_name(argv[0]), argv)
            continue
        return Command(argv, cmd.redirects)


def touches_protected(cmd: Command, place: Place, cwd: Path | None) -> str | None:
    """A word of the command (or a redirect) that names a protected location."""
    for word in [*cmd.argv[1:], *(t for _, t in cmd.redirects)]:
        if word and not word.startswith("-") and re.search(r"[/\\~.]", word) and _where(word, place, cwd) == "protected":
            return word
    return None


def worst(kinds: list[Kind]) -> Kind:
    return max(kinds, key=lambda k: ORDER[k]) if kinds else Kind.READ


def _git(args: list[str], place: Place, cwd: Path | None) -> tuple[Kind, str]:
    repo = None
    while args and args[0].startswith("-"):  # git -C path, git --no-pager …
        if args[0] == "-C" and len(args) > 1:
            repo = args[1]
        args = args[2:] if args[0] in ("-C", "-c") else args[1:]
    if not args:
        return Kind.READ, "git"
    kind, why = _git_kind(args[0], args[1:])
    if repo is not None and kind not in (Kind.NETWORK,):
        where = _where(repo, place, cwd)
        if where != "workspace" and not (where == "readable" and kind == Kind.READ):
            return Kind.UNKNOWN, f"git in {repo}"
    return kind, why


def _git_kind(sub: str, rest: list[str]) -> tuple[Kind, str]:
    if sub in GIT_NETWORK:
        if sub == "remote" and (not rest or rest[0] in ("-v", "show", "get-url")):
            return Kind.READ, "git remote"
        return Kind.NETWORK, f"git {sub}"
    if sub in GIT_READ:
        return Kind.READ, f"git {sub}"
    if sub == "stash" and rest[:1] in (["list"], ["show"]):
        return Kind.READ, "git stash list"
    if sub in ("branch", "tag") and all(a.startswith("-") and a not in ("-d", "-D", "-m", "-M", "--delete", "-f") for a in rest):
        return Kind.READ, f"git {sub}"
    if sub == "config" and rest[:1] and rest[0] in ("--get", "--list", "-l", "--get-all"):
        return Kind.READ, "git config"
    if sub == "config" and any(a in ("--global", "--system") for a in rest):
        return Kind.UNKNOWN, "git config outside the repo"
    if sub == "reset" and "--hard" in rest:
        return Kind.LOCAL, "git reset --hard drops uncommitted work"
    if sub in ("checkout", "restore") and ("--" in rest or "." in rest) and "--staged" not in rest and "-b" not in rest:
        return Kind.LOCAL, f"git {sub} drops uncommitted changes"
    if sub == "clean":
        return Kind.LOCAL, "git clean deletes untracked files"
    if sub in GIT_LOCAL:
        return Kind.WORKSPACE, f"git {sub}"
    return Kind.LOCAL, f"git {sub}"


def _paths(args: list[str], place: Place, cwd: Path | None, inside: Kind, why: str = "") -> tuple[Kind, str]:
    """`inside` when every path stays in the workspace (or a readable folder, for reads)."""
    for arg in args:
        if not arg or RANGE.fullmatch(arg):
            continue
        where = _where(arg, place, cwd)
        if where == "workspace" or (where == "readable" and inside == Kind.READ):
            continue
        if where == "protected":
            return Kind.UNKNOWN, f"{arg} is a protected location"
        return Kind.UNKNOWN, f"{arg} is outside the workspace"
    return inside, why


def resolve(raw: str, place: Place, cwd: Path | None) -> Path | None:
    """The absolute path a shell word names (None when it depends on variables)."""
    if "$" in raw or "%" in raw:
        return None
    text = os.path.expanduser(raw)
    drive = GIT_BASH_DRIVE.match(text)
    if os.name == "nt" and drive:
        text = f"{drive.group(1)}:/{text[3:]}"  # Git Bash: /c/Users → C:/Users
    glob = GLOB.search(text)
    if glob:
        head = text[: glob.start()]
        text = head.rsplit("/", 1)[0] if "/" in head else "."
    path = Path(text or ".")
    if not path.is_absolute():
        base = cwd or place.workspace
        if base is None:
            return None
        path = base / path
    try:
        return path.resolve()
    except OSError:
        return None


def _where(raw: str, place: Place, cwd: Path | None) -> str:
    resolved = resolve(raw, place, cwd)
    if resolved is None:
        return "unknown"
    if any(_within(resolved, p) for p in place.protected):
        return "protected"
    if place.workspace and _within(resolved, place.workspace):
        return "workspace"
    if any(_within(resolved, d) for d in place.readable):
        return "readable"
    return "outside"


# ---- wait loops (SI-6) -----------------------------------------------------------------------------------

# Where a shell keyword can start a command: the start, after a separator, or after then/do/else.
_CMD_START = r"(?:^|[;&|\n({]|\b(?:then|do|else|elif)\b)\s*"
_BASH_LOOP = re.compile(_CMD_START + r"(?:until|while)\s")
# The keyword is a whole word: `done=1`, `./done.sh`, `do-it` are not.
_DO_DONE = re.compile(r"(?<![\w./-])(?:do|done)(?=[\s;&|()<>]|$)")
_KEYWORD_BEFORE = re.compile(r"(?:^|[;&|\n(){}]|\b(?:then|do|else|elif))[ \t]*$")
_BASH_SLEEP = re.compile(_CMD_START + r"sleep\b")
_PS_START = r"(?:^|[;\n{}]|&&|\|\|)\s*"
_PS_WHILE = re.compile(_PS_START + r"while\s*\(", re.IGNORECASE)
_PS_DO = re.compile(_PS_START + r"do\s*\{", re.IGNORECASE)
_PS_DO_END = re.compile(r"\s*(?:until|while)\s*\(", re.IGNORECASE)
_PS_SLEEP = re.compile(r"(?<![\w$-])(?:start-sleep|sleep)\b", re.IGNORECASE)
# A quoted string that another shell runs: bash -c '…', sh -lc "…", powershell -NoProfile -Command "…"
_LAUNCH = re.compile(
    r"(?<![\w-])(?P<program>bash|sh|zsh|dash|powershell|pwsh)(?:\.exe)?(?:\s+\S+)*?\s+-(?:[a-z]*c|command)\s*$",
    re.IGNORECASE,
)
# `timeout [options] N[smhd] <command>` with N > 0. GNU timeout 0 never stops the command, and Windows'
# timeout.exe (`timeout /t 5`) only waits: neither bounds anything.
_BOUNDED = re.compile(
    r"^\s*(?:[A-Za-z_]\w*=\S*\s+)*timeout"
    r"(?:\s+(?:-[ks]\s*\S+|--[a-z-]+(?:=\S+)?|-[a-z]+))*\s+(?P<n>\d+(?:\.\d+)?)[smhd]?\s"
)


def wait_loop(command: str, depth: int = 0, dialect: str = "sh") -> bool:
    """True when the command waits in a loop that nothing bounds: `until/while …; do … sleep …; done` (sh) or
    `while (…) { Start-Sleep … }`, `do { … sleep … } until/while (…)` (PowerShell). Keywords count only outside
    quotes (`dialect` "ps": PowerShell quoting, where `\\` is no escape); a string run by `bash -c` /
    `powershell -Command` is looked into, unless `timeout N` (N > 0) wraps that command."""
    masked, strings = _mask_quotes(command, dialect)
    if _sh_wait(masked) or _ps_wait(masked):
        return True
    if depth >= 3:
        return False
    for start, inner in strings:
        segment = re.split(r"[;&|\n]", masked[:start])[-1]
        launch = _LAUNCH.search(segment)
        if launch is None or _bounded(segment[: launch.start()]):
            continue
        inner_dialect = "ps" if launch.group("program").lower() in ("powershell", "pwsh") else "sh"
        if wait_loop(inner, depth + 1, inner_dialect):
            return True
    return False


def _bounded(prefix: str) -> bool:
    """`prefix` is the command before `bash -c`; inside `(…)` / `$(…)` only the text after the last `(` counts,
    and none of it may close a bracket (`X=$(timeout 5 cat f) bash -c …` is not bounded)."""
    prefix = prefix.rsplit("(", 1)[-1]
    if ")" in prefix:
        return False
    m = _BOUNDED.match(prefix)
    return m is not None and float(m.group("n")) > 0


def _mask_quotes(command: str, dialect: str = "sh") -> tuple[str, list[tuple[int, str]]]:
    """The command with every quoted string blanked out, and the strings: (where it starts, its text).
    sh escapes with `\\` (outside quotes and inside "…"); ps escapes with a backtick and doubles a quote
    inside a string: 'it''s', "say ""hi"" "."""
    esc = "`" if dialect == "ps" else "\\"
    out = list(command)
    strings: list[tuple[int, str]] = []
    i = 0
    while i < len(command):
        ch = command[i]
        if ch == esc:
            i += 2
            continue
        if ch not in "'\"":
            i += 1
            continue
        start, text = i, []
        i += 1
        while i < len(command):
            if command[i] == ch:
                if dialect == "ps" and command[i + 1 : i + 2] == ch:
                    i += 1  # '' or "" inside a PowerShell string
                else:
                    break
            elif ch == '"' and command[i] == esc and i + 1 < len(command):
                i += 1  # \" \\ \$ (sh) or `" `$ (ps) inside double quotes
            text.append(command[i])
            i += 1
        end = min(i + 1, len(command))
        out[start:end] = " " * (end - start)
        strings.append((start, "".join(text)))
        i = end
    return "".join(out), strings


def _sh_wait(masked: str) -> bool:
    """A `until/while … do … done` loop whose body (nested loops included) runs `sleep`. The body ends at the
    `done` that pairs with the loop's `do`, counting the do/done of the loops inside it."""
    for m in _BASH_LOOP.finditer(masked):
        level, body_start = 0, None
        for kw in _DO_DONE.finditer(masked, m.end()):
            if not _KEYWORD_BEFORE.search(masked, 0, kw.start()):
                continue  # `echo done`: an argument, not the keyword
            level += 1 if kw.group() == "do" else -1
            if level < 0:
                break
            if level == 1 and kw.group() == "do" and body_start is None:
                body_start = kw.start()  # from the `do` itself, so `do sleep` counts as sleep at a command start
            if level == 0 and body_start is not None:
                if _BASH_SLEEP.search(masked, body_start, kw.start()):
                    return True
                break
        else:
            # Never closed: bash would reject it, but judge what is there rather than let it through.
            if body_start is not None and _BASH_SLEEP.search(masked, body_start):
                return True
    return False


def _ps_wait(masked: str) -> bool:
    for m in _PS_WHILE.finditer(masked):
        cond_end = _closing(masked, m.end() - 1, "(", ")")
        brace = re.compile(r"\s*\{").match(masked, cond_end) if cond_end is not None else None
        if brace:
            end = _closing(masked, brace.end() - 1, "{", "}")
            if _PS_SLEEP.search(masked[brace.end():end]):
                return True
    for m in _PS_DO.finditer(masked):
        end = _closing(masked, m.end() - 1, "{", "}")
        if end is not None and _PS_DO_END.match(masked, end) and _PS_SLEEP.search(masked[m.end():end]):
            return True
    return False


def _closing(text: str, at: int, open_: str, close: str) -> int | None:
    """The index just after the bracket that closes the one at `at` (None when it is never closed)."""
    level = 0
    for i in range(at, len(text)):
        if text[i] == open_:
            level += 1
        elif text[i] == close:
            level -= 1
            if level == 0:
                return i + 1
    return None


def program_name(word: str) -> str:
    name = word.replace("\\", "/").rsplit("/", 1)[-1].lower()
    return name[:-4] if name.endswith(".exe") else name


def _after_wrapper(program: str, argv: list[str]) -> list[str]:
    rest = argv[1:]
    if program == "timeout":
        rest = [a for a in rest if not a.startswith("-")]
        return rest[1:]  # timeout DURATION cmd …
    if program == "nice":
        while rest and rest[0].startswith("-"):
            rest = rest[2:] if rest[0] == "-n" else rest[1:]
        return rest
    if program == "command":
        return rest[1:] if rest[:1] == ["-v"] else rest
    return rest


def _worst(a: tuple[Kind, str], b: tuple[Kind, str]) -> tuple[Kind, str]:
    return b if ORDER[b[0]] >= ORDER[a[0]] else a


def _within(target: Path, root: Path) -> bool:
    try:
        target.relative_to(root)
        return True
    except ValueError:
        return False
