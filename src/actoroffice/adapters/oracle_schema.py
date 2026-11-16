"""`use: oracle_schema`: the schema of an Oracle database as a read-only document source.

The office's AI looks up tables (columns, keys, comments), views and stored procedures / functions / packages by
reference (`table:BOND.ORDERS`) or by words, so analysis and design documents can cite them.

What is sent to the database is fixed: every statement is one of the `SQL_*` constants below, a SELECT on the data
dictionary (`ALL_*`). An owner, an object name or a search word reaches the database only as a bind variable, after
the owner was checked against the connection's `schemas`. Nothing the AI writes is ever part of a statement's text.

The driver (`oracledb`, thin mode) is an optional extra and blocking: it is imported on first use and every call runs
in a worker thread, on a connection of its own that is closed when the call ends.
"""

from __future__ import annotations

import asyncio
import contextlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from typing import Any, ClassVar

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from actoroffice.adapters._http import now_iso
from actoroffice.adapters.generic.common import content_version, short_line
from actoroffice.ports.connection import Capability, ConnectorError, Health, Scope
from actoroffice.ports.docs import DocContent, DocHit, DocRef

NAME = "oracle_schema"
DEFAULT_PORT = 1521
MAX_HITS = 50
MAX_WORDS = 3  # search words that must all be found; the statements have this many binds
MAX_WORD_CHARS = 64
MAX_SCHEMA_OBJECTS = 300  # tables and views listed in a schema overview
MAX_ARGUMENTS = 500  # rows of ALL_ARGUMENTS read for one package
MAX_VIEW_CHARS = 20_000
MAX_CELL_CHARS = 300
MAX_LINKS = 50
CONNECT_TIMEOUT_S = 20.0

IDENT = r"[A-Za-z][A-Za-z0-9_$#]{0,127}"
IDENT_RE = re.compile(rf"^{IDENT}$")
_REF_RE = re.compile(rf"(?P<kind>table|view|proc|function|package|schema):(?P<a>{IDENT})(?:\.(?P<b>{IDENT}))?", re.IGNORECASE)
# In prose a reference ends at white space; the punctuation of a sentence or of markdown may stand in between. A quote
# or any other character right after the name means the text is not one of our references.
_FIND_RE = re.compile(rf"(?<![\w:.$#/-]){_REF_RE.pattern}(?=[.,;:!?)\]}}»\"`*|]*(?:\s|$))", re.IGNORECASE)
_DSN_RE = re.compile(r"^(?P<host>[A-Za-z0-9][A-Za-z0-9_.-]*)(?::(?P<port>\d{1,5}))?/(?P<service>[A-Za-z0-9][A-Za-z0-9_.$#-]*)$")
_HOST_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]*$")
_SERVICE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.$#-]*$")
_CODE_RE = re.compile(r"\b(?:ORA|DPY|DPI|TNS)-\d{4,5}\b")

# ---- the only statements this adapter ever sends ---------------------------------------------------------------
# Plain constants: no f-string, no formatting, no concatenation with a value. LIKE wildcards are part of the bind.

