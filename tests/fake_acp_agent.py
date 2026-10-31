"""A scripted ACP agent standing in for `kiro-cli acp` in tests (JSON-RPC over stdio, one message per line).

On session/prompt it behaves like a coding agent: narrates, thinks, reads a file through the client, asks
permission for a shell command and runs it in a client terminal, writes a file, calls an office tool over the
MCP server the client passed, and tries a write outside the workspace. With "DEFER" in the prompt it stops
after a permission request the client refuses.
"""

from __future__ import annotations

import json
import subprocess
import sys

_next = 0
_mcp: subprocess.Popen | None = None


def send(message: dict) -> None:
    sys.stdout.write(json.dumps(message, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def read() -> dict:
    line = sys.stdin.readline()
    if not line:
        sys.exit(0)
    return json.loads(line)


def request(method: str, params: dict) -> dict:
    """Ask the client and wait for its answer (requests from the client meanwhile are not expected)."""
    global _next
    _next += 1
    send({"jsonrpc": "2.0", "id": f"a{_next}", "method": method, "params": params})
    while True:
        message = read()
        if message.get("id") == f"a{_next}" and "method" not in message:
            return message
        if message.get("method") == "session/cancel":
            raise SystemExit("cancelled")


def update(session: str, body: dict) -> None:
    send({"jsonrpc": "2.0", "method": "session/update", "params": {"sessionId": session, "update": body}})


def mcp(server: dict, method: str, params: dict, msg_id: int) -> dict:
    global _mcp
    if _mcp is None:
        env = dict(__import__("os").environ, **{e["name"]: e["value"] for e in server["env"]})
        _mcp = subprocess.Popen([server["command"], *server["args"]], stdin=subprocess.PIPE, stdout=subprocess.PIPE, env=env)
    assert _mcp.stdin and _mcp.stdout
    _mcp.stdin.write((json.dumps({"jsonrpc": "2.0", "id": msg_id, "method": method, "params": params}) + "\n").encode())
    _mcp.stdin.flush()
    return json.loads(_mcp.stdout.readline())


def prompt(params: dict, sessions: dict) -> dict:
    session = params["sessionId"]
    state = sessions[session]
    text = params["prompt"][0]["text"]
    cwd = state["cwd"]
    sep = "\\" if "\\" in cwd else "/"
    update(session, {"sessionUpdate": "agent_thought_chunk", "content": {"type": "text", "text": "Cần đọc notes trước."}})
    update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "Kế hoạch: "}})
    update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "đọc, chạy, ghi."}})
    update(session, {"sessionUpdate": "plan", "entries": [{"content": "đọc notes", "status": "in_progress"}, {"content": "ghi out", "status": "pending"}]})

    if "DEFER" in text:
        update(session, {"sessionUpdate": "tool_call", "toolCallId": "t9", "title": "rm -rf build", "kind": "execute", "rawInput": {"command": "rm -rf build"}})
        answer = request("session/request_permission", {
            "sessionId": session,
            "toolCall": {"toolCallId": "t9", "title": "rm -rf build", "kind": "execute", "rawInput": {"command": "rm -rf build"}},
            "options": [{"optionId": "ok", "name": "Allow", "kind": "allow_once"}, {"optionId": "no", "name": "Reject", "kind": "reject_once"}],
        })
        state["last_permission"] = answer["result"]["outcome"]
        message = read()  # the client cancels the turn
        assert message.get("method") == "session/cancel", message
        return {"stopReason": "cancelled"}

    update(session, {"sessionUpdate": "tool_call", "toolCallId": "t1", "title": "Read notes.md", "kind": "read", "rawInput": {"path": cwd + sep + "notes.md"}})
    notes = request("fs/read_text_file", {"sessionId": session, "path": cwd + sep + "notes.md"})["result"]["content"]
    update(session, {"sessionUpdate": "tool_call_update", "toolCallId": "t1", "status": "completed"})

    cmd = [sys.executable, "-c", "print('xin chao')"]
    shown = " ".join(cmd)
    update(session, {"sessionUpdate": "tool_call", "toolCallId": "t2", "title": shown, "kind": "execute", "rawInput": {"command": shown}})
    allowed = request("session/request_permission", {
        "sessionId": session,
        "toolCall": {"toolCallId": "t2", "title": shown, "kind": "execute", "rawInput": {"command": shown}},
        "options": [{"optionId": "ok", "name": "Allow", "kind": "allow_once"}, {"optionId": "no", "name": "Reject", "kind": "reject_once"}],
    })["result"]["outcome"]
    out = ""
    if allowed.get("optionId") == "ok":
        term = request("terminal/create", {"sessionId": session, "command": cmd[0], "args": cmd[1:]})["result"]["terminalId"]
        request("terminal/wait_for_exit", {"sessionId": session, "terminalId": term})
        out = request("terminal/output", {"sessionId": session, "terminalId": term})["result"]["output"].strip()
        request("terminal/release", {"sessionId": session, "terminalId": term})

    request("fs/write_text_file", {"sessionId": session, "path": cwd + sep + "out.md", "content": f"{notes.strip()} | {out}"})
    outside = request("fs/write_text_file", {"sessionId": session, "path": state["outside"], "content": "x"})
    update(session, {"sessionUpdate": "tool_call", "toolCallId": "t3", "title": "Write outside", "kind": "edit", "rawInput": {"path": state["outside"]}})
    update(session, {"sessionUpdate": "tool_call_update", "toolCallId": "t3", "status": "failed", "content": [{"type": "content", "content": {"type": "text", "text": outside.get("error", {}).get("message", "")}}]})

    server = state["mcp"][0]
    mcp(server, "initialize", {"protocolVersion": "2025-06-18"}, 1)
    tools = mcp(server, "tools/list", {}, 2)["result"]["tools"]
    called = mcp(server, "tools/call", {"name": "ticket_comment", "arguments": {"body": "đã xong"}}, 3)["result"]
    state["tools"] = [t["name"] for t in tools]
    state["called"] = called

    update(session, {"sessionUpdate": "usage_update", "used": 1000, "size": 200000, "cost": {"amount": 0.0125, "currency": "USD"}})
    update(session, {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": f"Xong: {out}"}})
    with open(cwd + sep + "agent_log.json", "w", encoding="utf-8") as f:
        json.dump({"outside_error": "error" in outside, "tools": state["tools"], "called": called, "first_prompt": text[:200]}, f, ensure_ascii=False)
    return {"stopReason": "end_turn"}


def main() -> None:
    sys.stdin.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]  # ACP is UTF-8 JSON, whatever the console uses
    sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    sessions: dict = {}
    while True:
        message = read()
        method, msg_id, params = message.get("method"), message.get("id"), message.get("params") or {}
        if method == "initialize":
            result: dict | None = {"protocolVersion": 1, "agentCapabilities": {"loadSession": True}, "agentInfo": {"name": "fake-kiro"}}
        elif method == "session/new":
            sid = f"sess-{len(sessions) + 1}"
            sessions[sid] = {"cwd": params["cwd"], "mcp": params["mcpServers"], "outside": params["cwd"] + "/../outside.txt",
                             "model": ((params.get("_meta") or {}).get("kiro") or {}).get("modelId")}
            result = {"sessionId": sid}
        elif method == "session/load":
            sessions[params["sessionId"]] = {"cwd": params["cwd"], "mcp": params["mcpServers"], "outside": params["cwd"] + "/../outside.txt"}
            update(params["sessionId"], {"sessionUpdate": "agent_message_chunk", "content": {"type": "text", "text": "OLD"}, "_meta": {"kiro": {"replay": True}}})
            result = None
        elif method == "session/prompt":
            result = prompt(params, sessions)
        elif method == "session/cancel":
            continue
        else:
            send({"jsonrpc": "2.0", "id": msg_id, "error": {"code": -32601, "message": method}})
            continue
        send({"jsonrpc": "2.0", "id": msg_id, "result": result})


if __name__ == "__main__":
    main()
