"""`actoroffice connections …` and the network part of `actoroffice doctor`, with adapters that reach nothing."""

from __future__ import annotations

import argparse
import json
import ssl
from collections.abc import AsyncIterator
from typing import Any, ClassVar

import httpx
import pytest
from pydantic import BaseModel, Field

from actoroffice import cli_connections
from actoroffice.__main__ import build_parser
from actoroffice.adapters import registry
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.domain.work import ProjectAccess
from actoroffice.ports.code import CloneAuth, RepoRef
from actoroffice.ports.connection import Capability, ConnectionSpec, Health
from actoroffice.services import doctor

from .seed import seed_db

TOKEN = "s3cr3t-token-9f2c"


class HostConfig(BaseModel):
    base_url: str = "https://git.congty.test"
    projects: list[str] = Field(default_factory=list)
    write: bool = False


class FakeHost:
    name: ClassVar[str] = "cli_host"
    title: ClassVar[str] = "Code host giả"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.CODE_READ, Capability.CODE_PR})
    config_schema: ClassVar[type[BaseModel]] = HostConfig
    secret_fields: ClassVar[tuple[str, ...]] = ("token", "username", "webhook_secret")
    optional_secret_fields: ClassVar[tuple[str, ...]] = ("username",)
    required_scopes: ClassVar[dict[Capability, list[str]]] = {Capability.CODE_READ: ["Repository read"]}
    health: ClassVar[Health] = Health(state="ok", identity="bot")

    def __init__(self, config: HostConfig, secret: dict[str, str], *, transport: Any = None) -> None:
        self.config = config
        self.secret = secret

    async def git_auth(self, repo: RepoRef, *, write: bool) -> CloneAuth:
        return CloneAuth(url=f"{self.config.base_url}/scm/{repo.slug}.git", bearer=self.secret["token"])

    async def test(self) -> Health:
        return FakeHost.health

    async def close(self) -> None:
        return None


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home"))
    seed_db(o.settings.db_path)
    await o.open()
    registry.register("cli_host", FakeHost)
    FakeHost.health = Health(state="ok", identity="bot")
    yield o
    registry.unregister("cli_host")
    await o.close()


def args(*argv: str) -> argparse.Namespace:
    return build_parser().parse_args(["connections", *argv])


# ---- describe -----------------------------------------------------------------------------------------------------


def test_describe_tells_which_secrets_are_optional() -> None:
    registry.register("cli_host", FakeHost)
    try:
        info = registry.describe("cli_host")
    finally:
        registry.unregister("cli_host")
    assert info["secret_fields"] == ["token", "username", "webhook_secret"]
    assert info["optional_secret_fields"] == ["username", "webhook_secret"], "a webhook secret is optional for every adapter"
    server = registry.describe("bitbucket_server")
    assert server["optional_secret_fields"] == ["username", "webhook_secret"] and "token" in server["secret_fields"]
    assert registry.describe("jira_cloud")["optional_secret_fields"] == []
    assert server["title"] != registry.describe("bitbucket_cloud")["title"]
    assert "bitbucket.org" in registry.describe("bitbucket_cloud")["title"] and "tự cài" in server["title"]


# ---- connections add / list / test / remove ---------------------------------------------------------------------------


async def test_cli_add_list_test_remove(office: Office, tmp_path, capsys) -> None:
    secret_file = tmp_path / "token.json"
    secret_file.write_text(json.dumps({"token": TOKEN}), encoding="utf-8")
    await cli_connections.cmd_connections(office, args("list"))
    assert "Chưa có kết nối nào" in capsys.readouterr().out

    await cli_connections.cmd_connections(
        office, args("add", "git-main", "--use", "cli_host", "--config", '{"projects": ["PAY"]}', "--write", "--secret-file", str(secret_file))
    )
    printed = capsys.readouterr().out
    assert "Đã lưu kết nối git-main (Code host giả, phạm vi office, được ghi)" in printed and TOKEN not in printed
    spec = await office.connections.get("git-main")
    assert spec is not None and (spec.use, spec.write, spec.config) == ("cli_host", True, {"projects": ["PAY"]})
    assert office.connections.secret(spec) == {"token": TOKEN}

    await cli_connections.cmd_connections(office, args("list"))
    listed = capsys.readouterr().out
    assert "git-main" in listed and "cli_host" in listed and "được ghi" in listed and TOKEN not in listed

    await cli_connections.cmd_connections(office, args("test", "git-main"))
    assert "✓ git-main: hoạt động (bot)" in capsys.readouterr().out
    FakeHost.health = Health(state="error", detail="token hết hạn")
    with pytest.raises(SystemExit) as stopped:
        await cli_connections.cmd_connections(office, args("test", "git-main"))
    assert stopped.value.code == 1 and "✗ git-main: lỗi — token hết hạn" in capsys.readouterr().out

    await cli_connections.cmd_connections(office, args("remove", "git-main"))
    assert "Đã gỡ kết nối git-main" in capsys.readouterr().out and await office.connections.get("git-main") is None