SQL_WHOAMI = "SELECT USER FROM DUAL"
SQL_COUNT_TABLES = "SELECT COUNT(*) FROM all_tables WHERE owner = :owner"
SQL_OBJECT = (
    "SELECT object_type, status, last_ddl_time FROM all_objects WHERE owner = :owner AND object_name = :name "
    "AND object_type IN ('TABLE', 'VIEW', 'MATERIALIZED VIEW', 'PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY')"
)
SQL_TAB_COMMENT = "SELECT comments FROM all_tab_comments WHERE owner = :owner AND table_name = :name"
# DATA_DEFAULT is a LONG: it is selected on its own, never inside a join or a subquery (ORA-00997).
SQL_COLUMNS = (
    "SELECT column_name, data_type, data_length, data_precision, data_scale, char_length, char_used, nullable, data_default "
    "FROM all_tab_columns WHERE owner = :owner AND table_name = :name ORDER BY column_id"
)
SQL_COL_COMMENTS = "SELECT column_name, comments FROM all_col_comments WHERE owner = :owner AND table_name = :name AND comments IS NOT NULL"
SQL_CONSTRAINTS = (
    "SELECT constraint_name, constraint_type, status FROM all_constraints "
    "WHERE owner = :owner AND table_name = :name AND constraint_type IN ('P', 'U', 'R') ORDER BY constraint_type, constraint_name"
)
SQL_CONS_COLUMNS = "SELECT constraint_name, column_name FROM all_cons_columns WHERE owner = :owner AND table_name = :name ORDER BY constraint_name, position"
SQL_FK_TARGETS = (
    "SELECT c.constraint_name, r.owner, r.table_name, rc.column_name FROM all_constraints c "
    "JOIN all_constraints r ON r.owner = c.r_owner AND r.constraint_name = c.r_constraint_name "
    "JOIN all_cons_columns rc ON rc.owner = r.owner AND rc.constraint_name = r.constraint_name "
    "WHERE c.owner = :owner AND c.table_name = :name AND c.constraint_type = 'R' ORDER BY c.constraint_name, rc.position"
)
SQL_REFERENCED_BY = (
    "SELECT c.owner, c.table_name, c.constraint_name, cc.column_name FROM all_constraints r "
    "JOIN all_constraints c ON c.r_owner = r.owner AND c.r_constraint_name = r.constraint_name AND c.constraint_type = 'R' "
    "JOIN all_cons_columns cc ON cc.owner = c.owner AND cc.constraint_name = c.constraint_name "
    "WHERE r.owner = :owner AND r.table_name = :name AND r.constraint_type IN ('P', 'U') ORDER BY c.owner, c.table_name, c.constraint_name, cc.position"
)
SQL_INDEXES = (
    "SELECT i.index_name, i.uniqueness, i.index_type, c.column_name, c.descend FROM all_indexes i "
    "JOIN all_ind_columns c ON c.index_owner = i.owner AND c.index_name = i.index_name "
    "WHERE i.table_owner = :owner AND i.table_name = :name ORDER BY i.index_name, c.column_position"
)
SQL_VIEW_TEXT = "SELECT text FROM all_views WHERE owner = :owner AND view_name = :name"  # TEXT is a LONG, see SQL_COLUMNS
SQL_ARGUMENTS = (
    "SELECT * FROM (SELECT object_name, subprogram_id, argument_name, position, data_type, in_out, defaulted, type_owner, type_name, type_subname, pls_type "
    "FROM all_arguments WHERE owner = :owner AND ((package_name IS NULL AND object_name = :name) OR package_name = :name) AND data_level = 0 "
    "ORDER BY subprogram_id, sequence) WHERE ROWNUM <= :lim"
)
SQL_SOURCE = (
    "SELECT type, line, text FROM all_source WHERE owner = :owner AND name = :name "
    "AND type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY') AND line <= :lim ORDER BY type, line"
)
SQL_SOURCE_COUNT = (
    "SELECT type, COUNT(*) FROM all_source WHERE owner = :owner AND name = :name AND type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE', 'PACKAGE BODY') GROUP BY type"
)
SQL_SCHEMA_COUNT = "SELECT table_type, COUNT(*) FROM all_tab_comments WHERE owner = :owner AND table_type IN ('TABLE', 'VIEW') GROUP BY table_type"
SQL_SCHEMA_OBJECTS = (
    "SELECT * FROM (SELECT c.table_name, c.table_type, c.comments, t.num_rows FROM all_tab_comments c "
    "LEFT JOIN all_tables t ON t.owner = c.owner AND t.table_name = c.table_name "
    "WHERE c.owner = :owner AND c.table_type IN ('TABLE', 'VIEW') ORDER BY c.table_type, c.table_name) WHERE ROWNUM <= :lim"
)
SQL_SEARCH_TABLES = (
    "SELECT * FROM (SELECT c.table_name, c.table_type, c.comments FROM all_tab_comments c "
    "WHERE c.owner = :owner AND c.table_type IN ('TABLE', 'VIEW') "
    "AND (UPPER(c.table_name) LIKE :q1 ESCAPE '\\' OR UPPER(c.comments) LIKE :q1 ESCAPE '\\') "
    "AND (UPPER(c.table_name) LIKE :q2 ESCAPE '\\' OR UPPER(c.comments) LIKE :q2 ESCAPE '\\') "
    "AND (UPPER(c.table_name) LIKE :q3 ESCAPE '\\' OR UPPER(c.comments) LIKE :q3 ESCAPE '\\') "
    "ORDER BY c.table_name) WHERE ROWNUM <= :lim"
)
SQL_SEARCH_COLUMNS = (
    "SELECT * FROM (SELECT c.table_name, t.table_type, c.column_name, c.comments FROM all_col_comments c "
    "JOIN all_tab_comments t ON t.owner = c.owner AND t.table_name = c.table_name AND t.table_type IN ('TABLE', 'VIEW') "
    "WHERE c.owner = :owner "
    "AND (UPPER(c.column_name) LIKE :q1 ESCAPE '\\' OR UPPER(c.comments) LIKE :q1 ESCAPE '\\') "
    "AND (UPPER(c.column_name) LIKE :q2 ESCAPE '\\' OR UPPER(c.comments) LIKE :q2 ESCAPE '\\') "
    "AND (UPPER(c.column_name) LIKE :q3 ESCAPE '\\' OR UPPER(c.comments) LIKE :q3 ESCAPE '\\') "
    "ORDER BY c.table_name, c.column_name) WHERE ROWNUM <= :lim"
)
SQL_SEARCH_PROCS = (
    "SELECT * FROM (SELECT o.object_name, o.object_type FROM all_objects o "
    "WHERE o.owner = :owner AND o.object_type IN ('PROCEDURE', 'FUNCTION', 'PACKAGE') "
    "AND UPPER(o.object_name) LIKE :q1 ESCAPE '\\' AND UPPER(o.object_name) LIKE :q2 ESCAPE '\\' AND UPPER(o.object_name) LIKE :q3 ESCAPE '\\' "
    "ORDER BY o.object_name) WHERE ROWNUM <= :lim"
)

# ---- what a reference and an object type are called ------------------------------------------------------------

