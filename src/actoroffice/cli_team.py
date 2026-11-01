"""CLI for team copilot: teams, people, tasks on stage templates, stage controls, Jira, environment check."""

from __future__ import annotations

import argparse
import platform
import shutil
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.domain.errors import OfficeError
from actoroffice.domain.stage import default_templates
from actoroffice.domain.team import LOCAL_OWNER_ID, ROLE_VI

if TYPE_CHECKING:
    from actoroffice.app import Office


def out(text: str = "") -> None:
    print(text, flush=True)


# ---- teams & people ---------------------------------------------------------------------------------------


async def cmd_team(office: Office, args: argparse.Namespace) -> None:
    teams = office.teams
    if args.action == "add":
        team = await teams.create_team(args.name, jira_project_key=args.jira, confluence_space=args.confluence)
        out(f"Team {team.name} ({team.id})" + (f" · Jira {team.jira_project_key}" if team.jira_project_key else ""))
    elif args.action == "member":
        team = await teams.get_team(args.name)
        user = await teams.find_user(args.user or "")
        if user is None:
            raise OfficeError(f"Không có người '{args.user}'")
        await teams.add_member(team.id, user.id, args.roles or [])
        out(f"{user.name} ∈ {team.name}: {', '.join(args.roles or []) or '(không vai)'}")
    else:
        for team in await teams.list_teams():
            out(f"{team.name}" + (f"  Jira {team.jira_project_key}" if team.jira_project_key else ""))
            for m in await teams.members(team.id):
                out(f"   - {m.user.name}: {', '.join(m.roles)}")


async def cmd_user(office: Office, args: argparse.Namespace) -> None:
    teams = office.teams
    if args.action == "add":
        user = await teams.create_user(args.name, roles=args.roles or [], telegram_id=args.telegram, email=args.email)
        out(f"{user.name} ({user.id}) roles={','.join(user.default_roles) or '-'} telegram={user.telegram_id or '-'}")
    else:
        for user in await teams.list_users():
            out(f"{user.id:16} {user.name:16} roles={','.join(user.default_roles) or '-':12} telegram={user.telegram_id or '-'} email={user.email or '-'}")


# ---- tasks & stages ---------------------------------------------------------------------------------------


async def cmd_task(office: Office, args: argparse.Namespace) -> None:
    team = await office.teams.get_team(args.team) if args.team else None
    owner = await office.teams.find_user(args.owner) if args.owner else None
    if args.owner and owner is None:
        raise OfficeError(f"Không có người '{args.owner}'")
    project = await office.projects.find(args.project) if args.project else None
    if args.project and project is None:
        raise OfficeError(f"Không có project '{args.project}' (actoroffice project list)")
    ticket = await office.tickets.create(
        args.title,
        description=args.description or args.title,
        created_by=LOCAL_OWNER_ID,
        project_id=project.id if project else None,
        model=args.model,
    )
    ticket = await office.stages.start(
        ticket.id, template=args.template, by=LOCAL_OWNER_ID, team_id=team.id if team else None, owner_user_id=owner.id if owner else None, mode=args.mode
    )
    out(f"{ticket.key} · {ticket.title} → giai đoạn {ticket.stage} ({args.template}). Chạy `actoroffice serve` hoặc `actoroffice run` để AI làm.")


async def cmd_model(office: Office, args: argparse.Namespace) -> None:
    """Show or change the model of a task / one stage; `--when turn|stage` waits for a boundary, never interrupts."""
    models = office.models
    if args.cancel:
        await models.cancel_switch(args.key, by=LOCAL_OWNER_ID)
    elif args.clear:
        if args.stage:
            await models.set_stage_model(args.key, args.stage, None, by=LOCAL_OWNER_ID)
        else:
            await models.set_task_model(args.key, None, by=LOCAL_OWNER_ID)
    elif args.model and args.when == "set":
        if args.stage:
            await models.set_stage_model(args.key, args.stage, args.model, by=LOCAL_OWNER_ID)
        else:
            await models.set_task_model(args.key, args.model, by=LOCAL_OWNER_ID)
    elif args.model:
        await models.request_switch(args.key, args.model, scope=args.when, by=LOCAL_OWNER_ID, stage=args.stage)
    info = await models.info(await office.tickets.get(args.key))
    out(f"{args.key} · model hiện tại: {info['current']} · task: {info['task'] or '-'}")
    for stage, model in info["stages"].items():
        out(f"  giai đoạn {stage}: {model}")
    if info["pending"]:
        p = info["pending"]
        out(f"  đang chờ: {p['model']} ({p['scope_label']}) bởi {p.get('by_name') or p['by']} lúc {p['at']}")


