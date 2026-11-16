"""`actoroffice doctor`, the network part: can this machine reach what the office is connected to?

One line per item, ✓ or ✗ with what to do. Every check has a timeout and stands alone: a check that fails or raises
becomes its own ✗ line and the others still run. No line ever holds a token: credentials go to git through its
environment, proxy addresses are shown without their user and password, and everything printed passes the redactor.
"""

from __future__ import annotations

import asyncio
import os
import shutil
import ssl
import subprocess
from collections.abc import Awaitable, Callable, Mapping
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import urlsplit

import httpx

from actoroffice.adapters import registry
from actoroffice.codeintel.sync import auth_env
from actoroffice.core import http as office_http
from actoroffice.ports.code import RepoRef
from actoroffice.ports.connection import ConnectionSpec, ConnectorError

if TYPE_CHECKING:
    from actoroffice.app import Office

Line = tuple[bool, str]  # (ok, text for people)

HEALTH_TIMEOUT_S = 20
TLS_TIMEOUT_S = 10
GIT_TIMEOUT_S = 30
CLI_TIMEOUT_S = 10
PROXY_VARS = ("HTTPS_PROXY", "HTTP_PROXY", "ALL_PROXY", "NO_PROXY")
# where an adapter without an address in its config lives
DEFAULT_URL = {"bitbucket_cloud": "https://api.bitbucket.org", "github": "https://api.github.com"}
URL_KEYS = ("site", "base_url", "api_url", "url")
CA_HELP = (
    "Mạng công ty đang ký lại TLS bằng CA riêng: xin file CA (PEM) từ IT, đặt `ca_bundle: <đường dẫn file .pem>` trong config.yaml rồi chạy lại. "
    "Trên Windows có thể cài CA đó vào kho chứng chỉ của máy thay vì khai ca_bundle"
)


def render(lines: list[Line]) -> list[str]:
    return [f"{'✓' if ok else '✗'} {text}" for ok, text in lines]


def mask_proxy(value: str) -> str:
    """A proxy address without its user and password: "http://u:p@proxy:8080" → "proxy:8080"."""
    parts = urlsplit(value if "://" in value else f"//{value}")
    host = parts.hostname or ""
    return f"{host}:{parts.port}" if host and parts.port else host or "(không đọc được)"


def proxy_lines(env: Mapping[str, str]) -> list[Line]:
    found = []
    for name in PROXY_VARS:
        for key in (name, name.lower()):
            value = env.get(key, "").strip()
            if value:
                shown = ", ".join(mask_proxy(v.strip()) for v in value.split(",") if v.strip()) if name == "NO_PROXY" else mask_proxy(value)
                found.append(f"{key} → {shown}")
                break
    return [(True, "Proxy đang dùng: " + "; ".join(found) if found else "Không có biến proxy (HTTPS_PROXY, HTTP_PROXY, NO_PROXY): office gọi thẳng ra mạng")]


def trust_line() -> Line:
    bundle = office_http.ca_bundle()
    if not bundle:
        return True, "Chứng chỉ tin cậy: kho chứng chỉ của hệ điều hành (chưa khai ca_bundle trong config.yaml)"
    if not Path(bundle).is_file():
        return False, f"Chứng chỉ tin cậy: ca_bundle trỏ tới {bundle} nhưng không có file đó. Sửa `ca_bundle` trong config.yaml thành đường dẫn file PEM đúng"
    return True, f"Chứng chỉ tin cậy: file ca_bundle {bundle}"


def connection_url(spec: ConnectionSpec) -> str | None:
    """The https address a connection calls, from its config; None when it has none (a database, a local command)."""
    try:
        config = registry.validate(spec).model_dump()  # with the adapter's defaults
    except ConnectorError:
        config = spec.config
    for key in URL_KEYS:
        value = config.get(key)
        if isinstance(value, str) and value.strip():
            url = value.strip()
            return url if "://" in url else f"https://{url}"
    return DEFAULT_URL.get(spec.use)


def _is_certificate_error(exc: BaseException) -> bool:
    seen: BaseException | None = exc
    while seen is not None:
        if isinstance(seen, ssl.SSLCertVerificationError) or "CERTIFICATE_VERIFY_FAILED" in str(seen) or "certificate verify failed" in str(seen).lower():
            return True
        seen = seen.__cause__ or seen.__context__
    return False


