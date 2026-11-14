"""Doc Core in the office: the documents of a ticket checked against the standard pack, their sources pinned, the
findings people accepted, and the child tickets a breakdown creates.

doccore/ decides what "standard and matching" means and does no I/O. This service hands it the data: the documents of
the ticket and of its parent at the right revisions, the content each `Nguồn:` points to (through the code index and
the office's own tickets), what each revision was written from.

Document keys. A stored document `analysis`, `breakdown`, `spec`, `design` is the pack's doc type of that name. On an
epic, the spec of breakdown item `T-2` is the document `spec.T-2`; it becomes the `spec` of the child ticket. For the
core every document is `<ticket key>/<doc type>` (`EP-4/analysis`), and a spec that is still on the epic is
`EP-4.T-2/spec`, so a trace across documents is written `EP-4/REQ-2`.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from actoroffice.doccore import Approved, Baseline, Evidence, Finding, MatchState, ParsedDoc, StandardPack, impact, match_state, parse, run_rules
from actoroffice.doccore.model import MATCH_STATE_VI, SourceRef, make_finding
from actoroffice.domain.errors import NotFound, OfficeError, PolicyDenied
from actoroffice.domain.ids import utcnow
from actoroffice.domain.stage import StageDef
from actoroffice.domain.team import LOCAL_OWNER_ID
from actoroffice.domain.work import Document, Interaction, Ticket, TicketStatus

from .docflow import default_pack, is_pack_flow, spawn_of

if TYPE_CHECKING:
    from actoroffice.app import Office

EXCERPT_LIMIT = 4000
SPEC_PER_TASK = "R-SPEC-PER-TASK"


class DocError(OfficeError):
    code = 400


@dataclass
class DocSet:
    """The documents one check looks at."""

    docs: dict[str, ParsedDoc]  # core key → parsed document
    own: dict[str, Document]  # core key → the stored document of this ticket
    stored: dict[str, tuple[int, str]]  # core key → (ticket id, stored key), also for the parent's documents


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


class DocService:
    def __init__(self, office: Office) -> None:
        self.office = office

    @property
    def pack(self) -> StandardPack:
        return default_pack()

    # ---- keys ------------------------------------------------------------------------------------------------

    def doctype_of(self, stored_key: str) -> str | None:
        base = stored_key.split(".", 1)[0]
        return base if base in self.pack.doc_types else None

    def core_key(self, ticket: Ticket, stored_key: str) -> str:
        base, _, item = stored_key.partition(".")
        return f"{ticket.key}.{item}/{base}" if item else f"{ticket.key}/{base}"

    def applies(self, ticket: Ticket) -> bool:
        return is_pack_flow(ticket.stage_template)

    def template_text(self, doctype: str) -> str:
        if doctype not in self.pack.doc_types:
            raise NotFound(f"Không có loại tài liệu '{doctype}'. Có: {', '.join(self.pack.doc_types)}")
        return self.pack.template_text(doctype)

    # ---- what a check looks at -------------------------------------------------------------------------------

    async def docset(self, ticket: Ticket) -> DocSet:
        """The ticket's own documents at their current revision, and its parent's at the approved one (the current
        one when nothing is approved yet): what the ticket's documents are written from."""
        office = self.office
        docs: dict[str, ParsedDoc] = {}
        own: dict[str, Document] = {}
        stored: dict[str, tuple[int, str]] = {}
        for doc in await office.tickets.list_documents(ticket.id):
            doctype = self.doctype_of(doc.key)
            if doctype is None:
                continue
            key = self.core_key(ticket, doc.key)
            docs[key] = parse(doc.body, doctype, self.pack)
            own[key] = doc
            stored[key] = (ticket.id, doc.key)
        if ticket.parent_id is not None:
            parent = await office.tickets.get(ticket.parent_id)
            for doc in await office.tickets.list_documents(parent.id):
                doctype = self.doctype_of(doc.key)
                if doctype is None or "." in doc.key:  # the specs of the other tasks are not this task's business
                    continue
                key = self.core_key(parent, doc.key)
                body = await self._body(parent.id, doc.key, doc.approved_revision) if doc.approved_revision else doc.body
                docs[key] = parse(body, doctype, self.pack)
                stored[key] = (parent.id, doc.key)
        return DocSet(docs, own, stored)

    async def _body(self, ticket_id: int, stored_key: str, revision: int | None) -> str:
        row = await self.office.db.fetchone(
            "SELECT body FROM document_revisions WHERE ticket_id = ? AND key = ? AND revision = ?", (ticket_id, stored_key, revision)
        )
        return str(row["body"]) if row else ""

    # ---- baseline --------------------------------------------------------------------------------------------

    async def capture_baseline(self, ticket: Ticket, stored_key: str) -> Baseline | None:
        """Record what the current revision of a document is written from: the revisions of the documents its type
        is based on, as they are now. Called when the document is saved."""
        doctype = self.doctype_of(stored_key)
        if doctype is None:
            return None
        office = self.office
        wanted = set(self.pack.doc_types[doctype].based_on)
        found: dict[str, int] = {}
        chain = [ticket] + ([await office.tickets.get(ticket.parent_id)] if ticket.parent_id is not None else [])
        for owner in chain:
            for doc in await office.tickets.list_documents(owner.id):
                if doc.key in wanted and not any(k.endswith("/" + doc.key) for k in found):
                    # the nearest one: a task's own spec before the epic's
                    found[self.core_key(owner, doc.key)] = doc.approved_revision or doc.revision
        baseline = Baseline(docs=found)
        await office.db.execute("UPDATE documents SET baseline_json = ? WHERE ticket_id = ? AND key = ?", (baseline.model_dump_json(), ticket.id, stored_key))
        return baseline

    async def _baselines(self, ticket: Ticket, ds: DocSet) -> dict[str, Baseline]:
        out: dict[str, Baseline] = {}
        for key, doc in ds.own.items():
            row = await self.office.db.fetchone("SELECT baseline_json FROM documents WHERE ticket_id = ? AND key = ?", (ticket.id, doc.key))
            if row and row["baseline_json"]:
                out[key] = Baseline.model_validate_json(row["baseline_json"])
            else:  # saved before Doc Core, or by a path that does not capture: written from what is there now
                captured = await self.capture_baseline(ticket, doc.key)
                if captured is not None:
                    out[key] = captured
        return out

    async def _approved(self, ds: DocSet, baselines: dict[str, Baseline]) -> dict[str, Approved]:
        """For every document something was written from: its latest approved revision, and which items changed
        between each revision a document pinned and that one."""
        office = self.office
        out: dict[str, Approved] = {}
        pinned: dict[str, set[int]] = {}
        for baseline in baselines.values():
            for key, revision in baseline.docs.items():
                pinned.setdefault(key, set()).add(revision)
        for key, revisions in pinned.items():
            if key not in ds.stored:
                continue
            ticket_id, stored_key = ds.stored[key]
            doc = await office.tickets.get_document(ticket_id, stored_key)
            if doc is None or not doc.approved_revision:
                continue
            doctype = self.doctype_of(stored_key) or ""
            latest = parse(await self._body(ticket_id, stored_key, doc.approved_revision), doctype, self.pack)
            changed: dict[int, list[str]] = {}
            for revision in revisions:
                if revision == doc.approved_revision:
                    changed[revision] = []
                    continue
                old_body = await self._body(ticket_id, stored_key, revision)
                changed[revision] = sorted(impact(parse(old_body, doctype, self.pack) if old_body else None, latest))
            out[key] = Approved(revision=doc.approved_revision, changed_since=changed)
        return out

    # ---- evidence --------------------------------------------------------------------------------------------

    async def _resolve(self, ticket: Ticket, src: SourceRef) -> dict[str, Any] | None:
        """What a `Nguồn:` points to right now. None: this kind of source cannot be checked here (a URL, an issue of
        a tracker that is not connected), which is not a failure."""
        office = self.office
        if src.kind == "file":
            repos = await office.codeintel.for_ticket(ticket)
            if src.repo:
                repos = [r for r in repos if r.slug == src.repo.lower() or r.slug.endswith("/" + src.repo.lower())]
                if not repos:
                    return {"error": f"không có repo '{src.repo}' trong phạm vi của ticket"}
            if not repos:
                return None  # no code source configured: nothing to check against
            error = f"không có file {src.ref}"
            for repo in repos:
                try:
                    got = await office.codeintel.read(repo, src.ref, lines=src.lines())
                except OfficeError as exc:
                    error = str(exc)
                    continue
                if src.lines() and got["end"] < src.lines()[1]:  # type: ignore[index]
                    return {"error": f"{src.ref} chỉ có {got['total_lines']} dòng (trích tới dòng {src.lines()[1]})"}  # type: ignore[index]
                return {"provider": "code", "category": "code", "ref": f"{repo.slug}:{got['path']}", "version": got["commit"],
                        "hash": got["content_hash"], "excerpt": got["text"][:EXCERPT_LIMIT]}  # fmt: skip
            return {"error": error}
        if src.kind == "ticket":
            try:
                other = await office.tickets.get(src.ref)
            except (NotFound, OfficeError):
                return await self._outside(ticket, src)  # not an office ticket: an issue of an outside tracker
            text = f"{other.title}\n\n{other.description}"
            return {"provider": "office_ticket", "category": "docs", "ref": other.key, "version": other.updated_at, "hash": content_hash(text), "excerpt": text[:EXCERPT_LIMIT]}
        if src.kind == "url":
            return await self._outside(ticket, src)
        return None

    async def _outside(self, ticket: Ticket, src: SourceRef) -> dict[str, Any] | None:
        """A source in a connected system (an issue, a wiki page). None when nothing connected knows it: a document
        the office cannot open is not held against the author."""
        from actoroffice.ports.connection import ConnectorError

        if not await self.office.sourcehub.recognize(ticket, src.ref):
            return None  # nothing connected knows this kind of reference
        try:
            got = await self.office.sourcehub.get(ticket, src.ref)
        except ConnectorError as exc:
            if exc.kind in ("unconfigured", "network", "rate_limited", "auth"):
                return None  # cannot be checked right now: not the author's fault
            return {"error": str(exc)}  # the system knows the reference and says it is not there, or not allowed
        return {"provider": got.use, "category": "docs", "ref": got.ref.ref, "version": got.content.version, "hash": got.content_hash, "excerpt": got.content.text[:EXCERPT_LIMIT]}

    async def evidence(self, ticket: Ticket, ds: DocSet) -> list[Evidence]:
        """One record per source line of the ticket's own documents: pinned the first time this revision is seen,
        compared with the source as it is now on every call."""
        office = self.office
        out: list[Evidence] = []
        now = utcnow()
        for key, doc in ds.own.items():
            for item in ds.docs[key].items:
                for src in item.sources:
                    current = await self._resolve(ticket, src)
                    if current is None:
                        continue
                    row = await office.db.fetchone(
                        "SELECT * FROM doc_evidence WHERE ticket_id = ? AND doc_key = ? AND revision = ? AND item_id = ? AND source = ?",
                        (ticket.id, doc.key, doc.revision, item.id, src.raw),
                    )
                    if row is None or (row["error"] and not current.get("error")):
                        # an earlier revision already pinned the same source for this item: keep that pin, so editing
                        # another part of the document does not make changed code look fresh
                        earlier = await office.db.fetchone(
                            "SELECT * FROM doc_evidence WHERE ticket_id = ? AND doc_key = ? AND item_id = ? AND source = ? AND error IS NULL ORDER BY revision DESC LIMIT 1",
                            (ticket.id, doc.key, item.id, src.raw),
                        )
                        pin = dict(earlier) if earlier is not None and not current.get("error") else None
                        values = (
                            current.get("provider", ""), current.get("category", ""), current.get("ref", src.ref), src.locator,
                            (pin or {}).get("version") or current.get("version", ""), (pin or {}).get("content_hash") or current.get("hash", ""),
                            (pin or {}).get("excerpt") or current.get("excerpt", ""), current.get("error"), now,
                        )  # fmt: skip
                        await office.db.execute(
                            "INSERT INTO doc_evidence(ticket_id, doc_key, revision, item_id, source, provider, category, ref, locator, version, content_hash, excerpt, error, fetched_at) "
                            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?) ON CONFLICT(ticket_id, doc_key, revision, item_id, source) DO UPDATE SET "
                            "provider = excluded.provider, category = excluded.category, ref = excluded.ref, version = excluded.version, "
                            "content_hash = excluded.content_hash, excerpt = excluded.excerpt, error = excluded.error, fetched_at = excluded.fetched_at",
                            (ticket.id, doc.key, doc.revision, item.id, src.raw, *values),
                        )
                        row = await office.db.fetchone(
                            "SELECT * FROM doc_evidence WHERE ticket_id = ? AND doc_key = ? AND revision = ? AND item_id = ? AND source = ?",
                            (ticket.id, doc.key, doc.revision, item.id, src.raw),
                        )
                    assert row is not None
                    out.append(
                        Evidence(
                            doc=key, item=item.id, source=src.raw, provider=row["provider"], category=row["category"], ref=row["ref"], locator=row["locator"],
                            version=row["version"], content_hash=row["content_hash"], excerpt=row["excerpt"], fetched_at=row["fetched_at"],
                            error=current.get("error") or row["error"], current_hash=current.get("hash"),
                        )
                    )  # fmt: skip
        return out

    # ---- checking --------------------------------------------------------------------------------------------

    def output_keys(self, ticket: Ticket, stage: StageDef, ds: DocSet) -> set[str]:
        """The documents a stage is answerable for: its outputs, and on a breakdown the spec of every task."""
        wanted = {o for out in stage.outputs for o in out.split("|")}
        keys = {key for key, doc in ds.own.items() if doc.key in wanted}
        if "breakdown" in wanted:
            keys |= {key for key, doc in ds.own.items() if doc.key.startswith("spec.")}
        return keys

    async def check(self, ticket: Ticket, stage: StageDef | None = None, *, keys: set[str] | None = None) -> list[Finding]:
        """Every finding on the stage's documents (or on `keys`, or on all of the ticket's), accepted ones marked."""
        ds = await self.docset(ticket)
        only = keys if keys is not None else (self.output_keys(ticket, stage, ds) if stage is not None else set(ds.own))
        baselines = await self._baselines(ticket, ds)
        findings = run_rules(self.pack, ds.docs, await self.evidence(ticket, ds), baselines, await self._approved(ds, baselines), only_docs=only)
        findings += self._specs_per_task(ticket, ds, only)
        accepted = {
            r["finding_id"]: r for r in await self.office.db.fetchall("SELECT * FROM doc_findings WHERE ticket_id = ?", (ticket.id,))
        }
        for f in findings:
            hit = accepted.get(f.id)
            if hit is not None:
                f.status, f.accepted_by, f.reason = "accepted", hit["accepted_by"], hit["reason"]
        return findings

    def _specs_per_task(self, ticket: Ticket, ds: DocSet, only: set[str]) -> list[Finding]:
        """Every task of a breakdown has its spec (`spec.T-n`) before the business is approved."""
        key = self.core_key(ticket, "breakdown")
        if key not in ds.docs or key not in only:
            return []
        have = {doc.key.split(".", 1)[1] for doc in ds.own.values() if doc.key.startswith("spec.")}
        out = []
        for item in ds.docs[key].items:
            if item.kind == "T" and item.id not in have:
                out.append(make_finding(SPEC_PER_TASK, "error", key, item.id, f"{item.id} chưa có nghiệp vụ: lưu tài liệu `spec.{item.id}` theo mẫu `spec` (doc_template).", "spec"))
        for extra in sorted(have - {i.id for i in ds.docs[key].items}):
            out.append(make_finding(SPEC_PER_TASK, "error", key, extra, f"Có tài liệu `spec.{extra}` nhưng breakdown không có mục {extra}: thêm mục đó hoặc bỏ tài liệu.", "orphan"))
        return out

    async def states(self, ticket: Ticket, findings: list[Finding] | None = None) -> dict[str, MatchState]:
        ds = await self.docset(ticket)
        findings = findings if findings is not None else await self.check(ticket)
        baselines = await self._baselines(ticket, ds)
        approved = await self._approved(ds, baselines)
        evidence = await self.evidence(ticket, ds)
        return {
            doc.key: match_state(self.pack, key, ds.docs[key], [f for f in findings if f.doc == key], baselines.get(key), approved, [e for e in evidence if e.doc == key])
            for key, doc in ds.own.items()
        }

    async def all_findings(self, ticket: Ticket, stage: StageDef) -> list[Finding]:
        """The findings of the rules and of the AI cross-check of the ticket's current stage."""
        return await self.check(ticket, stage) + await self.office.crosscheck.findings(ticket)

    async def blocking(self, ticket: Ticket, stage: StageDef) -> list[Finding]:
        """What stops an approval: an error that is neither fixed nor accepted."""
        return [f for f in await self.all_findings(ticket, stage) if f.status == "open" and f.severity == "error"]

    # ---- accepting a finding ---------------------------------------------------------------------------------

    async def accept(self, ticket: Ticket, finding_id: str, *, by: str, reason: str) -> Finding:
        """Somebody who may approve the stage's gate accepts a finding as it is, with a reason. The author and the
        AI cannot: a difference between documents is either fixed or owned by the approver."""
        reason = reason.strip()
        if not reason:
            raise DocError("Cần lý do khi chấp nhận một điểm lệch")
        office = self.office
        stage = await office.stages.stage_def(ticket)
        finding = next((f for f in await self.all_findings(ticket, stage) if f.id == finding_id or f.id.startswith(finding_id)), None)
        if finding is None:
            raise NotFound(f"Không có phát hiện {finding_id} ở giai đoạn này của {ticket.key}")
        policy = await office.teams.gate_policy(ticket, stage.gate) if stage.gate else None
        roles = (policy.accept_findings or policy.roles) if policy is not None else []
        if by != LOCAL_OWNER_ID and not await office.teams.can_act(by, ticket, roles):
            raise PolicyDenied(f"Cần vai {' / '.join(roles) or 'người duyệt'} để chấp nhận điểm lệch")
        await office.db.execute(
            "INSERT INTO doc_findings(ticket_id, finding_id, rule, doc_key, item_id, message, accepted_by, reason, accepted_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticket_id, finding_id) DO UPDATE SET accepted_by = excluded.accepted_by, reason = excluded.reason, accepted_at = excluded.accepted_at",
            (ticket.id, finding.id, finding.rule, finding.doc, finding.item, finding.message, by, reason[:1000], utcnow()),
        )
        await office.audit.record("doc:finding", "accepted", ticket_id=ticket.id, target=f"{ticket.key}:{finding.rule}", detail={"by": by, "reason": reason, "item": finding.item})
        await office.tickets.add_comment(ticket.id, f"☑️ Chấp nhận điểm lệch {finding.rule} ({finding.item or finding.doc}): {reason}", author_kind="system", author_id=None)
        finding.status, finding.accepted_by, finding.reason = "accepted", by, reason
        return finding

    # ---- the gate card ---------------------------------------------------------------------------------------

    async def approval_view(self, ticket: Ticket, stage: StageDef) -> dict[str, Any]:
        """What the approver needs next to the documents, so that nobody has to trace by hand: the trace table,
        assumptions, declared changes with the old code, what was accepted, what each document was written from."""
        ds = await self.docset(ticket)
        keys = self.output_keys(ticket, stage, ds)
        findings = await self.check(ticket, stage)
        states = await self.states(ticket, findings)
        cross = await self.office.crosscheck.findings(ticket)
        findings = findings + cross
        baselines = await self._baselines(ticket, ds)
        evidence = await self.evidence(ticket, ds)
        trace, assumptions, changes, tasks = [], [], [], []
        for key in sorted(keys):
            for item in ds.docs[key].items:
                if item.kind == "A":
                    assumptions.append({"doc": ds.own[key].key, "id": item.id, "text": item.title or item.body[:200]})
                if item.traces:
                    trace.append({"doc": ds.own[key].key, "id": item.id, "title": item.title, "traces": item.traces})
                for changed in item.changes:
                    old = next((e.excerpt[:600] for e in evidence if e.item == changed), "")
                    changes.append({"doc": ds.own[key].key, "id": item.id, "title": item.title, "changes": changed, "old": old})
                if item.kind == "T":
                    tasks.append({"id": item.id, "title": item.title, "has_spec": any(d.key == f"spec.{item.id}" for d in ds.own.values())})
        return {
            "states": {k: {"state": v.value, "label": MATCH_STATE_VI.get(v, v.value)} for k, v in states.items() if self.core_key(ticket, k) in keys},
            "trace": trace,
            "assumptions": assumptions,
            "declared_changes": changes,
            "accepted": [f.model_dump() for f in findings if f.status == "accepted"],
            "warnings": [f.model_dump() for f in findings if f.status == "open" and f.severity != "error"],
            # differences the AI cross-check still sees: each one is fixed, or accepted here with a reason
            "open": [f.model_dump() for f in findings if f.status == "open" and f.severity == "error"],
            "baseline": {ds.own[k].key: baselines[k].docs for k in sorted(keys) if k in baselines},
            "tasks": tasks,
        }

    async def panel(self, ticket: Ticket) -> dict[str, Any] | None:
        """What the ticket page shows about its documents: the state of each, what is still open, its parent epic and
        its child tasks with their own states. None for a ticket that is not in a pack flow."""
        if not self.applies(ticket):
            return None
        office = self.office
        stage = await office.stages.stage_def(ticket) if ticket.stage else None
        findings = await self.check(ticket)
        if stage is not None:
            findings += await office.crosscheck.findings(ticket)
        states = await self.states(ticket, findings)
        docs = []
        for doc in await office.tickets.list_documents(ticket.id):
            if doc.key not in states:
                continue
            state = states[doc.key]
            docs.append({"key": doc.key, "revision": doc.revision, "approved_revision": doc.approved_revision, "state": state.value, "label": MATCH_STATE_VI.get(state, state.value)})
        children = []
        for row in await office.db.fetchall("SELECT id FROM tickets WHERE parent_id = ? ORDER BY id", (ticket.id,)):
            child = await office.tickets.get(int(row["id"]))
            people = [m.user.name for m in await office.teams.participants(child.id) if "dev" in m.roles]
            children.append({
                "key": child.key, "title": child.title, "status": child.status.value, "stage": child.stage, "template": child.stage_template, "assigned": people,
                "docs": [{"key": k, "state": v.value, "label": MATCH_STATE_VI.get(v, v.value)} for k, v in (await self.states(child)).items()] if self.applies(child) else [],
            })  # fmt: skip
        parent = None
        if ticket.parent_id is not None:
            up = await office.tickets.get(ticket.parent_id)
            parent = {"key": up.key, "title": up.title}
        return {
            "docs": docs,
            "open": [f.model_dump() for f in findings if f.status == "open" and f.severity == "error"],
            "warnings": [f.model_dump() for f in findings if f.status == "open" and f.severity != "error"],
            "accepted": [f.model_dump() for f in findings if f.status == "accepted"],
            "children": children,
            "parent": parent,
            "is_epic": spawn_of(ticket.stage_template, "breakdown") is not None,
        }

    # ---- a breakdown becomes tickets -------------------------------------------------------------------------

    async def spawn(self, ticket: Ticket, response: dict[str, Any], *, by: str) -> list[Ticket]:
        """The approved breakdown of an epic: one child ticket per task, with its spec already approved, the person
        it is handed to and the flow it follows. `response`: {"drop": ["T-3"], "flows": {"T-1": "task-spec"},
        "assign": {"T-1": "<user id or name>"}}. The epic then waits for its children."""
        office = self.office
        spawn = spawn_of(ticket.stage_template, ticket.stage)
        if spawn is None:
            return []
        breakdown = await office.tickets.get_document(ticket.id, "breakdown")
        if breakdown is None:
            raise DocError(f"{ticket.key} chưa có tài liệu breakdown")
        parsed = parse(breakdown.body, "breakdown", self.pack)
        drop = {str(d).upper() for d in response.get("drop") or []}
        flows = {str(k).upper(): str(v) for k, v in (response.get("flows") or {}).items()}
        assign = {str(k).upper(): str(v) for k, v in (response.get("assign") or {}).items()}
        # a breakdown approved a second time (the epic grew) creates only the tasks that are not tickets yet
        existing = await office.db.fetchall("SELECT description FROM tickets WHERE parent_id = ?", (ticket.id,))
        already = {item.id for item in parsed.items for row in existing if f"(mục {item.id} của {ticket.key})" in str(row["description"])}
        default_flow = spawn.flows[0] if spawn.flows else "task"
        created: list[Ticket] = []
        for item in parsed.items:
            if item.kind != "T" or item.id.upper() in drop or item.id in already:
                continue
            flow = flows.get(item.id.upper(), default_flow)
            if spawn.flows and flow not in spawn.flows:
                raise DocError(f"{item.id}: luồng '{flow}' không được phép ở đây. Chọn: {', '.join(spawn.flows)}")
            child = await office.tickets.create(
                item.title or item.id,
                description=f"{item.body.strip()}\n\n_Task của epic {ticket.key} (mục {item.id} của {ticket.key})._",
                type="task",
                project_id=ticket.project_id,
                parent_id=ticket.id,
                created_by=by,
            )
            for doc_key in spawn.copy_docs:
                source = await office.tickets.get_document(ticket.id, f"{doc_key}.{item.id}")
                if source is None:
                    continue
                copied = await office.tickets.put_document(child.id, doc_key, source.body, author=source.updated_by, base_revision=None)
                await office.db.execute("UPDATE documents SET approved_revision = ? WHERE ticket_id = ? AND key = ?", (copied.revision, child.id, doc_key))
                await self.capture_baseline(await office.tickets.get(child.id), doc_key)
            person = assign.get(item.id.upper())
            if person:
                user = await office.teams.find_user(person)
                if user is None:
                    raise DocError(f"{item.id}: không có người dùng '{person}'")
                await office.teams.set_participant(child.id, user.id, list(spawn.assign) or ["dev"])
            started = await office.stages.start(child.id, template=flow, by=by, team_id=ticket.team_id, owner_user_id=ticket.owner_user_id)
            await office.tickets.add_blocker(ticket.id, child.id)
            created.append(started)
        if created:
            keys = ", ".join(c.key for c in created)
            await office.tickets.add_comment(ticket.id, f"🧩 Tạo {len(created)} task từ breakdown: {keys}", author_kind="system", author_id=None)
        return created

    async def children_open(self, ticket: Ticket) -> list[str]:
        rows = await self.office.db.fetchall(
            "SELECT key FROM tickets WHERE parent_id = ? AND status NOT IN (?, ?) ORDER BY id", (ticket.id, TicketStatus.DONE.value, TicketStatus.CANCELLED.value)
        )
        return [str(r["key"]) for r in rows]

    # ---- after approval: the world moves on ------------------------------------------------------------------

    async def react(self, ticket: Ticket) -> list[dict[str, Any]]:
        """An approved document that no longer matches: what it was written from has a newer approved revision that
        changes an item it traces to ("lệch"), or the code it cites changed ("cũ"). The pack's `on_change` says what
        happens; each cause is acted on once. Returns what was done."""
        office = self.office
        if not self.applies(ticket) or ticket.status == TicketStatus.CANCELLED:
            return []
        approved_docs = [d for d in await office.tickets.list_documents(ticket.id) if self.doctype_of(d.key) and d.approved_revision == d.revision]
        if not approved_docs:
            return []  # nothing approved, or the author is already working on a newer revision
        ds = await self.docset(ticket)
        baselines = await self._baselines(ticket, ds)
        approved = await self._approved(ds, baselines)
        evidence = await self.evidence(ticket, ds)
        findings = run_rules(self.pack, ds.docs, evidence, baselines, approved, only_docs=set(ds.own))
        tpl = await office.teams.template(ticket)
        done: list[dict[str, Any]] = []
        for doc in approved_docs:
            key = self.core_key(ticket, doc.key)
            state = match_state(self.pack, key, ds.docs[key], [f for f in findings if f.doc == key], baselines.get(key), approved, [e for e in evidence if e.doc == key])
            if state == MatchState.LECH:
                kind, action = "lech", self.pack.on_change.upstream_changed.get("when_item_affected", "revise")
                cause = ",".join(f"{k}@{approved[k].revision}" for k in sorted(baselines[key].docs) if k in approved)
                detail = "; ".join(f.message for f in findings if f.doc == key and f.rule == "R-BASELINE")[:900]
                what = f"tài liệu nó dựa trên đã có bản duyệt mới. {detail}"
            elif state == MatchState.CU:
                kind, action = "cu", self.pack.on_change.evidence_stale.get("after_approval", "notify_author")
                stale = [e for e in evidence if e.doc == key and e.current_hash and e.current_hash != e.content_hash]
                cause = content_hash("|".join(sorted(f"{e.item}:{e.source}:{e.current_hash}" for e in stale)))[:16]
                what = "code được trích đã đổi ở " + ", ".join(sorted({f"{e.item} ({e.source})" for e in stale}))[:900]
            else:
                continue
            if action == "ignore":
                continue
            fresh = await office.db.execute(
                "INSERT OR IGNORE INTO doc_notices(ticket_id, doc_key, kind, cause, action, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ticket.id, doc.key, kind, cause, action, utcnow()),
            )
            if not fresh.rowcount:
                continue  # already announced for this very cause
            label = MATCH_STATE_VI.get(state, state.value)
            stage = next((s.id for s in (tpl.stages if tpl else []) if doc.key in {o for out in s.outputs for o in out.split("|")}), None)
            if action in ("revise", "reopen_gate") and stage is not None:
                await office.stages.reopen(ticket.id, stage, reason=f"Tài liệu `{doc.key}` đã {label}: {what}", by="office")
            else:
                await office.tickets.add_comment(
                    ticket.id, f"⚠️ Tài liệu `{doc.key}` (bản {doc.revision}, đã duyệt) hiện {label}: {what}. Xem lại và cập nhật nếu cần.", author_kind="system", author_id=None
                )
                action = "notify_author"
            office.bus.publish("doc.state", ticket_id=ticket.id, key=doc.key, state=state.value, action=action)
            done.append({"ticket": ticket.key, "doc": doc.key, "state": state.value, "action": action})
        return done

    async def sweep(self, *, around: Ticket | None = None) -> list[dict[str, Any]]:
        """React on every ticket of a pack flow that has an approved document (or only on `around`, its parent and its
        children: right after an approval). The timer runs the full sweep to notice code that changed."""
        office = self.office
        if around is not None:
            ids = {around.id} | {int(r["id"]) for r in await office.db.fetchall("SELECT id FROM tickets WHERE parent_id = ?", (around.id,))}
            if around.parent_id is not None:
                ids.add(around.parent_id)
        else:
            rows = await office.db.fetchall(
                "SELECT DISTINCT t.id FROM tickets t JOIN documents d ON d.ticket_id = t.id WHERE d.approved_revision IS NOT NULL AND t.status <> ?",
                (TicketStatus.CANCELLED.value,),
            )
            ids = {int(r["id"]) for r in rows}
        done: list[dict[str, Any]] = []
        for ticket_id in sorted(ids):
            try:
                done += await self.react(await office.tickets.get(ticket_id))
            except OfficeError:
                continue
        return done

    async def run_forever(self, every_s: float) -> None:
        import asyncio
        import logging

        while True:
            await asyncio.sleep(every_s)
            try:
                await self.sweep()
            except Exception:  # a sweep that breaks must not stop the next one
                logging.getLogger(__name__).exception("doc sweep failed")

    async def epic_grew(self, ticket: Ticket, note: str, *, by: str) -> Ticket:
        """The BA adds to an epic that is already past its analysis: the addition is recorded on the ticket and the
        epic goes back to `analyze`, where only what is new is analysed (ids already given never change). The tasks
        that exist stay; the next approved breakdown creates only the new ones."""
        note = note.strip()
        if not note:
            raise DocError("Cần nội dung bổ sung")
        office = self.office
        tpl = await office.teams.template(ticket)
        if not self.applies(ticket) or tpl is None or not tpl.stages or spawn_of(ticket.stage_template, "breakdown") is None:
            raise DocError(f"{ticket.key} không phải epic")
        first = tpl.stages[0].id
        name = (await office.teams.find_user(by)).name if await office.teams.find_user(by) else by
        description = f"{ticket.description.rstrip()}\n\n## Bổ sung {utcnow()[:10]} ({name})\n{note}"
        async with office.db.tx() as tx:
            await tx.execute("UPDATE tickets SET description = ?, updated_at = ? WHERE id = ?", (description, utcnow(), ticket.id))
        await office.sources.sync(ticket.id)
        if ticket.stage == first:
            await office.tickets.add_comment(ticket.id, f"➕ {name} bổ sung epic: {note[:500]}", author_kind="change", author_id=by)
            return await office.tickets.get(ticket.id)
        return await office.stages.reopen(ticket.id, first, reason=f"{name} bổ sung epic: {note[:500]}", by=by)

    # ---- hooks -----------------------------------------------------------------------------------------------

    async def on_saved(self, ticket: Ticket, stored_key: str) -> None:
        if self.doctype_of(stored_key) is not None:
            await self.capture_baseline(ticket, stored_key)

    async def on_approved(self, ticket: Ticket, item: Interaction) -> None:
        """A gate approved these revisions: remember what they were written from."""
        for doc in item.payload.get("docs", []):
            await self.office.db.execute(
                "UPDATE documents SET approved_baseline_json = baseline_json WHERE ticket_id = ? AND key = ?", (ticket.id, doc["key"])
            )

    @staticmethod
    def summary(findings: list[Finding], limit: int = 12) -> str:
        errors = [f for f in findings if f.status == "open" and f.severity == "error"]
        lines = [f"- [{f.id[:8]}] {f.rule} · {f.doc}{' · ' + f.item if f.item else ''}: {f.message}" for f in errors[:limit]]
        more = f"\n- … và {len(errors) - limit} điểm nữa" if len(errors) > limit else ""
        return "\n".join(lines) + more

    @staticmethod
    def dump(findings: list[Finding]) -> list[dict[str, Any]]:
        return [json.loads(f.model_dump_json()) for f in findings]

