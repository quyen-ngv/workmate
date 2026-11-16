"""People and teams: the humans the AI engineer works with (BA, Dev, QE, lead, admin)."""

from __future__ import annotations

import fnmatch
import re
from enum import StrEnum
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError, ValidationInfo, field_validator, model_validator

from .stage import GatePolicy, StageTemplate


class Role(StrEnum):
    BA = "ba"
    DEV = "dev"
    QE = "qe"
    LEAD = "lead"
    ADMIN = "admin"
    OWNER = "owner"  # relative to a ticket: the person who asked for it


ROLE_VI = {
    Role.BA: "BA",
    Role.DEV: "Dev",
    Role.QE: "QE",
    Role.LEAD: "Lead",
    Role.ADMIN: "Admin",
    Role.OWNER: "người yêu cầu",
}

# The person running the office locally (web login, CLI). Acts as admin everywhere.
LOCAL_OWNER_ID = "user"


class User(BaseModel):
    id: str
    name: str
    email: str | None = None
    telegram_id: int | None = None
    default_roles: list[str] = Field(default_factory=list)
    active: bool = True
    created_at: str = ""


MCP_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
MCP_RESERVED_NAMES = frozenset({"office"})  # the office's own tool server (mcp__office__…)
MCP_DEFAULT_ALLOWED = ("get_*", "list_*", "search_*", "read_*", "find_*")  # what the form suggests; never added silently
MCP_LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
# Programs that read a string as shell text: a command through one of them is a shell string in disguise
# (the same list as adapters/generic/command.py; the domain layer imports nothing of the office).
MCP_SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "cmd", "powershell", "pwsh", "wsl", "busybox"})
_MCP_TYPE_ALIASES = {"streamable-http": "http", "streamable_http": "http", "streamablehttp": "http", "local": "stdio"}
_SECRET_NAMES = frozenset({"authorization", "proxy-authorization", "cookie", "set-cookie"})
_SECRET_NAME_PARTS = ("TOKEN", "SECRET", "PASSWORD", "KEY")
_SECRET_VALUE_RE = re.compile(r"^\s*(?:bearer|basic|token)\s+\S{6,}|\b(?:ghp_|gho_|github_pat_|glpat-|xox[abprs]-|sk-|ATATT|ATBB)[\w-]{6,}", re.IGNORECASE)
_ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
_HEADER_NAME_RE = re.compile(r"^[A-Za-z0-9!#$%&'*+.^_`|~-]+$")
_SHELL_CHARS_RE = re.compile(r"[;&|<>`$\r\n]")


def secret_looking(name: str, value: str = "") -> bool:
    """A variable / header whose name or value looks like a credential (it belongs in the secret store)."""
    upper = name.strip().upper()
    return name.strip().lower() in _SECRET_NAMES or any(part in upper for part in _SECRET_NAME_PARTS) or bool(_SECRET_VALUE_RE.search(value or ""))


def check_mcp_name(name: str) -> str:
    if not MCP_NAME_RE.match(name):
        raise ValueError("tên MCP server chỉ gồm chữ thường, số, '-' và '_', bắt đầu bằng chữ hoặc số, tối đa 32 ký tự (ví dụ: bitbucket)")
    if name in MCP_RESERVED_NAMES:
        raise ValueError(f"tên '{name}' dành riêng cho bộ tool của office, hãy chọn tên khác")
    return name