async def tls_probe(url: str, *, transport: httpx.AsyncBaseTransport | None = None) -> tuple[str, str]:
    """("ok" | "certificate" | "unreachable", detail): whether the address answers over TLS with the office's trust.
    Any HTTP answer counts, also 401 or 404: this asks about the road, not the account."""
    try:
        async with httpx.AsyncClient(verify=office_http.verify(transport), transport=transport, timeout=TLS_TIMEOUT_S, follow_redirects=False) as client:
            response = await client.get(url)
        return "ok", f"HTTP {response.status_code}"
    except Exception as exc:  # every failure of the road is an answer for the report
        if _is_certificate_error(exc):
            return "certificate", str(exc)[:200]
        return "unreachable", f"{type(exc).__name__}: {exc}"[:200]


async def _guard(label: str, check: Awaitable[list[Line]], timeout: float) -> list[Line]:
    """One check, bounded and alone: a timeout or a bug in it is its own ✗ line."""
    try:
        return await asyncio.wait_for(check, timeout)
    except TimeoutError:
        return [(False, f"{label}: quá {int(timeout)} giây không trả lời. Kiểm tra mạng / proxy rồi chạy lại `actoroffice doctor`")]
    except Exception as exc:
        return [(False, f"{label}: không kiểm tra được ({type(exc).__name__}: {exc})"[:300])]


async def _health(office: Office, spec: ConnectionSpec) -> list[Line]:
    health = await office.connections.health(spec.id)
    who = f" ({health.identity})" if health.identity else ""
    if health.state == "ok":
        return [(True, f"Kết nối {spec.id} ({spec.use}): hoạt động{who}")]
    fix = health.detail or "mở Cài đặt › Kết nối, kiểm tra địa chỉ và token"
    return [(False, f"Kết nối {spec.id} ({spec.use}): {health.state}. {fix}")]


async def _tls(spec: ConnectionSpec, transport: httpx.AsyncBaseTransport | None) -> list[Line]:
    url = connection_url(spec)
    if url is None or not url.lower().startswith("https://"):
        return []  # nothing to reach over TLS
    host = urlsplit(url).netloc
    state, detail = await tls_probe(url, transport=transport)
    if state == "ok":
        return [(True, f"TLS tới {host} ({spec.id}): tới được, chứng chỉ được tin")]
    if state == "certificate":
        return [(False, f"TLS tới {host} ({spec.id}): LỖI CHỨNG CHỈ, chứng chỉ của máy chủ không được tin. {CA_HELP}")]
    return [(False, f"TLS tới {host} ({spec.id}): không tới được ({detail}). Kiểm tra địa chỉ, VPN, proxy (HTTPS_PROXY / NO_PROXY) và tường lửa")]


def git_ls_remote(url: str, env: dict[str, str]) -> tuple[int, str]:
    """(exit code, stderr) of `git ls-remote --heads <url>`; the credentials are in `env`, never in argv."""
    done = subprocess.run(["git", "ls-remote", "--heads", url], capture_output=True, text=True, timeout=GIT_TIMEOUT_S, env={**os.environ, **env}, errors="replace")
    return done.returncode, done.stderr.strip()


async def _project_git(office: Office, project: Any) -> list[Line]:
    label = f"Dự án {project.name}: git ls-remote qua kết nối {project.code_connection}"
    spec = await office.connections.get(project.code_connection)
    if spec is None or not spec.enabled:
        return [(False, f"{label}: kết nối không còn hoặc đang tắt. Chọn lại «Kết nối code» của dự án trong Cài đặt")]
    if not project.repo_url:
        return [(False, f"{label}: dự án chưa khai repo trên code host. Điền «Repo trên code host» trong Cài đặt")]
    adapter = await office.connections.adapter(spec.id)
    ask = getattr(adapter, "git_auth", None)
    if ask is None:
        return [(False, f"{label}: adapter {spec.use} không cho biết cách git đọc repo")]
    auth = await ask(RepoRef(connection=spec.id, slug=project.repo_url, default_branch=project.main_branch), write=False)
    env = auth_env(auth, office_http.git_tls_config())
    code, stderr = await asyncio.to_thread(git_ls_remote, auth.url, env)
    if code == 0:
        return [(True, f"{label}: đọc được {auth.url}")]
    text = stderr
    for secret in (auth.password, auth.bearer):
        if secret:
            text = text.replace(secret, "***")
    text = office.redact(text)[:300]
    if "certificate" in text.lower() or "ssl" in text.lower():
        fix = CA_HELP
    elif "401" in text or "403" in text or "authentication" in text.lower():
        fix = "Token của kết nối không đọc được repo này: kiểm tra quyền token (Repository read) và tên repo"
    else:
        fix = "Kiểm tra tên repo, mạng và proxy"
    return [(False, f"{label}: git báo lỗi ({text or f'mã {code}'}). {fix}")]


