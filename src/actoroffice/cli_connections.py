"""CLI for connections: `actoroffice connections list | test <id> | add <id> … | remove <id> | import-legacy`.

A secret never comes from the command line (it would stay in the shell history and in the process list): it is read
from a JSON file (`--secret-file`) or typed at a prompt that does not echo.
"""

from __future__ import annotations

import argparse
import getpass
import json
import sys
from pathlib import Path
from typing import TYPE_CHECKING, Any

from actoroffice.adapters import registry
from actoroffice.domain.errors import OfficeError
from actoroffice.ports.connection import ConnectionSpec, ConnectorError

if TYPE_CHECKING:
    from actoroffice.app import Office

HEALTH_VI = {"ok": "hoạt động", "degraded": "chập chờn", "error": "lỗi", "unconfigured": "chưa cấu hình"}
ADD_USAGE = (
    "Cách dùng: actoroffice connections add <id> --use <adapter> --config '<json>' [--scope office|team:<id>|project:<id>] [--write] "
    "[--secret-file <file json>]\n"
    'Ví dụ (Bitbucket Server): actoroffice connections add bitbucket-main --use bitbucket_server --config \'{"base_url": "https://git.congty.vn", '
    '"projects": ["PAY"]}\' --write --secret-file token.json   (token.json: {"token": "…"})'
)


def out(text: str = "") -> None:
    print(text, flush=True)


def _fail(message: str) -> None:
    out(f"error: {message}")
    raise SystemExit(1)


def read_secret_file(path: str) -> dict[str, str]:
    try:
        data = json.loads(Path(path).expanduser().read_text(encoding="utf-8-sig"))
    except OSError as exc:
        raise OfficeError(f"Không đọc được file bí mật {path}: {exc.strerror or exc}") from exc
    except ValueError as exc:
        raise OfficeError(f'File bí mật {path} phải là JSON, ví dụ {{"token": "…"}}') from exc
    if not isinstance(data, dict) or not all(isinstance(v, str) for v in data.values()):
        raise OfficeError(f'File bí mật {path} phải là một JSON object với giá trị là chuỗi, ví dụ {{"token": "…"}}')
    return {str(k): v for k, v in data.items() if v}


def prompt_secret(info: dict[str, Any]) -> dict[str, str] | None:
    """Ask for each secret field without echo. None when nobody is at the keyboard."""
    fields = info.get("secret_fields") or []
    if not fields or not sys.stdin.isatty():
        return None
    optional = set(info.get("optional_secret_fields") or [])
    secret = {}
    for field in fields:
        value = getpass.getpass(f"{field}{' (không bắt buộc, Enter để bỏ qua)' if field in optional else ''}: ").strip()
        if value:
            secret[field] = value
    return secret or None


async def _list(office: Office) -> None:
    rows = [(a, c) for a in await office.connections.overview() for c in a["connections"]]
    if not rows:
        out("Chưa có kết nối nào. Thêm: actoroffice connections add <id> --use <adapter> --config '<json>'")
        out("Adapter có sẵn: " + ", ".join(f"{name} ({registry.describe(name)['title']})" for name in registry.names() if name in registry.BUILTIN))
        return
    for adapter, c in sorted(rows, key=lambda row: row[1]["id"]):
        state = HEALTH_VI.get((c.get("health") or {}).get("state", ""), "?")
        flags = f"{'được ghi' if c['write'] else 'chỉ đọc'}{'' if c['enabled'] else ', đang tắt'}"
        out(f"{c['id']:20} {c['use']:18} {adapter['title']} · {c['scope']} · {flags} · {state}")


async def _test(office: Office, connection_id: str) -> None:
    if await office.connections.get(connection_id) is None:
        _fail(f"Không có kết nối '{connection_id}' (actoroffice connections list)")
    health = await office.connections.health(connection_id)
    ok = health.state == "ok"
    who = f" ({health.identity})" if health.identity else ""
    out(f"{'✓' if ok else '✗'} {connection_id}: {HEALTH_VI.get(health.state, health.state)}{who}{' — ' + health.detail if health.detail else ''}")
    if not ok:
        raise SystemExit(1)


