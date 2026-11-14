"""`use: command`: a system with a command-line client (`svn cat`, `p4 print`…) as a document source.

What is run is an argument list from the config with `{ref}` filled in, never a shell string. A reference reaches a
command only when it is entirely what `recognize` describes, does not start with `-` and has no `..` segment. The
command gets a small environment (not the office's), a time limit, an output limit, and is killed with everything it
started when it runs over.
"""

from __future__ import annotations

import asyncio
import contextlib
import os
import shutil
import sys
from pathlib import Path
from typing import Any, ClassVar, Literal

from pydantic import BaseModel, Field, field_validator

from actoroffice.adapters._http import now_iso
from actoroffice.adapters.generic.common import (
    SECRET_FIELDS,
    Recognizer,
    SourceConfig,
    allowed,
    check_secret_env,
    content_version,
    document,
    fill,
    missing_secrets,
    placeholders,
    short_line,
)
from actoroffice.adapters.generic.common import DocMapping as _DocMapping
from actoroffice.ports.connection import Capability, ConnectorError, Health, Scope
from actoroffice.ports.docs import DocContent, DocHit, DocRef
from actoroffice.proctree import kill_tree

NAME = "command"
LABEL = "Lệnh"
MAX_HITS = 50
MAX_QUERY_CHARS = 500
STDERR_KEPT = 4096
READ_CHUNK = 65536
REAP_WAIT_S = 5.0
NO_MATCH_EXIT = 1  # grep, rg and friends exit 1 when nothing matches: for a search that is "no results", not a failure
# Programs whose job is to read a string as shell text: an argv through one of them is a shell string in disguise.
SHELLS = frozenset({"sh", "bash", "zsh", "dash", "ksh", "csh", "tcsh", "fish", "cmd", "powershell", "pwsh", "wsl", "busybox"})
BATCH_SUFFIXES = (".bat", ".cmd")  # Windows runs these through cmd.exe, which re-reads the arguments as shell text
# What a command-line client needs to find itself and its settings. The office's own environment (API keys of the
# models, tokens of other connections) is not passed on.
PASSED_ENV = (
    "PATH", "PATHEXT", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "TEMP", "TMP", "TMPDIR", "HOME", "USERPROFILE", "HOMEDRIVE", "HOMEPATH",
    "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "USERNAME", "USER", "LOGNAME", "LANG", "LC_ALL", "LC_CTYPE", "TZ",
)  # fmt: skip


def check_argv(value: Any, required: str) -> list[str]:
    """An argument list for one program. Refuses what would let a reference be read as code or as an option."""
    if isinstance(value, str):
        raise ValueError('phải là danh sách đối số, ví dụ ["svn", "cat", "{ref}"], không phải một chuỗi lệnh shell')
    if not isinstance(value, list | tuple) or not value or any(isinstance(arg, dict | list | tuple) or arg is None for arg in value):
        raise ValueError('phải là danh sách đối số không rỗng, ví dụ ["svn", "cat", "{ref}"]')
    argv = [str(arg) for arg in value]
    program = argv[0].strip()
    if not program or "\x00" in "".join(argv):
        raise ValueError("đối số đầu tiên phải là tên chương trình")
    if placeholders(program):
        raise ValueError("tên chương trình (đối số đầu tiên) không được chứa chỗ điền như {ref}")
    stem = Path(program.replace("\\", "/")).name.lower().removesuffix(".exe")
    if stem in SHELLS or stem.endswith(BATCH_SUFFIXES):
        raise ValueError(f"không chạy qua shell hay file batch («{Path(program).name}»): hãy gọi thẳng chương trình cần chạy")
    if required not in placeholders(argv[1:]):
        raise ValueError(f"phải có chỗ điền {{{required}}} trong một đối số sau tên chương trình")
    return argv


