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
import logging
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

from actoroffice.doccore import LINK_VALID, Approved, Baseline, Evidence, Finding, MatchState, ParsedDoc, StandardPack, impact, link_target, match_state, parse, run_rules
from actoroffice.doccore.model import MATCH_STATE_VI, SourceRef, finding_id, make_finding
from actoroffice.domain.errors import NotFound, OfficeError, PolicyDenied
from actoroffice.domain.ids import utcnow
from actoroffice.domain.stage import StageDef
from actoroffice.domain.team import LOCAL_OWNER_ID
from actoroffice.domain.work import Document, Interaction, Ticket, TicketStatus

from .docflow import DocProfile, PackError, check_profile, default_pack, is_pack_flow, resolve, same_road, spawn_of

if TYPE_CHECKING:
    from actoroffice.app import Office

log = logging.getLogger(__name__)

EXCERPT_LIMIT = 4000
SPEC_PER_TASK = "R-SPEC-PER-TASK"
UPSTREAM_EDITED = "R-UPSTREAM-EDITED"
DEP_READY = "R-DEP-READY"  # a warning: what this ticket depends on has no approved document of the kind being written
MIN_ID_PREFIX = 8  # the length people see in a findings list


class DocError(OfficeError):
    code = 400


@dataclass
class DocSet:
    """The documents one check looks at."""

    docs: dict[str, ParsedDoc]  # core key → parsed document
    own: dict[str, Document]  # core key → the stored document of this ticket
    stored: dict[str, tuple[int, str]]  # core key → (ticket id, stored key), also for the parent's documents
    # core key → current revision of an own document that is read here at its approved revision, because somebody
    # rewrote it outside the stage that writes it
    edited: dict[str, int] = field(default_factory=dict)
    # core keys of the documents of tickets this one is only linked to (what it depends on): there to check
    # `<ticket>/<id>` references and to notice their changes, never what a bare id means
    far: set[str] = field(default_factory=set)


@dataclass
class SpawnStep:
    """What approving a breakdown does with one of its tasks."""

    item: Any  # the breakdown item (doccore Item)
    action: str  # "create" | "resume" (a child that was created but never started) | "sync" | "drop" | "skip"
    child: Ticket | None = None
    flow: str = ""
    user_id: str | None = None


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _is_output(stored_key: str, wanted: set[str]) -> bool:
    """A stage writes its outputs, and a breakdown stage the spec of every task."""
    return stored_key in wanted or ("breakdown" in wanted and stored_key.startswith("spec."))


