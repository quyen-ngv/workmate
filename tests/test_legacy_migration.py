"""The Jira / Bitbucket configuration from before connections: turned into connections once, at office start; the old
commands only say how to connect now."""

from __future__ import annotations

import json
import logging
from pathlib import Path

import pytest

from actoroffice import cli_team
from actoroffice.__main__ import build_parser
from actoroffice.adapters import registry
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.ports.connection import Capability, ConnectionSpec

from .fake_adapters import connect, install, uninstall
from .seed import seed_db

JIRA = {"base_url": "https://acme.atlassian.net", "email": "bot@acme.vn", "token": "jira-token"}
BITBUCKET = {"email": "bot@acme.vn", "token": "bb-token", "workspace": "acme"}


def old_files(home: Path, *, jira: dict[str, str] | None = JIRA, bitbucket: dict[str, str] | None = BITBUCKET) -> None:
    (home / "secrets").mkdir(parents=True, exist_ok=True)
    if jira is not None:
        (home / "secrets" / "jira.json").write_text(json.dumps(jira), encoding="utf-8")
    if bitbucket is not None:
        (home / "secrets" / "bitbucket.json").write_text(json.dumps(bitbucket), encoding="utf-8")


def an_office(home: Path) -> Office:
    office = Office(Settings(data_dir=home))
    if not office.settings.db_path.exists():
        seed_db(office.settings.db_path)
    return office


async def ids(office: Office) -> list[tuple[str, str, str, bool]]:
    return [(s.id, s.use, s.scope, s.write) for s in await office.connections.list()]


async def test_old_files_become_connections_at_the_first_start_and_only_then(tmp_path, caplog) -> None:
    home = tmp_path / "home"
    old_files(home)
    caplog.set_level(logging.WARNING, logger="actoroffice.services.connections")
    office = an_office(home)
    await office.open()
    try:
        assert await ids(office) == [
            ("bitbucket-main", "bitbucket_cloud", "office", True),
            ("confluence-main", "confluence_cloud", "office", True),
            ("jira-main", "jira_cloud", "office", True),
        ]
        jira = await office.connections.get("jira-main")
        assert jira is not None and jira.config == {"site": "https://acme.atlassian.net"}
        assert office.connections.secret(jira) == {"email": "bot@acme.vn", "token": "jira-token"}
        assert (await office.connections.get("bitbucket-main")).config == {"workspace": "acme"}  # type: ignore[union-attr]
        assert await office.jira.ready() and await office.connections.has(Capability.WIKI) and await office.connections.has(Capability.CODE_PR)
        for created in ("jira-main", "confluence-main", "bitbucket-main"):
            assert f"connection {created}" in caplog.text, "what was created is logged"
        assert "jira-token" not in caplog.text and "bb-token" not in caplog.text
        marker = json.loads((home / "legacy-import.json").read_text(encoding="utf-8"))
        assert marker["created"] == ["jira-main", "confluence-main", "bitbucket-main"] and marker["at"]
        assert json.loads((home / "secrets" / "jira.json").read_text(encoding="utf-8")) == JIRA, "the old files are left in place"
        await office.connections.remove("bitbucket-main")  # people remove one: it must not come back
    finally:
        await office.close()

    again = an_office(home)
    await again.open()
    try:
        assert [row[0] for row in await ids(again)] == ["confluence-main", "jira-main"], "the import ran once: nothing is created again"
    finally:
        await again.close()


async def test_a_capability_that_is_already_connected_is_left_alone(tmp_path) -> None:
    home = tmp_path / "home"
    first = an_office(home)
    install()
    try:
        await first.open()
        await connect(first, "fake_tracker", scope="office")  # the tracker is already a connection; wiki and code host are not
        await first.close()
        assert not (home / "legacy-import.json").exists(), "no old file yet: nothing ran, nothing recorded"
        old_files(home)
        office = an_office(home)
        await office.open()
        try:
            assert await ids(office) == [
                ("bitbucket-main", "bitbucket_cloud", "office", True),
                ("confluence-main", "confluence_cloud", "office", True),
                ("fake-tracker", "fake_tracker", "office", False),
            ]
        finally:
            await office.close()
    finally:
        uninstall()


