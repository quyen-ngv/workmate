"""Code intelligence on a real repository (this one by default): index it, ask the tools, and optionally let a real
Kiro CLI session answer a question with them (uses Kiro credits).

    uv run python scenarios/code_intel_live.py [--repo F:/path] [--home F:/_ao_ci] [--kiro] [--question "..."]
"""

from __future__ import annotations

import argparse
import asyncio
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from typing import Any

from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.run import TurnStatus
from actoroffice.domain.work import ProjectAccess
from actoroffice.runtimes.base import GateDecision, GateResult, TurnSpec
from actoroffice.runtimes.kiro_acp import KiroAcpRuntime
from actoroffice.tools.code_tools import CODE_TOOLS
from actoroffice.tools.registry import BoundTool, ToolCtx

QUESTION = (
    "Trong repo này hàm `sync_tx` làm gì, và những hàm nào gọi nó (trực tiếp)? "
    "Chỉ dùng các tool code_search, code_symbol, code_refs, code_outline, code_read của office (không dùng shell, không đọc file trực tiếp); tối đa 6 lần gọi tool. "
    "Trả lời ngắn bằng tiếng Việt, mỗi ý kèm một dòng `Nguồn: repo:path:dòng` lấy từ kết quả tool."
)


def say(text: str = "") -> None:
    print(text, flush=True)


def tool(name: str) -> Any:
    return next(t for t in CODE_TOOLS if t.name == name).handler