class DocService:
    def __init__(self, office: Office) -> None:
        self.office = office
        self._profiles: dict[str, DocProfile] = {}  # scope → what that layer changes of the standard
        self._parents: dict[int, int] = {}  # ticket id → parent id, to find the epic a ticket is under without a query

    @property
    def pack(self) -> StandardPack:
        """The office's standard: what a ticket follows when no nearer layer says otherwise."""
        return self.pack_of(None)

    # ---- which standard a ticket follows: office → team → project → ticket (an epic and what is under it) --------

    async def load_profiles(self) -> None:
        office = self.office
        self._profiles = {}
        for row in await office.db.fetchall("SELECT scope, profile_json FROM doc_profiles"):
            try:
                self._profiles[str(row["scope"])] = DocProfile.model_validate_json(row["profile_json"])
            except ValueError:
                log.warning("doc profile of %s is not readable, ignored", row["scope"])
        self._parents = {int(r["id"]): int(r["parent_id"]) for r in await office.db.fetchall("SELECT id, parent_id FROM tickets WHERE parent_id IS NOT NULL")}

    def _scopes(self, ticket: Ticket | None) -> list[str]:
        scopes = ["office"]
        if ticket is None:
            return scopes
        if ticket.team_id:
            scopes.append(f"team:{ticket.team_id}")
        if ticket.project_id:
            scopes.append(f"project:{ticket.project_id}")
        if ticket.parent_id is not None:
            self._parents[ticket.id] = ticket.parent_id
        chain, parent = [ticket.id], ticket.parent_id
        while parent is not None and parent not in chain:
            chain.append(parent)
            parent = self._parents.get(parent)
        return scopes + [f"ticket:{ticket_id}" for ticket_id in reversed(chain)]  # the epic first, the ticket itself last

    def layers(self, ticket: Ticket | None) -> list[tuple[str, DocProfile]]:
        return [(scope, self._profiles[scope]) for scope in self._scopes(ticket) if scope in self._profiles]

    def pack_of(self, ticket: Ticket | None) -> StandardPack:
        layers = [profile for _, profile in self.layers(ticket)] if self._profiles else []
        if not layers:
            return default_pack()
        try:
            return resolve(layers, self.office.settings.data_dir)
        except PackError as exc:  # a pack that was removed after the profile was saved: work goes on with the office's
            log.warning("pack of %s cannot be loaded, using the office's: %s", ticket.key if ticket else "office", exc)
            return default_pack()

    async def _before(self, scope: str) -> list[DocProfile]:
        """The layers under a scope, in order."""
        office = self.office
        kind, _, ref = scope.partition(":")
        if kind == "office":
            return []
        if kind == "team":
            if await office.db.fetchone("SELECT 1 FROM teams WHERE id = ?", (ref,)) is None:
                raise NotFound(f"Không có team '{ref}'")
            scopes = ["office"]
        elif kind == "project":
            project = await office.projects.get(ref)
            scopes = ["office"] + ([f"team:{project.team_id}"] if project.team_id else [])
        elif kind == "ticket":
            ticket = await office.tickets.get(int(ref))
            scopes = [s for s in self._scopes(ticket) if s != f"ticket:{ticket.id}"]
        else:
            raise DocError("scope phải là 'office', 'team:<id>', 'project:<id>' hoặc 'ticket:<mã>'")
        return [self._profiles[s] for s in scopes if s in self._profiles]

    async def _scope_key(self, scope: str) -> str:
        kind, _, ref = scope.partition(":")
        if kind == "ticket" and not ref.isdigit():
            return f"ticket:{(await self.office.tickets.get(ref)).id}"
        return scope

    async def save_profile(self, scope: str, profile: DocProfile | dict[str, Any] | None, *, by: str = "user") -> DocProfile | None:
        """Set what one layer changes of the standard (None or an empty profile removes the layer). It is checked on
        top of the layers under it first: a pack that does not load or a rule that does not exist is refused and what
        was in use stays. Takes effect on the next check, no restart."""
        office = self.office
        scope = await self._scope_key(scope.strip())
        before = await self._before(scope)
        try:
            new = profile if isinstance(profile, DocProfile) else DocProfile.model_validate(profile or {})
            if not new.empty():
                check_profile(new, before, office.settings.data_dir)
        except PackError as exc:
            raise DocError(str(exc)) from None
        except ValueError as exc:
            raise DocError(f"Hồ sơ tài liệu không hợp lệ: {exc}") from None
        if new.empty():
            await office.db.execute("DELETE FROM doc_profiles WHERE scope = ?", (scope,))
            self._profiles.pop(scope, None)
        else:
            await office.db.execute(
                "INSERT INTO doc_profiles(scope, profile_json, updated_by, updated_at) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(scope) DO UPDATE SET profile_json = excluded.profile_json, updated_by = excluded.updated_by, updated_at = excluded.updated_at",
                (scope, new.model_dump_json(), by, utcnow()),
            )
            self._profiles[scope] = new
        await office.audit.record("doc:profile", "removed" if new.empty() else "saved", target=scope, detail={"by": by, **new.model_dump()})
        office.bus.publish("doc.profile", scope=scope)
        return None if new.empty() else new

    async def profile_view(self, scope: str) -> dict[str, Any]:
        """For the settings page: this layer, the layers under it, and the pack they give together."""
        scope = await self._scope_key(scope.strip())
        before = await self._before(scope)
        own = self._profiles.get(scope)
        try:
            pack = resolve([*before, *([own] if own else [])], self.office.settings.data_dir) if before or own else default_pack()
        except PackError:
            pack = default_pack()
        packs_dir = self.office.settings.data_dir / "packs"
        return {
            "scope": scope,
            "profile": own.model_dump() if own else None,
            "inherited": [p.model_dump() for p in before],
            "pack": {"name": pack.name, "version": pack.version, "flows": list(pack.flows)},
            "rules": [{"id": rule_id, "type": rule.type, "severity": rule.severity} for rule_id, rule in pack.rules.items()],
            "packs": ["default", *sorted(p.name for p in packs_dir.iterdir() if (p / "pack.yaml").is_file())] if packs_dir.is_dir() else ["default"],
        }

    # ---- keys ------------------------------------------------------------------------------------------------

    def doctype_of(self, stored_key: str, pack: StandardPack | None = None) -> str | None:
        base = stored_key.split(".", 1)[0]
        return base if base in (pack or self.pack).doc_types else None

    def core_key(self, ticket: Ticket, stored_key: str) -> str:
        base, _, item = stored_key.partition(".")
        return f"{ticket.key}.{item}/{base}" if item else f"{ticket.key}/{base}"

    def applies(self, ticket: Ticket) -> bool:
        return is_pack_flow(ticket.stage_template, self.pack_of(ticket))

    def spawn_of(self, ticket: Ticket, stage_id: str | None) -> Any:
        return spawn_of(ticket.stage_template, stage_id, self.pack_of(ticket))

    def template_text(self, doctype: str, ticket: Ticket | None = None) -> str:
        pack = self.pack_of(ticket)
        if doctype not in pack.doc_types:
            raise NotFound(f"Không có loại tài liệu '{doctype}'. Có: {', '.join(pack.doc_types)}")
        return pack.template_text(doctype)

    # ---- what a check looks at -------------------------------------------------------------------------------

    async def docset(self, ticket: Ticket, *, linked: bool = False) -> DocSet:
        """The documents the ticket's current stage writes at their current revision; its other documents and its
        parent's at the approved one (the current one when nothing is approved yet): what the stage's documents are
        written from. An approved document rewritten outside the stage that writes it is still read as approved, and
        noted in `edited`. `linked`: also the documents of the tickets it depends on or names an item of
        (`LinkService.upstream`), noted in `far`: what the rules need to check `AO-14/C-2` and to see it change."""
        office = self.office
        docs: dict[str, ParsedDoc] = {}
        own: dict[str, Document] = {}
        stored: dict[str, tuple[int, str]] = {}
        edited: dict[str, int] = {}
        pack = self.pack_of(ticket)
        writes = await self.stage_outputs(ticket)
        for doc in await office.tickets.list_documents(ticket.id):
            doctype = self.doctype_of(doc.key, pack)
            if doctype is None:
                continue
            key = self.core_key(ticket, doc.key)
            if doc.approved_revision and doc.approved_revision != doc.revision and not _is_output(doc.key, writes):
                body = await self._body(ticket.id, doc.key, doc.approved_revision)
                if body != doc.body:
                    edited[key] = doc.revision
                doc = doc.model_copy(update={"revision": doc.approved_revision, "body": body})
            docs[key] = parse(doc.body, doctype, pack)
            own[key] = doc
            stored[key] = (ticket.id, doc.key)
        if ticket.parent_id is not None:
            parent = await office.tickets.get(ticket.parent_id)
            for doc in await office.tickets.list_documents(parent.id):
                doctype = self.doctype_of(doc.key, pack)
                if doctype is None or "." in doc.key:  # the specs of the other tasks are not this task's business
                    continue
                key = self.core_key(parent, doc.key)
                body = await self._body(parent.id, doc.key, doc.approved_revision) if doc.approved_revision else doc.body
                docs[key] = parse(body, doctype, pack)
                stored[key] = (parent.id, doc.key)
        far: set[str] = set()
        if linked:
            for other in await office.links.upstream(ticket):
                if other.id == ticket.parent_id:
                    continue
                for doc in await office.tickets.list_documents(other.id):
                    doctype = self.doctype_of(doc.key, pack)
                    key = self.core_key(other, doc.key)
                    if doctype is None or "." in doc.key or key in docs:
                        continue
                    body = await self._body(other.id, doc.key, doc.approved_revision) if doc.approved_revision else doc.body
                    docs[key] = parse(body, doctype, pack)
                    stored[key] = (other.id, doc.key)
                    far.add(key)
        return DocSet(docs, own, stored, edited, far)

    async def stage_outputs(self, ticket: Ticket) -> set[str]:
        """The stored keys the ticket's current stage writes (empty when it is at no stage)."""
        tpl = await self.office.teams.template(ticket) if ticket.stage else None
        stage = tpl.stage(ticket.stage) if tpl is not None else None
        return {o for out in stage.outputs for o in out.split("|")} if stage is not None else set()

    async def guard_put(self, ticket: Ticket, stored_key: str, body: str) -> None:
        """Whether the AI may save this document now (`doc_put`). Refused, with what to do instead:
        - a document of the pack that is approved and is not written by the ticket's current stage: what other
          documents were approved against cannot be rewritten under them (saving the approved text back is allowed);
        - a document that is on a pending approval card of the ticket: the approver must get what the card showed.
          Refusing keeps the flow simplest: nothing is withdrawn behind the approver's back, and the AI has one way to
          change it (reopen the stage, which withdraws the card).
        Documents that are not of the pack (dossier, handoff…) and tickets of other workflows are not looked at."""
        if not self.applies(ticket) or self.doctype_of(stored_key, self.pack_of(ticket)) is None:
            return
        office = self.office
        doc = await office.tickets.get_document(ticket.id, stored_key)
        if doc is None:
            return
        for card in await office.interactions.pending(ticket_id=ticket.id):
            if card.kind.value == "approve_stage" and any(d.get("key") == stored_key for d in card.payload.get("docs", [])):
                raise DocError(
                    f"Tài liệu `{stored_key}` đang chờ duyệt (thẻ {card.payload.get('stage_title') or card.payload.get('stage')}): không sửa khi thẻ đang mở. "
                    f"Chờ người duyệt trả lời; nếu phải sửa ngay, dùng stage_reopen về giai đoạn '{ticket.stage}' (thẻ bị rút), sửa rồi stage_complete lại."
                )
        if not doc.approved_revision or _is_output(stored_key, await self.stage_outputs(ticket)):
            return
        if body == await self._body(ticket.id, stored_key, doc.approved_revision):
            return  # putting the approved text back
        raise DocError(
            f"Tài liệu `{stored_key}` đã được duyệt (bản {doc.approved_revision}) và không thuộc giai đoạn hiện tại ('{ticket.stage}'): không sửa ở đây. "
            "Nếu nội dung phải đổi: dùng stage_reopen về giai đoạn viết tài liệu đó, hoặc hỏi người phụ trách bằng ask_user."
        )

    async def _approved_body(self, doc: Document) -> str:
        """The text people approved (the current text when nothing is approved yet)."""
        if doc.approved_revision and doc.approved_revision != doc.revision:
            return await self._body(doc.ticket_id, doc.key, doc.approved_revision)
        return doc.body

    async def _body(self, ticket_id: int, stored_key: str, revision: int | None) -> str:
        row = await self.office.db.fetchone(
            "SELECT body FROM document_revisions WHERE ticket_id = ? AND key = ? AND revision = ?", (ticket_id, stored_key, revision)
        )
        return str(row["body"]) if row else ""

    # ---- baseline --------------------------------------------------------------------------------------------

    async def capture_baseline(self, ticket: Ticket, stored_key: str) -> Baseline | None:
        """Record what the current revision of a document is written from: the revisions of the documents its type
        is based on, as they are now. Called when the document is saved."""
        pack = self.pack_of(ticket)
        doctype = self.doctype_of(stored_key, pack)
        if doctype is None:
            return None
        office = self.office
        wanted = set(pack.doc_types[doctype].based_on)
        found: dict[str, int] = {}
        chain = [ticket] + ([await office.tickets.get(ticket.parent_id)] if ticket.parent_id is not None else [])
        for owner in chain:
            for doc in await office.tickets.list_documents(owner.id):
                if doc.key in wanted and not any(k.endswith("/" + doc.key) for k in found):
                    # the nearest one: a task's own spec before the epic's
                    found[self.core_key(owner, doc.key)] = doc.approved_revision or doc.revision
        for other in await office.links.upstream(ticket):  # what it depends on: a change there must reach this document
            if other.id == ticket.parent_id:
                continue
            for doc in await office.tickets.list_documents(other.id):
                if self.doctype_of(doc.key, pack) is not None and "." not in doc.key:
                    found.setdefault(self.core_key(other, doc.key), doc.approved_revision or doc.revision)
        baseline = Baseline(docs=found)
        await office.db.execute("UPDATE documents SET baseline_json = ? WHERE ticket_id = ? AND key = ?", (baseline.model_dump_json(), ticket.id, stored_key))
        return baseline

    async def _baselines(self, ticket: Ticket, ds: DocSet) -> dict[str, Baseline]:
        out: dict[str, Baseline] = {}
        for key, doc in ds.own.items():
            row = await self.office.db.fetchone("SELECT baseline_json, approved_baseline_json FROM documents WHERE ticket_id = ? AND key = ?", (ticket.id, doc.key))
            if row and key in ds.edited and row["approved_baseline_json"]:  # read at its approved revision: what that one was written from
                out[key] = Baseline.model_validate_json(row["approved_baseline_json"])
            elif row and row["baseline_json"]:
                out[key] = Baseline.model_validate_json(row["baseline_json"])
            else:  # saved before Doc Core, or by a path that does not capture: written from what is there now
                captured = await self.capture_baseline(ticket, doc.key)
                if captured is not None:
                    out[key] = captured
        return out

    async def _approved(self, ticket: Ticket, ds: DocSet, baselines: dict[str, Baseline]) -> dict[str, Approved]:
        """For every document something was written from: its latest approved revision, and which items changed
        between each revision a document pinned and that one."""
        office = self.office
        pack = self.pack_of(ticket)
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
            doctype = self.doctype_of(stored_key, pack) or ""
            latest = parse(await self._body(ticket_id, stored_key, doc.approved_revision), doctype, pack)
            changed: dict[int, list[str]] = {}
            for revision in revisions:
                if revision == doc.approved_revision:
                    changed[revision] = []
                    continue
                old_body = await self._body(ticket_id, stored_key, revision)
                changed[revision] = sorted(impact(parse(old_body, doctype, pack) if old_body else None, latest))
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
        pack = self.pack_of(ticket)
        for key, doc in ds.own.items():
            earlier_items: dict[int, ParsedDoc] = {}  # revision → that revision parsed, to see whether an item was rewritten
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
                        # an earlier revision already pinned the same source for this item: keep that pin while the item
                        # reads the same, so editing another part of the document does not make changed code look
                        # fresh. An item the author rewrote was written against the source as it is now.
                        earlier = await office.db.fetchone(
                            "SELECT * FROM doc_evidence WHERE ticket_id = ? AND doc_key = ? AND item_id = ? AND source = ? AND error IS NULL AND revision < ? "
                            "ORDER BY revision DESC LIMIT 1",
                            (ticket.id, doc.key, item.id, src.raw, doc.revision),
                        )
                        pin = dict(earlier) if earlier is not None and not current.get("error") else None
                        if pin is not None:
                            revision = int(pin["revision"])
                            if revision not in earlier_items:
                                earlier_items[revision] = parse(await self._body(ticket.id, doc.key, revision), self.doctype_of(doc.key, pack) or "", pack)
                            before = earlier_items[revision].item(item.id)
                            if before is None or before.hash != item.hash:
                                pin = None
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
        return (await self._check(ticket, stage, keys))[0]

    async def _check(self, ticket: Ticket, stage: StageDef | None, keys: set[str] | None = None) -> tuple[list[Finding], dict[str, str | None]]:
        """The findings, and for each what an acceptance of it is about (`_about`)."""
        ds = await self.docset(ticket, linked=True)
        only = keys if keys is not None else (self.output_keys(ticket, stage, ds) if stage is not None else set(ds.own))
        baselines = await self._baselines(ticket, ds)
        pack = self.pack_of(ticket)
        evidence = await self.evidence(ticket, ds)
        findings = run_rules(pack, ds.docs, evidence, baselines, await self._approved(ticket, ds, baselines), only_docs=only, far=ds.far)
        findings = [f for f in findings if not self._parents_cover(pack, ds, f)]
        findings += self._specs_per_task(ticket, ds, only)
        findings += self._upstream_edited(ds)
        findings += await self._links_check(ticket, ds, only)
        about = {f.id: self._about(pack, ds, evidence, f) for f in findings}
        accepted = {
            r["finding_id"]: r for r in await self.office.db.fetchall("SELECT * FROM doc_findings WHERE ticket_id = ?", (ticket.id,))
        }
        for f in findings:
            hit = accepted.get(f.id)
            # an acceptance is about the item as it read then: a rewritten item (or a source that changed again) is looked at anew
            if hit is not None and (hit["item_hash"] is None or hit["item_hash"] == about[f.id]):
                f.status, f.accepted_by, f.reason = "accepted", hit["accepted_by"], hit["reason"]
        return findings, about

    def _parents_cover(self, pack: StandardPack, ds: DocSet, f: Finding) -> bool:
        """A cover finding that asks a document of this ticket to cover every item of a parent's document. A task
        answers for its own part only; the whole is checked where the parent's documents are written (the epic's gate)."""
        rule = pack.rules.get(f.rule)
        if rule is None or rule.type != "cover" or f.doc not in ds.own:
            return False
        return any(f.id == finding_id(f.rule, f.doc, f.item, key) for key in ds.docs if key not in ds.own)

    def _upstream_edited(self, ds: DocSet) -> list[Finding]:
        """An approved document rewritten outside the stage that writes it: the check keeps reading the approved
        revision, and says so until the rewrite is approved or undone."""
        out = []
        for key, current in sorted(ds.edited.items()):
            approved = ds.own[key].revision
            message = (
                f"{key}: bản đã duyệt là {approved} nhưng tài liệu đã bị sửa thành bản {current} ngoài giai đoạn viết nó; việc kiểm vẫn dùng bản {approved}. "
                f"Lưu lại đúng nội dung bản {approved}, hoặc mở lại giai đoạn viết tài liệu này (stage_reopen) để bản mới được duyệt."
            )
            out.append(make_finding(UPSTREAM_EDITED, "error", key, None, message, f"{approved}>{current}"))
        return out

    def _about(self, pack: StandardPack, ds: DocSet, evidence: list[Evidence], f: Finding) -> str | None:
        """What an acceptance of this finding is about: the text of its item and, for a source that changed, the
        content it changed to. None when the finding is about no item."""
        parts: list[str] = []
        ticket_key, _, target = (f.item or "").rpartition("/")
        if target:
            near = [f.doc, *(k for k in ds.docs if k != f.doc)] if not ticket_key else [k for k in ds.docs if k.rsplit("/", 1)[0] == ticket_key]
            item = next((ds.docs[k].item(target) for k in near if k in ds.docs and ds.docs[k].item(target)), None)
            if item is not None:
                parts.append(item.hash)
        rule = pack.rules.get(f.rule)
        if rule is not None and rule.type == "fresh":
            parts += [e.current_hash or "" for e in evidence if f.id == finding_id(f.rule, e.doc, e.item, e.source)]
        return content_hash("|".join(parts)) if parts else None

    async def _links_check(self, ticket: Ticket, ds: DocSet, only: set[str]) -> list[Finding]:
        """What the core cannot know about typed links: a ticket a link names must exist (error), and a ticket this
        one depends on should already have an approved document of the kind being written (a warning: the dependent
        document is looked at again when that one is approved)."""
        office = self.office
        pack = self.pack_of(ticket)
        out: list[Finding] = []
        known: dict[str, bool] = {}
        for key in sorted(only & set(ds.own)):
            parsed = ds.docs[key]
            holders = [(item.id, item.relations) for item in parsed.items] + [("", parsed.relations)]
            holders += [(item.id, {"trace": item.traces, "changes": item.changes}) for item in parsed.items]
            for holder, relations in holders:
                for relation, refs in relations.items():
                    for ref in refs:
                        name = link_target(pack, ref)[0] if relation not in ("trace", "changes") else ref.rpartition("/")[0]
                        if not name or "." in name:
                            continue
                        if name not in known:
                            try:
                                await office.tickets.get(name)
                                known[name] = True
                            except NotFound:
                                known[name] = False
                        if not known[name]:
                            word = (pack.syntax.relations.get(relation) or getattr(pack.syntax, relation, None) or [relation])[0]
                            where = f"của {holder}" if holder else "của tài liệu"
                            message = f'{key}: dòng "{word}" {where} nhắc {ref} nhưng văn phòng không có ticket {name}. Sửa lại mã ticket, hoặc bỏ nó khỏi dòng {word}.'
                            out.append(make_finding(LINK_VALID, "error", key, holder or None, message, f"{relation}:{ref}"))
        for link in await office.links.of(ticket):
            other = link.other
            if not link.outgoing or link.relation != "depends_on" or other.status in (TicketStatus.DONE, TicketStatus.CANCELLED):
                continue
            tpl = await office.teams.template(other) if other.stage_template else None
            writes = {o for s in (tpl.stages if tpl else []) for out_ in s.outputs for o in out_.split("|")}
            theirs = {d.key: d for d in await office.tickets.list_documents(other.id)}
            for key in sorted(only & set(ds.own)):
                name = ds.own[key].key
                if "." in name or name not in writes or (name in theirs and theirs[name].approved_revision):
                    continue
                state = f"mới ở bản {theirs[name].revision}, chưa duyệt" if name in theirs else "chưa viết"
                message = (
                    f"{key}: ticket này phụ thuộc {other.key} ({other.title[:60]}) nhưng `{name}` của {other.key} {state}. Điều {other.key} chốt có thể buộc "
                    f"tài liệu này phải sửa: khi `{name}` của {other.key} được duyệt, tài liệu này sẽ được kiểm lại."
                )
                out.append(make_finding(DEP_READY, "warn", key, None, message, other.key))
        return out

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
        ds = await self.docset(ticket, linked=True)
        findings = findings if findings is not None else await self.check(ticket)
        baselines = await self._baselines(ticket, ds)
        approved = await self._approved(ticket, ds, baselines)
        evidence = await self.evidence(ticket, ds)
        pack = self.pack_of(ticket)
        return {
            doc.key: match_state(pack, key, ds.docs[key], [f for f in findings if f.doc == key], baselines.get(key), approved, [e for e in evidence if e.doc == key])
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
        wanted = finding_id.strip()
        found, about = await self._check(ticket, stage)
        found += await office.crosscheck.findings(ticket)
        # the exact id, or the short one people see in a list when it names one finding only
        matches = [f for f in found if f.id == wanted] or ([f for f in found if f.id.startswith(wanted)] if len(wanted) >= MIN_ID_PREFIX else [])
        if len(matches) > 1:
            raise DocError(f"Mã '{wanted}' trùng {len(matches)} phát hiện của {ticket.key}: dùng mã đầy đủ")
        if not matches:
            raise NotFound(f"Không có phát hiện '{wanted}' ở giai đoạn này của {ticket.key} (cần mã đầy đủ hoặc ít nhất {MIN_ID_PREFIX} ký tự đầu)")
        finding = matches[0]
        policy = await office.teams.gate_policy(ticket, stage.gate) if stage.gate else None
        roles = (policy.accept_findings or policy.roles) if policy is not None else []
        if by != LOCAL_OWNER_ID and not await office.teams.can_act(by, ticket, roles) and not await self._asked_to_approve(ticket, by, policy):
            raise PolicyDenied(f"Cần vai {' / '.join(roles) or 'người duyệt'} để chấp nhận điểm lệch")
        await office.db.execute(
            "INSERT INTO doc_findings(ticket_id, finding_id, rule, doc_key, item_id, message, accepted_by, reason, accepted_at, item_hash) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ticket_id, finding_id) DO UPDATE SET accepted_by = excluded.accepted_by, reason = excluded.reason, accepted_at = excluded.accepted_at, "
            "item_hash = excluded.item_hash",
            (ticket.id, finding.id, finding.rule, finding.doc, finding.item, finding.message, by, reason[:1000], utcnow(), about.get(finding.id)),
        )
        await office.audit.record("doc:finding", "accepted", ticket_id=ticket.id, target=f"{ticket.key}:{finding.rule}", detail={"by": by, "reason": reason, "item": finding.item})
        await office.tickets.add_comment(ticket.id, f"☑️ Chấp nhận điểm lệch {finding.rule} ({finding.item or finding.doc}): {reason}", author_kind="system", author_id=None)
        finding.status, finding.accepted_by, finding.reason = "accepted", by, reason
        return finding

    async def _asked_to_approve(self, ticket: Ticket, by: str, policy: Any) -> bool:
        """The person the gate card of the ticket's stage was sent to (the owner when nobody holds the role). Only
        where accepting is the approvers' business, and never the author's card of the submit phase."""
        if policy is None or (policy.accept_findings and set(policy.accept_findings) != set(policy.roles)):
            return False
        for card in await self.office.interactions.pending(ticket_id=ticket.id):
            if card.kind.value != "approve_stage" or card.payload.get("stage") != ticket.stage or card.payload.get("phase") == "submit":
                continue
            if by in (card.payload.get("approver_ids") or []):
                return True
        return False

    # ---- the gate card ---------------------------------------------------------------------------------------

    async def approval_view(self, ticket: Ticket, stage: StageDef) -> dict[str, Any]:
        """What the approver needs next to the documents, so that nobody has to trace by hand: the trace table,
        assumptions, declared changes with the old code, what was accepted, what each document was written from."""
        ds = await self.docset(ticket)
        keys = self.output_keys(ticket, stage, ds)
        # what the rules found and what the AI cross-check still sees: a document with an open difference is not "khớp"
        findings = await self.check(ticket, stage) + await self.office.crosscheck.findings(ticket)
        states = await self.states(ticket, findings)
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
                    old = await self._old_text(ds, evidence, changed)
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
            "crosscheck": await self._crosscheck_state(ticket),
        }

    async def _crosscheck_state(self, ticket: Ticket) -> dict[str, Any]:
        """Whether the AI cross-check of the ticket's stage run really read the documents, from the result the check
        recorded on the run: a round that broke is "skipped" with the reason, and nobody compared the meaning."""
        srun = await self.office.stages.current_run(ticket)
        result = next((c for c in (srun.checks if srun else []) if c.get("id") == "crosscheck_clean"), None)
        if result is None:
            return {"ran": False, "reason": "giai đoạn này không chạy đối chiếu bằng AI"}
        ran = result.get("status") != "skipped"
        return {"ran": ran, "reason": "" if ran else str(result.get("summary") or "")}

    async def _old_text(self, ds: DocSet, evidence: list[Evidence], ref: str) -> str:
        """The pinned text of the item a `Thay đổi:` line names: an item of this ticket, or `EP-4/F-1` of its parent
        (from what the parent's own check pinned)."""
        ticket_key, _, target = ref.rpartition("/")
        own = next((e.excerpt[:600] for e in evidence if e.item == target and (not ticket_key or e.doc.rsplit("/", 1)[0] == ticket_key)), "")
        if own or not ticket_key:
            return own
        for key, (ticket_id, stored_key) in ds.stored.items():
            if key in ds.own or key.rsplit("/", 1)[0] != ticket_key or ds.docs[key].item(target) is None:
                continue
            row = await self.office.db.fetchone(
                "SELECT excerpt FROM doc_evidence WHERE ticket_id = ? AND doc_key = ? AND item_id = ? AND error IS NULL AND excerpt <> '' ORDER BY revision DESC LIMIT 1",
                (ticket_id, stored_key, target),
            )
            if row is not None:
                return str(row["excerpt"])[:600]
        return ""

    # ---- remarks on items ------------------------------------------------------------------------------------

    async def add_comments(self, ticket: Ticket, raw: Any, *, by: str, interaction_id: str | None = None) -> list[dict[str, Any]]:
        """Store what an approver wrote about single items when answering a gate card: [{doc, item, text}]."""
        saved = []
        for entry in raw if isinstance(raw, list) else []:
            text = str((entry or {}).get("text") or "").strip() if isinstance(entry, dict) else ""
            if not text:
                continue
            doc_key, item_id = str(entry.get("doc") or "").strip(), str(entry.get("item") or "").strip() or None
            cur = await self.office.db.execute(
                "INSERT INTO doc_comments(ticket_id, interaction_id, doc_key, item_id, text, by, created_at) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (ticket.id, interaction_id, doc_key, item_id, text[:2000], by, utcnow()),
            )
            saved.append({"id": int(cur.lastrowid or 0), "doc": doc_key, "item": item_id, "text": text[:2000], "by": by})
        return saved

    async def comments(self, ticket: Ticket, open_only: bool = True) -> list[dict[str, Any]]:
        """The remarks approvers left on items of the ticket's documents (only the unresolved ones by default)."""
        sql = "SELECT * FROM doc_comments WHERE ticket_id = ?" + (" AND resolved_at IS NULL" if open_only else "") + " ORDER BY id"
        return [
            {"id": r["id"], "doc": r["doc_key"], "item": r["item_id"], "text": r["text"], "by": r["by"], "at": r["created_at"],
             "interaction_id": r["interaction_id"], "resolved_at": r["resolved_at"], "reply": r["reply"]}
            for r in await self.office.db.fetchall(sql, (ticket.id,))
        ]  # fmt: skip

    async def resolve_comments(self, ticket: Ticket) -> None:
        """An approved gate closes what was still open on the ticket's documents."""
        await self.office.db.execute("UPDATE doc_comments SET resolved_at = ? WHERE ticket_id = ? AND resolved_at IS NULL", (utcnow(), ticket.id))

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
            "is_epic": self.spawn_of(ticket, "breakdown") is not None,
            "switch_to": self.switchable(ticket),
        }

    def switchable(self, ticket: Ticket) -> list[dict[str, str]]:
        """Flows of the pack this ticket can move to from where it is, keeping its place (`task-spec` ↔ `task`)."""
        from actoroffice.domain.stage import TEMPLATE_TITLES

        pack = self.pack_of(ticket)
        if ticket.status == TicketStatus.CANCELLED:
            return []
        return [
            {"template": name, "title": TEMPLATE_TITLES.get(name, name)}
            for name, stages in pack.flows.items()
            if same_road(pack, ticket.stage_template or "", name) and ticket.stage in [s.id for s in stages]
        ]

    # ---- a breakdown becomes tickets -------------------------------------------------------------------------

    async def spawn(self, ticket: Ticket, response: dict[str, Any], *, by: str) -> list[Ticket]:
        """The approved breakdown of an epic: one child ticket per task, with its spec already approved, the person
        it is handed to and the flow it follows. `response`: {"drop": ["T-3"], "flows": {"T-1": "task-spec"},
        "assign": {"T-1": "<user id or name>"}, "restore": ["T-3"]}. The epic then waits for its children.

        Everything in `response` is checked before anything is created (`spawn_plan`). A child is linked to its item in
        `doc_spawned`: one that was created but never started is started here; one that exists gets the newly approved
        text of its documents; an item dropped earlier stays dropped unless `response["restore"]` names it."""
        office = self.office
        spawn = self.spawn_of(ticket, ticket.stage)
        if spawn is None:
            return []
        created: list[Ticket] = []
        for step in await self.spawn_plan(ticket, response):
            item = step.item
            if step.action == "drop":
                await self._link(ticket.id, item.id, None, dropped=True)
            elif step.action == "sync" and step.child is not None:
                await self._sync_child(ticket, step.child, item, spawn)
            elif step.action in ("create", "resume"):
                child = step.child
                if child is None:
                    child = await office.tickets.create(
                        item.title or item.id,
                        description=f"{item.body.strip()}\n\n_Task của epic {ticket.key} (mục {item.id} của {ticket.key})._",
                        type="task",
                        project_id=ticket.project_id,
                        parent_id=ticket.id,
                        created_by=by,
                    )
                await self._link(ticket.id, item.id, child.id, dropped=False)  # from here on a failure is resumed, not repeated
                await self._give_docs(ticket, child, item, spawn)
                if step.user_id:
                    await office.teams.set_participant(child.id, step.user_id, list(spawn.assign) or ["dev"])
                started = await office.stages.start(child.id, template=step.flow, by=by, team_id=ticket.team_id, owner_user_id=ticket.owner_user_id)
                await office.tickets.add_blocker(ticket.id, child.id)
                created.append(started)
        if created:
            keys = ", ".join(c.key for c in created)
            await office.tickets.add_comment(ticket.id, f"🧩 Tạo {len(created)} task từ breakdown: {keys}", author_kind="system", author_id=None)
        await office.links.sync_family(ticket)  # what the breakdown says between its tasks now holds between their tickets
        return created

    async def spawn_plan(self, ticket: Ticket, response: dict[str, Any], *, revision: int | None = None) -> list[SpawnStep]:
        """What approving the breakdown with this response will do, task by task; nothing is written. Raises a DocError
        naming the item when the response cannot be carried out (an item the breakdown does not have, a flow that is
        not allowed, a person who does not exist), so that an approval is refused whole instead of stopping halfway.
        `revision`: the breakdown revision about to be approved (default: the approved one, else the current one)."""
        office = self.office
        spawn = self.spawn_of(ticket, ticket.stage)
        if spawn is None:
            return []
        breakdown = await office.tickets.get_document(ticket.id, "breakdown")
        if breakdown is None:
            raise DocError(f"{ticket.key} chưa có tài liệu breakdown")
        if revision:
            body = breakdown.body if revision == breakdown.revision else await self._body(ticket.id, "breakdown", revision)
        else:
            body = await self._approved_body(breakdown)
        tasks = [i for i in parse(body, "breakdown", self.pack_of(ticket)).items if i.kind == "T"]
        known = {i.id.upper() for i in tasks}
        drop = {str(d).upper() for d in response.get("drop") or []}
        restore = {str(d).upper() for d in response.get("restore") or []}
        flows = {str(k).upper(): str(v) for k, v in (response.get("flows") or {}).items() if v}
        assign = {str(k).upper(): str(v) for k, v in (response.get("assign") or {}).items() if v}
        for field_name, ids in (("drop", drop), ("restore", restore), ("flows", flows), ("assign", assign)):
            for item_id in sorted(set(ids) - known):
                raise DocError(f"{item_id}: breakdown của {ticket.key} không có mục này (trong '{field_name}'). Các mục: {', '.join(sorted(known))}")
        for item_id, flow in flows.items():
            if spawn.flows and flow not in spawn.flows:
                raise DocError(f"{item_id}: luồng '{flow}' không được phép ở đây. Chọn: {', '.join(spawn.flows)}")
        people: dict[str, str] = {}
        for item_id, person in assign.items():
            user = await office.teams.find_user(person)
            if user is None:
                raise DocError(f"{item_id}: không có người dùng '{person}'")
            people[item_id] = user.id
        linked = {str(r["item_id"]): r for r in await office.db.fetchall("SELECT * FROM doc_spawned WHERE parent_id = ?", (ticket.id,))}
        children = await office.db.fetchall("SELECT id, description FROM tickets WHERE parent_id = ?", (ticket.id,))
        taken = {int(r["child_id"]) for r in linked.values() if r["child_id"] is not None}
        default_flow = spawn.flows[0] if spawn.flows else "task"
        steps: list[SpawnStep] = []
        for item in tasks:
            row, upper = linked.get(item.id), item.id.upper()
            child_id = int(row["child_id"]) if row is not None and row["child_id"] is not None else None
            if child_id is None:  # a child created before the link existed says which item it is in its description
                child_id = next((int(c["id"]) for c in children if int(c["id"]) not in taken and f"(mục {item.id} của {ticket.key})" in str(c["description"])), None)
            child = None
            if child_id is not None and any(int(c["id"]) == child_id for c in children):
                child = await office.tickets.get(child_id)
            step = SpawnStep(item=item, action="skip", child=child, flow=flows.get(upper, default_flow), user_id=people.get(upper))
            if child is not None:
                waiting = not child.stage and child.status not in (TicketStatus.DONE, TicketStatus.CANCELLED)
                step.action = "resume" if waiting else ("skip" if child.status == TicketStatus.CANCELLED else "sync")
            elif upper in drop:
                step.action = "drop"
            elif row is None or not row["dropped"] or upper in restore:
                step.action = "create"
            steps.append(step)
        return steps

    async def _link(self, parent_id: int, item_id: str, child_id: int | None, *, dropped: bool) -> None:
        await self.office.db.execute(
            "INSERT INTO doc_spawned(parent_id, item_id, child_id, dropped, created_at) VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(parent_id, item_id) DO UPDATE SET child_id = excluded.child_id, dropped = excluded.dropped",
            (parent_id, item_id, child_id, int(dropped), utcnow()),
        )

    async def _give_docs(self, ticket: Ticket, child: Ticket, item: Any, spawn: Any) -> list[str]:
        """The child's documents are what was approved on the epic for its item (`spec.T-2` → the child's `spec`),
        approved. Returns the keys of the documents the child already had and that were replaced by a newer text."""
        office = self.office
        replaced: list[str] = []
        for doc_key in spawn.copy_docs:
            source = await office.tickets.get_document(ticket.id, f"{doc_key}.{item.id}")
            if source is None:
                continue
            body = await self._approved_body(source)
            mine = await office.tickets.get_document(child.id, doc_key)
            if mine is not None and mine.approved_revision and await self._body(child.id, doc_key, mine.approved_revision) == body:
                continue
            revision = mine.revision if mine is not None else None
            if mine is None or mine.body != body:
                revision = (await office.tickets.put_document(child.id, doc_key, body, author=source.updated_by, base_revision=revision)).revision
            await office.db.execute("UPDATE documents SET approved_revision = ? WHERE ticket_id = ? AND key = ?", (revision, child.id, doc_key))
            await self.capture_baseline(await office.tickets.get(child.id), doc_key)
            await office.db.execute("UPDATE documents SET approved_baseline_json = baseline_json WHERE ticket_id = ? AND key = ?", (child.id, doc_key))
            if mine is not None and mine.approved_revision:
                replaced.append(doc_key)
        return replaced

    async def _sync_child(self, ticket: Ticket, child: Ticket, item: Any, spawn: Any) -> None:
        """The epic's documents for a task were approved again with another text: the task gets it as a new approved
        revision and is told. What the task wrote from the old text is then found by the baseline rule: `react` sends
        an approved document back (the sweep after the approval), and a card that is waiting is withdrawn here."""
        office = self.office
        replaced = await self._give_docs(ticket, child, item, spawn)
        if not replaced:
            return
        names = ", ".join(f"`{k}`" for k in replaced)
        await office.tickets.add_comment(
            child.id, f"📝 {names} của task được cập nhật theo bản vừa duyệt lại ở {ticket.key} (mục {item.id}). Tài liệu viết từ bản cũ sẽ được kiểm lại.",
            author_kind="system", author_id=None,
        )  # fmt: skip
        child = await office.tickets.get(child.id)
        if not child.stage or child.status in (TicketStatus.DONE, TicketStatus.CANCELLED):
            return
        try:
            if await office.stages.waits_for_card(child) and await self.blocking(child, await office.stages.stage_def(child)):
                reason = f"{names} đổi theo bản vừa duyệt lại ở {ticket.key}: tài liệu đang chờ duyệt không còn khớp"
                await office.stages.reopen(child.id, child.stage, reason=reason, by="office")
        except OfficeError as exc:
            log.warning("could not withdraw the card of %s after its documents changed: %s", child.key, exc)

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
        pack = self.pack_of(ticket)
        approved_docs = [d for d in await office.tickets.list_documents(ticket.id) if self.doctype_of(d.key, pack) and d.approved_revision == d.revision]
        if not approved_docs:
            return []  # nothing approved, or the author is already working on a newer revision
        ds = await self.docset(ticket, linked=True)
        baselines = await self._baselines(ticket, ds)
        approved = await self._approved(ticket, ds, baselines)
        evidence = await self.evidence(ticket, ds)
        findings = run_rules(pack, ds.docs, evidence, baselines, approved, only_docs=set(ds.own), far=ds.far)
        tpl = await office.teams.template(ticket)
        done: list[dict[str, Any]] = []
        for doc in approved_docs:
            key = self.core_key(ticket, doc.key)
            state = match_state(pack, key, ds.docs[key], [f for f in findings if f.doc == key], baselines.get(key), approved, [e for e in evidence if e.doc == key])
            if state == MatchState.LECH:
                kind, action = "lech", pack.on_change.upstream_changed.get("when_item_affected", "revise")
                cause = ",".join(f"{k}@{approved[k].revision}" for k in sorted(baselines[key].docs) if k in approved)
                detail = "; ".join(f.message for f in findings if f.doc == key and f.rule == "R-BASELINE")[:900]
                what = f"tài liệu nó dựa trên đã có bản duyệt mới. {detail}"
            elif state == MatchState.CU:
                kind, action = "cu", pack.on_change.evidence_stale.get("after_approval", "notify_author")
                stale = [e for e in evidence if e.doc == key and e.current_hash and e.current_hash != e.content_hash]
                cause = content_hash("|".join(sorted(f"{e.item}:{e.source}:{e.current_hash}" for e in stale)))[:16]
                what = "code được trích đã đổi ở " + ", ".join(sorted({f"{e.item} ({e.source})" for e in stale}))[:900]
            else:
                continue
            if action == "ignore":
                continue
            seen = await office.db.fetchone(
                "SELECT 1 FROM doc_notices WHERE ticket_id = ? AND doc_key = ? AND kind = ? AND cause = ?", (ticket.id, doc.key, kind, cause)
            )
            if seen is not None:
                continue  # already announced for this very cause
            label = MATCH_STATE_VI.get(state, state.value)
            stage = next((s.id for s in (tpl.stages if tpl else []) if doc.key in {o for out in s.outputs for o in out.split("|")}), None)
            reopened = False
            if action in ("revise", "reopen_gate") and stage is not None:
                try:
                    await office.stages.reopen(ticket.id, stage, reason=f"Tài liệu `{doc.key}` đã {label}: {what}", by="office")
                    reopened = True
                except OfficeError as exc:  # the stage cannot be reopened from where the ticket is: people still hear about it
                    log.warning("could not reopen %s at %s for %s: %s", ticket.key, stage, doc.key, exc)
            if not reopened:
                await office.tickets.add_comment(
                    ticket.id, f"⚠️ Tài liệu `{doc.key}` (bản {doc.revision}, đã duyệt) hiện {label}: {what}. Xem lại và cập nhật nếu cần.", author_kind="system", author_id=None
                )
                action = "notify_author"
            # recorded only once the cause was acted on: an action that broke is tried again at the next sweep
            await office.db.execute(
                "INSERT OR IGNORE INTO doc_notices(ticket_id, doc_key, kind, cause, action, created_at) VALUES (?, ?, ?, ?, ?, ?)",
                (ticket.id, doc.key, kind, cause, action, utcnow()),
            )
            office.bus.publish("doc.state", ticket_id=ticket.id, key=doc.key, state=state.value, action=action)
            done.append({"ticket": ticket.key, "doc": doc.key, "state": state.value, "action": action})
        return done

    async def sweep(self, *, around: Ticket | None = None) -> list[dict[str, Any]]:
        """React on every ticket of a pack flow that has an approved document (or only on `around`, its parent, its
        children and the tickets that depend on it: right after an approval). The timer runs the full sweep to notice
        code that changed."""
        office = self.office
        if around is not None:
            ids = {around.id} | {int(r["id"]) for r in await office.db.fetchall("SELECT id FROM tickets WHERE parent_id = ?", (around.id,))}
            ids |= set(await office.links.dependents(around.id))
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
        if not self.applies(ticket) or tpl is None or not tpl.stages or self.spawn_of(ticket, "breakdown") is None:
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
        if self.doctype_of(stored_key, self.pack_of(ticket)) is not None:
            await self.office.links.sync_family(ticket)  # first: what the new text links to is part of what it is written from
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