class GenericCommandConfig(SourceConfig):
    fetch: list[str] = Field(description='Lệnh in nội dung tài liệu ra stdout, dạng danh sách đối số: ["svn", "cat", "svn://host/docs/{ref}"]')
    version: list[str] | None = Field(
        default=None, description='Lệnh in phiên bản của tài liệu (dòng đầu của stdout), ví dụ ["svn", "info", "--show-item", "revision", "…/{ref}"]. Không khai: mã băm nội dung'
    )
    search: list[str] | None = Field(default=None, description='Lệnh tìm theo từ khoá, mỗi dòng stdout chứa một tham chiếu: ["rg", "-n", "--", "{query}", "."]')
    format: Literal["markdown", "html", "plain"] = Field(default="markdown", description="Dạng của nội dung; html được chuyển thành markdown")
    cwd: str = Field(default="", description="Thư mục chạy lệnh (đường dẫn tuyệt đối)")
    timeout_s: float = Field(default=30.0, gt=0, le=600, description="Quá thời gian này (giây) thì lệnh và mọi tiến trình con bị dừng")
    max_output_kb: int = Field(default=512, ge=1, le=16384, description="Giới hạn dung lượng stdout (KB); phần vượt bị cắt")
    env: dict[str, str] = Field(default_factory=dict, description="Biến môi trường không bí mật cho lệnh")
    secret_env: dict[str, str] = Field(default_factory=dict, description="Biến môi trường lấy từ bí mật của kết nối: {SVN_PASSWORD: password} hoặc [SVN_PASSWORD] (ô bí mật trùng tên biến)")

    @field_validator("fetch", "version", mode="before")
    @classmethod
    def _ref_argv(cls, value: Any) -> Any:
        return None if value is None else check_argv(value, "ref")

    @field_validator("search", mode="before")
    @classmethod
    def _query_argv(cls, value: Any) -> Any:
        return None if value is None else check_argv(value, "query")

    @field_validator("cwd")
    @classmethod
    def _cwd(cls, value: str) -> str:
        value = value.strip()
        if value and not Path(value).is_absolute():
            raise ValueError("phải là đường dẫn tuyệt đối")
        return value

    @field_validator("secret_env", mode="before")
    @classmethod
    def _secret_env(cls, value: Any) -> Any:
        return check_secret_env(value)


