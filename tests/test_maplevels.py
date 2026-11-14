"""The service map below the system level (one service, one module) and between two commits."""

from __future__ import annotations

import pytest

from actoroffice.codeintel import services
from actoroffice.codeintel.query import CodeQuery
from actoroffice.codeintel.services import MODULE_ROOT, Modules, ServiceDef
from actoroffice.codeintel.store import IndexStore
from actoroffice.domain.errors import NotFound

from .seed import IDENT, git
from .test_codeintel import PY_REPO, store, three  # noqa: F401  (fixtures)
from .test_docweb import web  # noqa: F401  (fixture)


def edges_of(graph) -> set[tuple[str, str, str, str]]:
    names = {n.id: f"{n.kind}:{n.name}" for n in graph.nodes}
    return {(names[e.src], e.kind, names[e.dst], e.confidence) for e in graph.edges}


def rows(*paths: str, kind: str = "source") -> list[dict]:
    return [{"path": p, "kind": kind, "lang": "", "lines": 1} for p in paths]


# ---- modules -----------------------------------------------------------------------------------------------------------


def test_modules_are_the_folders_under_what_the_code_shares() -> None:
    java = Modules(
        rows("src/main/java/com/acme/pay/refund/A.java", "src/main/java/com/acme/pay/refund/B.java", "src/main/java/com/acme/pay/limit/C.java")
        + rows("pom.xml", "src/main/resources/application.yml", kind="config"),
        ServiceDef("pay", "pay"),
    )
    assert java.of("src/main/java/com/acme/pay/refund/A.java") == "refund" and java.of("src/main/java/com/acme/pay/limit/C.java") == "limit"
    assert java.path_of("refund") == "src/main/java/com/acme/pay/refund"
    assert java.of("pom.xml") == MODULE_ROOT and java.of("src/main/resources/application.yml") == "resources", "what is not code gets a place too"
    assert sorted(java.files) == [MODULE_ROOT, "limit", "refund", "resources"]


def test_a_folder_holding_most_of_the_code_is_split_and_small_ones_next_to_it_stay() -> None:
    mods = Modules(
        rows(*[f"src/app/api/h{i}.py" for i in range(4)], *[f"src/app/core/c{i}.py" for i in range(4)], "src/app/main.py", "scripts/seed.py")
        + rows("tests/test_api.py", kind="test"),
        ServiceDef("app", "app"),
    )
    assert (mods.of("src/app/api/h1.py"), mods.of("src/app/core/c2.py"), mods.of("scripts/seed.py"), mods.of("tests/test_api.py")) == ("api", "core", "scripts", "tests")
    assert mods.of("src/app/main.py") == "app" and mods.path_of("app") == "src/app", "files next to the modules are a module named after their folder"


def test_modules_of_a_service_inside_a_monorepo() -> None:
    mods = Modules(rows("services/pay/src/a/x.py", "services/pay/src/b/y.py", "services/ledger/src/z.py"), ServiceDef("pay", "mono", "services/pay"))
    assert sorted(mods.files) == ["a", "b"] and mods.path_of("a") == "services/pay/src/a", "only the service's own files, paths from the repository root"


# ---- one service -------------------------------------------------------------------------------------------------------


def test_inside_a_service_modules_endpoints_and_who_it_talks_to(three: list[str], store: IndexStore) -> None:
    graph = services.build_map(CodeQuery(store), three, service="payments")
    assert {n.name: (n.meta["path"], n.meta["files"]) for n in graph.nodes if n.kind == "module"} == {
        "pay": ("src/main/java/com/acme/pay", 2), "resources": ("src/main/resources", 1), MODULE_ROOT: ("", 1),
    }  # fmt: skip
    edges = edges_of(graph)
    assert ("module:pay", "exposes", "endpoint:POST /v1/payments/{}/refund", "exact") in edges
    assert ("module:pay", "calls_http", "service:ledger-service", "resolved") in edges
    assert ("module:pay", "publishes", "topic:payments.refund.requested", "exact") in edges
    assert ("topic:payments.refund.requested", "subscribes", "service:notify", "exact") in edges, "who listens to what this service publishes"
    assert ("module:resources", "writes", "table:refund", "heuristic") in edges and ("service:ledger-service", "reads", "table:refund", "heuristic") in edges
    assert all(e.cite for e in graph.edges)


def test_a_call_from_outside_lands_on_the_endpoint(three: list[str], store: IndexStore) -> None:
    edges = edges_of(services.build_map(CodeQuery(store), three, service="ledger-service"))
    assert ("service:payments", "calls_http", "endpoint:GET /v1/accounts/{}", "resolved") in edges
    assert ("module:ledger", "exposes", "endpoint:GET /v1/accounts/{}", "exact") in edges
    assert not any("notify" in a or "notify" in b for a, _, b, _ in edges), "what two other services do between them is not shown"


def test_unknown_service_or_module_is_named(three: list[str], store: IndexStore) -> None:
    with pytest.raises(KeyError, match="không có service 'nope'"):
        services.build_map(CodeQuery(store), three, service="nope")
    with pytest.raises(KeyError, match="không có module 'nope'.*pay"):
        services.build_map(CodeQuery(store), three, service="payments", module="nope")


# ---- one module --------------------------------------------------------------------------------------------------------