TABLE_TYPES = ("TABLE",)
VIEW_TYPES = ("VIEW",)
CODE_TYPES = ("PROCEDURE", "FUNCTION", "PACKAGE")
SOURCE_ORDER = ("PROCEDURE", "FUNCTION", "PACKAGE", "PACKAGE BODY")
WANTED: dict[str, tuple[str, ...]] = {"table": TABLE_TYPES, "view": VIEW_TYPES, "proc": CODE_TYPES, "function": ("FUNCTION",), "package": ("PACKAGE",)}
PREFIX_OF = {"TABLE": "table", "VIEW": "view", "PROCEDURE": "proc", "FUNCTION": "function", "PACKAGE": "package"}
TITLE_OF = {"TABLE": "Bảng", "VIEW": "View", "PROCEDURE": "Thủ tục", "FUNCTION": "Hàm", "PACKAGE": "Gói"}
SIZED_TYPES = frozenset({"CHAR", "VARCHAR2", "NCHAR", "NVARCHAR2", "VARCHAR"})

# ---- driver errors people can act on ---------------------------------------------------------------------------
# Codes of the database (ORA-) and of python-oracledb (DPY-; thin mode reports DPY-6001 where the Oracle client says
# ORA-12514). The text is ours: a driver message is quoted only for a code that is not listed here.

_LISTENER = "không kết nối được tới listener. Kiểm tra host, port, tường lửa/VPN và listener của máy chủ có đang chạy"
_DROPPED = "kết nối bị ngắt giữa chừng. Thử lại; nếu lặp lại, kiểm tra mạng tới máy chủ"
_TIMED_OUT = "câu truy vấn chạy quá thời gian cho phép (`statement_timeout_s`) nên bị dừng"
_NO_SERVICE = "listener không biết service này. Kiểm tra `service_name` (hoặc phần sau dấu / của `dsn`)"
ERRORS: dict[str, tuple[str, str]] = {
    "ORA-01017": ("auth", "sai tên đăng nhập hoặc mật khẩu. Kiểm tra `user` và `password` của kết nối"),
    "ORA-28000": ("auth", "tài khoản đang bị khoá"),
    "ORA-28001": ("auth", "mật khẩu của tài khoản đã hết hạn"),
    "ORA-01005": ("auth", "chưa có mật khẩu"),
    "ORA-01045": ("forbidden", "tài khoản không có quyền CREATE SESSION"),
    "ORA-01031": ("forbidden", "tài khoản không đủ quyền"),
    "ORA-00942": ("forbidden", "tài khoản không đọc được view từ điển dữ liệu (ALL_*)"),
    "DPY-6005": ("network", _LISTENER),
    "DPY-6000": ("network", _LISTENER),
    "ORA-12541": ("network", _LISTENER),
    "ORA-12170": ("network", _LISTENER),
    "ORA-12535": ("network", _LISTENER),
    "ORA-12543": ("network", _LISTENER),
    "ORA-12545": ("network", _LISTENER),
    "DPY-4011": ("network", _DROPPED),
    "ORA-03113": ("network", _DROPPED),
    "ORA-03114": ("network", _DROPPED),
    "ORA-03135": ("network", _DROPPED),
    "DPY-4024": ("network", _TIMED_OUT),
    "DPI-1067": ("network", _TIMED_OUT),
    "ORA-03156": ("network", _TIMED_OUT),
    "ORA-01013": ("network", _TIMED_OUT),
    "ORA-12514": ("not_found", _NO_SERVICE),
    "DPY-6001": ("not_found", _NO_SERVICE),
    "ORA-12505": ("not_found", _NO_SERVICE),
    "DPY-6003": ("not_found", _NO_SERVICE),
    "DPY-3015": ("invalid", "kiểu mật khẩu của tài khoản (verifier 10G) không dùng được với oracledb thin mode; đặt lại mật khẩu của tài khoản"),
    "DPY-3010": ("invalid", "phiên bản Oracle này quá cũ cho oracledb thin mode (cần 12.1 trở lên)"),
}


def _error_code(exc: BaseException) -> str:
    """`ORA-01017` / `DPY-6005` of a driver error: from its error object when it has one, else from its text."""
    first = exc.args[0] if exc.args else None
    full = getattr(first, "full_code", "")
    if isinstance(full, str) and _CODE_RE.fullmatch(full):
        return full
    found = _CODE_RE.search(str(exc))
    return found.group(0) if found else ""


# ---- config ----------------------------------------------------------------------------------------------------


class OracleSchemaConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    dsn: str = Field(default="", description="Địa chỉ dạng host:port/service_name, ví dụ db.acme.example:1521/ORCLPDB1. Hoặc khai host + service_name bên dưới")
    host: str = Field(default="", description="Tên máy chủ Oracle (khi không dùng dsn)")
    port: int = Field(default=DEFAULT_PORT, ge=1, le=65535, description="Cổng của listener, mặc định 1521")
    service_name: str = Field(default="", description="Tên service của cơ sở dữ liệu, ví dụ ORCLPDB1 (khi không dùng dsn)")
    schemas: list[str] = Field(description="Các schema (owner) được phép đọc, ví dụ [BOND, REF]. Không gì ngoài các schema này được trả về")
    max_source_lines: int = Field(default=400, ge=1, le=5000, description="Số dòng mã nguồn tối đa của một thủ tục / hàm / gói; phần còn lại được ghi chú là bị cắt")
    statement_timeout_s: int = Field(default=30, ge=1, le=600, description="Thời gian chờ tối đa cho mỗi câu truy vấn (giây)")

    @field_validator("dsn")
    @classmethod
    def _dsn(cls, value: str) -> str:
        value = value.strip()
        if value and not _DSN_RE.fullmatch(value):
            raise ValueError("dsn phải có dạng host:port/service_name (ví dụ db.acme.example:1521/ORCLPDB1), không kèm tài khoản hay mật khẩu")
        return value

    @field_validator("host")
    @classmethod
    def _host(cls, value: str) -> str:
        value = value.strip()
        if value and not _HOST_RE.fullmatch(value):
            raise ValueError("host phải là tên máy hoặc địa chỉ IP, ví dụ db.acme.example")
        return value

    @field_validator("service_name")
    @classmethod
    def _service(cls, value: str) -> str:
        value = value.strip()
        if value and not _SERVICE_RE.fullmatch(value):
            raise ValueError("service_name chỉ gồm chữ, số và . _ - $ #, ví dụ ORCLPDB1")
        return value

    @field_validator("schemas", mode="before")
    @classmethod
    def _one_or_many(cls, value: Any) -> Any:
        return [value] if isinstance(value, str) else value

    @field_validator("schemas")
    @classmethod
    def _schemas(cls, value: list[str]) -> list[str]:
        found: list[str] = []
        for item in value:
            name = item.strip().upper()
            if not IDENT_RE.fullmatch(name):
                raise ValueError(f"schemas: «{item[:40]}» không phải tên schema hợp lệ (chữ cái đầu, rồi chữ, số, _ $ #)")
            if name not in found:
                found.append(name)
        if not found:
            raise ValueError("schemas: cần ít nhất một schema được phép đọc, ví dụ [BOND]")
        return found

    @model_validator(mode="after")
    def _address(self) -> OracleSchemaConfig:
        if self.dsn and (self.host or self.service_name):
            raise ValueError("khai `dsn` hoặc `host` + `service_name`, không khai cả hai")
        if not self.dsn and not (self.host and self.service_name):
            raise ValueError("thiếu địa chỉ cơ sở dữ liệu: khai `dsn` (host:port/service_name) hoặc cả `host` và `service_name`")
        return self

    def address(self) -> tuple[str, int, str]:
        """(host, port, service name), whichever way the config wrote them."""
        if not self.dsn:
            return self.host, self.port, self.service_name
        m = _DSN_RE.fullmatch(self.dsn)
        assert m is not None  # validated above
        return m.group("host"), int(m.group("port") or DEFAULT_PORT), m.group("service")


# ---- references ------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Ref:
    kind: str  # table | view | proc | function | package | schema
    owner: str
    name: str = ""  # "" for a schema

    @property
    def text(self) -> str:
        return f"{self.kind}:{self.owner}" + (f".{self.name}" if self.name else "")

    @property
    def qualified(self) -> str:
        return f"{self.owner}.{self.name}" if self.name else self.owner


def _cell(value: Any, limit: int = MAX_CELL_CHARS) -> str:
    """A value as one cell of a markdown table."""
    text = " ".join(str(value).split()) if value is not None else ""
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text.replace("|", "\\|")


def _data_type(data_type: Any, length: Any, precision: Any, scale: Any, char_length: Any, char_used: Any) -> str:
    """`VARCHAR2(50 CHAR)`, `NUMBER(19)`, `NUMBER(18,2)`: the type as it is written in DDL."""
    name = str(data_type or "?")
    if name in SIZED_TYPES:
        size = char_length or length
        return f"{name}({size}{' CHAR' if char_used == 'C' and name in ('CHAR', 'VARCHAR2') else ''})" if size else name
    if name == "NUMBER":
        if precision is None:
            return "NUMBER" if scale is None else f"NUMBER(*,{scale})"
        return f"NUMBER({precision})" if not scale else f"NUMBER({precision},{scale})"
    if name == "FLOAT" and precision is not None:
        return f"FLOAT({precision})"
    if name == "RAW" and length:
        return f"RAW({length})"
    return name


def _fenced(text: str) -> str:
    return "```sql\n" + text.replace("```", "` ` `").rstrip() + "\n```"


class _Session:
    """One open connection for the length of one call. `query` takes a `SQL_*` constant and bind values, nothing else."""

    def __init__(self, conn: Any, error: Callable[[BaseException], ConnectorError]) -> None:
        self._conn = conn
        self._error = error

    def query(self, sql: str, **binds: Any) -> list[tuple[Any, ...]]:
        cursor = None
        try:
            cursor = self._conn.cursor()
            cursor.execute(sql, binds)
            return [tuple(row) for row in cursor.fetchall()]
        except Exception as exc:  # whatever the driver raises becomes a ConnectorError with a kind
            raise self._error(exc) from None
        finally:
            if cursor is not None:
                with contextlib.suppress(Exception):
                    cursor.close()


