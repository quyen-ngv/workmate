"""What the generic adapters (`http`, `mcp`, `command`) share: finding references in text, reading values out of JSON
by a simple path, filling `{ref}` / `{query}` / `{limit}` into templates, and turning an answer into a document.

A generic adapter is driven by its config only, so everything a config can get wrong is checked here when it is saved.
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import re
from collections.abc import Iterable
from typing import Any, Literal
from urllib.parse import urlsplit

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

from actoroffice.adapters._http import host_of
from actoroffice.adapters.atlassian.storage_html import storage_to_markdown
from actoroffice.ports.connection import ConnectorError
from actoroffice.ports.docs import DocContent, DocHit, DocRef

SECRET_FIELDS = ("token", "username", "password")  # what the settings form asks for; a config says which one it uses
MAX_TEXT_CHARS = 400_000  # a longer document is cut and flagged `truncated`
MAX_LINKS = 50
MAX_LINE = 200  # the one line of an outside system's own words an error may carry
LOOPBACK_HOSTS = frozenset({"localhost", "127.0.0.1", "::1"})
_TRAILING = ".,;:!?"  # punctuation that follows a reference in a sentence
_PLACEHOLDER_RE = re.compile(r"\{(\w+)\}")
_PATH_STEP_RE = re.compile(r"\[(\*|\d+)\]|([^.\[\]/]+)")
_HEADER_NAME_RE = re.compile(r"[A-Za-z0-9!#$%&'*+.^_`|~-]+")
_SECRET_HEADERS = frozenset({"authorization", "proxy-authorization", "cookie"})
_ENV_NAME_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_HTML_DROP_RE = re.compile(r"<!--.*?-->|<(script|style|head)\b.*?</\1\s*>", re.DOTALL | re.IGNORECASE)
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


# ---- references --------------------------------------------------------------------------------------------


def _group_source(pattern: str, name: str = "ref") -> str | None:
    """The source text of the named group in a regular expression; None when the pattern has no such group."""
    start = pattern.find(f"(?P<{name}>")
    if start < 0:
        return None
    begin = pos = start + len(f"(?P<{name}>")
    depth, in_class = 1, False
    while pos < len(pattern):
        ch = pattern[pos]
        if ch == "\\":
            pos += 2
            continue
        if in_class:
            in_class = ch != "]"
        elif ch == "[":
            in_class = True
            pos += 1 if pattern[pos + 1 : pos + 2] == "^" else 0
            pos += 1 if pattern[pos + 1 : pos + 2] == "]" else 0  # a ']' right after the opening is a literal
        elif ch == "(":
            depth += 1
        elif ch == ")":
            depth -= 1
            if depth == 0:
                return pattern[begin:pos]
        pos += 1
    return None


class Recognizer:
    """The connection's `recognize` patterns. A reference is the named group `ref` of a match, or the whole match when
    the pattern has no such group. `canonical` is the gate before anything is fetched: a value passes only when it is
    entirely one reference (or entirely one match), never when it merely contains one."""

    def __init__(self, patterns: Iterable[str]) -> None:
        self._pairs: list[tuple[re.Pattern[str], re.Pattern[str]]] = []
        for pattern in patterns:
            whole = re.compile(pattern)
            inner = _group_source(pattern)
            self._pairs.append((whole, re.compile(inner, whole.flags) if inner is not None else whole))

    def find(self, text: str) -> list[str]:
        found: list[str] = []
        for whole, inner in self._pairs:
            for m in whole.finditer(text):
                value = m.group("ref") if "ref" in whole.groupindex else m.group(0)
                if not value:
                    continue
                bare = value.rstrip(_TRAILING)
                if bare and bare != value and inner.fullmatch(bare):
                    value = bare  # "…/page/12." at the end of a sentence
                if value not in found:
                    found.append(value)
        return found

    def canonical(self, value: str) -> str | None:
        value = value.strip()
        for whole, inner in self._pairs:
            m = whole.fullmatch(value)
            if m is not None:
                ref = m.group("ref") if "ref" in whole.groupindex else value
                if ref:
                    return ref
            if inner.fullmatch(value):
                return value
        return None

    def resolve(self, text: str) -> str | None:
        """A reference out of a search result: the value itself, or the first one it contains."""
        return self.canonical(text) or next(iter(self.find(text)), None)


def check_patterns(patterns: list[str]) -> list[str]:
    if not patterns:
        raise ValueError("cần ít nhất một mẫu regex để nhận ra tham chiếu, ví dụ (?P<ref>DOC-\\d+)")
    for pattern in patterns:
        try:
            whole = re.compile(pattern)
            inner = _group_source(pattern)
            if inner is not None:
                re.compile(inner, whole.flags)
        except re.error as exc:
            raise ValueError(f"regex không hợp lệ «{pattern}»: {exc}") from None
        if whole.search("") is not None:
            raise ValueError(f"regex «{pattern}» khớp cả chuỗi rỗng nên sẽ nhận mọi thứ là tham chiếu")
    return patterns


def allowed(ref: str, allow: list[str]) -> bool:
    """`allow` entries are prefixes, or glob patterns when they hold `*`, `?` or `[`. Empty: every reference."""
    return not allow or any(fnmatch.fnmatchcase(ref, a) if any(c in a for c in "*?[") else ref.startswith(a) for a in allow)


# ---- reading a value out of JSON ----------------------------------------------------------------------------


def path_steps(path: str) -> list[str]:
    """`$.body.content`, `body.content`, `/body/content`, `items[0].title`, `value[*].url` → steps. `*` means each."""
    text = path.strip().removeprefix("$")
    steps: list[str] = []
    pos = 0
    while pos < len(text):
        if text[pos] in "./":
            pos += 1
            continue
        m = _PATH_STEP_RE.match(text, pos)
        if m is None:
            raise ValueError(f"đường dẫn «{path}» không đọc được. Viết như $.body.content, items[0].title hoặc value[*].url")
        steps.append(m.group(1) if m.group(1) is not None else m.group(2))
        pos = m.end()
    return steps


def check_path(path: str) -> str:
    path = path.strip()
    path_steps(path)
    return path


def pick(data: Any, path: str) -> Any:
    """The value at `path`; None when it is not there. A path with `*` gives the list of every match."""
    current, many = [data], False
    for step in path_steps(path):
        following: list[Any] = []
        for item in current:
            if isinstance(item, dict):
                if step in item:
                    following.append(item[step])
            elif isinstance(item, list):
                if step == "*":
                    following += item
                elif step.isdigit() and int(step) < len(item):
                    following.append(item[int(step)])
        many = many or step == "*"
        current = following
    if many:
        return current
    return current[0] if current else None


def as_text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, bool | int | float):
        return str(value)
    if isinstance(value, list) and all(isinstance(v, str) for v in value):
        return "\n".join(value)
    return json.dumps(value, ensure_ascii=False, indent=2)


# ---- templates ----------------------------------------------------------------------------------------------


def placeholders(template: Any) -> set[str]:
    if isinstance(template, str):
        return set(_PLACEHOLDER_RE.findall(template))
    if isinstance(template, dict):
        return set().union(*(placeholders(v) for v in template.values())) if template else set()
    if isinstance(template, list | tuple):
        return set().union(*(placeholders(v) for v in template)) if template else set()
    return set()


def fill(template: Any, values: dict[str, Any]) -> Any:
    """`{name}` replaced by `values[name]` in every string of a template (other braces are left alone). A string that
    is exactly one placeholder becomes the value itself, so `{limit}` stays a number."""
    if isinstance(template, str):
        whole = _PLACEHOLDER_RE.fullmatch(template)
        if whole is not None and whole.group(1) in values:
            return values[whole.group(1)]
        return _PLACEHOLDER_RE.sub(lambda m: str(values[m.group(1)]) if m.group(1) in values else m.group(0), template)
    if isinstance(template, dict):
        return {key: fill(value, values) for key, value in template.items()}
    if isinstance(template, list | tuple):
        return [fill(value, values) for value in template]
    return template


def require_placeholder(template: Any, name: str, where: str) -> None:
    if name not in placeholders(template):
        raise ValueError(f"{where} phải có chỗ điền {{{name}}}")


# ---- errors that never carry a secret ------------------------------------------------------------------------


def scrub(text: str, secrets: Iterable[str]) -> str:
    for secret in sorted({s for s in secrets if s}, key=len, reverse=True):
        text = text.replace(secret, "***")
    return text


def short_line(text: str, secrets: Iterable[str] = ()) -> str:
    """The first non-empty line of what an outside system said, without control characters or secrets, cut short."""
    line = next((part.strip() for part in text.splitlines() if part.strip()), "")
    return scrub(_CONTROL_RE.sub("", line), secrets)[:MAX_LINE]


# ---- config pieces -------------------------------------------------------------------------------------------


class Strict(BaseModel):
    """A mistyped key in a config-only adapter would silently do nothing, so unknown keys are refused."""

    model_config = ConfigDict(extra="forbid")


class SourceConfig(Strict):
    recognize: list[str] = Field(
        validation_alias=AliasChoices("recognize", "match"),
        description="Mẫu regex nhận ra tham chiếu trong văn bản. Nhóm có tên `ref` là phần dùng để đọc, ví dụ (?P<ref>DOC-\\d+); không có nhóm thì lấy cả đoạn khớp",
    )
    allow: list[str] = Field(default_factory=list, description='Tham chiếu được phép đọc: tiền tố ("DOC-") hoặc mẫu có * ("docs/*.md"). Để trống: mọi tham chiếu nhận ra được')

    @field_validator("recognize", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @field_validator("recognize")
    @classmethod
    def _patterns(cls, value: list[str]) -> list[str]:
        return check_patterns(value)

    @field_validator("allow")
    @classmethod
    def _allow(cls, value: list[str]) -> list[str]:
        return [v.strip() for v in value if v.strip()]


class DocMapping(Strict):
    """Where the parts of a document are in a JSON answer. Every path is optional."""

    text: str = Field(default="", description="Đường dẫn tới nội dung, ví dụ $.body.content. Để trống: cả phản hồi là nội dung")
    title: str = Field(default="", description="Đường dẫn tới tiêu đề, ví dụ $.title. Để trống: dùng chính tham chiếu")
    version: str = Field(default="", description="Đường dẫn tới phiên bản, ví dụ $.eTag. Để trống: dùng mã băm của nội dung")
    url: str = Field(default="", description="Đường dẫn tới địa chỉ xem tài liệu trên web")
    format: Literal["markdown", "html", "plain"] = Field(default="markdown", description="Dạng của nội dung; html được chuyển thành markdown")

    @field_validator("text", "title", "version", "url")
    @classmethod
    def _paths(cls, value: str) -> str:
        return check_path(value)


class HitMapping(Strict):
    """Where search results are in a JSON answer. An item may also be a plain string: a reference or a URL."""

    items: str = Field(default="", description="Đường dẫn tới danh sách kết quả, ví dụ $.results hoặc $.value[*].webUrl. Để trống: cả phản hồi là danh sách")
    ref: str = Field(default="", description="Trong mỗi kết quả: đường dẫn tới tham chiếu (hoặc địa chỉ) của tài liệu")
    title: str = Field(default="", description="Trong mỗi kết quả: đường dẫn tới tiêu đề")
    snippet: str = Field(default="", description="Trong mỗi kết quả: đường dẫn tới đoạn trích")
    url: str = Field(default="", description="Trong mỗi kết quả: đường dẫn tới địa chỉ web")

    @field_validator("items", "ref", "title", "snippet", "url")
    @classmethod
    def _paths(cls, value: str) -> str:
        return check_path(value)


def check_header_name(name: str) -> str:
    name = name.strip()
    if not _HEADER_NAME_RE.fullmatch(name):
        raise ValueError(f"tên header không hợp lệ: «{name}»")
    return name


def check_plain_headers(headers: dict[str, str]) -> dict[str, str]:
    """Headers written in a config are not secret. One that carries a credential belongs in the secret, not here."""
    for name in headers:
        check_header_name(name)
        if name.strip().lower() in _SECRET_HEADERS:
            raise ValueError(f"header «{name}» mang bí mật nên không được ghi trong cấu hình. Dùng `auth` và nhập bí mật ở ô bí mật của kết nối")
    return {name.strip(): str(value) for name, value in headers.items()}


def check_url(value: str, *, sends_secret: bool, what: str) -> str:
    """An http(s) URL with a host and no credentials in it. Plain http only when no secret travels, or on this machine."""
    value = value.strip()
    parts = urlsplit(value)
    host = host_of(value)
    if parts.scheme not in ("http", "https") or not host:
        raise ValueError(f"{what} phải là địa chỉ như https://docs.acme.example/api")
    if parts.username or parts.password:
        raise ValueError(f"{what} không được chứa tài khoản/mật khẩu. Nhập bí mật ở ô bí mật của kết nối")
    if parts.scheme == "http" and sends_secret and host not in LOOPBACK_HOSTS:
        raise ValueError(f"{what} phải là https khi kết nối có gửi bí mật (http chỉ dùng được với máy này hoặc auth: none)")
    return value


def check_secret_env(value: Any) -> Any:
    """`[NAME, …]` (the secret field has the variable's name) or `{NAME: secret field}`."""
    if isinstance(value, list | tuple):
        value = {str(name): str(name) for name in value}
    if isinstance(value, dict):
        for name in value:
            if not _ENV_NAME_RE.fullmatch(str(name)):
                raise ValueError(f"tên biến môi trường không hợp lệ: «{name}»")
    return value


def missing_secrets(names: Iterable[str], secret: dict[str, str]) -> list[str]:
    return [name for name in names if not secret.get(name)]


# ---- an answer as a document ---------------------------------------------------------------------------------


def content_version(text: str) -> str:
    return "sha256:" + hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def html_to_markdown(html: str) -> tuple[str, list[str]]:
    markdown, _links, notes, _files = storage_to_markdown(_HTML_DROP_RE.sub("", html))
    return markdown, notes


def parse_json(raw: str) -> Any:
    """The answer as JSON; None when it is not JSON (a plain-text document is a normal answer)."""
    try:
        return json.loads(raw)
    except ValueError:
        return None


def document(kind: str, recognizer: Recognizer, ref: str, raw: str, data: Any, mapping: DocMapping, label: str, *, truncated: bool = False) -> DocContent:
    """`raw` is the answer as text, `data` the same answer as JSON (None when it is not JSON)."""
    if mapping.text:
        if data is None:
            raise ConnectorError("invalid", f"{label}: câu trả lời không phải JSON nên không có «{mapping.text}». Để trống `text` nếu cả câu trả lời là nội dung")
        value = pick(data, mapping.text)
        if value is None:
            raise ConnectorError("invalid", f"{label}: câu trả lời không có trường «{mapping.text}» (kiểm tra `text` trong cấu hình)")
        body = as_text(value)
    else:
        body = raw
    body = body.replace("\r\n", "\n")  # a Windows command line or server: the same document, the same content hash
    notes: list[str] = []
    if mapping.format == "html":
        body, notes = html_to_markdown(body)
    if len(body) > MAX_TEXT_CHARS:
        body, truncated = body[:MAX_TEXT_CHARS], True
    if truncated:
        notes.append("Tài liệu dài quá giới hạn nên bị cắt bớt phần cuối")

    def meta(path: str) -> str:
        return as_text(pick(data, path)).strip() if path and data is not None else ""

    return DocContent(
        title=meta(mapping.title)[:300] or ref,
        text=body,
        version=meta(mapping.version)[:MAX_LINE] or content_version(body),
        url=meta(mapping.url) or (ref if ref.startswith(("http://", "https://")) else ""),
        links=[DocRef(ref=found, kind=kind) for found in recognizer.find(body) if found != ref][:MAX_LINKS],
        truncated=truncated,
        notes=notes,
    )


def hits(kind: str, recognizer: Recognizer, allow: list[str], data: Any, mapping: HitMapping, limit: int, label: str) -> list[DocHit]:
    """Search results the connection can later fetch: an item whose reference is not recognised or not allowed is dropped."""
    items = pick(data, mapping.items) if mapping.items else data
    if items is None:
        return []
    if not isinstance(items, list):
        raise ConnectorError("invalid", f"{label}: «{mapping.items or '$'}» trong kết quả tìm kiếm không phải một danh sách (kiểm tra `items` trong cấu hình)")
    found: dict[str, DocHit] = {}
    for item in items:
        if isinstance(item, dict):
            ref_text, title, snippet, url = (as_text(pick(item, path)).strip() if path else "" for path in (mapping.ref, mapping.title, mapping.snippet, mapping.url))
        else:
            ref_text, title, snippet, url = as_text(item).strip(), "", "", ""
            url = ref_text if ref_text.startswith(("http://", "https://")) else ""
        ref = next((r for r in (recognizer.resolve(text) for text in (ref_text, url) if text) if r), None)
        if ref is None or ref in found or not allowed(ref, allow):
            continue
        found[ref] = DocHit(ref=DocRef(ref=ref, kind=kind), title=title[:300] or ref, snippet=snippet[:240], url=url)
        if len(found) >= limit:
            break
    return list(found.values())