async def cmd_stage(office: Office, args: argparse.Namespace) -> None:
    stages = office.stages
    action = args.action or "status"
    if action == "pause":
        await stages.pause(args.key, by=LOCAL_OWNER_ID)
    elif action == "resume":
        await stages.resume(args.key, by=LOCAL_OWNER_ID)
    elif action == "retry":
        await stages.retry(args.key, by=LOCAL_OWNER_ID)
    elif action == "merged":
        await stages.external_passed(args.key, "merge", by=LOCAL_OWNER_ID, note=" ".join(args.text) or "PR đã merge")
    elif action == "steer":
        await stages.steer(args.key, " ".join(args.text), by=LOCAL_OWNER_ID)
    elif action == "reopen":
        target = args.text[0] if args.text else ""
        await stages.reopen(args.key, target, reason=" ".join(args.text[1:]) or "quay lại", by=LOCAL_OWNER_ID)
    st = await stages.status(args.key, viewer=LOCAL_OWNER_ID)
    out(f"{st['ticket']} · {st['title']}")
    out(f"  giai đoạn: {st['stage']} (lần {st['attempt']}) · {st['status_label']}" + (f" · chờ {', '.join(st['who'])}" if st["who"] else ""))
    for s in st["steps"]:
        out(f"   [{'x' if s['done'] else ' '}] {s['text']}")
    if st["run_id"]:
        last = await office.runs.last_activity(st["run_id"])
        if last:
            out(f"  đang: {last['title']}")
    for r in await stages.runs((await office.tickets.get(args.key)).id):
        tokens = sum(int(r.usage.get(k) or 0) for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"))
        out(f"  - {r.stage:10} #{r.attempt} {r.outcome or 'đang mở':10} {tokens / 1000:.1f}k tok")


async def cmd_jira(office: Office, args: argparse.Namespace) -> None:
    from actoroffice.services.jira import JiraClient, JiraMirror, save_credentials

    if args.action == "login":
        if not (args.base_url and args.email and args.token):
            raise OfficeError("Cách dùng: actoroffice jira login <https://x.atlassian.net> <email> <api token>")
        client = JiraClient(args.base_url, args.email, args.token)
        try:
            me = await client._call("GET", "/rest/api/3/myself")
        finally:
            await client.close()
        save_credentials(office.settings.data_dir, args.base_url, args.email, args.token)
        out(f"Đã lưu Jira ({me.get('displayName')}). Gắn project cho team: actoroffice team add <tên> --jira KEY")
    else:
        mirror = JiraMirror(office)
        if not mirror.ready():
            raise OfficeError("Chưa đăng nhập Jira: actoroffice jira login <url> <email> <token>")
        try:
            created = await mirror.sync()
        finally:
            await mirror.close()
        out(f"Đã đồng bộ: {len(created)} ticket mới" + (f" ({', '.join(created)})" if created else ""))


async def cmd_rules(office: Office, args: argparse.Namespace) -> None:
    rules = office.rules
    if args.action == "add":
        import json as _json

        spec = _json.loads(args.spec) if args.spec else {}
        rule = await rules.propose(
            title=args.title or "", body=args.body or "", kind=args.kind, scope=args.scope, stage=args.stage, spec=spec, by=LOCAL_OWNER_ID, activate=True
        )
        out(f"{rule.id} [{rule.kind.value} · {rule.scope} · {rule.status.value}] {rule.title}")
    elif args.action in ("approve", "disable"):
        rule = await (rules.approve if args.action == "approve" else rules.disable)(args.title or "", by=LOCAL_OWNER_ID)
        out(f"{rule.id}: {rule.status.value}")
    elif args.action == "import":
        project = next((p for p in await office.projects.list() if p.name == args.title or p.id == args.title), None)
        if project is None:
            raise OfficeError(f"Không có project '{args.title}'")
        for rule in await rules.import_conventions(project, by=LOCAL_OWNER_ID):
            out(f"{rule.id} ← {rule.source}")
    else:
        for rule in await rules.list(scope=args.scope if args.scope != "global" or args.only_global else None):
            out(f"{rule.id:6} {rule.status.value:8} {rule.kind.value:9} {rule.scope:24} {rule.stage:10} {rule.title}")


async def cmd_bitbucket(office: Office, args: argparse.Namespace) -> None:
    from actoroffice.services.atlassian import BitbucketCloud, save_bitbucket

    if not (args.email and args.token and args.workspace):
        raise OfficeError("Cách dùng: actoroffice bitbucket <email> <api token> <workspace>")
    client = BitbucketCloud(args.email, args.token, args.workspace)
    try:
        from actoroffice.services.atlassian import _call

        await _call(client.http, "GET", f"/workspaces/{args.workspace}")
    finally:
        await client.close()
    save_bitbucket(office.settings.data_dir, args.email, args.token, args.workspace)
    out(f"Đã lưu Bitbucket Cloud (workspace {args.workspace}).")


async def cmd_project(office: Office, args: argparse.Namespace) -> None:
    import json as _json

    from actoroffice.domain.work import ProjectAccess

    if args.action == "add":
        if not args.path:
            raise OfficeError("Cách dùng: actoroffice project add <tên> --path <thư mục repo> [--access worktree]")
        await office.projects.create(args.name, path=args.path, access=ProjectAccess(args.access))
    elif args.action == "list":
        for p in await office.projects.list():
            out(f"{p.name:16} {p.access.value:10} team={p.team_id or '-':18} {p.path or '-'}")
        return
    project = await office.projects.find(args.name)
    if project is None:
        raise OfficeError(f"Không có project '{args.name}'")
    team = await office.teams.get_team(args.team) if args.team else None
    profile = _json.loads(args.profile) if args.profile else None
    project = await office.projects.configure(
        project.id, team_id=team.id if team else None, kind=args.kind, repo_url=args.repo_url, profile=profile
    )
    out(f"{project.name}: team={project.team_id or '-'} kind={project.kind} repo={project.repo_url or '-'} profile={list(project.profile) or '-'}")


async def cmd_resource(office: Office, args: argparse.Namespace) -> None:
    import json as _json

    from actoroffice.services.access import save_secret

    team = await office.teams.get_team(args.team) if args.team else None
    if args.action == "add":
        config = _json.loads(args.config) if args.config else {}
        if args.secret_json:
            name = config.get("secret") or args.name
            save_secret(office.settings.data_dir, name, _json.loads(args.secret_json))
            config["secret"] = name
        resource = await office.access.add_resource(
            args.name, args.kind, team_id=team.id if team else None, env=args.env, config=config, max_level=args.max_level
        )
        out(f"{resource.name} [{resource.kind.value}, {resource.env}, max {resource.max_level.value}] → env {resource.env_name}_URL khi được cấp")
    else:
        for r in await office.access.resources_for(team.id if team else None):
            out(f"{r.name:20} {r.kind.value:6} {r.env:8} max={r.max_level.value:16} {r.config.get('base_url', '')}")


# ---- environment ------------------------------------------------------------------------------------------


def detect_environment() -> dict[str, Any]:
    """What this machine can run (shown by `doctor`, saved in config.yaml under `environment`)."""
    system = platform.system().lower()
    wsl = False
    if system == "linux":
        try:
            wsl = "microsoft" in Path("/proc/version").read_text(encoding="utf-8").lower()
        except OSError:
            wsl = False
    env: dict[str, Any] = {"os": "windows" if system == "windows" else ("macos" if system == "darwin" else "linux"), "wsl": wsl, "python": sys.version.split()[0]}
    for tool in ("git", "uv", "docker", "kiro-cli", "claude", "node", "java", "mvn", "gradle"):
        env[tool] = shutil.which(tool) is not None
    if env["os"] == "windows":
        env["bash"] = shutil.which("bash") is not None
        env["git_longpaths"] = _git_config("core.longpaths") == "true"
    try:
        import claude_agent_sdk  # noqa: F401

        bundled = list(Path(claude_agent_sdk.__file__).parent.rglob("claude*"))
        env["claude_sdk"] = True
        env["claude_bundled"] = any(p.is_file() and p.suffix in ("", ".exe") for p in bundled)
    except ImportError:
        env["claude_sdk"] = False
    if env["docker"]:
        env["docker_running"] = _ok(["docker", "info", "--format", "{{.ServerVersion}}"])
    if wsl:
        env["data_on_windows_drive"] = str(Path.home()).startswith("/mnt/")
    return env


def _git_config(key: str) -> str:
    try:
        return subprocess.run(["git", "config", "--get", key], capture_output=True, text=True, timeout=5).stdout.strip()
    except (OSError, subprocess.TimeoutExpired):
        return ""


def _ok(argv: list[str]) -> bool:
    try:
        return subprocess.run(argv, capture_output=True, timeout=10).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def advice(env: dict[str, Any]) -> list[str]:
    tips = []
    if not env.get("git"):
        tips.append("Cần cài git.")
    if env.get("os") == "windows" and not env.get("bash"):
        tips.append("Windows: cài Git for Windows để AI có bash (Bash tool).")
    if env.get("os") == "windows" and not env.get("git_longpaths"):
        tips.append("Windows: bật đường dẫn dài cho git: git config --global core.longpaths true")
    if env.get("wsl") and env.get("data_on_windows_drive"):
        tips.append("WSL: đặt thư mục dữ liệu và repo trong FS Linux (~), không dưới /mnt/c (chậm, SQLite WAL không ổn định).")
    if not env.get("claude_sdk"):
        tips.append("Thiếu claude-agent-sdk: uv sync.")
    if not env.get("kiro-cli"):
        tips.append("Chưa có kiro-cli (chỉ cần khi team chạy model 'kiro').")
    if env.get("docker") and not env.get("docker_running"):
        tips.append("Docker đã cài nhưng chưa chạy (cần cho test tích hợp dùng compose/Testcontainers).")
    if not env.get("docker"):
        tips.append("Chưa có Docker (chỉ cần khi project dùng compose/Testcontainers).")
    return tips


async def cmd_doctor(office: Office, _args: argparse.Namespace) -> None:
    env = detect_environment()
    office.settings.environment = env
    save_environment(office, env)
    for key, value in env.items():
        out(f"  {key:22} {value}")
    tips = advice(env)
    if tips:
        out("")
        for tip in tips:
            out(f"• {tip}")
    else:
        out("\nMôi trường đủ để chạy.")


def save_environment(office: Office, env: dict[str, Any]) -> None:
    import yaml

    path = office.settings.config_path
    if not path.exists():
        return
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    data["environment"] = env
    path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")


# ---- parser -----------------------------------------------------------------------------------------------


def register(sub: Any) -> None:
    roles = ", ".join(ROLE_VI)
    p = sub.add_parser("team", help="teams: list | add <name> [--jira KEY] | member <team> <user> <roles…>")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "member"])
    p.add_argument("name", nargs="?")
    p.add_argument("user", nargs="?")
    p.add_argument("roles", nargs="*", help=f"roles: {roles}")
    p.add_argument("--jira")
    p.add_argument("--confluence")
    p.set_defaults(fn=cmd_team)

    p = sub.add_parser("user", help="people: list | add <name> --roles ba dev --telegram ID --email x")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add"])
    p.add_argument("name", nargs="?")
    p.add_argument("--roles", nargs="*")
    p.add_argument("--telegram", type=int)
    p.add_argument("--email")
    p.set_defaults(fn=cmd_user)

    p = sub.add_parser("task", help="create a ticket for the AI and start its stages")
    p.add_argument("title")
    p.add_argument("-d", "--description")
    p.add_argument("--template", default="code", choices=list(default_templates()))
    p.add_argument("--team")
    p.add_argument("--owner")
    p.add_argument("--project", help="project (repo) the AI works in")
    p.add_argument("--mode", choices=["wait", "assume"])
    p.add_argument("--model", help="haiku | sonnet | opus | kiro | kiro:<id> for this task (default: workspace settings)")
    p.set_defaults(fn=cmd_task)

    p = sub.add_parser("model", help="model of a task: model <key> [<model>] [--stage S] [--when turn|stage|set] [--clear] [--cancel]")
    p.add_argument("key")
    p.add_argument("model", nargs="?")
    p.add_argument("--stage", help="only this stage (default: the whole task)")
    p.add_argument("--when", choices=["turn", "stage", "set"], default="turn", help="turn = next run · stage = next stage · set = store now")
    p.add_argument("--clear", action="store_true", help="remove the task/stage model")
    p.add_argument("--cancel", action="store_true", help="cancel the switch that is waiting")
    p.set_defaults(fn=cmd_model)

    p = sub.add_parser("stage", help="stage status / control: stage <key> [pause|resume|retry|merged|steer|reopen] [text]")
    p.add_argument("key")
    p.add_argument("action", nargs="?", choices=["status", "pause", "resume", "retry", "merged", "steer", "reopen"])
    p.add_argument("text", nargs="*")
    p.set_defaults(fn=cmd_stage)

    p = sub.add_parser("jira", help="jira login <url> <email> <token> | jira sync")
    p.add_argument("action", choices=["login", "sync"])
    p.add_argument("base_url", nargs="?")
    p.add_argument("email", nargs="?")
    p.add_argument("token", nargs="?")
    p.set_defaults(fn=cmd_jira)

    p = sub.add_parser("rules", help="rules: list | add --kind guideline --scope team:<id> --title … --body … | approve R-1 | disable R-1 | import <project>")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add", "approve", "disable", "import"])
    p.add_argument("title", nargs="?", help="rule title (add), rule id (approve/disable) or project (import)")
    p.add_argument("--body")
    p.add_argument("--kind", default="guideline", choices=["guideline", "check", "policy", "gate"])
    p.add_argument("--scope", default="global")
    p.add_argument("--stage", default="any")
    p.add_argument("--spec", help='JSON, e.g. {"cmd": ["mvn", "-q", "verify"]} or {"deny_patterns": ["--force"]}')
    p.add_argument("--only-global", action="store_true")
    p.set_defaults(fn=cmd_rules)

    p = sub.add_parser("bitbucket", help="connect Bitbucket Cloud: bitbucket <email> <api token> <workspace>")
    p.add_argument("email", nargs="?")
    p.add_argument("token", nargs="?")
    p.add_argument("workspace", nargs="?")
    p.set_defaults(fn=cmd_bitbucket)

    p = sub.add_parser(
        "project",
        help='project add <name> --path <repo> | list | set <name> --team T --kind code|reference --repo-url ws/slug '
        "--profile '{\"test\": [\"mvn\",\"-q\",\"test\"]}'",
    )
    p.add_argument("action", choices=["add", "set", "list"])
    p.add_argument("name", nargs="?", default="")
    p.add_argument("--path", help="repo folder (add)")
    p.add_argument("--access", default="worktree", choices=["worktree", "read_only", "direct"])
    p.add_argument("--team")
    p.add_argument("--kind", choices=["code", "reference", "automation"])
    p.add_argument("--repo-url")
    p.add_argument("--profile", help="JSON: setup/build/test/lint/run argv lists, test_glob, os overrides")
    p.set_defaults(fn=cmd_project)

    p = sub.add_parser("resource", help="shared environments: list | add <name> --kind http --config '{\"base_url\": …}' --secret-json '{\"headers\": {…}}'")
    p.add_argument("action", nargs="?", default="list", choices=["list", "add"])
    p.add_argument("name", nargs="?")
    p.add_argument("--kind", default="http", choices=["http", "db", "kafka", "mq", "redis", "logs"])
    p.add_argument("--team")
    p.add_argument("--env", default="dev")
    p.add_argument("--max-level", default="read", choices=["read", "write-sandboxed", "write-shared"])
    p.add_argument("--config", help="JSON (base_url, sandbox_paths, …)")
    p.add_argument("--secret-json", help="JSON stored in secrets/resources.json (headers / dsn), never shown to the AI")
    p.set_defaults(fn=cmd_resource)

    sub.add_parser("doctor", help="check what this machine can run (OS, WSL, git, bash, docker, kiro, claude)").set_defaults(fn=cmd_doctor)

