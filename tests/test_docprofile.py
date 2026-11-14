"""The document standard in layers (office → team → project → epic), and a task that moves between "design only" and
"design then code" without losing its place."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

from actoroffice.app import Office
from actoroffice.domain.errors import OfficeError
from actoroffice.domain.work import TicketStatus
from actoroffice.services import docflow
from actoroffice.services.docflow import DocProfile

from .test_docflow import design, office, put, runtime, setup, spec  # noqa: F401  (fixtures)
from .test_docpack import STRICT, write_pack
from .test_docweb import web  # noqa: F401  (fixture)
from .test_web import HEADERS

S = TicketStatus
ORPHAN = "### C-9 [mới]: Dọn code\nKhông liên quan AC nào.\n\n## 3. Mô hình dữ liệu"


@pytest.fixture(autouse=True)
def builtin_pack_afterwards():
    yield
    docflow.configure(None)


async def task_with_orphan(office: Office, epic, team_id: str | None = None):
    """A child task whose design has one change that serves no AC: R-NO-ORPHAN (a warning in the built-in pack)."""
    await office.db.execute("UPDATE tickets SET stage_template = 'epic', stage = 'breakdown', team_id = ? WHERE id = ?", (team_id, epic.id))
    child = await office.tickets.create("Task", parent_id=epic.id)
    await office.db.execute("UPDATE tickets SET stage_template = 'task-spec', stage = 'design', team_id = ? WHERE id = ?", (team_id, child.id))
    child = await office.tickets.get(child.id)
    await put(office, child, "spec", spec(epic.key, "T-1"))
    await put(office, child, "design", design(child.key, epic.key).replace("## 3. Mô hình dữ liệu", ORPHAN))
    return await office.tickets.get(child.id)


async def orphan(office: Office, ticket) -> list[str]:
    return [f.severity for f in await office.docs.check(await office.tickets.get(ticket.id)) if f.rule == "R-NO-ORPHAN"]


async def test_each_layer_overrides_the_one_before(office: Office, setup) -> None:
    team, _lan, _minh, _hung, _repo, epic = setup
    child = await task_with_orphan(office, epic, team.id)
    assert await orphan(office, child) == ["warn"], "the built-in pack only warns"

    await office.docs.save_profile(f"team:{team.id}", {"rules": {"R-NO-ORPHAN": "error"}})
    assert await orphan(office, child) == ["error"], "the team is stricter than the office"
    other = await office.tickets.create("Việc của team khác")
    assert office.docs.pack_of(other).rules["R-NO-ORPHAN"].severity == "warn", "another team is not touched"

    await office.docs.save_profile(f"project:{epic.project_id}", {"rules": {"R-NO-ORPHAN": "off"}})
    assert await orphan(office, child) == [], "the project switches the rule off"

    await office.docs.save_profile(f"ticket:{epic.key}", DocProfile(rules={"R-NO-ORPHAN": "warn"}))
    assert await orphan(office, child) == ["warn"], "what is set on the epic holds for the tasks under it"
    assert [scope.split(":")[0] for scope, _ in office.docs.layers(child)] == ["team", "project", "ticket"]

    await office.docs.save_profile(f"ticket:{epic.key}", None)
    assert await orphan(office, child) == [], "removing a layer gives back the one under it"

    await office.docs.load_profiles()  # what a restart reads
    assert await orphan(office, child) == [] and office.docs.pack_of(other).rules["R-NO-ORPHAN"].severity == "warn"


async def test_a_team_follows_its_own_pack(office: Office, setup, tmp_path: Path) -> None:
    team, *_rest, epic = setup
    write_pack(office.settings.data_dir / "packs" / "acme", STRICT)
    child = await task_with_orphan(office, epic, team.id)
    await office.docs.save_profile(f"team:{team.id}", {"pack": "acme"})
    assert office.docs.pack_of(child).name == "acme" and await orphan(office, child) == ["error"]
    assert (await office.teams.gate_policy(child, "design")).roles == ["architect"], "gates come from the team's pack"
    outsider = await office.tickets.create("x")
    assert (await office.teams.gate_policy(outsider, "design")).roles == ["lead"] and office.docs.pack.name == "default"
    view = await office.docs.profile_view(f"team:{team.id}")
    assert view["pack"]["name"] == "acme" and "acme" in view["packs"] and {"id": "R-NO-ORPHAN", "type": "traces_to", "severity": "error"} in view["rules"]

    # the epic goes back to the built-in pack but keeps one rule strict
    await office.docs.save_profile(f"ticket:{epic.id}", {"pack": "default", "rules": {"R-NO-ORPHAN": "error"}})
    assert office.docs.pack_of(child).name == "default" and await orphan(office, child) == ["error"]


async def test_a_profile_that_is_not_valid_is_refused_and_nothing_changes(office: Office, setup) -> None:
    team, *_rest, epic = setup
    scope = f"team:{team.id}"
    await office.docs.save_profile(scope, {"rules": {"R-NO-ORPHAN": "error"}})
    for bad, said in (
        ({"pack": "khong-co"}, "pack 'khong-co'"),
        ({"rules": {"R-KHONG-CO": "error"}}, "R-KHONG-CO"),
        ({"rules": {"R-NO-ORPHAN": "fatal"}}, "fatal"),
        ({"packs": "x"}, "không hợp lệ"),
    ):
        with pytest.raises(OfficeError) as err:
            await office.docs.save_profile(scope, bad)
        assert said in str(err.value)
    with pytest.raises(OfficeError):
        await office.docs.save_profile("team:khong-co", {"rules": {"R-NO-ORPHAN": "error"}})
    with pytest.raises(OfficeError):
        await office.docs.save_profile("phong:1", {"rules": {"R-NO-ORPHAN": "error"}})
    ticket = await office.tickets.get(epic.id)
    await office.db.execute("UPDATE tickets SET team_id = ? WHERE id = ?", (team.id, ticket.id))
    assert office.docs.pack_of(await office.tickets.get(ticket.id)).rules["R-NO-ORPHAN"].severity == "error", "what was saved before stays"


async def test_an_edited_pack_is_used_without_a_restart_and_a_broken_edit_is_not(office: Office, setup) -> None:
    team, *_rest, epic = setup
    folder = write_pack(office.settings.data_dir / "packs" / "acme", STRICT)
    await office.docs.save_profile(f"team:{team.id}", {"pack": "acme"})
    await office.db.execute("UPDATE tickets SET team_id = ? WHERE id = ?", (team.id, epic.id))
    ticket = await office.tickets.get(epic.id)
    assert office.docs.pack_of(ticket).version == 2

    def edit(text: str) -> None:
        file = folder / "pack.yaml"
        before = file.stat().st_mtime_ns
        file.write_text(text, encoding="utf-8")
        os.utime(file, ns=(before + 5_000_000_000, before + 5_000_000_000))  # a later save, whatever the clock resolution

    edit(STRICT.replace("version: 2", "version: 3"))
    assert office.docs.pack_of(ticket).version == 3, "the next check reads the edited pack"
    edit(STRICT.replace("version: 2", "version: 4").replace("roles: [architect]", "roles: [cto]"))
    assert office.docs.pack_of(ticket).version == 3, "an edit that names a role that does not exist is not taken"
    edit(STRICT.replace("version: 2", "version: 5"))
    assert office.docs.pack_of(ticket).version == 5

    import shutil

    shutil.rmtree(folder)
    docflow.configure(None)  # nothing loaded any more, as after a restart
    assert office.docs.pack_of(ticket).name == "default", "a pack that is gone does not stop the work"


# ---- task-spec ↔ task ----------------------------------------------------------------------------------------------


async def test_a_spec_only_task_goes_on_to_code_and_back(office: Office, setup) -> None:
    team, lan, *_rest, epic = setup
    child = await office.tickets.create("Task", parent_id=epic.id)
    started = await office.stages.start(child.id, template="task-spec", by="user", team_id=team.id, owner_user_id=lan.id)
    assert started.stage == "design"
    assert [s["template"] for s in office.docs.switchable(started)] == ["task"]

    moved = await office.stages.switch_template(child.id, "task", reason="làm luôn", by="user")
    assert (moved.stage_template, moved.stage) == ("task", "design"), "the ticket keeps its place"
    runs = await office.stages.runs(child.id)
    assert len(runs) == 1 and runs[0].finished_at is None, "the design in progress is not thrown away"

    back = await office.stages.switch_template(child.id, "task-spec", reason="chưa code", by="user")
    assert (back.stage_template, back.stage) == ("task-spec", "design")

    # done as "design only", then somebody decides to build it: it continues at the stage after design
    await office.stages.advance(child.id, by="user")
    assert (await office.tickets.get(child.id)).status == S.DONE
    resumed = await office.stages.switch_template(child.id, "task", reason="giờ thì code", by="user")
    assert (resumed.stage_template, resumed.stage, resumed.status) == ("task", "build", S.TODO)

    with pytest.raises(OfficeError) as err:
        await office.stages.switch_template(child.id, "task-spec", reason="thôi", by="user")
    assert "không đổi được nữa" in str(err.value), "past the design there is no going back to design only"
    assert office.docs.switchable(await office.tickets.get(child.id)) == []
    with pytest.raises(OfficeError):
        await office.stages.switch_template(child.id, "epic", reason="x", by="user")


# ---- the JSON API ------------------------------------------------------------------------------------------------------


def test_profile_api(web) -> None:
    office, c, team, _people, _project = web
    scope = f"team:{team.id}"
    view = c.get("/api/doc-profile", params={"scope": scope}).json()
    assert view["profile"] is None and view["pack"]["name"] == "default" and "default" in view["packs"]
    saved = c.put("/api/doc-profile", json={"scope": scope, "rules": {"R-NO-ORPHAN": "error"}}, headers=HEADERS)
    assert saved.status_code == 200 and saved.json()["profile"] == {"pack": None, "rules": {"R-NO-ORPHAN": "error"}}
    assert {"id": "R-NO-ORPHAN", "type": "traces_to", "severity": "error"} in saved.json()["rules"]
    bad = c.put("/api/doc-profile", json={"scope": scope, "pack": "khong-co"}, headers=HEADERS)
    assert bad.status_code == 400 and "khong-co" in bad.text
    assert c.get("/api/doc-profile", params={"scope": scope}).json()["profile"]["rules"] == {"R-NO-ORPHAN": "error"}, "a refused change leaves the layer as it was"
    assert c.put("/api/doc-profile", json={"scope": scope}, headers=HEADERS).json()["profile"] is None, "nothing in it: the layer is removed"
    assert c.get("/api/doc-profile", params={"scope": "team:khong-co"}).status_code == 404
