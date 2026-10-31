"""Drive a running office over its HTTP API like a business user would (stdlib only).

The driver plays the user: it sends messages, answers questions from a scripted persona, approves tool
calls / ticket proposals / automations, and waits until the office is quiet. Scenarios then check the end
state (tickets, documents, files, test results) — not the transcript.
"""

from __future__ import annotations

import http.cookiejar
import json
import re
import time
from datetime import datetime
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any


class ApiError(RuntimeError):
    pass


@dataclass
class Persona:
    """How the user answers. `answers` are (regex on the question, answer); first match wins."""

    answers: list[tuple[str, str]] = field(default_factory=list)
    default: str = "Theo phương án bạn đề xuất (mặc định). Không có yêu cầu gì thêm."
    approve_tools: bool = True
    remember_prefix: bool = False  # choose "always allow <prefix>" instead of "allow once"
    confirm_tickets: bool = True
    approve_automations: bool = True

    def reply(self, question: str) -> str:
        for pattern, answer in self.answers:
            if re.search(pattern, question, re.IGNORECASE | re.DOTALL):
                return answer
        return self.default


@dataclass
class Log:
    lines: list[str] = field(default_factory=list)

    def __call__(self, text: str) -> None:
        stamp = time.strftime("%H:%M:%S")
        line = f"[{stamp}] {text}"
        self.lines.append(line)
        print(line, flush=True)