async def test_an_office_without_old_files_or_with_broken_ones_starts_as_usual(tmp_path, caplog) -> None:
    home = tmp_path / "home"
    office = an_office(home)
    await office.open()
    assert await ids(office) == [] and not (home / "legacy-import.json").exists()
    await office.close()

    old_files(home, jira={"base_url": "", "email": "x", "token": ""}, bitbucket=None)  # incomplete: as good as absent
    (home / "secrets" / "bitbucket.json").write_text("{not json", encoding="utf-8")
    office = an_office(home)
    await office.open()
    assert await ids(office) == [] and not (home / "legacy-import.json").exists()
    await office.close()

    old_files(home, bitbucket={"email": "x@y.z", "token": "t", "workspace": "not a workspace!"})
    (home / "secrets" / "jira.json").unlink()
    office = an_office(home)
    await office.open()  # the adapter refuses the workspace: logged, the office still starts, and it is not tried again
    try:
        assert await ids(office) == [] and (home / "legacy-import.json").exists()
        assert "was not turned into connections" in caplog.text
    finally:
        await office.close()


async def test_nothing_but_the_import_reads_the_old_files(tmp_path) -> None:
    home = tmp_path / "home"
    office = an_office(home)
    await office.open()
    try:
        old_files(home)  # appear while the office runs (someone restores a backup): not a configuration any more
        assert not await office.jira.ready() and await office.jira.sync() == []
        assert not await office.connections.has(Capability.TRACKER) and not await office.connections.has(Capability.CODE_PR)
        assert not hasattr(office, "atlassian")
        created = await office.connections.import_legacy()  # `actoroffice connections import-legacy`, by hand
        assert [s.id for s in created] == ["jira-main", "confluence-main", "bitbucket-main"]
        await office.connections.save(ConnectionSpec(id="jira-main", use="jira_cloud", config={"site": "https://khac.atlassian.net"}))
        assert await office.connections.import_legacy() == [], "an id that exists is never overwritten"
        assert (await office.connections.get("jira-main")).config == {"site": "https://khac.atlassian.net"}  # type: ignore[union-attr]
    finally:
        await office.close()


@pytest.mark.parametrize(
    ("argv", "says"),
    [
        (["jira", "login", "https://acme.atlassian.net", "bot@acme.vn", "tok"], ["connections add jira-main --use jira_cloud", "--secret-file"]),
        (["bitbucket", "bot@acme.vn", "tok", "acme"], ["--use bitbucket_server", "--use bitbucket_cloud", "--secret-file"]),
    ],
)
async def test_the_old_commands_say_how_to_connect_and_fail(tmp_path, capsys, argv: list[str], says: list[str]) -> None:
    office = an_office(tmp_path / "home")
    await office.open()
    try:
        args = build_parser().parse_args(argv)
        with pytest.raises(SystemExit) as stopped:
            await args.fn(office, args)
        printed = capsys.readouterr().out
        assert stopped.value.code == 2 and all(text in printed for text in says)
        assert await office.connections.list() == [] and not (tmp_path / "home" / "secrets" / "jira.json").exists(), "nothing is saved"
    finally:
        await office.close()


async def test_jira_sync_from_the_cli_needs_a_tracker_connection(tmp_path, capsys) -> None:
    from actoroffice.domain.errors import OfficeError

    from .fake_adapters import FakeTracker

    office = an_office(tmp_path / "home")
    await office.open()
    install()
    try:
        args = build_parser().parse_args(["jira", "sync"])
        with pytest.raises(OfficeError, match="Chưa có kết nối Jira nào"):
            await cli_team.cmd_jira(office, args)
        await connect(office, "fake_tracker")
        await office.teams.create_team("Billing", jira_project_key="BIL")
        FakeTracker.add("BIL-7", "Hoàn tiền", "x", ["ai"])
        await cli_team.cmd_jira(office, args)
        assert "Đã đồng bộ: 1 ticket mới" in capsys.readouterr().out
    finally:
        uninstall()
        await office.close()


def test_the_old_adapters_are_what_the_import_creates() -> None:
    assert {"jira_cloud", "confluence_cloud", "bitbucket_cloud"} <= set(registry.names())