async def test_cli_refuses_what_is_wrong_and_takes_no_secret_from_argv(office: Office, capsys) -> None:
    parser = build_parser()
    for flag in ("--token", "--secret-json", "--password"):
        with pytest.raises(SystemExit):
            parser.parse_args(["connections", "add", "x", "--use", "cli_host", flag, TOKEN])
    capsys.readouterr()
    for argv, said in (
        (("add", "x"), "Cách dùng: actoroffice connections add"),
        (("add", "x", "--use", "cli_host", "--config", "{not json"), "--config phải là JSON"),
        (("add", "x", "--use", "nothing_like_this"), "không có adapter tên"),
        (("add", "Bad Id", "--use", "cli_host"), "id của kết nối"),
        (("test", "missing"), "Không có kết nối 'missing'"),
        (("remove", "missing"), "Không có kết nối 'missing'"),
        (("test",), "Cách dùng: actoroffice connections test <id>"),
    ):
        with pytest.raises(SystemExit) as stopped:
            await cli_connections.cmd_connections(office, args(*argv))
        assert stopped.value.code == 1 and said in capsys.readouterr().out, argv
    assert await office.connections.list() == []
    # nobody at the keyboard and no file: saved without a secret, and the output says what is missing
    await cli_connections.cmd_connections(office, args("add", "git-main", "--use", "cli_host"))
    assert "Chưa có bí mật (token)" in capsys.readouterr().out


# ---- doctor: the network section --------------------------------------------------------------------------------------


def road(kind: str) -> httpx.MockTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        if kind == "certificate":
            raise httpx.ConnectError("boom") from ssl.SSLCertVerificationError(1, "[SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate")
        if kind == "unreachable":
            raise httpx.ConnectTimeout("timed out")
        return httpx.Response(401)  # an answer, whatever it says

    return httpx.MockTransport(handler)


async def bound_project(office: Office, profile: dict[str, Any]) -> None:
    """A project whose repository is PAY/billing on the connection git-main."""
    project = await office.projects.create("billing", path="", access=ProjectAccess.READ_ONLY)
    bound = project.model_copy(update={"code_connection": "git-main", "repo_url": "PAY/billing", "profile": profile})
    await office.db.execute("UPDATE projects SET config_json = ? WHERE id = ?", (bound.model_dump_json(), project.id))


async def report(office: Office, kind: str, **kwargs: Any) -> str:
    lines = await doctor.network_report(office, transport=road(kind), env=kwargs.pop("env", {}), which=kwargs.pop("which", lambda _name: None), **kwargs)
    return "\n".join(doctor.render(lines))


async def test_doctor_reports_each_connection_and_never_a_token(office: Office, monkeypatch) -> None:
    await office.connections.save(ConnectionSpec(id="git-main", use="cli_host", config={"base_url": "https://git.congty.test"}), secret={"token": TOKEN})
    await bound_project(office, {"test": ["mvn", "-q", "test"]})
    seen: dict[str, Any] = {}

    def ls_remote(url: str, env: dict[str, str]) -> tuple[int, str]:
        seen.update(url=url, env=env)
        return 0, ""

    monkeypatch.setattr(doctor, "git_ls_remote", ls_remote)
    env = {"HTTPS_PROXY": "http://alice:hunter2@proxy.congty.test:8080", "no_proxy": "localhost,.congty.test"}
    text = await report(office, "ok", env=env, which=lambda name: "/usr/bin/mvn" if name == "mvn" else None)
    assert "✓ Chứng chỉ tin cậy: kho chứng chỉ của hệ điều hành" in text
    assert "HTTPS_PROXY → proxy.congty.test:8080" in text and "no_proxy → localhost, .congty.test" in text and "hunter2" not in text and "alice" not in text
    assert "✓ Kết nối git-main (cli_host): hoạt động (bot)" in text
    assert "✓ TLS tới git.congty.test (git-main): tới được" in text
    assert "✓ Dự án billing: git ls-remote qua kết nối git-main: đọc được https://git.congty.test/scm/PAY/billing.git" in text
    assert "✓ Công cụ mvn (dự án billing): /usr/bin/mvn" in text
    assert seen["url"] == "https://git.congty.test/scm/PAY/billing.git" and f"Bearer {TOKEN}" in "".join(seen["env"].values()), "the token goes through git's environment"
    assert TOKEN not in text
    assert all(line[0] in "✓✗" for line in text.splitlines())