def test_inside_a_module_files_symbols_and_what_they_reach(three: list[str], store: IndexStore) -> None:
    graph = services.build_map(CodeQuery(store), three, service="payments", module="pay")
    by_kind: dict[str, list[str]] = {}
    for n in graph.nodes:
        by_kind.setdefault(n.kind, []).append(n.name)
    assert sorted(by_kind["file"]) == ["src/main/java/com/acme/pay/LedgerClient.java", "src/main/java/com/acme/pay/RefundController.java"]
    assert by_kind["class"] == ["RefundController"] and by_kind["interface"] == ["LedgerClient"]
    controller = next(n for n in graph.nodes if n.name == "RefundController")
    assert controller.path.endswith("RefundController.java") and controller.line, "a symbol leads to its line"
    edges = edges_of(graph)
    assert ("file:src/main/java/com/acme/pay/RefundController.java", "contains", "class:RefundController", "exact") in edges
    assert ("class:RefundController", "exposes", "endpoint:POST /v1/payments/{}/refund", "exact") in edges
    assert ("interface:LedgerClient", "calls_http", "endpoint:GET /v1/accounts/{}", "resolved") in edges


def test_a_module_too_large_to_draw_is_drawn_by_file(three: list[str], store: IndexStore, monkeypatch) -> None:
    monkeypatch.setattr(services, "MAX_MODULE_NODES", 3)
    graph = services.build_map(CodeQuery(store), three, service="payments", module="pay")
    assert graph.truncated and not [n for n in graph.nodes if n.kind in ("class", "interface")]
    assert ("file:src/main/java/com/acme/pay/RefundController.java", "exposes", "endpoint:POST /v1/payments/{}/refund", "exact") in edges_of(graph)


# ---- two commits -------------------------------------------------------------------------------------------------------


def test_diff_marks_what_is_added_and_removed(three: list[str], store: IndexStore) -> None:
    q = CodeQuery(store)
    base = services.build_map(q, three)
    head = services.build_map(q, ["payments", "notify"])  # as if the ledger service had gone
    diff = services.diff_maps(base, head)
    marks = {(n["kind"], n["name"]): n["diff"] for n in diff["nodes"]}
    assert marks[("service", "payments")] == "same" and marks[("service", "ledger-service")] == "removed" and marks[("external", "ledger-service")] == "added"
    names = {n["id"]: n["name"] for n in diff["nodes"]}
    gone = {(names[e["src"]], e["kind"], names[e["dst"]]) for e in diff["edges"] if e["diff"] == "removed"}
    assert ("ledger-service", "reads", "refund") in gone
    assert diff["removed"]["nodes"] >= 1 and diff["added"]["nodes"] == 1 and services.diff_maps(base, base)["added"] == {"nodes": 0, "edges": 0}


async def test_map_of_a_branch_against_main(web) -> None:
    office, c, team, _people, project = web
    repo = project.path
    git(repo, "checkout", "-q", "-b", "t/ao-9-sms")
    with open(f"{repo}/src/shop/audit.py", "w", encoding="utf-8") as f:
        f.write(PY_REPO["src/shop/audit.py"].replace("https://audit.internal/v1/events", "https://sms.internal/v1/send"))
    git(repo, *IDENT, "commit", "-qam", "sms instead of audit")
    git(repo, "checkout", "-q", "main")

    main = c.get(f"/api/map?team={team.id}").json()
    assert {n["name"] for n in main["nodes"]} == {"shop", "audit.internal"}
    diff = c.get(f"/api/map/diff?team={team.id}&head=t/ao-9-sms").json()
    marks = {n["name"]: n["diff"] for n in diff["nodes"]}
    assert marks == {"shop": "same", "sms.internal": "added", "audit.internal": "removed"}
    assert diff["added"] == {"nodes": 1, "edges": 1} and diff["removed"] == {"nodes": 1, "edges": 1} and diff["repos"] == ["shop"]
    added = next(e for e in diff["edges"] if e["diff"] == "added")
    assert added["kind"] == "calls_http" and "audit.py" in added["cite"]
    again = c.get(f"/api/map?team={team.id}").json()
    assert {n["name"] for n in again["nodes"]} == {"shop", "audit.internal"}, "the index of the main branch is not touched"
    assert not list((office.settings.data_dir / "tmp").glob("map-*")), "the copy used for the comparison is thrown away"

    assert c.get(f"/api/map/diff?team={team.id}&head=khong-co-nhanh").status_code == 404
    assert c.get(f"/api/map/diff?team={team.id}").status_code == 422
    with pytest.raises(NotFound):
        await office.codeintel.map_diff(await office.codeintel.repos(team_id=team.id), head="khong-co")


def test_map_api_levels(web) -> None:
    _office, c, team, _people, _project = web
    inside = c.get(f"/api/map?team={team.id}&service=shop").json()
    assert inside["level"] == "service" and {n["name"] for n in inside["nodes"] if n["kind"] == "module"} >= {"shop"}
    assert any(n["kind"] == "endpoint" and n["name"] == "POST /v1/refunds/{}" for n in inside["nodes"]) and "flowchart" in inside["mermaid"]
    module = next(n["name"] for n in inside["nodes"] if n["kind"] == "module" and n["meta"]["files"] >= 2)
    deep = c.get(f"/api/map?team={team.id}&service=shop&module={module}").json()
    assert deep["level"] == "module" and any(n["kind"] == "function" and n["name"] == "create_refund" for n in deep["nodes"])
    assert c.get(f"/api/map?team={team.id}&service=nope").status_code == 404
    assert c.get(f"/api/map?team={team.id}&module=shop").status_code == 422