class Lab:
    def __init__(self, base: str, token: str, log: Log | None = None) -> None:
        self.base = base.rstrip("/")
        self.log = log or Log()
        self.jar = http.cookiejar.CookieJar()
        self.http = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(self.jar))
        self.http.open(f"{self.base}/login?t={token}").read()
        self.approvals = 0
        self.questions = 0
        self.scope: set[str] = set()  # actor ids this driver is responsible for (empty = everyone)

    def focus(self, *names: str) -> None:
        """Only answer for (and wait on) these actors, so several scenarios can run side by side."""
        self.scope = {self.actor(n)["id"] for n in names}

    def _mine(self, actor_id: str | None) -> bool:
        return not self.scope or actor_id in self.scope

    # ---- http ---------------------------------------------------------------------------------

    def call(self, method: str, path: str, body: Any = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(
            self.base + path,
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Requested-With": "actoroffice"},
        )
        try:
            with self.http.open(req, timeout=60) as res:
                raw = res.read()
        except urllib.error.HTTPError as exc:
            raise ApiError(f"{method} {path} → {exc.code}: {exc.read().decode('utf-8', 'replace')[:300]}") from exc
        return json.loads(raw) if raw else None

    def get(self, path: str) -> Any:
        return self.call("GET", path)

    def post(self, path: str, body: Any = None) -> Any:
        return self.call("POST", path, body if body is not None else {})

    # ---- setup --------------------------------------------------------------------------------

    def state(self) -> dict[str, Any]:
        return self.get("/api/state")

    def actor(self, name: str) -> dict[str, Any]:
        return next(a for a in self.state()["actors"] if a["name"] == name)

    def hire(self, position: str, name: str, *, model: str | None = None, specialty: str = "") -> dict[str, Any]:
        st = self.state()
        for a in st["actors"]:
            if a["name"] == name:
                return a
        pos = next(p for p in st["positions"] if p["name"] == position)
        actor = self.post("/api/actors", {"position_id": pos["id"], "name": name, "model": model, "specialty": specialty})
        self.log(f"hired {name} as {pos['title']} ({model or pos['model']})")
        return actor

    def project(self, name: str, path: str | None, access: str = "read_only") -> dict[str, Any]:
        for p in self.state()["projects"]:
            if p["name"] == name:
                return p
        return self.post("/api/projects", {"name": name, "path": path, "access": access})

    def dm(self, actor_name: str, project_id: str | None = None) -> str:
        actor = self.actor(actor_name)
        return self.post("/api/conversations", {"kind": "dm", "actor_id": actor["id"], "project_id": project_id})["id"]

    def group(self, title: str, names: list[str], project_id: str | None = None) -> str:
        ids = [self.actor(n)["id"] for n in names]
        return self.post("/api/conversations", {"kind": "group", "title": title, "actor_ids": ids, "project_id": project_id})["id"]

    # ---- conversation -------------------------------------------------------------------------

    def say(self, conv: str, text: str) -> int:
        msg = self.post(f"/api/conversations/{conv}/messages", {"body": text})
        self.log(f"USER → {text[:160]}")
        return int(msg["id"])

    def messages(self, conv: str, after: int = 0) -> list[dict[str, Any]]:
        return self.get(f"/api/conversations/{conv}/messages?after={after}")

    def actor_replies(self, conv: str, after: int) -> list[dict[str, Any]]:
        return [m for m in self.messages(conv, after) if m["author_kind"] == "actor"]

    # ---- autopilot ----------------------------------------------------------------------------

    def handle_pending(self, persona: Persona) -> int:
        handled = 0
        for item in self.state()["pending"]:
            if not self._mine(item.get("actor_id")):
                continue
            try:
                handled += self._handle(item, persona)
            except ApiError as exc:
                if "already" not in str(exc):
                    raise
        return handled

    def _handle(self, item: dict[str, Any], persona: Persona) -> int:
        handled = 0
        if True:
            p = item["payload"]
            kind = item["kind"]
            if kind == "approve_tool" and persona.approve_tools:
                body: dict[str, Any] = {"approved": True}
                prefix = next((s["rule"] for s in p.get("suggestions", []) if s["scope"] == "prefix"), None)
                if persona.remember_prefix and prefix:
                    body["remember"] = prefix
                self.post(f"/api/interactions/{item['id']}/answer", body)
                self.approvals += 1
                self.log(f"approved {p.get('actor')}: {p.get('tool')} {str(p.get('target'))[:120]}" + (f" (always {prefix})" if "remember" in body else ""))
            elif kind == "confirm_ticket" and persona.confirm_tickets:
                res = self.post(f"/api/interactions/{item['id']}/answer", {"confirmed": True})
                self.log(f"confirmed ticket proposal by {p.get('proposed_by')}: {p.get('title')} → {res.get('response', {}).get('ticket_key')}")
            elif kind == "question":
                answer = persona.reply(p.get("question", "") + " " + " ".join(p.get("options") or []))
                self.post(f"/api/interactions/{item['id']}/answer", {"answer": answer})
                self.questions += 1
                self.log(f"{p.get('asked_by')} asked: {p.get('question', '')[:200]!r} → {answer[:120]!r}")
            elif kind == "approve_automation" and persona.approve_automations:
                self.post(f"/api/interactions/{item['id']}/answer", {"approved": True, "run_now": True})
                self.log(f"approved automation {p.get('name')} ({p.get('schedule_text')})")
            else:
                return 0
            handled += 1
        return handled

    def busy(self, persona: Persona | None = None) -> bool:
        st = self.state()
        kinds = {"approve_tool", "confirm_ticket", "question", "approve_automation"}
        if persona is not None and not persona.confirm_tickets:
            kinds.discard("confirm_ticket")  # deliberately left pending: not something we wait for
        waiting_on_user = any(i["kind"] in kinds and self._mine(i.get("actor_id")) for i in st["pending"])
        queued = any(self._mine(w["actor_id"]) for w in st.get("queued", []))  # confirmed work not started yet
        return any(self._mine(r["actor_id"]) for r in st["runs"]) or queued or waiting_on_user

    def settle(self, persona: Persona, *, timeout: float = 900, quiet: float = 12) -> bool:
        """Answer whatever comes up until nothing runs and nothing waits on the user for `quiet` seconds."""
        deadline = time.monotonic() + timeout
        calm_since: float | None = None
        while time.monotonic() < deadline:
            self.handle_pending(persona)
            if self.busy(persona):
                calm_since = None
            else:
                calm_since = calm_since or time.monotonic()
                if time.monotonic() - calm_since >= quiet:
                    return True
            time.sleep(3)
        self.log("settle: TIMEOUT")
        return False

    def converse(self, conv: str, opening: str, persona: Persona, *, follow_ups: list[str] | None = None, max_rounds: int = 4,
                 timeout: float = 900) -> list[dict[str, Any]]:
        """Send the opening; whenever the actor replies with questions (and nothing else is pending), answer
        them from `follow_ups` (in order) or from the persona. Returns every actor message."""
        follow_ups = list(follow_ups or [])
        last = self.say(conv, opening)
        replies: list[dict[str, Any]] = []
        for round_no in range(max_rounds):
            self.settle(persona, timeout=timeout)
            new = self.actor_replies(conv, last)
            for m in new:
                at = datetime.fromisoformat(m["created_at"]).astimezone().strftime("%H:%M:%S")
                self.log(f"{self.name_of(m['author_id'])} (posted {at}) → {m['body'][:400]!r}")
            replies += new
            if not new:
                break
            text = new[-1]["body"]
            last = max(m["id"] for m in self.messages(conv, last)) if self.messages(conv, last) else last
            if not asks_question(text) or round_no == max_rounds - 1:
                break
            answer = follow_ups.pop(0) if follow_ups else persona.reply(text)
            last = self.say(conv, answer)
        return replies

    def name_of(self, actor_id: str) -> str:
        return next((a["name"] for a in self.state()["actors"] if a["id"] == actor_id), actor_id)

    # ---- inspection ---------------------------------------------------------------------------

    def tickets(self, project_id: str | None = None) -> list[dict[str, Any]]:
        return self.get(f"/api/tickets?all=true&project={project_id or ''}")

    def ticket(self, key: str) -> dict[str, Any]:
        return self.get(f"/api/tickets/{key}")

    def tree(self, key: str) -> list[dict[str, Any]]:
        out, todo = [], [key]
        while todo:
            data = self.ticket(todo.pop(0))
            out.append(data)
            todo += [c["key"] for c in data["children"]]
        return out


def asks_question(text: str) -> bool:
    return "?" in text or bool(re.search(r"\b(câu hỏi|cho (em|mình) hỏi|xác nhận giúp|anh/chị chọn)\b", text, re.IGNORECASE))


def doc_has_sections(body: str, *needles: str) -> list[str]:
    """Return the needles missing from a document (case-insensitive)."""
    low = body.lower()
    return [n for n in needles if n.lower() not in low]