def tool_lines(projects: list[Any], which: Callable[[str], str | None]) -> list[Line]:
    """Are the programs the project profiles call (mvn, npm, gradle…) on PATH?"""
    from actoroffice.services.checks import profile_commands

    users: dict[str, list[str]] = {}
    for project in projects:
        for argv in profile_commands(project.profile):
            users.setdefault(str(argv[0]), [])
            if project.name not in users[str(argv[0])]:
                users[str(argv[0])].append(project.name)
    lines = []
    for tool, names in sorted(users.items()):
        local = any(sep in tool for sep in ("/", "\\"))  # ./mvnw, scripts/test.sh: a file of the repository, not of PATH
        if local:
            continue
        found = which(tool)
        who = ", ".join(names)
        lines.append((True, f"Công cụ {tool} (dự án {who}): {found}") if found else (False, f"Công cụ {tool} (dự án {who}): không có trên PATH. Cài {tool} hoặc thêm nó vào PATH của máy chạy office"))
    return lines


def _run_cli(argv: list[str]) -> tuple[int, str]:
    done = subprocess.run(argv, capture_output=True, text=True, timeout=CLI_TIMEOUT_S, errors="replace", stdin=subprocess.DEVNULL)
    return done.returncode, (done.stdout or done.stderr).strip()


async def ai_cli_lines(office: Office, which: Callable[[str], str | None], run: Callable[[list[str]], tuple[int, str]]) -> list[Line]:
    model = str(office.settings.ai_model or office.settings.default_model or "")
    if model.startswith("kiro"):
        from actoroffice.runtimes.kiro_acp import find_kiro_cli

        path = office.settings.kiro_cli_path or which("kiro-cli") or find_kiro_cli()
        if not path:
            return [(False, "AI CLI (kiro-cli): không tìm thấy. Cài Kiro CLI rồi chạy `kiro-cli login`, hoặc đặt kiro_cli_path trong config.yaml")]
        try:
            code, _ = await asyncio.to_thread(run, [path, "whoami"])
        except (OSError, subprocess.TimeoutExpired) as exc:
            return [(False, f"AI CLI (kiro-cli): có ở {path} nhưng `kiro-cli whoami` không chạy được ({type(exc).__name__}). Thử chạy tay `kiro-cli whoami`")]
        if code == 0:
            return [(True, f"AI CLI (kiro-cli): có ở {path}, đã đăng nhập")]
        return [(False, f"AI CLI (kiro-cli): có ở {path} nhưng chưa đăng nhập. Chạy `kiro-cli login`")]
    path = which("claude")
    try:
        import claude_agent_sdk  # noqa: F401

        sdk = True
    except ImportError:
        sdk = False
    if not sdk and not path:
        return [(False, "AI CLI (claude): thiếu claude-agent-sdk và không có lệnh claude. Chạy `uv sync`")]
    where = "lệnh " + path if path else "bản đi kèm claude-agent-sdk"
    return [(True, f"AI CLI (claude, model {model or '?'}): {where}; không có cách kiểm tra đăng nhập nhanh, nếu lượt chạy báo chưa đăng nhập thì chạy `claude` rồi gõ /login")]


async def network_report(
    office: Office,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
    env: Mapping[str, str] | None = None,
    which: Callable[[str], str | None] = shutil.which,
    run: Callable[[list[str]], tuple[int, str]] = _run_cli,
) -> list[Line]:
    """Every network check of `doctor`, in the order people read them. `transport`, `env`, `which`, `run`: for tests."""
    lines: list[Line] = [trust_line(), *proxy_lines(os.environ if env is None else env)]
    specs = [s for s in await office.connections.list() if s.enabled]
    if not specs:
        lines.append((True, "Chưa có kết nối nào (thêm ở Cài đặt › Kết nối hoặc `actoroffice connections add`)"))
    for spec in specs:
        lines += await _guard(f"Kết nối {spec.id}", _health(office, spec), HEALTH_TIMEOUT_S)
        lines += await _guard(f"TLS của {spec.id}", _tls(spec, transport), TLS_TIMEOUT_S + 5)
    projects = [p for p in await office.projects.list() if not p.is_default]
    for project in projects:
        if project.code_connection:
            lines += await _guard(f"Dự án {project.name}: git ls-remote", _project_git(office, project), GIT_TIMEOUT_S + 10)
    try:
        lines += tool_lines(projects, which)
    except Exception as exc:
        lines.append((False, f"Công cụ của dự án: không kiểm tra được ({type(exc).__name__}: {exc})"))
    lines += await _guard("AI CLI", ai_cli_lines(office, which, run), CLI_TIMEOUT_S + 5)
    return [(ok, office.redact(text)) for ok, text in lines]
