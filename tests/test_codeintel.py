"""The code index, the code graph and the service map (codeintel/), on small repositories built for each test."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from actoroffice.codeintel import extractors, parse, services
from actoroffice.codeintel.index import IndexOptions, chunks_of, classify, index_repo
from actoroffice.codeintel.query import CodeQuery
from actoroffice.codeintel.store import IndexStore
from actoroffice.codeintel.sync import FolderSource, local_source, mirror
from actoroffice.ports.intel import RepoFacts

from .seed import IDENT, build_repo, git

PY_REPO = {
    "src/shop/__init__.py": "",
    "src/shop/limits.py": '''"""Daily limits."""

MAX_PER_DAY = 5


class LimitService:
    """Checks how much a customer may still refund today."""

    def check(self, customer_id: int) -> bool:
        return self._count(customer_id) < MAX_PER_DAY

    def _count(self, customer_id: int) -> int:
        return 0
''',
    "src/shop/refund.py": '''from fastapi import APIRouter

from shop.limits import LimitService
from . import audit

router = APIRouter()
limit_service = LimitService()


def refund_allowed(days: int) -> bool:
    return 0 <= days <= 30


@router.post("/v1/refunds/{refund_id}")
async def create_refund(refund_id: int, days: int) -> dict:
    if not refund_allowed(days) or not limit_service.check(refund_id):
        return {"ok": False}
    audit.record("refund", refund_id)
    return {"ok": True}
''',
    "src/shop/audit.py": '''import httpx


def record(kind: str, ref: int) -> None:
    httpx.post("https://audit.internal/v1/events", json={"kind": kind, "ref": ref})
''',
    "tests/test_refund.py": "from shop.refund import refund_allowed\n\n\ndef test_ok():\n    assert refund_allowed(3)\n",
    "README.md": "# Shop\n\nRefund rules.\n",
    ".env": "TOKEN=should-never-be-indexed\n",
    "pyproject.toml": '[project]\nname = "shop"\ndependencies = [\n    "fastapi>=0.1",\n    "httpx",\n]\n',
}


@pytest.fixture
def store(tmp_path: Path) -> IndexStore:
    s = IndexStore(tmp_path / "index.db")
    yield s
    s.close()


@pytest.fixture
def shop(tmp_path: Path, store: IndexStore) -> Path:
    repo = build_repo(tmp_path / "shop", PY_REPO)
    state = index_repo(store, "shop", local_source(repo))
    assert state.state == "ready" and state.level == "graph", state
    return repo


# ---- parsing --------------------------------------------------------------------------------------------------------


def test_parse_python_symbols_calls_imports() -> None:
    p = parse.parse("src/shop/refund.py", PY_REPO["src/shop/refund.py"])
    by_name = {s.name: s for s in p.symbols}
    assert by_name["create_refund"].qualified == "shop.refund.create_refund"
    assert by_name["create_refund"].decorators == ['router.post("/v1/refunds/{refund_id}")']
    assert by_name["create_refund"].signature.startswith("async def create_refund(refund_id: int, days: int) -> dict")
    assert {c.name for c in p.calls if c.caller == "shop.refund.create_refund"} >= {"refund_allowed", "limit_service.check", "audit.record"}
    assert [(i.module, i.names) for i in p.imports] == [("fastapi", ["APIRouter"]), ("shop.limits", ["LimitService"]), (".", ["audit"])]


def test_parse_python_class_members_docs_constants() -> None:
    p = parse.parse("src/shop/limits.py", PY_REPO["src/shop/limits.py"])
    kinds = {s.qualified: s.kind for s in p.symbols}
    assert kinds == {
        "shop.limits.MAX_PER_DAY": "constant", "shop.limits.LimitService": "class", "shop.limits.LimitService.check": "method",
        "shop.limits.LimitService._count": "method",
    }  # fmt: skip
    assert next(s for s in p.symbols if s.name == "LimitService").doc.startswith("Checks how much")


JAVA = """package com.acme.pay;

