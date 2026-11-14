"""The JSON API of connections, the code index and the service map."""

from __future__ import annotations

import json

from .test_docweb import web  # noqa: F401  (fixture)
from .test_web import HEADERS


def test_connections_are_listed_before_anything_is_connected_and_never_show_a_secret(web) -> None:
    office, c, team, people, project = web
    listed = {a["name"]: a for a in c.get("/api/connections").json()}
    assert {"jira_cloud", "confluence_cloud", "bitbucket_cloud", "github"} <= set(listed)
    gh = listed["github"]
    assert gh["state"] == "unconfigured" and gh["connections"] == [] and "org" in gh["config_schema"]["properties"] and "token" in gh["secret_fields"]

    body = {"id": "gh-acme", "use": "github", "scope": f"team:{team.id}", "config": {"org": "acme", "repos": ["pay-*"]}, "secret": {"token": "ghp_secretsecretsecretsecretsecretsecret"}}
    saved = c.post("/api/connections", json=body, headers=HEADERS)
    assert saved.status_code == 200 and saved.json()["secret_ref"] == "gh-acme" and "secret" not in saved.json()
    text = json.dumps(c.get("/api/connections").json())
    assert "ghp_secret" not in text and '"gh-acme"' in text
    after = next(a for a in c.get("/api/connections").json() if a["name"] == "github")
    assert after["state"] == "connected" and after["connections"][0]["config"] == {"org": "acme", "repos": ["pay-*"]} and after["connections"][0]["write"] is False

    # changing the settings without sending the secret again keeps it
    body2 = {"id": "gh-acme", "use": "github", "scope": f"team:{team.id}", "config": {"org": "acme"}, "write": True}
    assert c.post("/api/connections", json=body2, headers=HEADERS).json()["secret_ref"] == "gh-acme"
    assert (office.settings.data_dir / "secrets" / "gh-acme.json").read_text(encoding="utf-8").count("ghp_secret") == 1


def test_bad_connections_are_refused_with_a_reason(web) -> None:
    office, c, team, people, project = web
    for body, status, word in (
        ({"id": "x", "use": "sharepoint", "config": {}}, 422, "không có adapter"),
        ({"id": "gh", "use": "github", "config": {}}, 422, "org"),
        ({"id": "Bad Id", "use": "github", "config": {"org": "a"}}, 422, "id"),
        ({"id": "gh", "use": "github", "config": {"org": "a"}, "scope": "team:nope"}, 422, "không có team"),
    ):
        r = c.post("/api/connections", json=body, headers=HEADERS)
        assert r.status_code == status and word in r.text, (body, r.text)
    assert all(a["connections"] == [] for a in c.get("/api/connections").json())
    assert c.post("/api/connections/nope/test", headers=HEADERS).status_code == 404


def test_test_and_remove_a_connection(web) -> None:
    office, c, team, people, project = web
    c.post("/api/connections", json={"id": "gh", "use": "github", "config": {"org": "acme"}}, headers=HEADERS)
    health = c.post("/api/connections/gh/test", headers=HEADERS).json()
    assert health["state"] == "unconfigured" and "token" in health["detail"], "no secret yet: a normal state, not a failure"
    assert c.delete("/api/connections/gh", headers=HEADERS).json() == {"removed": "gh"}
    assert next(a for a in c.get("/api/connections").json() if a["name"] == "github")["connections"] == []


def test_index_and_service_map_of_a_team(web) -> None:
    office, c, team, people, project = web
    assert c.get(f"/api/index?team={team.id}").json()[0]["state"] == "none"
    graph = c.get(f"/api/map?team={team.id}").json()
    assert [(s["repo"], s["state"], s["level"]) for s in graph["index"]] == [("shop", "ready", "graph")]
    names = {n["id"]: (n["kind"], n["name"]) for n in graph["nodes"]}
    assert set(names.values()) == {("service", "shop"), ("external", "audit.internal")}
    (edge,) = graph["edges"]
    assert (names[edge["src"]][1], names[edge["dst"]][1], edge["kind"]) == ("shop", "audit.internal", "calls_http")
    assert edge["cite"].startswith("shop:src/shop/audit.py:"), "an edge leads back to a line of code"
    assert graph["mermaid"].startswith("flowchart LR") and graph["commit"]["shop"]
    assert c.get(f"/api/index?team={team.id}").json()[0]["state"] == "ready"
    assert c.get(f"/api/map?team={team.id}&tables=everything").status_code == 422
    # a team without repositories has an empty map, not an error
    empty = c.post("/api/teams", json={"name": "Platform"}, headers=HEADERS).json()
    assert c.get(f"/api/map?team={empty['id']}").json()["nodes"] == []


def test_overlay_shows_what_a_tickets_documents_touch(web) -> None:
    office, c, team, people, project = web
    from .test_docflow import analysis, put

    epic = c.post("/api/tasks", json={"title": "Nâng hạn mức", "template": "epic", "team_id": team.id, "project_id": project.id}, headers=HEADERS).json()

    async def analyse():
        ticket = await office.tickets.get(epic["key"])
        await put(office, ticket, "analysis", analysis(ticket.key))
        await office.docs.check(ticket)  # pins the cited code

    c.portal.call(analyse)
    overlay = c.get(f"/api/map/overlay?ticket={epic['key']}").json()
    assert overlay["targets"] == ["src/shop/limits.py:3"] and overlay["services"] == ["shop"]
    assert c.get("/api/map/overlay?ticket=AO-999").status_code == 404