async def test_doctor_names_a_certificate_error_and_goes_on(office: Office, monkeypatch) -> None:
    await office.connections.save(ConnectionSpec(id="git-main", use="cli_host"), secret={"token": TOKEN})
    await office.connections.save(ConnectionSpec(id="git-off", use="cli_host", enabled=False), secret={"token": TOKEN})
    await bound_project(office, {"build": ["gradle", "build"]})
    monkeypatch.setattr(doctor, "git_ls_remote", lambda url, env: (128, f"fatal: unable to access '{url}': SSL certificate problem; header Bearer {TOKEN}"))
    FakeHost.health = Health(state="error", detail=f"401 với token {TOKEN}")
    text = await report(office, "certificate")
    assert "✗ Kết nối git-main (cli_host): error." in text
    assert "✗ TLS tới git.congty.test (git-main): LỖI CHỨNG CHỈ" in text and "ca_bundle: <đường dẫn file .pem>" in text
    assert "✗ Dự án billing: git ls-remote qua kết nối git-main: git báo lỗi" in text
    assert "✗ Công cụ gradle (dự án billing): không có trên PATH" in text, "one failing item does not stop the next"
    assert "git-off" not in text, "a connection that is off is not checked"
    assert TOKEN not in text

    FakeHost.health = Health(state="ok")
    text = await report(office, "unreachable")
    assert "✗ TLS tới git.congty.test (git-main): không tới được (ConnectTimeout" in text and "proxy" in text
    assert "LỖI CHỨNG CHỈ" not in text


async def test_doctor_survives_a_check_that_raises_and_says_which_trust(office: Office, tmp_path, monkeypatch) -> None:
    from actoroffice.core import http as office_http

    await office.connections.save(ConnectionSpec(id="git-main", use="cli_host"), secret={"token": TOKEN})

    async def broken(connection_id: str) -> Health:
        raise RuntimeError("adapter nổ")

    monkeypatch.setattr(office.connections, "health", broken)
    bundle = tmp_path / "ca.pem"
    monkeypatch.setattr(office_http, "_ca_bundle", str(bundle))
    text = await report(office, "ok")
    assert f"✗ Chứng chỉ tin cậy: ca_bundle trỏ tới {bundle} nhưng không có file đó" in text
    assert "✗ Kết nối git-main: không kiểm tra được (RuntimeError: adapter nổ)" in text
    assert "✓ TLS tới git.congty.test (git-main)" in text
    bundle.write_text("", encoding="utf-8")
    assert f"✓ Chứng chỉ tin cậy: file ca_bundle {bundle}" in await report(office, "ok")


async def test_doctor_checks_the_ai_cli(office: Office) -> None:
    office.settings.ai_model = "kiro"
    office.settings.kiro_cli_path = "/opt/kiro-cli"
    calls: list[list[str]] = []

    def run(argv: list[str]) -> tuple[int, str]:
        calls.append(argv)
        return (0, "bot@congty.test") if len(calls) == 1 else (1, "not logged in")

    assert "✓ AI CLI (kiro-cli): có ở /opt/kiro-cli, đã đăng nhập" in await report(office, "ok", run=run)
    assert "✗ AI CLI (kiro-cli): có ở /opt/kiro-cli nhưng chưa đăng nhập. Chạy `kiro-cli login`" in await report(office, "ok", run=run)
    assert calls == [["/opt/kiro-cli", "whoami"]] * 2