import com.acme.ledger.LedgerClient;

/** Refund API. */
@RestController
@RequestMapping("/v1/payments")
public class RefundController extends Base implements Auditable {
    private final LimitService limitService;

    @PostMapping("/{id}/refund")
    public Refund refund(@PathVariable Long id) {
        limitService.check(id);
        kafkaTemplate.send("payments.refund.requested", id);
        return new Refund(id);
    }
}
"""


def test_parse_java() -> None:
    p = parse.parse("src/main/java/com/acme/pay/RefundController.java", JAVA)
    cls, method = p.symbols
    assert (cls.kind, cls.qualified, cls.bases) == ("class", "com.acme.pay.RefundController", ["Base", "Auditable"])
    assert cls.signature == "public class RefundController extends Base implements Auditable" and cls.doc == "Refund API."
    assert method.qualified == "com.acme.pay.RefundController.refund" and method.decorators == ['PostMapping("/{id}/refund")']
    assert {c.name for c in p.calls} >= {"limitService.check", "Refund"}
    assert (p.imports[0].module, p.imports[0].names) == ("com.acme.ledger", ["LedgerClient"])


def test_parse_typescript() -> None:
    src = "import { api } from './client';\nexport class Svc extends Base {\n  async check(id: number) { return helper(id); }\n}\nexport const helper = (id: number) => api.get(`/v1/x/${id}`);\n"
    p = parse.parse("web/src/svc.ts", src)
    assert {(s.kind, s.qualified) for s in p.symbols} == {("class", "web/src/svc.Svc"), ("method", "web/src/svc.Svc.check"), ("function", "web/src/svc.helper")}
    assert ("web/src/svc.Svc.check", "helper") in {(c.caller, c.name) for c in p.calls}
    assert p.imports[0].module == "./client" and p.imports[0].names == ["api"]


def test_unknown_language_keeps_text_only() -> None:
    p = parse.parse("notes/x.md", "# Title\n")
    assert p.lang == "markdown" and p.symbols == [] and p.text == "# Title\n"


# ---- manifest and chunks ----------------------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("path", "kind"),
    [
        ("src/shop/refund.py", "source"), ("tests/test_refund.py", "test"), ("src/main/java/a/RefundTest.java", "test"), ("web/a.spec.ts", "test"),
        ("README.md", "doc"), ("config/app.yaml", "config"), ("node_modules/x/index.js", "skip"), ("dist/app.js", "skip"), ("app.min.js", "skip"),
        ("uv.lock", "skip"), (".env", "skip"), ("deploy/prod-credentials.yaml", "skip"), ("certs/server.pem", "skip"), ("logo.png", "skip"),
    ],
)  # fmt: skip
def test_classify(path: str, kind: str) -> None:
    assert classify(path, parse.language_of(path)) == kind


def test_chunks_cover_the_file_and_keep_functions_whole() -> None:
    p = parse.parse("src/shop/refund.py", PY_REPO["src/shop/refund.py"])
    spans = chunks_of(p)
    lines = PY_REPO["src/shop/refund.py"].count("\n") + 1
    covered = sorted(i for a, b, _ in spans for i in range(a, b + 1))
    assert covered == sorted(set(covered)), "chunks overlap"
    assert set(range(1, lines)) <= set(covered)
    assert any(sym == "shop.refund.create_refund" for _, _, sym in spans)


# ---- indexing -----------------------------------------------------------------------------------------------------------


def test_index_is_incremental(shop: Path, store: IndexStore) -> None:
    assert store.one("SELECT stats_json FROM repos")[0].count('"changed": 7')  # .env is never indexed
    assert store.one("SELECT count(*) FROM files WHERE path = '.env'")[0] == 0
    assert index_repo(store, "shop", local_source(shop)).state == "ready"
    assert '"changed": 0' in store.one("SELECT stats_json FROM repos")[0]

    (shop / "src/shop/limits.py").write_text(PY_REPO["src/shop/limits.py"].replace("MAX_PER_DAY = 5", "MAX_PER_DAY = 9"), encoding="utf-8")
    (shop / "README.md").unlink()
    (shop / "src/shop/extra.py").write_text("def extra() -> int:\n    return 1\n", encoding="utf-8")
    git(shop, "add", "-A")
    git(shop, *IDENT, "commit", "-q", "-m", "change")
    state = index_repo(store, "shop", local_source(shop))
    stats = store.one("SELECT stats_json FROM repos")[0]
    assert '"changed": 2' in stats and '"removed": 1' in stats, stats
    assert state.indexed_commit == git(shop, "rev-parse", "HEAD")
    q = CodeQuery(store)
    assert "MAX_PER_DAY = 9" in q.search("MAX_PER_DAY", ["shop"])[0].text
    assert q.symbol("extra", ["shop"])[0].qualified == "shop.extra.extra"
    assert q.outline("shop", "README.md") is None


def test_uncommitted_changes_are_not_indexed(shop: Path, store: IndexStore) -> None:
    (shop / "src/shop/limits.py").write_text("BROKEN = True\n", encoding="utf-8")
    index_repo(store, "shop", local_source(shop))
    assert CodeQuery(store).symbol("LimitService", ["shop"]), "the index reads the committed main branch, not the working tree"


def test_plain_folder_is_indexed_too(tmp_path: Path, store: IndexStore) -> None:
    folder = tmp_path / "plain"
    (folder / "pkg").mkdir(parents=True)
    (folder / "pkg" / "a.py").write_text("def alpha():\n    return 1\n", encoding="utf-8")
    state = index_repo(store, "plain", FolderSource(folder))
    assert state.state == "ready" and state.indexed_commit.startswith("dir-")
    assert CodeQuery(store).symbol("alpha", ["plain"])[0].path == "pkg/a.py"


def test_text_level_when_asked(tmp_path: Path, store: IndexStore) -> None:
    repo = build_repo(tmp_path / "r", {"a.py": "def alpha():\n    return 1\n"})
    state = index_repo(store, "r", local_source(repo), IndexOptions(level="text"))
    assert state.level == "text" and state.symbols == 0
    assert CodeQuery(store).search("alpha", ["r"])[0].path == "a.py"


def test_mirror_of_a_remote_repository(tmp_path: Path, store: IndexStore) -> None:
    remote = build_repo(tmp_path / "remote", {"a.py": "def alpha():\n    return 1\n"})
    source = mirror(str(remote), tmp_path / "mirrors" / "remote.git")
    assert index_repo(store, "remote", source).state == "ready"
    (remote / "b.py").write_text("def beta():\n    return 2\n", encoding="utf-8")
    git(remote, "add", "-A")
    git(remote, *IDENT, "commit", "-q", "-m", "more")
    source = mirror(str(remote), tmp_path / "mirrors" / "remote.git")
    assert source.commit() == git(remote, "rev-parse", "HEAD")
    index_repo(store, "remote", source)
    assert CodeQuery(store).symbol("beta", ["remote"])
    assert not (tmp_path / "mirrors" / "remote.git" / "a.py").exists(), "the mirror is bare"


def test_broken_repository_is_recorded_not_raised(tmp_path: Path, store: IndexStore) -> None:
    from actoroffice.codeintel.sync import GitSource

    state = index_repo(store, "gone", GitSource(tmp_path / "nowhere"))
    assert state.state == "error" and state.error


# ---- graph --------------------------------------------------------------------------------------------------------------


def _edges(store: IndexStore, kind: str) -> set[tuple[str, str, str]]:
    rows = store.all(
        "SELECT s.qualified AS a, d.qualified AS b, e.confidence AS c FROM edges e JOIN nodes s ON s.id = e.src JOIN nodes d ON d.id = e.dst WHERE e.kind = ?", (kind,)
    )
    return {(r["a"], r["b"], r["c"]) for r in rows}


def test_calls_are_resolved_through_file_import_and_name(shop: Path, store: IndexStore) -> None:
    calls = _edges(store, "calls")
    assert ("shop.refund.create_refund", "shop.refund.refund_allowed", "resolved") in calls  # same file
    assert ("shop.refund.create_refund", "shop.audit.record", "resolved") in calls  # `from . import audit` then audit.record
    assert ("shop.refund.create_refund", "shop.limits.LimitService.check", "heuristic") in calls  # limit_service → LimitService
    assert ("shop.limits.LimitService.check", "shop.limits.LimitService._count", "resolved") in calls  # self._count
    assert ("src/shop/refund.py", "shop.limits.LimitService", "resolved") in calls  # module level LimitService()
    assert not any(b in ("len", "range", "print") for _, b, _ in calls)


def test_imports_become_file_edges(shop: Path, store: IndexStore) -> None:
    imports = {(a, b) for a, b, _ in _edges(store, "imports")}
    assert ("src/shop/refund.py", "src/shop/limits.py") in imports
    assert ("src/shop/refund.py", "src/shop/audit.py") in imports
    assert ("tests/test_refund.py", "src/shop/refund.py") in imports


def test_facts_of_extractors(shop: Path, store: IndexStore) -> None:
    assert ("shop.refund.create_refund", "POST /v1/refunds/{}", "exact") in _edges(store, "exposes")
    assert ("shop.audit.record", "audit.internal", "exact") in _edges(store, "calls_http")
    assert {b for _, b, _ in _edges(store, "depends_on")} == {"fastapi", "httpx"}


# ---- extractors ---------------------------------------------------------------------------------------------------------

FACTS = RepoFacts(slug="x", languages=["java", "xml", "python"])


def test_spring_extractor() -> None:
    facts = extractors.Spring().extract(parse.parse("src/main/java/com/acme/pay/RefundController.java", JAVA), FACTS)
    assert {(f.node_kind, f.name, f.edge, f.symbol) for f in facts} == {
        ("endpoint", "POST /v1/payments/{}/refund", "exposes", "com.acme.pay.RefundController.refund"),
        ("topic", "payments.refund.requested", "publishes", "com.acme.pay.RefundController.refund"),
    }


def test_spring_feign_and_listener() -> None:
    src = """package com.acme.pay;