class McpServer(BaseModel):
    """An outside MCP server attached to a team's AI. Secret values are never here: `secret_env` / `secret_headers`
    hold only the NAMES; the values live in the secret store (services/mcp_servers.py)."""

    model_config = ConfigDict(extra="forbid")

    type: Literal["stdio", "http", "sse"] = "stdio"
    command: str = ""  # stdio: the program, e.g. npx
    args: list[str] = Field(default_factory=list)
    env: dict[str, str] = Field(default_factory=dict)  # not secret
    secret_env: list[str] = Field(default_factory=list)  # names of variables whose values are secrets
    url: str = ""  # http / sse
    headers: dict[str, str] = Field(default_factory=dict)  # not secret
    secret_headers: list[str] = Field(default_factory=list)  # names of headers whose values are secrets
    # Glob patterns on the server's bare tool names (case-sensitive). A matching tool runs without asking a person;
    # empty: every tool asks.
    allowed_tools: list[str] = Field(default_factory=list)
    disabled: bool = False
    timeout_s: int | None = Field(default=None, ge=1, le=600)
    # Set only when reading stored settings (never accepted from a person):
    legacy_inline_secret: bool = False  # an entry of the old free shape holding a secret in env / headers: re-enter it
    invalid: str = ""  # why a stored entry of the old shape cannot be used; such a server never runs

    @field_validator("args", mode="before")
    @classmethod
    def _args(cls, value: Any) -> Any:
        if isinstance(value, str):
            raise ValueError('phải là danh sách đối số, ví dụ ["-y", "@acme/bitbucket-mcp"], không phải một chuỗi')
        return [str(arg) for arg in value] if isinstance(value, list | tuple) else value

    @field_validator("secret_env", "secret_headers", "allowed_tools", mode="before")
    @classmethod
    def _names(cls, value: Any) -> Any:
        if isinstance(value, str):
            raise ValueError("phải là danh sách tên")
        return list(dict.fromkeys(str(item).strip() for item in value)) if isinstance(value, list | tuple) else value

    @model_validator(mode="after")
    def _check(self) -> McpServer:
        if self.invalid:
            return self  # a stored entry that cannot run: kept so a person can see and fix it
        self.command, self.url = self.command.strip(), self.url.strip()
        if self.type == "stdio":
            self._check_stdio()
        else:
            self._check_http()
        for pattern in self.allowed_tools:
            if not pattern or len(pattern) > 100 or re.search(r"[\s,]", pattern):
                raise ValueError(f"allowed_tools: mẫu «{pattern}» không hợp lệ (mỗi mẫu là một tên tool hoặc mẫu như get_*, không có khoảng trắng hay dấu phẩy)")
        if len(self.allowed_tools) > 100:
            raise ValueError("allowed_tools: tối đa 100 mẫu")
        return self

    def _check_stdio(self) -> None:
        if self.url or self.headers or self.secret_headers:
            raise ValueError("url / headers / secret_headers: chỉ dùng với type http hoặc sse; server stdio dùng command, args, env, secret_env")
        if not self.command:
            raise ValueError("command: cần chương trình chạy MCP server khi type là stdio, ví dụ npx")
        stem = self.command.replace("\\", "/").rsplit("/", 1)[-1].lower().removesuffix(".exe")
        if stem in MCP_SHELLS:
            raise ValueError(f"command: không chạy qua shell («{stem}»). Ghi thẳng chương trình cần chạy, đối số để ở args")
        spaced = re.search(r"\s", self.command) is not None
        if _SHELL_CHARS_RE.search(self.command) or (spaced and ("/" not in self.command and "\\" not in self.command or re.search(r"\s-", self.command))):
            raise ValueError("command: chỉ ghi tên chương trình (ví dụ npx), không phải cả dòng lệnh; đối số để ở args, mỗi đối số một mục")
        if "\x00" in "".join([self.command, *self.args]):
            raise ValueError("args: có ký tự không hợp lệ")
        self._check_plain("env", self.env, "secret_env", _ENV_NAME_RE)
        self._check_secret_names("secret_env", self.secret_env, self.env, _ENV_NAME_RE)

    def _check_http(self) -> None:
        if self.command or self.args or self.env or self.secret_env:
            raise ValueError("command / args / env / secret_env: chỉ dùng với type stdio; server http / sse dùng url, headers, secret_headers")
        parts = urlsplit(self.url)
        host = (parts.hostname or "").lower()
        if parts.scheme not in ("http", "https") or not host:
            raise ValueError("url: cần địa chỉ của MCP server, ví dụ https://mcp.acme.example/mcp")
        if parts.username or parts.password:
            raise ValueError("url: không được chứa tài khoản / mật khẩu; bí mật khai tên ở secret_headers")
        if parts.scheme == "http" and host not in MCP_LOOPBACK_HOSTS:
            raise ValueError("url: phải là https (http chỉ dùng được với máy này: localhost, 127.0.0.1)")
        self._check_plain("headers", self.headers, "secret_headers", _HEADER_NAME_RE)
        lowered = {name.lower() for name in self.headers}
        self._check_secret_names("secret_headers", self.secret_headers, lowered, _HEADER_NAME_RE, lower=True)

    def _check_plain(self, field: str, values: dict[str, str], secret_field: str, name_re: re.Pattern[str]) -> None:
        for name, value in values.items():
            if not name_re.match(name):
                raise ValueError(f"{field}: tên «{name}» không hợp lệ")
            if not self.legacy_inline_secret and secret_looking(name, value):
                raise ValueError(
                    f"{field}: «{name}» trông như bí mật nên không được ghi ở đây. Ghi tên «{name}» vào {secret_field} rồi nhập giá trị ở ô bí mật"
                )

    @staticmethod
    def _check_secret_names(field: str, names: list[str], plain: Any, name_re: re.Pattern[str], *, lower: bool = False) -> None:
        for name in names:
            if not name_re.match(name):
                raise ValueError(f"{field}: tên «{name}» không hợp lệ")
            if (name.lower() if lower else name) in plain:
                raise ValueError(f"{field}: «{name}» đã có giá trị thường ở trên; mỗi tên chỉ khai một nơi")

    @classmethod
    def from_stored(cls, raw: Any) -> McpServer:
        """An entry as it sits in stored settings: the current shape, or the old free dict the runtimes took as is
        (`{command, args, env}` without a type, `{type: http|sse, url, headers}` with a token inline). Never raises:
        what cannot be mapped is kept as an entry that does not run (`invalid`)."""
        if isinstance(raw, McpServer):
            return raw
        if not isinstance(raw, dict):
            return cls.model_construct(disabled=True, invalid="mục này không phải một cấu hình MCP server")
        data = {key: value for key, value in raw.items() if key in cls.model_fields}
        kind = str(raw.get("type") or raw.get("transport") or ("http" if raw.get("url") and not raw.get("command") else "stdio")).lower()
        data["type"] = _MCP_TYPE_ALIASES.get(kind, kind)
        for other in ("url", "headers", "secret_headers") if data["type"] == "stdio" else ("command", "args", "env", "secret_env"):
            data.pop(other, None)  # the old shape let both transports' keys sit side by side; only one was ever read
        for field in ("env", "headers"):
            if isinstance(data.get(field), dict):
                data[field] = {str(k): "" if v is None else str(v) for k, v in data[field].items()}
        inline = any(secret_looking(name, value) for field in ("env", "headers") for name, value in (data.get(field) or {}).items())
        data["legacy_inline_secret"], data["invalid"] = inline, ""
        try:
            return cls.model_validate(data)
        except ValidationError as exc:
            kept = {k: v for k, v in data.items() if k in ("type", "command", "url") and isinstance(v, str) and (k != "type" or v in ("stdio", "http", "sse"))}
            return cls.model_construct(**kept, disabled=True, invalid=validation_text(exc))