class ReadOnlyGate:
    """Office tools and reading are allowed; anything that writes or runs a command is refused."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def before_tool(self, name: str, args: dict[str, Any], tool_use_id: str | None) -> GateResult:
        self.calls.append((name, args))
        if self.allowed(name):
            return GateResult(GateDecision.ALLOW)
        return GateResult(GateDecision.DENY, "Chỉ được dùng các tool code_* của office trong bài thử này.")

    async def after_tool(self, name: str, args: dict[str, Any], result: Any) -> None:
        return None

    @staticmethod
    def allowed(name: str) -> bool:
        """Kiro names an office tool `@office/x` when it asks and the office server sees `mcp__office__x`."""
        return name.startswith(("mcp__office__", "@office/")) or name in ("Read", "Glob", "Grep", "TodoWrite", "Tool Load")


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo", default=str(Path(__file__).resolve().parent.parent))
    parser.add_argument("--home", default=None)
    parser.add_argument("--kiro", action="store_true", help="also run a real Kiro CLI turn that uses the tools")
    parser.add_argument("--question", default=QUESTION)
    parser.add_argument("--minutes", type=float, default=6)
    args = parser.parse_args()

    home = Path(args.home) if args.home else Path(tempfile.mkdtemp(prefix="ao_ci_"))
    office = Office(Settings(data_dir=home))
    await office.open()
    failures: list[str] = []
    try:
        project = await office.projects.create("actor-office", path=args.repo, access=ProjectAccess.READ_ONLY)
        ticket = await office.tickets.create("Thử code intelligence", project_id=project.id)
        ctx = ToolCtx(office, None, None, "run_live", ticket_id=ticket.id)  # type: ignore[arg-type]

        say(f"== 1. Lập chỉ mục {args.repo}")
        started = time.monotonic()
        repos = await office.codeintel.for_ticket(ticket)
        state = (await office.codeintel.ensure(repos))[0]
        first = time.monotonic() - started
        size = os.path.getsize(home / "index.db") / 1e6
        say(f"   {state.state} · mức {state.level} · {state.files} file · {state.symbols} ký hiệu · {state.edges} cạnh · commit {str(state.indexed_commit)[:10]}")
        say(f"   lần đầu {first:.1f}s · index.db {size:.1f} MB")
        started = time.monotonic()
        await office.codeintel.ensure(repos)
        say(f"   lần hai (không có gì đổi) {time.monotonic() - started:.2f}s")
        if state.state != "ready" or state.level != "graph" or state.symbols < 100:
            failures.append(f"index: {state}")

        checks = [
            ("code_search", {"query": "sync_tx", "limit": 3}, "def sync_tx"),
            ("code_symbol", {"name": "SourceService.refresh"}, "Nguồn: actor-office:src/actoroffice/services/sources.py:"),
            ("code_refs", {"symbol": "sync_tx", "direction": "callers"}, "actoroffice.services.tickets.TicketService"),
            ("code_outline", {"path": "src/actoroffice/domain/stage.py"}, "[class] class StageTemplate"),
            ("code_read", {"path": "src/actoroffice/domain/stage.py", "start": 17, "end": 21}, "class GatePolicy"),
            ("code_impact", {"targets": ["parse_refs"]}, "sync_tx"),
            ("code_pack", {"question": "gate approval roles fallback owner", "budget": 2500}, "Gói đọc ~"),
            ("service_map", {}, "Service (1): actor-office"),
        ]
        for i, (name, call_args, expect) in enumerate(checks, start=2):
            started = time.monotonic()
            out = await tool(name)(call_args, ctx)
            ok = expect in out
            say(f"\n== {i}. {name}({call_args})  [{time.monotonic() - started:.2f}s, ~{len(out) // 4} token] {'OK' if ok else 'SAI: thiếu ' + expect!r}")
            say("\n".join("   " + line for line in out.split("\n")[:14]) + ("\n   …" if out.count("\n") >= 14 else ""))
            if not ok:
                failures.append(name)

        whole = len((Path(args.repo) / "src/actoroffice/domain/stage.py").read_text(encoding="utf-8")) // 4
        outline = len(await tool("code_outline")({"path": "src/actoroffice/domain/stage.py"}, ctx)) // 4
        say(f"\n== Đọc khung thay cho cả file: stage.py ~{whole} token → khung ~{outline} token ({outline * 100 // whole}%)")

        if args.kiro:
            say("\n== Kiro CLI thật dùng các tool này")
            cwd = home / "kiro_ws"
            cwd.mkdir(exist_ok=True)
            gate = ReadOnlyGate()
            events: list[tuple[str, dict[str, Any]]] = []

            async def sink(kind: str, data: dict[str, Any]) -> None:
                events.append((kind, data))
                if kind == "tool_use":
                    say(f"   → {data.get('tool')} {str(data.get('input') or data.get('args') or '')[:110]}")

            spec = TurnSpec(
                run_id="run_live", prompt=args.question, cwd=cwd, system_prompt="Bạn là Copilot, kỹ sư AI của team. Trả lời ngắn, có dẫn nguồn.",
                model="kiro", tools=[], internal_tools=[BoundTool(t, ctx) for t in CODE_TOOLS], max_turns=20,
            )  # fmt: skip
            runtime = KiroAcpRuntime(cli_path=office.settings.kiro_cli_path, engine=office.settings.kiro_engine)
            started = time.monotonic()
            try:
                result = await asyncio.wait_for(runtime.run_turn(spec, gate, sink), timeout=args.minutes * 60)
            except TimeoutError:
                await runtime.cancel("run_live")
                failures.append("kiro: quá thời gian")
                result = None
            if result is not None:
                used = [n.removeprefix("mcp__office__") for n, _ in gate.calls if n.startswith("mcp__office__")]
                denied = [n for n, _ in gate.calls if not gate.allowed(n)]
                say(f"\n   trạng thái: {result.status} · {time.monotonic() - started:.0f}s · tool office đã gọi: {used} · bị từ chối: {denied}")
                say("   --- câu trả lời của Kiro ---")
                say("\n".join("   " + line for line in (result.text or "").strip().split("\n")))
                if result.status != TurnStatus.COMPLETED:
                    failures.append(f"kiro: {result.status} {result.error}")
                if not any(n.startswith("code_") for n in used):
                    failures.append("kiro: không gọi tool code_* nào")
                if "sync_tx" not in (result.text or "") or "Nguồn:" not in (result.text or ""):
                    failures.append("kiro: câu trả lời thiếu sync_tx hoặc dòng Nguồn:")
    finally:
        await office.close()
        if not args.home:
            shutil.rmtree(home, ignore_errors=True)
    say("\n== KẾT QUẢ: " + ("ĐẠT" if not failures else "KHÔNG ĐẠT: " + "; ".join(failures)))
    return 0 if not failures else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