@FeignClient(name = "ledger-service", path = "/v1")
public interface LedgerClient {
    @GetMapping("/accounts/{id}")
    Account account(@PathVariable Long id);
}
"""
    facts = extractors.Spring().extract(parse.parse("LedgerClient.java", src), FACTS)
    assert [(f.name, f.edge, f.meta["target"], f.confidence) for f in facts] == [("GET /v1/accounts/{}", "calls_http", "ledger-service", "resolved")]
    listener = 'package a;\npublic class L {\n    @KafkaListener(topics = {"a.created", "a.updated"})\n    public void on(String m) {}\n}\n'
    facts = extractors.Spring().extract(parse.parse("L.java", listener), FACTS)
    assert {(f.name, f.edge) for f in facts} == {("a.created", "subscribes"), ("a.updated", "subscribes")}


def test_mybatis_extractor() -> None:
    xml = """<?xml version="1.0"?>
<mapper namespace="com.acme.pay.RefundMapper">
  <select id="findByOrder" resultType="Refund">
    SELECT r.id FROM refund r JOIN payment p ON p.id = r.payment_id WHERE p.order_id = #{orderId}
  </select>
  <update id="markDone">UPDATE refund SET status = 'DONE' WHERE id = #{id}</update>
</mapper>
"""
    facts = extractors.MyBatis().extract(parse.parse("src/main/resources/mapper/RefundMapper.xml", xml), FACTS)
    assert {(f.name, f.edge, f.symbol) for f in facts} == {
        ("refund", "reads", "com.acme.pay.RefundMapper.findByOrder"),
        ("payment", "reads", "com.acme.pay.RefundMapper.findByOrder"),
        ("refund", "writes", "com.acme.pay.RefundMapper.markDone"),
    }
    assert all(f.confidence == "heuristic" for f in facts)


def test_mybatis_statement_joins_the_java_mapper_method(tmp_path: Path, store: IndexStore) -> None:
    repo = build_repo(
        tmp_path / "pay",
        {
            "src/main/java/com/acme/pay/RefundMapper.java": "package com.acme.pay;\npublic interface RefundMapper {\n    Refund findByOrder(Long orderId);\n}\n",
            "src/main/resources/mapper/RefundMapper.xml": '<mapper namespace="com.acme.pay.RefundMapper">\n<select id="findByOrder">SELECT * FROM refund</select>\n</mapper>\n',
        },
    )
    index_repo(store, "pay", local_source(repo))
    assert ("com.acme.pay.RefundMapper.findByOrder", "refund", "heuristic") in _edges(store, "reads")


def test_norm_path() -> None:
    assert {extractors.norm_path(p) for p in ("/v1/pay/{id}", "v1/pay/:id/", "/v1/pay/${id}?x=1", "/v1//pay/<int:id>")} == {"/v1/pay/{}"}


def test_sql_tables() -> None:
    assert extractors.sql_tables("select * from a join b on a.x = b.x") == ({"a", "b"}, set())
    assert extractors.sql_tables("insert into log(x) select x from src") == ({"src"}, {"log"})
    assert extractors.sql_tables("DELETE FROM old WHERE 1 = 1") == (set(), {"old"})


# ---- queries --------------------------------------------------------------------------------------------------------------


def test_search_finds_part_of_an_identifier_and_cites(shop: Path, store: IndexStore) -> None:
    q = CodeQuery(store)
    hit = q.search("LimitServ", ["shop"])[0]
    assert hit.path == "src/shop/limits.py" and hit.cite.startswith("shop:src/shop/limits.py:") and hit.commit == git(shop, "rev-parse", "HEAD")
    assert q.search("refund_allowed", ["shop"])[0].path == "src/shop/refund.py", "the definition ranks above the test that uses it"
    assert q.search("refund_allowed", ["shop"], kinds=["test"])[0].path == "tests/test_refund.py"
    assert q.search("should-never-be-indexed", ["shop"]) == []
    assert q.search("LimitService", []) == [] and q.search("LimitService", ["other"]) == [], "nothing outside the scope"


def test_a_rare_word_outweighs_the_common_words_of_a_question(tmp_path: Path, store: IndexStore) -> None:
    common = "def handle_{i}(ticket):\n    # update the ticket description, then save the ticket description\n    return ticket.description\n"
    files = {f"mod{i}.py": common.format(i=i) for i in range(12)}
    files["rare.py"] = "def refresh(ticket):\n    return zebra_marker(ticket)\n"
    index_repo(store, "big", local_source(build_repo(tmp_path / "big", files)))
    hits = CodeQuery(store).search("ticket description update save zebra_marker", ["big"])
    assert hits[0].path == "rare.py", [h.path for h in hits]


def test_attribute_named_after_a_class_resolves_as_a_guess(tmp_path: Path, store: IndexStore) -> None:
    repo = build_repo(
        tmp_path / "o",
        {
            "svc.py": "class SourceService:\n    def sync(self, ticket_id):\n        return ticket_id\n\n\nclass JiraMirror:\n    def sync(self):\n        return 1\n",
            "use.py": "def mirror(office, ticket):\n    office.sources.sync(ticket.id)\n",
        },
    )
    index_repo(store, "o", local_source(repo))
    assert ("use.mirror", "svc.SourceService.sync", "heuristic") in _edges(store, "calls")
    assert ("use.mirror", "svc.JiraMirror.sync", "heuristic") not in _edges(store, "calls")


def test_outline_symbol_references(shop: Path, store: IndexStore) -> None:
    q = CodeQuery(store)
    outline = q.outline("shop", "src/shop/limits.py")
    assert [(s.kind, s.name) for s in outline.symbols] == [("constant", "MAX_PER_DAY"), ("class", "LimitService"), ("method", "check"), ("method", "_count")]
    assert q.symbol("shop.limits.LimitService.check", ["shop"])[0].cite == "shop:src/shop/limits.py:9-10"
    matched, graph = q.references("refund_allowed", ["shop"], direction="callers")
    names = {n.id: n.qualified for n in graph.nodes}
    assert {names[e.src] for e in graph.edges} == {"shop.refund.create_refund", "tests.test_refund.test_ok"}
    _, callees = q.references("create_refund", ["shop"], direction="callees")
    assert {n.qualified for n in callees.nodes} >= {"shop.refund.refund_allowed", "shop.audit.record"}


def test_impact_reaches_endpoints_through_callers(shop: Path, store: IndexStore) -> None:
    q = CodeQuery(store)
    impact = q.impact(["LimitService._count"], ["shop"])
    assert {n.qualified for n in impact.callers} >= {"shop.limits.LimitService.check", "shop.refund.create_refund"}
    assert [n.name for n in impact.endpoints] == ["POST /v1/refunds/{}"]
    by_line = q.impact(["src/shop/limits.py:13"], ["shop"])
    assert [n.qualified for n in by_line.targets] == ["shop.limits.LimitService._count"]
    assert q.impact(["nothing_like_this"], ["shop"]).unknown == ["nothing_like_this"]


def test_pack_respects_the_budget(shop: Path, store: IndexStore) -> None:
    q = CodeQuery(store)
    pack = q.pack("refund limit check", ["shop"], budget_tokens=4000)
    assert pack.items and pack.tokens <= 4000 and pack.outline
    small = q.pack("refund limit check", ["shop"], budget_tokens=60)
    assert small.tokens <= 60 and small.truncated


# ---- service map ----------------------------------------------------------------------------------------------------------

PAYMENTS = {
    "pom.xml": "<project><dependencies><dependency><groupId>org.springframework.boot</groupId><artifactId>spring-boot-starter-web</artifactId></dependency></dependencies></project>",
    "src/main/java/com/acme/pay/RefundController.java": JAVA,
    "src/main/java/com/acme/pay/LedgerClient.java": 'package com.acme.pay;\n@FeignClient(name = "ledger-service")\npublic interface LedgerClient {\n    @GetMapping("/v1/accounts/{id}")\n    Account account(Long id);\n}\n',  # noqa: E501
    "src/main/resources/mapper/RefundMapper.xml": '<mapper namespace="com.acme.pay.RefundMapper">\n<update id="mark">UPDATE refund SET s = 1</update>\n</mapper>\n',
}
LEDGER = {
    "pom.xml": "<project/>",
    "src/main/java/com/acme/ledger/AccountController.java": 'package com.acme.ledger;\n@RestController\npublic class AccountController {\n    @GetMapping("/v1/accounts/{accountId}")\n    public Account get(Long accountId) { return null; }\n}\n',  # noqa: E501
    "src/main/resources/mapper/LedgerMapper.xml": '<mapper namespace="com.acme.ledger.LedgerMapper">\n<select id="refunds">SELECT * FROM refund</select>\n</mapper>\n',
}
NOTIFY = {
    "package.json": '{"name": "notify", "dependencies": {"kafkajs": "1"}}',
    "src/main/java/com/acme/notify/Listener.java": 'package com.acme.notify;\npublic class Listener {\n    @KafkaListener(topics = "payments.refund.requested")\n    public void on(String m) {}\n}\n',
}


@pytest.fixture
def three(tmp_path: Path, store: IndexStore) -> list[str]:
    for name, files in (("payments", PAYMENTS), ("ledger-service", LEDGER), ("notify", NOTIFY)):
        index_repo(store, name, local_source(build_repo(tmp_path / name, files)))
    return ["payments", "ledger-service", "notify"]


def _map_edges(graph) -> set[tuple[str, str, str, str]]:
    names = {n.id: n.name for n in graph.nodes}
    return {(names[e.src], names[e.dst], e.kind, e.confidence) for e in graph.edges}


def test_service_map_links_http_topics_and_shared_tables(three: list[str], store: IndexStore) -> None:
    graph = services.build_map(CodeQuery(store), three)
    assert {n.name for n in graph.nodes if n.kind == "service"} == {"payments", "ledger-service", "notify"}
    edges = _map_edges(graph)
    assert ("payments", "ledger-service", "calls_http", "resolved") in edges
    assert ("payments", "payments.refund.requested", "publishes", "exact") in edges
    assert ("payments.refund.requested", "notify", "subscribes", "exact") in edges
    assert ("payments", "refund", "writes", "heuristic") in edges and ("ledger-service", "refund", "reads", "heuristic") in edges
    assert all(e.cite for e in graph.edges), "every edge leads back to a line of code"


def test_service_map_is_limited_to_the_repos_asked_for(three: list[str], store: IndexStore) -> None:
    graph = services.build_map(CodeQuery(store), ["payments"])
    assert {n.name for n in graph.nodes if n.kind == "service"} == {"payments"}
    assert ("payments", "ledger-service", "calls_http", "resolved") in _map_edges(graph)
    assert next(n for n in graph.nodes if n.name == "ledger-service").kind == "external", "another team's service shows as an outside node"


def test_declared_services_win(three: list[str], store: IndexStore) -> None:
    declared = services.load_declared(
        "edges:\n  - {from: payments, to: notify, kind: calls_http, note: qua gateway}\nignore:\n  - {from: payments, to: ledger-service}\n"
        "external:\n  - {name: vnpay, kind: payment-gateway}\n"
    )
    edges = _map_edges(services.build_map(CodeQuery(store), three, {"payments": declared}))
    assert ("payments", "notify", "calls_http", "declared") in edges
    assert not any(a == "payments" and b == "ledger-service" for a, b, _, _ in edges)
    assert services.load_declared("{not yaml").services == []


def test_detect_services() -> None:
    assert [s.name for s in services.detect("acme/shop", ["pyproject.toml", "src/a.py"], services.Declared())] == ["shop"]
    mono = ["services/pay/Dockerfile", "services/pay/a.py", "services/ledger/Dockerfile", "README.md"]
    defs = services.detect("acme/mono", mono, services.Declared())
    assert sorted(s.name for s in defs) == ["ledger", "pay"]
    assert services.service_of("services/pay/a.py", defs) == "pay"
    named = services.detect("acme/mono", mono, services.load_declared("services:\n  - {name: payments-api, path: services/pay}\n"))
    assert [(s.name, s.declared) for s in named] == [("payments-api", True)]


def test_mermaid_marks_guesses(three: list[str], store: IndexStore) -> None:
    text = services.to_mermaid(services.build_map(CodeQuery(store), three))
    assert text.startswith("flowchart LR") and "-.->|write|" in text and "-->|HTTP|" in text


def test_focus_keeps_the_neighbourhood(three: list[str], store: IndexStore) -> None:
    graph = services.focus(services.build_map(CodeQuery(store), three), "notify", 1)
    assert {n.name for n in graph.nodes} == {"notify", "payments.refund.requested"}


# ---- purity ---------------------------------------------------------------------------------------------------------------


def test_codeintel_and_ports_do_not_import_the_office() -> None:
    root = Path(__file__).parent.parent / "src" / "actoroffice"
    allowed = {"ports": ("actoroffice.ports",), "codeintel": ("actoroffice.ports", "actoroffice.codeintel")}
    for package, ok in allowed.items():
        for file in (root / package).rglob("*.py"):
            for node in ast.walk(ast.parse(file.read_text(encoding="utf-8"))):
                names = [a.name for a in node.names] if isinstance(node, ast.Import) else [node.module or ""] if isinstance(node, ast.ImportFrom) and node.level == 0 else []
                for name in names:
                    assert not name.startswith("actoroffice") or name.startswith(ok), f"{file.name} imports {name}"