async def _add(office: Office, args: argparse.Namespace) -> None:
    if not args.id or not args.use:
        _fail(ADD_USAGE)
    try:
        config = json.loads(args.config) if args.config else {}
    except ValueError:
        _fail("--config phải là JSON, ví dụ '{\"base_url\": \"https://git.congty.vn\", \"projects\": [\"PAY\"]}'")
    if not isinstance(config, dict):
        _fail("--config phải là một JSON object")
    try:
        info = registry.describe(args.use)
        secret = read_secret_file(args.secret_file) if args.secret_file else prompt_secret(info)
        spec = await office.connections.save(ConnectionSpec(id=args.id, use=args.use, scope=args.scope, config=config, write=args.write), secret=secret)
    except ConnectorError as exc:
        _fail(str(exc))
    out(f"Đã lưu kết nối {spec.id} ({info['title']}, phạm vi {spec.scope}, {'được ghi' if spec.write else 'chỉ đọc'}).")
    needed = [f for f in info["secret_fields"] if f not in info["optional_secret_fields"]]
    if secret is None and needed:
        out(f"Chưa có bí mật ({', '.join(needed)}): chạy lại với --secret-file <file json>, hoặc nhập ở Cài đặt › Kết nối.")
    else:
        out(f"Kiểm tra: actoroffice connections test {spec.id}")


async def _remove(office: Office, connection_id: str) -> None:
    if await office.connections.get(connection_id) is None:
        _fail(f"Không có kết nối '{connection_id}' (actoroffice connections list)")
    await office.connections.remove(connection_id)
    out(f"Đã gỡ kết nối {connection_id}. File bí mật của nó (secrets/) còn nguyên; xoá tay nếu không dùng nữa.")


async def _import_legacy(office: Office) -> None:
    try:
        created = await office.connections.import_legacy()
    except ConnectorError as exc:
        _fail(str(exc))
    for spec in created:
        out(f"Đã tạo kết nối {spec.id} ({spec.use}, phạm vi {spec.scope}, {'được ghi' if spec.write else 'chỉ đọc'}).")
    if not created:
        out("Không có gì để chuyển: chưa có cấu hình Jira / Bitbucket cũ, hoặc các kết nối jira-main, confluence-main, bitbucket-main đã có.")
    else:
        out("Kiểm tra từng kết nối ở Cài đặt › Kết nối (phạm vi dự án / space / repo, quyền ghi).")


async def cmd_connections(office: Office, args: argparse.Namespace) -> None:
    action = args.action
    if action == "list":
        await _list(office)
    elif action == "add":
        await _add(office, args)
    elif action == "import-legacy":
        await _import_legacy(office)
    elif not args.id:
        _fail(f"Cách dùng: actoroffice connections {action} <id>")
    elif action == "test":
        await _test(office, args.id)
    else:
        await _remove(office, args.id)


def register(sub: Any) -> None:
    p = sub.add_parser(
        "connections",
        help="accounts on outside systems: list | test <id> | add <id> --use <adapter> --config '<json>' [--scope …] [--write] [--secret-file f.json] | remove <id> | import-legacy",
    )
    p.add_argument("action", nargs="?", default="list", choices=["list", "test", "add", "remove", "import-legacy"])
    p.add_argument("id", nargs="?")
    p.add_argument("--use", help="adapter: bitbucket_server, bitbucket_cloud, github, jira_cloud, confluence_cloud, oracle_schema, http, mcp, command")
    p.add_argument("--config", help="JSON by the adapter's config (address, allowed projects / spaces / repositories…); never a secret")
    p.add_argument("--scope", default="office", help="office (default) | team:<id> | project:<id>")
    p.add_argument("--write", action="store_true", help="may comment, open pull requests, publish (read-only without it)")
    p.add_argument("--secret-file", help='a JSON file with the secret, e.g. {"token": "…"}; without it the secret is asked at a prompt')
    p.set_defaults(fn=cmd_connections)