class GenericCommand:
    name: ClassVar[str] = NAME
    title: ClassVar[str] = "Lệnh CLI (svn, p4…)"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DOCS})
    config_schema: ClassVar[type[BaseModel]] = GenericCommandConfig
    secret_fields: ClassVar[tuple[str, ...]] = SECRET_FIELDS
    optional_secret_fields: ClassVar[tuple[str, ...]] = SECRET_FIELDS  # only what `secret_env` names is needed

    def __init__(self, config: GenericCommandConfig, secret: dict[str, str], *, transport: Any = None) -> None:
        self.config = config
        self.recognizer = Recognizer(config.recognize)
        self._mapping = _DocMapping(format=config.format)
        self._secrets = [value for value in secret.values() if value]
        missing = missing_secrets(dict.fromkeys(config.secret_env.values()), secret)
        self.blocked = ConnectorError("unconfigured", f"{LABEL}: chưa có bí mật ({', '.join(missing)}) cho biến môi trường của lệnh. Vào Cài đặt › Kết nối để nhập.") if missing else None
        self._env = {name: os.environ[name] for name in PASSED_ENV if name in os.environ}
        self._env.update(config.env)
        self._env.update({name: secret.get(field, "") for name, field in config.secret_env.items()})
        self._running: set[int] = set()

    async def close(self) -> None:
        for pid in list(self._running):
            kill_tree(pid)

    # ---- running

    def _program(self, argv: list[str]) -> str:
        """The executable file of an argv. On Windows a batch file is refused: cmd.exe would re-read the arguments."""
        path = self._env.get("PATH")
        found = shutil.which(argv[0], path=path) if path else shutil.which(argv[0])
        if found is None:
            raise ConnectorError("invalid", f"{LABEL}: không tìm thấy chương trình «{Path(argv[0]).name}» trên máy chạy office")
        if sys.platform == "win32" and found.lower().endswith(BATCH_SUFFIXES):
            raise ConnectorError("invalid", f"{LABEL}: «{Path(found).name}» là file batch; trên Windows nó chạy qua cmd.exe nên không an toàn. Hãy gọi thẳng file .exe")
        return found

    def _cwd(self) -> str | None:
        if self.config.cwd and not Path(self.config.cwd).is_dir():
            raise ConnectorError("invalid", f"{LABEL}: không có thư mục chạy lệnh «{self.config.cwd}» (cwd)")
        return self.config.cwd or None

    async def _run(self, argv: list[str], *, ok_codes: tuple[int, ...] = (0,)) -> tuple[str, bool]:
        """stdout of one command as text and whether it was cut at the output limit. Never through a shell."""
        if self.blocked is not None:
            raise self.blocked
        name = Path(argv[0]).name
        program, cwd = self._program(argv), self._cwd()
        try:
            proc = await asyncio.create_subprocess_exec(program, *argv[1:], stdin=asyncio.subprocess.DEVNULL, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, cwd=cwd, env=self._env)
        except (OSError, ValueError) as exc:
            raise ConnectorError("invalid", f"{LABEL}: không chạy được «{name}» ({type(exc).__name__})") from None
        self._running.add(proc.pid)
        limit = self.config.max_output_kb * 1024
        out, err = bytearray(), bytearray()
        assert proc.stdout is not None and proc.stderr is not None

        async def read_stderr() -> None:
            while chunk := await proc.stderr.read(READ_CHUNK):  # read to the end so a chatty command never blocks on a full pipe
                err.extend(chunk[: max(0, STDERR_KEPT - len(err))])

        stderr_task = asyncio.ensure_future(read_stderr())
        truncated = False
        try:
            async with asyncio.timeout(self.config.timeout_s):
                while chunk := await proc.stdout.read(READ_CHUNK):
                    room = limit - len(out)
                    out.extend(chunk[:room])
                    if len(chunk) > room:
                        truncated = True
                        kill_tree(proc.pid)  # nobody reads the rest
                        break
                code = await proc.wait()
                await stderr_task
        except TimeoutError:
            raise ConnectorError("network", f"{LABEL}: «{name}» chạy quá {self.config.timeout_s:g} giây nên đã bị dừng") from None
        finally:
            if proc.returncode is None:  # ran over, or the caller was cancelled
                kill_tree(proc.pid)
                with contextlib.suppress(Exception):
                    await asyncio.wait_for(proc.wait(), REAP_WAIT_S)
            stderr_task.cancel()
            self._running.discard(proc.pid)
        if not truncated and code not in ok_codes:
            said = short_line(err.decode("utf-8", errors="replace"), self._secrets)
            raise ConnectorError("network", f"{LABEL}: «{name}» kết thúc với mã lỗi {code}" + (f": {said}" if said else ""))
        return out.decode("utf-8", errors="replace"), truncated

    async def test(self) -> Health:
        if self.blocked is not None:
            return Health(state="unconfigured", checked_at=now_iso(), detail=str(self.blocked))
        try:
            self._cwd()
            names = sorted({Path(self._program(argv)).name for argv in (self.config.fetch, self.config.version, self.config.search) if argv})
        except ConnectorError as exc:
            return Health(state="error", checked_at=now_iso(), detail=str(exc))
        return Health(state="ok", checked_at=now_iso(), detail=f"Tìm thấy chương trình: {', '.join(names)}. Chưa chạy thử lệnh nào (cần một tham chiếu thật).")

    # ---- references

    @staticmethod
    def _unsafe(ref: str) -> bool:
        """A reference that could be read as an option, climb out of a directory, or break an argument."""
        return ref.startswith("-") or any(ord(c) < 32 or c == "\x7f" for c in ref) or ".." in ref.replace("\\", "/").split("/")

    def _ref(self, text: str) -> str:
        value = self.recognizer.canonical(text)
        if value is None or self._unsafe(value):
            raise ConnectorError("invalid", f"{LABEL}: «{short_line(text)[:80]}» không phải tham chiếu mà kết nối này nhận ra, nên không lệnh nào được chạy")
        return value

    def recognize(self, text: str) -> list[DocRef]:
        return [DocRef(ref=ref, kind=NAME) for ref in self.recognizer.find(text) if not self._unsafe(ref)]

    def in_scope(self, ref: DocRef, scope: Scope) -> bool:
        value = self.recognizer.canonical(ref.ref)
        return value is not None and not self._unsafe(value) and allowed(value, self.config.allow)

    # ---- reading

    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent:
        value = self._ref(ref.ref)
        if not allowed(value, self.config.allow):
            raise ConnectorError("forbidden", f"{LABEL}: {value} nằm ngoài các tham chiếu được phép của kết nối (allow)")
        text, truncated = await self._run(fill(self.config.fetch, {"ref": value}))
        content = document(NAME, self.recognizer, value, text, None, self._mapping, LABEL, truncated=truncated)
        if self.config.version:
            said, _ = await self._run(fill(self.config.version, {"ref": value}))
            content.version = short_line(said) or content_version(content.text)
        return content

    async def search(self, query: str, scope: Scope, limit: int = 10) -> list[DocHit]:
        query = " ".join(query.split())[:MAX_QUERY_CHARS]
        if self.config.search is None or not query:
            return []
        if query.startswith("-") or any(ord(c) < 32 for c in query):
            raise ConnectorError("invalid", f"{LABEL}: từ khoá tìm kiếm không được bắt đầu bằng '-' (sẽ bị đọc thành tuỳ chọn của lệnh)")
        limit = max(1, min(limit, MAX_HITS))
        text, _ = await self._run(fill(self.config.search, {"query": query, "limit": limit}), ok_codes=(0, NO_MATCH_EXIT))
        found: dict[str, DocHit] = {}
        for line in text.splitlines():
            ref = next((r for r in self.recognizer.find(line) if not self._unsafe(r) and allowed(r, self.config.allow)), None)
            if ref is None or ref in found:
                continue
            found[ref] = DocHit(ref=DocRef(ref=ref, kind=NAME), title=ref, snippet=line.strip()[:240])
            if len(found) >= limit:
                break
        return list(found.values())
