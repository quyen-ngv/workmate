"""Command line: `uv run actoroffice <command>` (or `uv run py -m actoroffice …`)."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import logging
import os
import sys
from collections.abc import Awaitable, Callable
from pathlib import Path
from typing import Any

from actoroffice import cli_team
from actoroffice.app import Office
from actoroffice.config import load_settings, write_default_config
from actoroffice.core.events import Event
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.work import InteractionKind, Priority, TicketStatus
from actoroffice.engine.reaper import reap_orphans

Command = Callable[[Office, argparse.Namespace], Awaitable[None]]


def out(text: str = "") -> None:
    print(text, flush=True)


# ---- setup -----------------------------------------------------------------------------------


async def cmd_init(office: Office, _args: argparse.Namespace) -> None:
    from actoroffice.cli_team import advice, detect_environment, save_environment

    write_default_config(office.settings)
    created = await office.install_default_positions()
    ai = await office.ensure_ai()
    out(f"Office ready at {office.settings.data_dir}")
    out("Positions: " + ", ".join(p.name for p in await office.org.list_positions()) + (f" (new: {len(created)})" if created else ""))
    out(f"AI engineer: {ai.name}")
    env = detect_environment()
    save_environment(office, env)
    for tip in advice(env):
        out(f"• {tip}")


async def cmd_positions(office: Office, _args: argparse.Namespace) -> None:
    for p in await office.org.list_positions():
        out(f"{p.name:10} {p.title:24} model={p.model:7} workspace={p.workspace_kind.value}")


async def cmd_hire(office: Office, args: argparse.Namespace) -> None:
    position = await office.org.find_position(args.position)
    if position is None:
        raise OfficeError(f"No position '{args.position}'. See `positions`.")
    actor = await office.org.hire(position.id, args.name, emoji=args.emoji or "", specialty=args.specialty or "", model=args.model)
    out(f"Hired {actor.emoji} {actor.name} as {position.title}")


async def cmd_actors(office: Office, _args: argparse.Namespace) -> None:
    positions = {p.id: p for p in await office.org.list_positions()}
    for a in await office.org.list_actors():
        out(f"{a.emoji or '·'} {a.name:12} {positions[a.position_id].title:24} {a.specialty} [{a.status.value}]")


# ---- tickets ---------------------------------------------------------------------------------


async def _actor_id(office: Office, name: str | None) -> str | None:
    if not name:
        return None
    actor = await office.org.find_actor(name)
    if actor is None:
        raise OfficeError(f"No actor named {name}")
    return actor.id


async def cmd_ticket(office: Office, args: argparse.Namespace) -> None:
    ticket = await office.tickets.create(
        args.title,
        description=args.description or "",
        type=args.type,
        priority=Priority(args.priority),
        assignee_actor_id=await _actor_id(office, args.assign),
    )
    out(f"Created {ticket.key}: {ticket.title} ({ticket.status.value})")


async def cmd_tickets(office: Office, args: argparse.Namespace) -> None:
    statuses = None if args.all else [s for s in TicketStatus if s not in (TicketStatus.DONE, TicketStatus.CANCELLED)]
    names = {a.id: a.name for a in await office.org.list_actors()}
    for t in await office.tickets.list(status=statuses):
        who = names.get(t.assignee_actor_id or "", "user" if t.assignee_user else "-")
        out(f"{t.key:8} {t.status.value:12} {t.priority.value:8} {who:10} {t.title}")


async def cmd_show(office: Office, args: argparse.Namespace) -> None:
    t = await office.tickets.get(args.key)
    names = {a.id: a.name for a in await office.org.list_actors()}
    out(f"{t.key} — {t.title}\nstatus={t.status.value} priority={t.priority.value} assignee={names.get(t.assignee_actor_id or '', '-')}")
    if t.description:
        out(f"\n{t.description}")
    for d in await office.tickets.list_documents(t.id):
        out(f"\n--- document {d.key} (rev {d.revision}, {d.updated_by}) ---\n{d.body}")
    out("\n--- activity ---")
    for c in await office.tickets.comments(t.id, limit=500):
        out(f"[{c.created_at[11:19]}] {c.author_id or c.author_kind}: {c.body}")


async def cmd_comment(office: Office, args: argparse.Namespace) -> None:
    c = await office.tickets.add_comment(args.key, args.text, author_kind="user", author_id="user")
    out(f"Comment #{c.id} added")


async def cmd_assign(office: Office, args: argparse.Namespace) -> None:
    t = await office.tickets.assign(args.key, await _actor_id(office, args.actor), by="user")
    out(f"{t.key} assigned")


# ---- human in the loop -----------------------------------------------------------------------


async def cmd_inbox(office: Office, _args: argparse.Namespace) -> None:
    items = await office.interactions.pending()
    if not items:
        out("Nothing waiting for you.")
    for item in items:
        out(_describe_interaction(item.model_dump()))


async def cmd_answer(office: Office, args: argparse.Namespace) -> None:
    await _answer(office, args.id, " ".join(args.reply))


async def _answer(office: Office, interaction_id: str, reply: str) -> None:
    item = await office.interactions.get(interaction_id)
    yes = reply.strip().lower() in ("y", "yes", "ok", "approve", "co", "có", "đồng ý")
    if item.kind == InteractionKind.APPROVE_TOOL:
        await office.answer(item.id, {"approved": yes})
        out(f"{'Approved' if yes else 'Rejected'} {item.id}")
    elif item.kind == InteractionKind.CONFIRM_TICKET:
        resolved = await office.answer(item.id, {"confirmed": yes})
        out(f"Created {resolved.response.get('ticket_key')}" if yes and resolved.response else f"Dropped {item.id}")
    elif item.kind == InteractionKind.PROPOSE_TICKETS:
        resolved = await office.answer(item.id, {"approved": yes})
        created = (resolved.response or {}).get("created") or []
        out(f"Created {', '.join(created)}" if created else f"Dropped {item.id}")
    else:
        options = item.payload.get("options") or []
        answer = options[int(reply) - 1] if reply.isdigit() and 0 < int(reply) <= len(options) else reply
        await office.answer(item.id, {"answer": answer})
        out(f"Answered {item.id}: {answer}")


def _describe_interaction(data: dict[str, Any]) -> str:
    p = data["payload"]
    if data["kind"] == InteractionKind.APPROVE_TOOL.value:
        return f"⚠ [{data['id']}] {p.get('actor')} · {p.get('ticket')} wants {p['tool']}: {p.get('target')}  ({p.get('reason')})"
    if data["kind"] == InteractionKind.CONFIRM_TICKET.value:
        return f"+ [{data['id']}] {p.get('proposed_by')} proposes ticket '{p.get('title')}' → {p.get('assignee')}  (y/n)"
    if data["kind"] == InteractionKind.PROPOSE_TICKETS.value:
        titles = "; ".join(f"{i.get('n')}. {i.get('title')}" for i in p.get("items") or [])
        return f"+ [{data['id']}] {p.get('proposed_by')} proposes {len(p.get('items') or [])} tickets: {titles}  (y/n)"
    options = "  ".join(f"{i}) {o}" for i, o in enumerate(p.get("options") or [], 1))
    return f"? [{data['id']}] {p.get('asked_by')} · asks: {p.get('question')}  {options}"


# ---- running ---------------------------------------------------------------------------------


async def cmd_run(office: Office, args: argparse.Namespace) -> None:
    """Run the office in this terminal. Answer with: y <id> | n <id> | a <id> <text> | q"""
    await reap_orphans(office)
    await office.start_notifier()
    names = {a.id: f"{a.emoji} {a.name}".strip() for a in await office.org.list_actors()}
    run_actor: dict[str, str] = {}

    async def printer() -> None:
        async with office.bus.subscribe() as queue:
            while True:
                _print_event(await queue.get(), names, run_actor, verbose=args.verbose)

    async def commands() -> None:
        while True:
            line = (await asyncio.to_thread(sys.stdin.readline)).strip()
            if not line:
                continue
            if line == "q":
                raise KeyboardInterrupt
            verb, _, rest = line.partition(" ")
            ident, _, text = rest.partition(" ")
            try:
                if verb in ("y", "n"):
                    await _answer(office, ident, "yes" if verb == "y" else "no")
                elif verb == "a":
                    await _answer(office, ident, text)
                elif verb == "inbox":
                    await cmd_inbox(office, args)
                else:
                    out("commands: y <id> | n <id> | a <id> <answer> | inbox | q")
            except OfficeError as exc:
                out(f"! {exc}")

    tasks = [asyncio.create_task(printer()), asyncio.create_task(commands())]
    out("Office running. Commands: y <id> | n <id> | a <id> <answer> | inbox | q")
    try:
        if args.once:
            await office.scheduler.wait_idle()
            pending = await office.interactions.pending()
            out(f"Idle. {len(pending)} item(s) waiting for you." if pending else "Idle.")
        else:
            await office.scheduler.run_forever()
    finally:
        for t in tasks:
            t.cancel()
        await office.scheduler.shutdown()


def _print_event(event: Event, names: dict[str, str], run_actor: dict[str, str], *, verbose: bool) -> None:
    d = event.data
    if event.kind == "run.started":
        run = d["run"]
        run_actor[run["id"]] = names.get(run["actor_id"], run["actor_id"])
        out(f"▶ {run_actor[run['id']]} starts {run['kind']} run {run['id']} (ticket #{run['ticket_id']})")
    elif event.kind == "run.event":
        who = run_actor.get(d["run_id"], "?")
        payload = d["payload"]
        if d["kind"] == "text" and not payload.get("subagent"):
            out(f"{who}: {payload['text'].strip()[:600]}")
        elif d["kind"] == "tool_use" and verbose:
            out(f"  · {who} uses {payload['tool']}")
        elif d["kind"] == "turn":
            u = payload["usage"]
            out(f"  · turn {payload['status']} in={u['input_tokens']} out={u['output_tokens']} cache_read={u['cache_read_tokens']} cost≈${payload['cost_usd']:.4f}")
        elif d["kind"] == "rate_limit" and payload.get("status") != "allowed":
            out(f"  ! usage limit {payload.get('status')} ({payload.get('kind')})")
    elif event.kind == "run.finished":
        out(f"■ run {d['run_id']} {d['status']}" + (f": {d['error']}" if d.get("error") else ""))
    elif event.kind == "interaction.created":
        out(_describe_interaction(d["interaction"]))
    elif event.kind == "ticket.updated":
        t = d["ticket"]
        out(f"  ≡ {t['key']} → {t['status']}")
    elif event.kind == "ticket.created":
        t = d["ticket"]
        out(f"  + {t['key']} created: {t['title']}")


async def cmd_serve(office: Office, args: argparse.Namespace) -> None:
    """Run the office with the web UI (scheduler + notifier + HTTP in one process)."""
    import webbrowser

    import uvicorn

    from actoroffice.web.server import create_app, load_or_create_token

    settings = office.settings
    write_default_config(settings)
    # Add positions and standard skills that are new in this version; existing ones are never overwritten
    # ("Cập nhật vị trí mẫu" in the UI does that on request).
    added = await office.install_default_positions()
    if added:
        out("Vị trí mới: " + ", ".join(p.title for p in added))
    if args.token_env:
        # a temporary office (UI screenshots, SI-23): the token comes from the caller and is never written to disk
        token = os.environ.get(args.token_env, "").strip()
        if len(token) < 32:
            raise OfficeError(f"Biến môi trường {args.token_env} cần chứa token dài ít nhất 32 ký tự")
    else:
        token = load_or_create_token(settings)
    await office.ensure_ai()  # the AI engineer exists from the first start (no separate init needed)
    await office.start(schedule=not args.view_only)  # --view-only: look around without the AI running
    if args.view_only:
        out("Chế độ chỉ xem: AI không chạy việc nào.")
    from actoroffice.channels.telegram import load_token

    bot_token = load_token(settings)
    if bot_token:
        await office.start_telegram(bot_token)
        out("Telegram: đang chạy" + ("" if office.telegram.chat_id else " — thêm bot vào group có bật Topics rồi nhắn /start"))
    host, port = args.host or settings.web_host, args.port or settings.web_port
    server = uvicorn.Server(uvicorn.Config(create_app(office, token), host=host, port=port, log_level="warning", ws_ping_interval=20))
    url = f"http://{'localhost' if host in ('127.0.0.1', '0.0.0.0') else host}:{port}/login?t={token}&next=/team"
    out(f"Văn phòng AI đang chạy: {url.replace(token, f'${args.token_env}') if args.token_env else url}")
    out("(link chứa mã đăng nhập — đừng chia sẻ; Ctrl+C để dừng)")
    if not args.no_browser:
        webbrowser.open(url)
    await server.serve()


async def cmd_telegram(office: Office, args: argparse.Namespace) -> None:
    """Save, check or remove the Telegram bot token (secrets/telegram_token)."""
    from actoroffice.channels.telegram import TOKEN_RE, BotApi, TelegramError, load_token, token_path

    path = token_path(office.settings)
    if args.off:
        path.unlink(missing_ok=True)
        out("Đã tắt Telegram (xoá token). Khởi động lại `actoroffice serve` để áp dụng.")
        return
    token = (args.token or "").strip() or load_token(office.settings)
    if not token:
        out("Chưa có token. Tạo bot với @BotFather rồi chạy: actoroffice telegram <token>")
        return
    if not TOKEN_RE.match(token):
        raise OfficeError("Token không đúng dạng 123456789:AA… (lấy từ @BotFather)")
    api = BotApi(token)
    try:
        me = await api.call("getMe")
    except TelegramError as exc:
        raise OfficeError(f"Telegram từ chối token: {exc}") from None
    finally:
        await api.close()
    if args.token:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(token, encoding="utf-8")
        out(f"Đã lưu token của bot @{me.get('username')}.")
    else:
        out(f"Bot @{me.get('username')} hoạt động.")
    row = await office.db.fetchone("SELECT value FROM telegram_state WHERE key = 'chat_id'")
    if row:
        out(f"Đang gắn với group {row['value']}.")
    else:
        out("Bước tiếp: tạo group Telegram, bật Topics, thêm bot làm admin (quyền quản lý topic), "
            "chạy `actoroffice serve` rồi nhắn /start trong group.")
    if not me.get("can_read_all_group_messages"):
        out("Lưu ý: bot phải là admin của group để đọc được mọi tin nhắn (hoặc tắt privacy mode bằng /setprivacy ở @BotFather).")


async def cmd_pack(office: Office, args: argparse.Namespace) -> None:
    """Check a standard pack before using it: is it valid, and what would it say about documents that already exist."""
    from actoroffice.doccore import PackError, load_default, load_pack, parse, run_rules

    try:
        pack = load_pack(Path(args.path)) if args.path else load_default()
    except PackError as exc:
        out(f"Pack không hợp lệ ({len(exc.problems)} lỗi):")
        for problem in exc.problems:
            out(f"  - {problem}")
        raise SystemExit(1) from None
    out(f"Pack '{pack.name}' v{pack.version} hợp lệ: {len(pack.doc_types)} loại tài liệu, {len(pack.item_kinds)} loại mục, {len(pack.rules)} luật, "
        f"luồng: {', '.join(pack.flows)}.")
    if not args.against:
        return
    ticket = await office.tickets.get(args.against)
    ds = await office.docs.docset(ticket)
    docs = {}
    for key, (ticket_id, stored_key) in ds.stored.items():
        doctype = stored_key.split(".", 1)[0]
        doc = await office.tickets.get_document(ticket_id, stored_key)
        if doc is not None and doctype in pack.doc_types:
            docs[key] = parse(doc.body, doctype, pack)
    findings = run_rules(pack, docs, None, None, None, only_docs={k for k in ds.own if k in docs})
    errors = [f for f in findings if f.severity == "error"]
    out(f"Trên tài liệu của {ticket.key} ({len(docs)} tài liệu): {len(errors)} lỗi, {len(findings) - len(errors)} cảnh báo "
        "(chưa kiểm nguồn và baseline: hai thứ đó không phụ thuộc pack).")
    for f in findings:
        out(f"  - [{'LỖI' if f.severity == 'error' else 'cảnh báo'}] {f.rule} · {f.doc}{' · ' + f.item if f.item else ''}: {f.message}")
    if errors:
        raise SystemExit(2)


# ---- entry -----------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="actoroffice", description="An office of AI actors working through tickets.")
    parser.add_argument("--home", help="data directory (default ~/.actoroffice or $ACTOROFFICE_HOME)")
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init", help="create the data dir, config and default positions").set_defaults(fn=cmd_init)
    sub.add_parser("positions").set_defaults(fn=cmd_positions)
    sub.add_parser("actors").set_defaults(fn=cmd_actors)

    p = sub.add_parser("pack", help="check a document standard pack (and what it would say about a ticket's documents)")
    p.add_argument("path", nargs="?", help="folder with pack.yaml (default: the built-in pack)")
    p.add_argument("--against", help="a ticket key: run the pack's rules on its documents")
    p.set_defaults(fn=cmd_pack)

    p = sub.add_parser("hire", help="hire an actor into a position")
    p.add_argument("position")
    p.add_argument("name")
    p.add_argument("--specialty")
    p.add_argument("--emoji")
    p.add_argument("--model")
    p.set_defaults(fn=cmd_hire)

    p = sub.add_parser("ticket", help="create a ticket")
    p.add_argument("title")
    p.add_argument("-d", "--description")
    p.add_argument("-a", "--assign")
    p.add_argument("--type", default="task")
    p.add_argument("--priority", default="medium", choices=[x.value for x in Priority])
    p.set_defaults(fn=cmd_ticket)

    p = sub.add_parser("tickets")
    p.add_argument("--all", action="store_true")
    p.set_defaults(fn=cmd_tickets)

    p = sub.add_parser("show")
    p.add_argument("key")
    p.set_defaults(fn=cmd_show)

    p = sub.add_parser("comment")
    p.add_argument("key")
    p.add_argument("text")
    p.set_defaults(fn=cmd_comment)

    p = sub.add_parser("assign")
    p.add_argument("key")
    p.add_argument("actor")
    p.set_defaults(fn=cmd_assign)

    sub.add_parser("inbox", help="what is waiting for you").set_defaults(fn=cmd_inbox)

    p = sub.add_parser("answer")
    p.add_argument("id")
    p.add_argument("reply", nargs="+")
    p.set_defaults(fn=cmd_answer)

    p = sub.add_parser("run", help="run the office in this terminal")
    p.add_argument("--once", action="store_true", help="exit when there is nothing left to do")
    p.add_argument("-v", "--verbose", action="store_true")
    p.set_defaults(fn=cmd_run)

    p = sub.add_parser("telegram", help="connect a Telegram bot: actoroffice telegram <token from @BotFather>")
    p.add_argument("token", nargs="?")
    p.add_argument("--off", action="store_true", help="remove the token")
    p.set_defaults(fn=cmd_telegram)

    cli_team.register(sub)

    p = sub.add_parser("serve", help="run the office with the web UI")
    p.add_argument("--host")
    p.add_argument("--port", type=int)
    p.add_argument("--no-browser", action="store_true")
    p.add_argument("--view-only", action="store_true", help="web UI only: the scheduler does not start, the AI runs nothing")
    p.add_argument("--token-env", metavar="NAME", help="take the sign-in token from this environment variable (not saved to disk)")
    p.set_defaults(fn=cmd_serve)
    return parser


async def _main(args: argparse.Namespace) -> int:
    from pathlib import Path

    office = Office(load_settings(Path(args.home) if args.home else None))
    await office.open()
    try:
        await args.fn(office, args)
        return 0
    except OfficeError as exc:
        out(f"error: {exc}")
        return 1
    finally:
        await office.close()


def main() -> None:
    with contextlib.suppress(AttributeError):
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[union-attr]
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    args = build_parser().parse_args()
    try:
        sys.exit(asyncio.run(_main(args)))
    except KeyboardInterrupt:
        sys.exit(130)


if __name__ == "__main__":
    main()