class OracleSchema:
    name: ClassVar[str] = NAME
    title: ClassVar[str] = "Lược đồ Oracle (chỉ đọc)"
    capabilities: ClassVar[frozenset[Capability]] = frozenset({Capability.DOCS})
    config_schema: ClassVar[type[BaseModel]] = OracleSchemaConfig
    secret_fields: ClassVar[tuple[str, ...]] = ("user", "password")

    def __init__(self, config: OracleSchemaConfig, secret: dict[str, str], *, transport: Callable[[], Any] | None = None) -> None:
        """`transport` (tests): a callable that returns a DB-API-like connection instead of the driver's."""
        self.config = config
        self.schemas = tuple(config.schemas)
        self.host, self.port, self.service = config.address()
        self.label = f"Oracle {self.host}:{self.port}/{self.service}"
        self._user = secret.get("user", "")
        self._password = secret.get("password", "")
        self._transport = transport
        missing = [field for field in self.secret_fields if not secret.get(field)]
        self.blocked = ConnectorError("unconfigured", f"{self.label}: chưa có bí mật ({', '.join(missing)}). Vào Cài đặt › Kết nối để nhập.") if missing else None

    async def close(self) -> None:
        return None  # a connection lives for one call and is closed there

    # ---- the driver

    def _driver_connect(self) -> Any:
        try:
            import oracledb  # optional extra, thin mode: no Oracle client needed
        except ImportError:
            raise ConnectorError("unconfigured", "chưa cài gói oracledb: uv sync --extra oracle") from None
        return oracledb.connect(user=self._user, password=self._password, host=self.host, port=self.port, service_name=self.service, tcp_connect_timeout=CONNECT_TIMEOUT_S)

    def _to_error(self, exc: BaseException) -> ConnectorError:
        """A driver error as a kind and a message for people. Never the password; the driver's text only for an unknown code."""
        if isinstance(exc, ConnectorError):
            return exc
        code = _error_code(exc)
        known = ERRORS.get(code)
        if known is not None:
            return ConnectorError(known[0], f"{self.label}: {known[1]} ({code})")
        if isinstance(exc, TimeoutError | OSError):
            return ConnectorError("network", f"{self.label}: {_LISTENER} ({type(exc).__name__})")
        said = short_line(str(exc), [self._password])
        return ConnectorError("invalid", f"{self.label}: lỗi {code or type(exc).__name__}" + (f": {said}" if said else ""))

    def _call(self, work: Callable[[_Session], Any]) -> Any:
        """Blocking: open a connection, run `work` on it, close it. Runs in a worker thread."""
        if self.blocked is not None:
            raise self.blocked
        try:
            conn = (self._transport or self._driver_connect)()
            conn.call_timeout = self.config.statement_timeout_s * 1000  # milliseconds, for each round trip
        except Exception as exc:
            raise self._to_error(exc) from None
        try:
            return work(_Session(conn, self._to_error))
        finally:
            with contextlib.suppress(Exception):
                conn.close()

    async def _run(self, work: Callable[[_Session], Any]) -> Any:
        return await asyncio.to_thread(self._call, work)

    # ---- health

    async def test(self) -> Health:
        def work(s: _Session) -> tuple[str, dict[str, int]]:
            user = str(s.query(SQL_WHOAMI)[0][0])
            return user, {owner: int(s.query(SQL_COUNT_TABLES, owner=owner)[0][0] or 0) for owner in self.schemas}

        try:
            user, counts = await self._run(work)
        except ConnectorError as exc:
            return Health(state="unconfigured" if exc.kind == "unconfigured" else "error", checked_at=now_iso(), detail=str(exc))
        seen = "; ".join(f"{owner}: {count} bảng" for owner, count in counts.items() if count)
        hidden = [owner for owner, count in counts.items() if not count]
        if hidden:
            blind = "; ".join(f"tài khoản không có quyền đọc schema {owner} (không thấy bảng nào)" for owner in hidden)
            detail = f"Đã kết nối {self.label} với tài khoản {user}, nhưng {blind}." + (f" Đọc được {seen}." if seen else "") + " Cấp quyền SELECT (hoặc SELECT ANY DICTIONARY) cho tài khoản."
            return Health(state="degraded" if seen else "error", checked_at=now_iso(), detail=detail, identity=user)
        return Health(state="ok", checked_at=now_iso(), detail=f"Đã kết nối {self.label} với tài khoản {user}. Đọc được {seen}.", identity=user)

    # ---- references

    def _parse(self, text: str) -> Ref | None:
        """A reference when `text` is entirely one: `table:OWNER.NAME`, `schema:OWNER`; the owner may be left out when
        the connection has exactly one schema. Names are upper-cased."""
        m = _REF_RE.fullmatch(text.strip())
        if m is None:
            return None
        kind, a, b = m.group("kind").lower(), m.group("a").upper(), (m.group("b") or "").upper()
        if kind == "schema":
            return Ref(kind, a) if not b else None
        if b:
            return Ref(kind, a, b)
        return Ref(kind, self.schemas[0], a) if len(self.schemas) == 1 else None

    def recognize(self, text: str) -> list[DocRef]:
        found: list[str] = []
        for m in _FIND_RE.finditer(text):
            ref = self._parse(m.group(0))
            if ref is not None and ref.text not in found:
                found.append(ref.text)
        return [DocRef(ref=value, kind=NAME) for value in found]

    def in_scope(self, ref: DocRef, scope: Scope) -> bool:
        parsed = self._parse(ref.ref)
        return parsed is not None and parsed.owner in self.schemas

    def _allowed(self, text: str) -> Ref:
        """The gate before any query: the text is one reference and its owner is one of the connection's schemas."""
        ref = self._parse(text)
        if ref is None:
            raise ConnectorError("invalid", f"{self.label}: «{short_line(text)[:80]}» không phải tham chiếu hợp lệ (table:OWNER.TÊN, view:…, proc:…, schema:OWNER), nên không truy vấn nào được chạy")
        if ref.owner not in self.schemas:
            raise ConnectorError("forbidden", f"{self.label}: schema {ref.owner} nằm ngoài các schema được phép của kết nối ({', '.join(self.schemas)})")
        return ref

    # ---- reading

    async def fetch(self, ref: DocRef, scope: Scope) -> DocContent:
        wanted = self._allowed(ref.ref)
        if wanted.kind == "schema":
            return await self._run(lambda s: self._schema(s, wanted))
        if wanted.kind == "table":
            return await self._run(lambda s: self._table(s, wanted))
        if wanted.kind == "view":
            return await self._run(lambda s: self._view(s, wanted))
        return await self._run(lambda s: self._code(s, wanted))

    def _object(self, s: _Session, ref: Ref) -> dict[str, tuple[str, Any]]:
        """object type → (status, last DDL time) of the object a reference names. Raises `not_found` when it is not
        there, or is another kind of object than the reference says."""
        found = {str(kind): (str(status or ""), ddl) for kind, status, ddl in s.query(SQL_OBJECT, owner=ref.owner, name=ref.name)}
        if not found:
            raise ConnectorError("not_found", f"{self.label}: không có đối tượng {ref.qualified} (hoặc tài khoản không có quyền xem nó)")
        if not any(kind in found for kind in WANTED[ref.kind]):
            others = [kind for kind in PREFIX_OF if kind in found]
            hint = f"; dùng {PREFIX_OF[others[0]]}:{ref.qualified}" if others else ""
            raise ConnectorError("not_found", f"{self.label}: {ref.qualified} là {', '.join(sorted(found))}, không phải thứ mà «{ref.kind}:» đọc{hint}")
        return found

    @staticmethod
    def _version(text: str, *times: Any) -> str:
        """The content's hash, after the object's last DDL time when the database gave one."""
        stamps = [t for t in times if isinstance(t, datetime)]
        return (f"{max(stamps):%Y%m%dT%H%M%S}-" if stamps else "") + content_version(text)

    def _columns(self, s: _Session, ref: Ref) -> list[str]:
        comments = {str(name): comment for name, comment in s.query(SQL_COL_COMMENTS, owner=ref.owner, name=ref.name)}
        lines = ["| Cột | Kiểu | Null | Mặc định | Ghi chú |", "|---|---|---|---|---|"]
        for name, data_type, length, precision, scale, char_length, char_used, nullable, default in s.query(SQL_COLUMNS, owner=ref.owner, name=ref.name):
            kind = _data_type(data_type, length, precision, scale, char_length, char_used)
            lines.append(f"| {_cell(name)} | {_cell(kind)} | {'có' if nullable == 'Y' else 'không'} | {_cell(default, 80)} | {_cell(comments.get(str(name)))} |")
        return lines

    def _comment(self, s: _Session, ref: Ref) -> str:
        rows = s.query(SQL_TAB_COMMENT, owner=ref.owner, name=ref.name)
        return " ".join(str(rows[0][0]).split()) if rows and rows[0][0] else "(chưa có ghi chú)"

    def _table(self, s: _Session, ref: Ref) -> DocContent:
        obj = self._object(s, ref)
        binds = {"owner": ref.owner, "name": ref.name}
        out = [f"# Bảng {ref.qualified}", "", self._comment(s, ref), "", "## Cột", "", *self._columns(s, ref)]

        columns_of: dict[str, list[str]] = {}
        for constraint, column in s.query(SQL_CONS_COLUMNS, **binds):
            columns_of.setdefault(str(constraint), []).append(str(column))
        targets: dict[str, tuple[str, str, list[str]]] = {}
        for constraint, owner, table, column in s.query(SQL_FK_TARGETS, **binds):
            targets.setdefault(str(constraint), (str(owner), str(table), []))[2].append(str(column))
        keys: dict[str, list[str]] = {"P": [], "U": [], "R": []}
        links: list[str] = []
        for constraint, kind, status in s.query(SQL_CONSTRAINTS, **binds):
            line = f"- {constraint} ({', '.join(columns_of.get(str(constraint), []))})"
            if kind == "R":
                owner, table, columns = targets.get(str(constraint), ("", "", []))
                if owner in self.schemas:
                    line += f" → {owner}.{table}({', '.join(columns)})"
                    links.append(f"table:{owner}.{table}")
                else:  # what the key points to is in a schema this connection may not read
                    line += " → (bảng ở schema ngoài phạm vi của kết nối)"
            if status and status != "ENABLED":
                line += f" [{status}]"
            keys.get(str(kind), []).append(line)
        for heading, kind in (("Khoá chính", "P"), ("Khoá duy nhất", "U"), ("Khoá ngoại", "R")):
            out += ["", f"## {heading}", "", *(keys[kind] or ["(không có)"])]

        indexes: dict[str, tuple[str, str, list[str]]] = {}
        for index, uniqueness, index_type, column, descend in s.query(SQL_INDEXES, **binds):
            indexes.setdefault(str(index), (str(uniqueness or ""), str(index_type or ""), []))[2].append(f"{column}{' DESC' if descend == 'DESC' else ''}")
        described = [f"- {index} ({', '.join(columns)}) — {uniqueness}" + (f", {kind}" if kind and kind != "NORMAL" else "") for index, (uniqueness, kind, columns) in indexes.items()]
        out += ["", "## Chỉ mục", "", *(described or ["(không có)"])]

        referring: dict[tuple[str, str, str], list[str]] = {}
        for owner, table, constraint, column in s.query(SQL_REFERENCED_BY, **binds):
            if owner in self.schemas:  # a table of another schema is never named
                referring.setdefault((str(owner), str(table), str(constraint)), []).append(str(column))
        links += [f"table:{owner}.{table}" for owner, table, _ in referring]
        referred = [f"- {owner}.{table}({', '.join(columns)}) qua {constraint}" for (owner, table, constraint), columns in referring.items()]
        out += ["", "## Được tham chiếu bởi", "", *(referred or ["(không có)"])]

        text = "\n".join(out)
        related = [DocRef(ref=link, kind=NAME) for link in dict.fromkeys(links) if link != ref.text][:MAX_LINKS]
        return DocContent(title=f"Bảng {ref.qualified}", text=text, version=self._version(text, obj["TABLE"][1]), links=related)

    def _view(self, s: _Session, ref: Ref) -> DocContent:
        obj = self._object(s, ref)
        out = [f"# View {ref.qualified}", "", self._comment(s, ref), "", "## Cột", "", *self._columns(s, ref)]
        rows = s.query(SQL_VIEW_TEXT, owner=ref.owner, name=ref.name)
        body = str(rows[0][0] or "").strip() if rows else ""
        notes: list[str] = []
        truncated = len(body) > MAX_VIEW_CHARS
        if truncated:
            notes.append(f"Câu lệnh của view dài {len(body)} ký tự, chỉ hiện {MAX_VIEW_CHARS} ký tự đầu")
            body = body[:MAX_VIEW_CHARS]
        out += ["", "## Câu lệnh của view", "", _fenced(body) if body else "(không đọc được câu lệnh của view)"]
        if truncated:
            out += ["", f"… {notes[0]}"]
        text = "\n".join(out)
        return DocContent(title=f"View {ref.qualified}", text=text, version=self._version(text, obj["VIEW"][1]), truncated=truncated, notes=notes)

    def _signatures(self, s: _Session, ref: Ref, kind: str) -> list[str]:
        """One line per subprogram from ALL_ARGUMENTS: `NAME(P_ID IN NUMBER, P_OUT OUT VARCHAR2) RETURN NUMBER`."""
        rows = s.query(SQL_ARGUMENTS, owner=ref.owner, name=ref.name, lim=MAX_ARGUMENTS)
        programs: dict[Any, dict[str, Any]] = {}
        for name, subprogram, argument, position, data_type, in_out, defaulted, type_owner, type_name, type_subname, pls_type in rows:
            program = programs.setdefault(subprogram, {"name": str(name), "args": [], "returns": ""})
            if data_type is None and argument is None:
                continue  # before 12.1 a procedure without arguments has one empty row
            shown = ".".join(str(part) for part in (type_owner, type_name, type_subname) if part) if type_name else str(pls_type or data_type)
            if argument is None and position == 0:
                program["returns"] = shown
            else:
                program["args"].append(f"{argument} {in_out} {shown}" + (" DEFAULT" if defaulted == "Y" else ""))
        lines = [f"- `{p['name']}({', '.join(p['args'])})" + (f" RETURN {p['returns']}" if p["returns"] else "") + "`" for p in programs.values()]
        if kind == "PACKAGE":
            lines.append("(Thủ tục không có tham số không có trong ALL_ARGUMENTS: xem phần mã nguồn.)")
        elif not lines:
            lines.append(f"- `{ref.name}()`")
        if len(rows) >= MAX_ARGUMENTS:
            lines.append(f"… chỉ đọc {MAX_ARGUMENTS} tham số đầu; phần còn lại xem trong mã nguồn")
        return lines

    def _code(self, s: _Session, ref: Ref) -> DocContent:
        obj = self._object(s, ref)
        kind = next(k for k in CODE_TYPES if k in obj and k in WANTED[ref.kind])
        title = f"{TITLE_OF[kind]} {ref.qualified}"
        parts = [kind, "PACKAGE BODY"] if kind == "PACKAGE" and "PACKAGE BODY" in obj else [kind]
        status = " · ".join(f"{part} {obj[part][0]}" for part in parts) if len(parts) > 1 else obj[kind][0]
        out = [f"# {title}", "", f"Loại: {kind} · Trạng thái: {status}", "", "## Tham số", "", *self._signatures(s, ref, kind)]

        cap = self.config.max_source_lines
        totals = {str(part): int(count or 0) for part, count in s.query(SQL_SOURCE_COUNT, owner=ref.owner, name=ref.name)}
        by_part: dict[str, list[str]] = {}
        for part, _line, line_text in s.query(SQL_SOURCE, owner=ref.owner, name=ref.name, lim=cap):
            by_part.setdefault(str(part), []).append(str(line_text or ""))
        notes: list[str] = []
        shown = 0
        for part in SOURCE_ORDER:
            lines = by_part.get(part, [])[: cap - shown]
            if not lines:
                continue
            shown += len(lines)
            out += ["", f"## Mã nguồn ({part})", "", _fenced("".join(line if line.endswith("\n") else line + "\n" for line in lines))]
            if lines[0].rstrip().lower().endswith(" wrapped"):
                notes.append(f"mã nguồn {part} đã bị mã hoá (wrapped), AI không đọc được")
        total = max(sum(totals.values()), shown)
        left = total - shown
        if not shown:
            out += ["", "## Mã nguồn", "", "(không đọc được mã nguồn: tài khoản không có quyền xem ALL_SOURCE của đối tượng này)"]
        if left > 0:
            out += ["", f"… còn {left} dòng chưa hiện (đã hiện {shown}/{total} dòng; tăng `max_source_lines` của kết nối để đọc hết)"]
            notes.append(f"mã nguồn bị cắt: còn {left} dòng chưa hiện")
        text = "\n".join(out)
        return DocContent(title=title, text=text, version=self._version(text, *(obj[part][1] for part in parts)), truncated=left > 0, notes=notes)

    def _schema(self, s: _Session, ref: Ref) -> DocContent:
        totals = {str(kind): int(count or 0) for kind, count in s.query(SQL_SCHEMA_COUNT, owner=ref.owner)}
        if not sum(totals.values()):
            raise ConnectorError("not_found", f"{self.label}: tài khoản không có quyền đọc schema {ref.owner} (không thấy bảng hay view nào)")
        rows = s.query(SQL_SCHEMA_OBJECTS, owner=ref.owner, lim=MAX_SCHEMA_OBJECTS)
        tables = [f"| {_cell(name)} | {'' if count is None else count} | {_cell(comment)} |" for name, kind, comment, count in rows if kind == "TABLE"]
        views = [f"| {_cell(name)} | {_cell(comment)} |" for name, kind, comment, _ in rows if kind == "VIEW"]
        out = [f"# Schema {ref.owner}", "", f"Bảng: {totals.get('TABLE', 0)} · View: {totals.get('VIEW', 0)}. Đọc chi tiết bằng table:{ref.owner}.TÊN hoặc view:{ref.owner}.TÊN."]
        if tables:
            out += ["", "## Bảng", "", "| Bảng | Số dòng (ước tính) | Ghi chú |", "|---|---|---|", *tables]
        if views:
            out += ["", "## View", "", "| View | Ghi chú |", "|---|---|", *views]
        left = sum(totals.values()) - len(rows)
        notes: list[str] = []
        if left > 0:
            out += ["", f"… còn {left} bảng/view chưa liệt kê (giới hạn {MAX_SCHEMA_OBJECTS}); tìm theo tên bằng source_search"]
            notes.append(f"danh sách bị cắt: còn {left} bảng/view chưa liệt kê")
        text = "\n".join(out)
        return DocContent(title=f"Schema {ref.owner}", text=text, version=self._version(text), truncated=left > 0, notes=notes)

    # ---- search

    @staticmethod
    def _patterns(query: str) -> dict[str, str] | None:
        """The words of a query as LIKE patterns `%WORD%` with the wildcards a word holds escaped. A bind that has no
        word matches everything. None when the query has no word."""
        words = [word[:MAX_WORD_CHARS].upper() for word in query.split()][:MAX_WORDS]
        if not words:
            return None
        escaped = ["%" + word.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%" for word in words]
        escaped += ["%"] * (MAX_WORDS - len(escaped))
        return {f"q{i + 1}": pattern for i, pattern in enumerate(escaped)}

    async def search(self, query: str, scope: Scope, limit: int = 10) -> list[DocHit]:
        patterns = self._patterns(query)
        if patterns is None:
            return []
        limit = max(1, min(limit, MAX_HITS))

        def work(s: _Session) -> list[DocHit]:
            found: dict[str, DocHit] = {}

            def add(kind: str, owner: str, name: Any, snippet: str) -> None:
                ref = f"{PREFIX_OF[kind]}:{owner}.{name}"
                if ref not in found and len(found) < limit:
                    found[ref] = DocHit(ref=DocRef(ref=ref, kind=NAME), title=f"{TITLE_OF[kind]} {owner}.{name}", snippet=snippet[:240])

            for owner in self.schemas:  # only the allowed schemas are ever asked about
                binds = {"owner": owner, "lim": limit, **patterns}
                for name, kind, comment in s.query(SQL_SEARCH_TABLES, **binds):
                    add(str(kind), owner, name, " ".join(str(comment or "").split()))
                for name, kind, column, comment in s.query(SQL_SEARCH_COLUMNS, **binds):
                    add(str(kind), owner, name, f"cột {column}" + (f": {' '.join(str(comment).split())}" if comment else ""))
                for name, kind in s.query(SQL_SEARCH_PROCS, **binds):
                    add(str(kind), owner, name, str(kind))
                if len(found) >= limit:
                    break
            return list(found.values())

        return await self._run(work)