def mcp_unattended(patterns: list[str], tool: str) -> bool:
    """Whether a server's bare tool name matches one of its `allowed_tools` globs (case-sensitive)."""
    return any(fnmatch.fnmatchcase(tool, pattern) for pattern in patterns)


def validation_text(exc: ValidationError) -> str:
    """A pydantic error as one plain Vietnamese line that names the field."""
    lines = []
    for error in exc.errors():
        where = ".".join(str(part) for part in error.get("loc", ()) if part != "__root__")
        said = str(error.get("msg", "")).removeprefix("Value error, ")
        if error.get("type") == "extra_forbidden":
            said = "không có trường này"
        elif not said or said.startswith(("Input should", "Field required")):
            said = "thiếu giá trị" if error.get("type") == "missing" else "giá trị không đúng kiểu hoặc ngoài khoảng cho phép"
        # our own messages already start with the field they are about
        lines.append(said if not where or said.startswith(where.rsplit(".", 1)[-1]) or ":" in said[:40] else f"{where}: {said}")
    return "; ".join(dict.fromkeys(lines))


class TeamSettings(BaseModel):
    """Per-team overrides. Anything left empty falls back to the office defaults (domain/stage.py)."""

    gates: dict[str, GatePolicy] = Field(default_factory=dict)
    stage_templates: dict[str, StageTemplate] = Field(default_factory=dict)
    model: str | None = None  # runtime for this team's tickets: "sonnet", "opus", "kiro", "kiro:<id>"
    jira_jql: str | None = None  # extra JQL for the Jira mirror (default: project = KEY AND labels = ai)
    default_template: str = "code"
    # Outside MCP servers for this team's tickets (see TurnSpec.mcp_servers), e.g. a shared Bitbucket / Jira MCP server.
    # Managed through services/mcp_servers.py (secrets are stored apart).
    mcp_servers: dict[str, McpServer] = Field(default_factory=dict)

    @field_validator("mcp_servers", mode="before")
    @classmethod
    def _mcp_servers(cls, value: Any, info: ValidationInfo) -> Any:
        if not isinstance(value, dict):
            return value
        if (info.context or {}).get("stored"):
            # Read back from the database: settings written before the typed shape must still load.
            return {str(name): McpServer.from_stored(raw) for name, raw in value.items()}
        for name, raw in value.items():
            check_mcp_name(str(name))
            if isinstance(raw, dict) and (raw.get("legacy_inline_secret") or raw.get("invalid")):
                raise ValueError(f"mcp_servers.{name}: cấu hình cũ còn giữ bí mật trong env / headers; hãy nhập lại bí mật ở màn hình MCP của team")
        return value


class Team(BaseModel):
    id: str
    name: str
    jira_project_key: str | None = None
    confluence_space: str | None = None
    confluence_parent_id: str | None = None
    settings: TeamSettings = Field(default_factory=TeamSettings)
    created_at: str = ""


class Member(BaseModel):
    user: User
    roles: list[str] = Field(default_factory=list)
