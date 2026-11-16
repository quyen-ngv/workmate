"""One TLS trust for everything the office calls: a configured CA bundle, else the operating system's store."""

from __future__ import annotations

import ssl
import sys
from pathlib import Path

import httpx
import pytest

from actoroffice.adapters import _http as adapter_http
from actoroffice.codeintel.sync import auth_env
from actoroffice.core import http as core_http
from actoroffice.ports.code import CloneAuth


@pytest.fixture(autouse=True)
def default_trust_afterwards():
    yield
    core_http.configure(None)
    adapter_http.set_tls(None)


def bundle(tmp_path: Path) -> str:
    import certifi

    path = tmp_path / "ca.pem"
    path.write_text(Path(certifi.where()).read_text(encoding="utf-8"), encoding="utf-8")
    return str(path)


def test_the_operating_systems_store_is_trusted_and_verification_stays_on() -> None:
    core_http.configure(None)
    ctx = core_http.ssl_context()
    assert ctx.verify_mode == ssl.CERT_REQUIRED and ctx.check_hostname
    assert type(ctx).__module__.startswith("truststore"), "without a bundle the system store is used"
    assert core_http.ssl_context() is ctx, "built once"


def test_a_configured_bundle_is_used_everywhere(tmp_path: Path) -> None:
    path = bundle(tmp_path)
    core_http.configure(path)
    ctx = core_http.ssl_context()
    assert isinstance(ctx, ssl.SSLContext) and not type(ctx).__module__.startswith("truststore") and ctx.verify_mode == ssl.CERT_REQUIRED
    assert core_http.git_tls_config() == [("http.sslCAInfo", path)]
    assert core_http.node_ca_file(tmp_path) == path


def test_adapters_get_the_offices_trust_and_a_test_transport_loads_nothing() -> None:
    adapter_http.set_tls(None)
    assert adapter_http.tls() is True, "an adapter used without the office keeps httpx's default"
    adapter_http.set_tls(core_http.ssl_context)
    assert adapter_http.tls() is core_http.ssl_context()
    assert adapter_http.tls(httpx.MockTransport(lambda r: httpx.Response(200))) is True
    assert core_http.verify(object()) is True and core_http.verify() is core_http.ssl_context()


def test_nothing_switches_verification_off() -> None:
    src = Path(core_http.__file__).resolve().parents[1]
    hits = [str(p.relative_to(src)) for p in src.rglob("*.py") if "verify=False" in p.read_text(encoding="utf-8")]
    assert hits == []


def test_git_is_told_what_to_trust_and_how_to_sign_in() -> None:
    env = auth_env(CloneAuth(url="https://git.example/scm/a/b.git", bearer="tok-123"), [("http.sslBackend", "schannel")])
    assert env["GIT_CONFIG_COUNT"] == "2"
    assert (env["GIT_CONFIG_KEY_0"], env["GIT_CONFIG_VALUE_0"]) == ("http.sslBackend", "schannel")
    assert (env["GIT_CONFIG_KEY_1"], env["GIT_CONFIG_VALUE_1"]) == ("http.extraHeader", "Authorization: Bearer tok-123")
    basic = auth_env(CloneAuth(url="https://x/y.git", username="u", password="p"))
    assert basic["GIT_CONFIG_COUNT"] == "1" and basic["GIT_CONFIG_VALUE_0"].startswith("Authorization: Basic ")
    assert auth_env(None) == {"GIT_TERMINAL_PROMPT": "0"}
    if sys.platform == "win32":
        core_http.configure(None)
        assert core_http.git_tls_config() == [("http.sslBackend", "schannel")]


@pytest.mark.skipif(sys.platform != "win32", reason="the Windows certificate store")
def test_the_ais_cli_gets_the_system_certificates_on_windows(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.delenv("NODE_EXTRA_CA_CERTS", raising=False)
    core_http.configure(None)
    path = core_http.node_ca_file(tmp_path)
    assert path and Path(path).read_text(encoding="ascii").count("BEGIN CERTIFICATE") > 5
    monkeypatch.setenv("NODE_EXTRA_CA_CERTS", "C:/mine.pem")
    assert core_http.node_ca_file(tmp_path) is None, "what the person set is left alone"
