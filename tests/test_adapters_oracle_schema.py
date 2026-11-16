"""The `oracle_schema` adapter: an Oracle schema as a read-only document source. No database here: the driver is a
fake connection that answers each of the adapter's fixed statements from a small data dictionary, and refuses any
text that is not one of them."""

from __future__ import annotations

import ast
import json
import re
import sys
import types
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any

import pytest

from actoroffice.adapters import oracle_schema as mod
from actoroffice.adapters import registry
from actoroffice.adapters.oracle_schema import OracleSchema
from actoroffice.app import Office
from actoroffice.config import Settings
from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError, Scope
from actoroffice.ports.docs import DocRef
from actoroffice.security.policy import brings_untrusted
from actoroffice.tools.registry import ToolCtx
from actoroffice.tools.source_tools import SOURCE_TOOLS

from .fakes import FakeRuntime
from .seed import seed_db

SCOPE = Scope()
PASSWORD = "s3cret-Ora-pw"
SECRET = {"user": "app_ro", "password": PASSWORD}
CONFIG: dict[str, Any] = {"dsn": "db.acme.test:1521/BONDPDB", "schemas": ["bond", "REF"]}
SQL = {name: value for name, value in vars(mod).items() if name.startswith("SQL_")}
MODULE = Path(mod.__file__)


def like(value: Any, pattern: str) -> bool:
    """Oracle's `value LIKE pattern ESCAPE '\\'` on upper-cased text."""
    if value is None:
        return False
    out, i = "", 0
    while i < len(pattern):
        ch = pattern[i]
        if ch == "\\":
            i += 1
            out += re.escape(pattern[i])
        elif ch == "%":
            out += ".*"
        elif ch == "_":
            out += "."
        else:
            out += re.escape(ch)
        i += 1
    return re.fullmatch(out, str(value).upper(), re.DOTALL) is not None


class FakeOracle:
    """A data dictionary and the `connect()` the adapter gets as `transport`. Rows come back as tuples, like the driver's."""

    def __init__(self) -> None:
        self.user = "APP_RO"
        self.calls: list[tuple[str, dict[str, Any]]] = []  # (name of the SQL constant, binds)
        self.connects = self.closed = 0
        self.timeouts: list[int] = []
        self.connect_error: Exception | None = None
        self.query_error: Exception | None = None
        ddl = datetime(2026, 11, 1, 8, 0, 0)
        # owner, name → comment, rows estimate, DDL time
        self.tables: dict[tuple[str, str], dict[str, Any]] = {
            ("BOND", "ORDERS"): {"comment": "Lệnh đặt mua trái phiếu", "rows": 1200, "ddl": ddl},
            ("BOND", "CUSTOMERS"): {"comment": "Khách hàng", "rows": 300, "ddl": ddl},
            ("BOND", "ORDER_ITEMS"): {"comment": None, "rows": None, "ddl": ddl},
            ("REF", "BRANCHES"): {"comment": "Chi nhánh", "rows": 12, "ddl": ddl},
            ("HR", "SALARIES"): {"comment": "Lương nhân viên theo lệnh", "rows": 50, "ddl": ddl},
            ("HR", "AUDIT_LOG"): {"comment": "Nhật ký", "rows": 9, "ddl": ddl},
        }
        self.views: dict[tuple[str, str], dict[str, Any]] = {
            ("BOND", "V_OPEN_ORDERS"): {"comment": "Lệnh đang mở", "ddl": ddl, "text": "SELECT o.id, o.status\n  FROM orders o\n WHERE o.status = 'OPEN'"},
        }
        # owner, table → (column, type, length, precision, scale, char length, char used, nullable, default, comment)
        self.columns: dict[tuple[str, str], list[tuple[Any, ...]]] = {
            ("BOND", "ORDERS"): [
                ("ID", "NUMBER", 22, 19, 0, 0, None, "N", None, "Khoá chính"),
                ("ORDER_CODE", "VARCHAR2", 200, None, None, 50, "C", "N", None, "Mã lệnh | duy nhất"),
                ("CUSTOMER_ID", "NUMBER", 22, 19, 0, 0, None, "N", None, None),
                ("BRANCH_ID", "NUMBER", 22, None, None, 0, None, "Y", None, None),
                ("STATUS", "VARCHAR2", 20, None, None, 20, "B", "N", "'NEW'\n", "Trạng thái: NEW, OPEN, DONE"),
                ("AMOUNT", "NUMBER", 22, 18, 2, 0, None, "Y", "0", "Giá trị lệnh"),
                ("CREATED_AT", "TIMESTAMP(6)", 11, None, 6, 0, None, "N", "SYSTIMESTAMP", None),
            ],
            ("BOND", "V_OPEN_ORDERS"): [("ID", "NUMBER", 22, 19, 0, 0, None, "N", None, None), ("STATUS", "VARCHAR2", 20, None, None, 20, "B", "N", None, "Trạng thái")],
            ("HR", "SALARIES"): [("AMOUNT", "NUMBER", 22, 18, 2, 0, None, "Y", None, "Giá trị lương")],
        }
        # owner, table → (constraint, type, columns, (owner, table, columns) it references)
        self.constraints: dict[tuple[str, str], list[tuple[str, str, list[str], tuple[str, str, list[str]] | None]]] = {
            ("BOND", "ORDERS"): [
                ("PK_ORDERS", "P", ["ID"], None),
                ("UK_ORDERS_CODE", "U", ["ORDER_CODE"], None),
                ("FK_ORDERS_CUSTOMER", "R", ["CUSTOMER_ID"], ("BOND", "CUSTOMERS", ["ID"])),
                ("FK_ORDERS_BRANCH", "R", ["BRANCH_ID"], ("REF", "BRANCHES", ["ID"])),
                ("FK_ORDERS_AUDIT", "R", ["ID"], ("HR", "AUDIT_LOG", ["ORDER_ID"])),
            ],
            ("BOND", "ORDER_ITEMS"): [("FK_ITEMS_ORDER", "R", ["ORDER_ID"], ("BOND", "ORDERS", ["ID"]))],
            ("HR", "SALARIES"): [("FK_SALARY_ORDER", "R", ["ORDER_ID"], ("BOND", "ORDERS", ["ID"]))],
        }
        # owner, table → (index, uniqueness, type, [(column, descend)])
        self.indexes: dict[tuple[str, str], list[tuple[str, str, str, list[tuple[str, str]]]]] = {
            ("BOND", "ORDERS"): [("IX_ORDERS_STATUS", "NONUNIQUE", "NORMAL", [("STATUS", "ASC"), ("CREATED_AT", "DESC")]), ("PK_ORDERS", "UNIQUE", "NORMAL", [("ID", "ASC")])],
        }
        # owner, name → type → {status, ddl, source lines}; arguments as ALL_ARGUMENTS rows
        self.code: dict[tuple[str, str], dict[str, dict[str, Any]]] = {
            ("BOND", "PLACE_ORDER"): {"PROCEDURE": {"status": "VALID", "ddl": ddl, "source": ["PROCEDURE place_order(p_customer_id IN NUMBER) IS\n", "BEGIN\n", "  NULL;\n", "END;\n"]}},
            ("BOND", "CALC_FEE"): {"FUNCTION": {"status": "INVALID", "ddl": ddl, "source": ["FUNCTION calc_fee(p_amount NUMBER) RETURN NUMBER IS\n", "BEGIN RETURN 0; END;\n"]}},
            ("BOND", "ORDER_API"): {
                "PACKAGE": {"status": "VALID", "ddl": ddl, "source": ["PACKAGE order_api IS\n", "  PROCEDURE cancel(p_id NUMBER);\n", "END;\n"]},
                "PACKAGE BODY": {"status": "INVALID", "ddl": datetime(2026, 11, 2, 9, 30, 0), "source": [f"  -- body line {n}\n" for n in range(1, 11)]},
            },
            ("HR", "PAY_ORDER"): {"PROCEDURE": {"status": "VALID", "ddl": ddl, "source": ["PROCEDURE pay_order IS BEGIN NULL; END;\n"]}},
        }
        self.arguments: dict[tuple[str, str], list[tuple[Any, ...]]] = {
            ("BOND", "PLACE_ORDER"): [
                ("PLACE_ORDER", 1, "P_CUSTOMER_ID", 1, "NUMBER", "IN", "N", None, None, None, None),
                ("PLACE_ORDER", 1, "P_AMOUNT", 2, "NUMBER", "IN", "Y", None, None, None, None),
                ("PLACE_ORDER", 1, "P_ORDER_ID", 3, "NUMBER", "OUT", "N", None, None, None, None),
            ],
            ("BOND", "CALC_FEE"): [("CALC_FEE", 1, None, 0, "NUMBER", "OUT", "N", None, None, None, None), ("CALC_FEE", 1, "P_AMOUNT", 1, "NUMBER", "IN", "N", None, None, None, None)],
            ("BOND", "ORDER_API"): [
                ("CANCEL", 1, "P_ID", 1, "NUMBER", "IN", "N", None, None, None, None),
                ("FIND", 2, None, 0, "PL/SQL RECORD", "OUT", "N", "BOND", "ORDERS", "%ROWTYPE", None),
                ("FIND", 2, "P_CODE", 1, "VARCHAR2", "IN", "N", None, None, None, None),
            ],
        }
        self.answers = {
            mod.SQL_WHOAMI: lambda b: [(self.user,)],
            mod.SQL_COUNT_TABLES: lambda b: [(sum(1 for owner, _ in self.tables if owner == b["owner"]),)],
            mod.SQL_OBJECT: self._object,
            mod.SQL_TAB_COMMENT: lambda b: [(item["comment"],) for key, item in {**self.tables, **self.views}.items() if key == (b["owner"], b["name"])],
            mod.SQL_COLUMNS: lambda b: [row[:9] for row in self.columns.get((b["owner"], b["name"]), [])],
            mod.SQL_COL_COMMENTS: lambda b: [(row[0], row[9]) for row in self.columns.get((b["owner"], b["name"]), []) if row[9] is not None],
            mod.SQL_CONSTRAINTS: lambda b: sorted(((name, kind, "ENABLED") for name, kind, _, _ in self.constraints.get((b["owner"], b["name"]), [])), key=lambda r: (r[1], r[0])),
            mod.SQL_CONS_COLUMNS: lambda b: [(name, column) for name, _, columns, _ in self.constraints.get((b["owner"], b["name"]), []) for column in columns],
            mod.SQL_FK_TARGETS: lambda b: [(name, to[0], to[1], column) for name, _, _, to in self.constraints.get((b["owner"], b["name"]), []) if to for column in to[2]],
            mod.SQL_REFERENCED_BY: self._referenced_by,
            mod.SQL_INDEXES: lambda b: [(name, uniq, kind, column, descend) for name, uniq, kind, columns in self.indexes.get((b["owner"], b["name"]), []) for column, descend in columns],
            mod.SQL_VIEW_TEXT: lambda b: [(view["text"],) for key, view in self.views.items() if key == (b["owner"], b["name"])],
            mod.SQL_ARGUMENTS: lambda b: self.arguments.get((b["owner"], b["name"]), [])[: b["lim"]],
            mod.SQL_SOURCE: lambda b: [(kind, n, line) for kind, part in sorted(self.code.get((b["owner"], b["name"]), {}).items()) for n, line in enumerate(part["source"], 1) if n <= b["lim"]],
            mod.SQL_SOURCE_COUNT: lambda b: [(kind, len(part["source"])) for kind, part in self.code.get((b["owner"], b["name"]), {}).items()],
            mod.SQL_SCHEMA_COUNT: self._schema_count,
            mod.SQL_SCHEMA_OBJECTS: self._schema_objects,
            mod.SQL_SEARCH_TABLES: self._search_tables,
            mod.SQL_SEARCH_COLUMNS: self._search_columns,
            mod.SQL_SEARCH_PROCS: self._search_procs,
        }

    # ---- the driver's surface

    def connect(self) -> FakeConnection:
        self.connects += 1
        if self.connect_error is not None:
            raise self.connect_error
        return FakeConnection(self)

    def execute(self, sql: str, binds: Any) -> list[tuple[Any, ...]]:
        assert isinstance(binds, dict), "bind variables go as a dict"
        name = next((n for n, text in SQL.items() if text == sql), None)
        assert name is not None, f"not one of the adapter's statements: {sql!r}"
        assert set(re.findall(r":(\w+)", sql)) == set(binds), f"{name}: binds {sorted(binds)} do not match the statement"
        self.calls.append((name, dict(binds)))
        if self.query_error is not None:
            raise self.query_error
        return list(self.answers[sql](binds))

    def names(self) -> list[str]:
        return [name for name, _ in self.calls]

    # ---- answers

    def _object(self, b: dict[str, Any]) -> list[tuple[Any, ...]]:
        key = (b["owner"], b["name"])
        rows = [("TABLE", "VALID", self.tables[key]["ddl"])] if key in self.tables else []
        rows += [("VIEW", "VALID", self.views[key]["ddl"])] if key in self.views else []
        return rows + [(kind, part["status"], part["ddl"]) for kind, part in self.code.get(key, {}).items()]

    def _referenced_by(self, b: dict[str, Any]) -> list[tuple[Any, ...]]:
        found = [
            (owner, table, name, column)
            for (owner, table), items in self.constraints.items()
            for name, _, columns, to in items
            if to is not None and to[:2] == (b["owner"], b["name"])
            for column in columns
        ]
        return sorted(found)

    def _named(self, owner: str) -> list[tuple[str, str, Any, Any]]:
        tables = [(name, "TABLE", item["comment"], item["rows"]) for (o, name), item in self.tables.items() if o == owner]
        return sorted(tables) + sorted((name, "VIEW", item["comment"], None) for (o, name), item in self.views.items() if o == owner)

    def _schema_count(self, b: dict[str, Any]) -> list[tuple[Any, ...]]:
        kinds = [kind for _, kind, _, _ in self._named(b["owner"])]
        return [(kind, kinds.count(kind)) for kind in dict.fromkeys(kinds)]

    def _schema_objects(self, b: dict[str, Any]) -> list[tuple[Any, ...]]:
        return self._named(b["owner"])[: b["lim"]]

    @staticmethod
    def _all(b: dict[str, Any], *values: Any) -> bool:
        return all(any(like(value, b[q]) for value in values) for q in ("q1", "q2", "q3"))

    def _search_tables(self, b: dict[str, Any]) -> list[tuple[Any, ...]]:
        return sorted((name, kind, comment) for name, kind, comment, _ in self._named(b["owner"]) if self._all(b, name, comment))[: b["lim"]]

    def _search_columns(self, b: dict[str, Any]) -> list[tuple[Any, ...]]:
        kinds = {name: kind for name, kind, _, _ in self._named(b["owner"])}
        found = [(table, kinds[table], row[0], row[9]) for (owner, table), rows in self.columns.items() if owner == b["owner"] for row in rows if self._all(b, row[0], row[9])]
        return sorted(found)[: b["lim"]]

    def _search_procs(self, b: dict[str, Any]) -> list[tuple[Any, ...]]:
        found = [(name, kind) for (owner, name), parts in self.code.items() if owner == b["owner"] for kind in parts if kind != "PACKAGE BODY" and self._all(b, name)]
        return sorted(found)[: b["lim"]]


class FakeConnection:
    def __init__(self, db: FakeOracle) -> None:
        self.db = db

    def __setattr__(self, name: str, value: Any) -> None:
        if name == "call_timeout":
            self.db.timeouts.append(value)
        object.__setattr__(self, name, value)

    def cursor(self) -> FakeCursor:
        return FakeCursor(self.db)

    def close(self) -> None:
        self.db.closed += 1


class FakeCursor:
    def __init__(self, db: FakeOracle) -> None:
        self.db = db
        self.rows: list[tuple[Any, ...]] = []

    def execute(self, sql: str, binds: Any = None) -> None:
        self.rows = self.db.execute(sql, binds)

    def fetchall(self) -> list[tuple[Any, ...]]:
        return self.rows

    def close(self) -> None:
        return None


class DriverError(Exception):
    """Shaped like python-oracledb's: `args[0]` is an error object with `full_code` and `message`."""

    def __init__(self, full_code: str, message: str) -> None:
        super().__init__(types.SimpleNamespace(full_code=full_code, message=message))
        self.text = message

    def __str__(self) -> str:
        return self.text


def build(db: FakeOracle, config: dict[str, Any] | None = None, secret: dict[str, str] | None = None) -> OracleSchema:
    return registry.build(ConnectionSpec(id="ora", use="oracle_schema", config=config or CONFIG), SECRET if secret is None else secret, transport=db.connect)


def refused(config: dict[str, Any]) -> str:
    with pytest.raises(ConnectorError) as err:
        registry.validate(ConnectionSpec(id="ora", use="oracle_schema", config=config))
    assert err.value.kind == "invalid"
    return str(err.value)


@pytest.fixture
def db() -> FakeOracle:
    return FakeOracle()


@pytest.fixture
async def office(tmp_path) -> AsyncIterator[Office]:
    o = Office(Settings(data_dir=tmp_path / "home", approval_wait_s=0.05, ai_review=False), runtime=FakeRuntime(scripts=[]))
    seed_db(o.settings.db_path)
    await o.open()
    yield o
    await o.close()


# ---- registry, config, imports ----------------------------------------------------------------------------------------


def test_registered_and_described_for_the_settings_page() -> None:
    assert "oracle_schema" in registry.names() and registry.BUILTIN["oracle_schema"].endswith(":OracleSchema")
    info = registry.describe("oracle_schema")
    assert info["title"] == "Lược đồ Oracle (chỉ đọc)" and info["capabilities"] == ["docs"] and info["secret_fields"] == ["user", "password"]
    schema = info["config_schema"]
    assert set(schema["properties"]) == {"dsn", "host", "port", "service_name", "schemas", "max_source_lines", "statement_timeout_s"} and schema["required"] == ["schemas"]
    assert all(prop.get("description") for prop in schema["properties"].values()), "every field of the form is explained"
    assert json.loads(json.dumps(info)) == info
    assert registry.capabilities("oracle_schema") == frozenset({Capability.DOCS})


def test_config_either_way_of_writing_the_address_and_upper_cased_schemas() -> None:
    by_dsn = registry.validate(ConnectionSpec(id="ora", use="oracle_schema", config=CONFIG))
    assert by_dsn.address() == ("db.acme.test", 1521, "BONDPDB") and by_dsn.schemas == ["BOND", "REF"]
    assert (by_dsn.max_source_lines, by_dsn.statement_timeout_s) == (400, 30)
    by_parts = registry.validate(ConnectionSpec(id="ora", use="oracle_schema", config={"host": "10.0.0.5", "service_name": "orcl.acme", "schemas": "bond"}))
    assert by_parts.address() == ("10.0.0.5", 1521, "orcl.acme") and by_parts.schemas == ["BOND"]
    assert registry.validate(ConnectionSpec(id="ora", use="oracle_schema", config={"dsn": "db/SVC", "schemas": ["A", "a"]})).address() == ("db", 1521, "SVC")


@pytest.mark.parametrize(
    ("config", "named"),
    [
        ({"schemas": ["BOND"]}, ["dsn", "host", "service_name"]),
        ({"host": "db", "schemas": ["BOND"]}, ["service_name"]),
        ({"dsn": "db:1521/SVC", "host": "db", "schemas": ["BOND"]}, ["dsn", "không khai cả hai"]),
        ({"dsn": "app_ro/pw@db:1521/SVC", "schemas": ["BOND"]}, ["dsn", "host:port/service_name"]),
        ({"dsn": "db:1521", "schemas": ["BOND"]}, ["dsn"]),
        ({"dsn": "db/SVC"}, ["schemas"]),
        ({"dsn": "db/SVC", "schemas": []}, ["schemas", "ít nhất một schema"]),
        ({"dsn": "db/SVC", "schemas": ["BOND; DROP"]}, ["schemas", "không phải tên schema"]),
        ({"dsn": "db/SVC", "schemas": ["X' OR '1'='1"]}, ["schemas"]),
        ({"host": "db host", "service_name": "S", "schemas": ["BOND"]}, ["host"]),
        ({"host": "db", "service_name": "S)(X", "schemas": ["BOND"]}, ["service_name"]),
        ({"dsn": "db/SVC", "schemas": ["BOND"], "port": 0}, ["port"]),
        ({"dsn": "db/SVC", "schemas": ["BOND"], "max_source_lines": 0}, ["max_source_lines"]),
        ({"dsn": "db/SVC", "schemas": ["BOND"], "statement_timeout_s": 0}, ["statement_timeout_s"]),
        ({"dsn": "db/SVC", "schemas": ["BOND"], "pasword": "x"}, ["pasword"]),
    ],
)
def test_bad_config_is_refused_with_the_field_named(config: dict[str, Any], named: list[str]) -> None:
    message = refused(config)
    assert all(part in message for part in named), message


def test_imports_only_ports_adapters_stdlib_pydantic_and_the_optional_driver() -> None:
    names: list[str] = []
    for node in ast.walk(ast.parse(MODULE.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names += [alias.name for alias in node.names]
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            names.append(node.module or "")
    assert "oracledb" in names
    for name in names:
        top = name.split(".")[0]
        if top == "actoroffice":
            assert name.startswith(("actoroffice.ports", "actoroffice.adapters")), name
        else:
            assert top in sys.stdlib_module_names or top in {"pydantic", "oracledb"}, name


# ---- SQL safety -------------------------------------------------------------------------------------------------------


def test_every_statement_is_a_plain_constant_select_and_nothing_else_is_ever_executed() -> None:
    tree = ast.parse(MODULE.read_text(encoding="utf-8"))
    assigned = {node.targets[0].id: node.value for node in tree.body if isinstance(node, ast.Assign) and isinstance(node.targets[0], ast.Name) and node.targets[0].id.startswith("SQL_")}
    assert set(assigned) == set(SQL) and len(SQL) >= 15
    for name, value in assigned.items():
        assert isinstance(value, ast.Constant) and isinstance(value.value, str), f"{name} is built at run time (f-string, %, .format or +)"
        text = value.value
        assert "%" not in text and "{" not in text and "}" not in text, f"{name}: a wildcard or a placeholder in the statement's text"
        assert text.startswith("SELECT ") and ";" not in text and "--" not in text and "/*" not in text, name
        assert not re.search(r"\b(INSERT|UPDATE|DELETE|MERGE|DROP|ALTER|CREATE|TRUNCATE|GRANT|REVOKE|EXECUTE|CALL|BEGIN|COMMIT|INTO|FOR UPDATE)\b", text, re.IGNORECASE), name
        tables = set(re.findall(r"\b(?:FROM|JOIN)\s+([A-Za-z_]+)", text))
        assert tables and all(t == "DUAL" or t.lower().startswith("all_") for t in tables), f"{name} reads {tables}: only the data dictionary"
        assert text.count("'") % 2 == 0 and all(":" not in literal for literal in re.findall(r"'[^']*'", text)), f"{name}: a bind inside a literal"
    # the module never formats a string that goes to the database: `query` is only called with a SQL_* name, and
    # `execute` only inside `query` with what it was given
    queries = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr == "query"]
    assert queries and all(isinstance(call.args[0], ast.Name) and call.args[0].id in SQL and len(call.args) == 1 for call in queries), "query(SQL_X, **binds) only"
    executes = [node for node in ast.walk(tree) if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr.startswith("execute")]
    assert len(executes) == 1 and [type(a) for a in executes[0].args] == [ast.Name, ast.Name] and [a.id for a in executes[0].args] == ["sql", "binds"]


@pytest.mark.parametrize(
    "bad",
    [
        "table:X' OR '1'='1",
        "table:BOND.X' OR '1'='1",
        "table:A;DROP",
        "table:BOND.A;DROP TABLE ORDERS",
        "table:BOND.ORDERS--",
        "table:BOND.ORDERS.ID",
        "table:BOND.\"orders\"",
        "table:BOND.1ORDERS",
        "table:BOND." + "A" * 129,
        "table:BOND.ĐƠN",
        "table:ORDERS",  # two schemas configured: the owner is needed
        "schema:BOND.ORDERS",
        "sequence:BOND.SEQ",
        "table:",
        "BOND.ORDERS",
        "select * from bond.orders",
    ],
)
async def test_a_malicious_or_malformed_name_is_refused_before_any_query(db: FakeOracle, bad: str) -> None:
    adapter = build(db)
    assert adapter.recognize(bad) == [] and not adapter.in_scope(DocRef(ref=bad), SCOPE)
    with pytest.raises(ConnectorError) as err:
        await adapter.fetch(DocRef(ref=bad), SCOPE)
    assert err.value.kind == "invalid" and "không truy vấn nào được chạy" in str(err.value)
    assert (db.connects, db.calls) == (0, []), "not even a connection was opened"


async def test_an_owner_outside_the_allowed_schemas_is_recognised_but_never_queried(db: FakeOracle) -> None:
    adapter = build(db)
    for ref in ("table:HR.SALARIES", "view:hr.v_pay", "proc:HR.PAY_ORDER", "schema:HR", "schema:SYS"):
        assert [r.ref for r in adapter.recognize(ref)] == [ref.split(":")[0] + ":" + ref.split(":")[1].upper()]
        assert not adapter.in_scope(DocRef(ref=ref), SCOPE)
        with pytest.raises(ConnectorError) as err:
            await adapter.fetch(DocRef(ref=ref), SCOPE)
        assert err.value.kind == "forbidden" and "BOND, REF" in str(err.value)
    assert (db.connects, db.calls) == (0, [])


# ---- references -------------------------------------------------------------------------------------------------------


def test_references_in_prose_are_found_upper_cased_and_whole(db: FakeOracle) -> None:
    adapter = build(db)
    text = "Xem `table:bond.orders`, view:BOND.V_OPEN_ORDERS. Gọi proc:Bond.place_order; hàm (function:BOND.CALC_FEE) và **package:BOND.ORDER_API**, tổng quan schema:ref."
    assert [r.ref for r in adapter.recognize(text)] == [
        "table:BOND.ORDERS", "view:BOND.V_OPEN_ORDERS", "proc:BOND.PLACE_ORDER", "function:BOND.CALC_FEE", "package:BOND.ORDER_API", "schema:REF",
    ]  # fmt: skip
    assert all(r.kind == "oracle_schema" for r in adapter.recognize(text))
    assert adapter.recognize("xtable:BOND.ORDERS http://x/table:BOND.ORDERS a.table:BOND.ORDERS table: BOND.ORDERS table:BOND.ORDERS'") == []
    assert adapter.in_scope(DocRef(ref=" TABLE:Bond.Orders "), SCOPE)
    one = build(db, {"dsn": "db/SVC", "schemas": ["BOND"]})
    assert [r.ref for r in one.recognize("table:orders và proc:HR.PAY_ORDER")] == ["table:BOND.ORDERS", "proc:HR.PAY_ORDER"], "one schema: the owner may be left out"


# ---- documents --------------------------------------------------------------------------------------------------------


async def test_table_document_has_columns_keys_indexes_and_who_refers_to_it(db: FakeOracle) -> None:
    adapter = build(db)
    doc = await adapter.fetch(DocRef(ref="table:bond.orders"), SCOPE)
    assert (doc.title, doc.url, doc.truncated) == ("Bảng BOND.ORDERS", "", False)
    text = doc.text
    assert text.startswith("# Bảng BOND.ORDERS\n\nLệnh đặt mua trái phiếu\n\n## Cột\n\n| Cột | Kiểu | Null | Mặc định | Ghi chú |\n|---|---|---|---|---|\n")
    for row in (
        "| ID | NUMBER(19) | không |  | Khoá chính |",
        "| ORDER_CODE | VARCHAR2(50 CHAR) | không |  | Mã lệnh \\| duy nhất |",
        "| BRANCH_ID | NUMBER | có |  |  |",
        "| STATUS | VARCHAR2(20) | không | 'NEW' | Trạng thái: NEW, OPEN, DONE |",
        "| AMOUNT | NUMBER(18,2) | có | 0 | Giá trị lệnh |",
        "| CREATED_AT | TIMESTAMP(6) | không | SYSTIMESTAMP |  |",
    ):
        assert row in text, row
    assert "## Khoá chính\n\n- PK_ORDERS (ID)\n" in text
    assert "## Khoá duy nhất\n\n- UK_ORDERS_CODE (ORDER_CODE)\n" in text
    assert "- FK_ORDERS_CUSTOMER (CUSTOMER_ID) → BOND.CUSTOMERS(ID)" in text and "- FK_ORDERS_BRANCH (BRANCH_ID) → REF.BRANCHES(ID)" in text
    assert "## Chỉ mục\n\n- IX_ORDERS_STATUS (STATUS, CREATED_AT DESC) — NONUNIQUE\n- PK_ORDERS (ID) — UNIQUE\n" in text
    assert text.endswith("## Được tham chiếu bởi\n\n- BOND.ORDER_ITEMS(ORDER_ID) qua FK_ITEMS_ORDER")
    # another schema's tables are never named: neither the one a key points to nor the one that points here
    assert "- FK_ORDERS_AUDIT (ID) → (bảng ở schema ngoài phạm vi của kết nối)" in text and "HR" not in text and "SALARIES" not in text and "AUDIT_LOG" not in text
    assert [link.ref for link in doc.links] == ["table:REF.BRANCHES", "table:BOND.CUSTOMERS", "table:BOND.ORDER_ITEMS"]
    assert re.fullmatch(r"20261101T080000-sha256:[0-9a-f]{16}", doc.version), doc.version
    # what was sent: the fixed statements, the names as bind variables only
    assert db.names() == ["SQL_OBJECT", "SQL_TAB_COMMENT", "SQL_COL_COMMENTS", "SQL_COLUMNS", "SQL_CONS_COLUMNS", "SQL_FK_TARGETS", "SQL_CONSTRAINTS", "SQL_INDEXES", "SQL_REFERENCED_BY"]
    assert all(binds == {"owner": "BOND", "name": "ORDERS"} for _, binds in db.calls)
    assert (db.connects, db.closed, db.timeouts) == (1, 1, [30_000]), "one connection for the call, closed, with the call timeout in milliseconds"
    # a table without comments or keys still reads well
    bare = (await adapter.fetch(DocRef(ref="table:REF.BRANCHES"), SCOPE)).text
    assert "## Khoá chính\n\n(không có)" in bare and "## Được tham chiếu bởi\n\n- BOND.ORDERS(BRANCH_ID) qua FK_ORDERS_BRANCH" in bare
    assert "(chưa có ghi chú)" in (await adapter.fetch(DocRef(ref="table:BOND.ORDER_ITEMS"), SCOPE)).text


async def test_version_changes_when_a_column_changes_and_stays_when_nothing_does(db: FakeOracle) -> None:
    adapter = build(db)
    first = await adapter.fetch(DocRef(ref="table:BOND.ORDERS"), SCOPE)
    again = await adapter.fetch(DocRef(ref="table:BOND.ORDERS"), SCOPE)
    assert (again.version, again.text) == (first.version, first.text)
    db.columns[("BOND", "ORDERS")].append(("NOTE", "VARCHAR2", 4000, None, None, 4000, "B", "Y", None, "Ghi chú của lệnh"))
    changed = await adapter.fetch(DocRef(ref="table:BOND.ORDERS"), SCOPE)
    assert changed.version != first.version and "| NOTE | VARCHAR2(4000) | có |  | Ghi chú của lệnh |" in changed.text, "LAST_DDL_TIME did not move, the content hash did"
    db.tables[("BOND", "ORDERS")]["ddl"] = datetime(2026, 11, 16, 10, 0, 0)
    moved = await adapter.fetch(DocRef(ref="table:BOND.ORDERS"), SCOPE)
    assert moved.version.startswith("20261116T100000-") and moved.version.split("-")[1] == changed.version.split("-")[1]


async def test_view_document_has_comment_columns_and_the_query(db: FakeOracle) -> None:
    adapter = build(db)
    doc = await adapter.fetch(DocRef(ref="view:BOND.V_OPEN_ORDERS"), SCOPE)
    assert doc.title == "View BOND.V_OPEN_ORDERS" and doc.text.startswith("# View BOND.V_OPEN_ORDERS\n\nLệnh đang mở\n\n## Cột\n")
    assert "| STATUS | VARCHAR2(20) | không |  | Trạng thái |" in doc.text
    assert doc.text.endswith("## Câu lệnh của view\n\n```sql\nSELECT o.id, o.status\n  FROM orders o\n WHERE o.status = 'OPEN'\n```")
    assert db.names() == ["SQL_OBJECT", "SQL_TAB_COMMENT", "SQL_COL_COMMENTS", "SQL_COLUMNS", "SQL_VIEW_TEXT"]
    db.views[("BOND", "V_OPEN_ORDERS")]["text"] = "SELECT 1 FROM dual -- " + "x" * 30_000
    long = await adapter.fetch(DocRef(ref="view:BOND.V_OPEN_ORDERS"), SCOPE)
    assert long.truncated and "chỉ hiện 20000 ký tự đầu" in long.text and long.notes and len(long.text) < 21_000


async def test_procedure_function_and_package_documents(db: FakeOracle) -> None:
    adapter = build(db)
    proc = await adapter.fetch(DocRef(ref="proc:BOND.PLACE_ORDER"), SCOPE)
    assert proc.title == "Thủ tục BOND.PLACE_ORDER" and not proc.truncated
    assert proc.text.startswith("# Thủ tục BOND.PLACE_ORDER\n\nLoại: PROCEDURE · Trạng thái: VALID\n\n## Tham số\n\n")
    assert "- `PLACE_ORDER(P_CUSTOMER_ID IN NUMBER, P_AMOUNT IN NUMBER DEFAULT, P_ORDER_ID OUT NUMBER)`" in proc.text
    assert proc.text.endswith("## Mã nguồn (PROCEDURE)\n\n```sql\nPROCEDURE place_order(p_customer_id IN NUMBER) IS\nBEGIN\n  NULL;\nEND;\n```")
    assert db.names() == ["SQL_OBJECT", "SQL_ARGUMENTS", "SQL_SOURCE_COUNT", "SQL_SOURCE"]
    assert db.calls[1][1] == {"owner": "BOND", "name": "PLACE_ORDER", "lim": 500} and db.calls[3][1] == {"owner": "BOND", "name": "PLACE_ORDER", "lim": 400}

    func = await adapter.fetch(DocRef(ref="function:BOND.CALC_FEE"), SCOPE)
    assert func.title == "Hàm BOND.CALC_FEE" and "Loại: FUNCTION · Trạng thái: INVALID" in func.text and "- `CALC_FEE(P_AMOUNT IN NUMBER) RETURN NUMBER`" in func.text
    assert (await adapter.fetch(DocRef(ref="proc:BOND.CALC_FEE"), SCOPE)).text == func.text, "proc: reads any stored code"

    pack = await adapter.fetch(DocRef(ref="package:BOND.ORDER_API"), SCOPE)
    assert pack.title == "Gói BOND.ORDER_API" and "Loại: PACKAGE · Trạng thái: PACKAGE VALID · PACKAGE BODY INVALID" in pack.text
    assert "- `CANCEL(P_ID IN NUMBER)`" in pack.text and "- `FIND(P_CODE IN VARCHAR2) RETURN BOND.ORDERS.%ROWTYPE`" in pack.text
    assert "## Mã nguồn (PACKAGE)\n\n```sql\nPACKAGE order_api IS" in pack.text and "## Mã nguồn (PACKAGE BODY)\n\n```sql\n  -- body line 1\n" in pack.text
    assert pack.version.startswith("20261102T093000-"), "the newer of the specification's and the body's DDL time"

    for wrong, hint in (("function:BOND.PLACE_ORDER", "proc:BOND.PLACE_ORDER"), ("table:BOND.V_OPEN_ORDERS", "view:BOND.V_OPEN_ORDERS"), ("view:BOND.ORDERS", "table:BOND.ORDERS")):
        with pytest.raises(ConnectorError) as err:
            await adapter.fetch(DocRef(ref=wrong), SCOPE)
        assert err.value.kind == "not_found" and hint in str(err.value), wrong


async def test_source_longer_than_the_cap_is_cut_with_a_note(db: FakeOracle) -> None:
    adapter = build(db, {**CONFIG, "max_source_lines": 5})
    pack = await adapter.fetch(DocRef(ref="package:BOND.ORDER_API"), SCOPE)
    assert pack.truncated and pack.notes == ["mã nguồn bị cắt: còn 8 dòng chưa hiện"]
    assert pack.text.endswith("… còn 8 dòng chưa hiện (đã hiện 5/13 dòng; tăng `max_source_lines` của kết nối để đọc hết)")
    assert "-- body line 2\n```" in pack.text and "body line 3" not in pack.text, "the specification first, then what is left of the cap for the body"
    assert db.calls[-1] == ("SQL_SOURCE", {"owner": "BOND", "name": "ORDER_API", "lim": 5}), "the cap is a bind variable too"
    whole = await build(db).fetch(DocRef(ref="package:BOND.ORDER_API"), SCOPE)
    assert not whole.truncated and "còn" not in whole.text and "body line 10" in whole.text


async def test_schema_overview_lists_tables_and_views_with_comments_and_row_estimates(db: FakeOracle, monkeypatch) -> None:
    adapter = build(db)
    doc = await adapter.fetch(DocRef(ref="schema:bond"), SCOPE)
    assert doc.title == "Schema BOND" and doc.text.startswith("# Schema BOND\n\nBảng: 3 · View: 1. Đọc chi tiết bằng table:BOND.TÊN hoặc view:BOND.TÊN.\n")
    assert "| Bảng | Số dòng (ước tính) | Ghi chú |\n|---|---|---|\n| CUSTOMERS | 300 | Khách hàng |\n| ORDERS | 1200 | Lệnh đặt mua trái phiếu |\n| ORDER_ITEMS |  |  |" in doc.text
    assert doc.text.endswith("## View\n\n| View | Ghi chú |\n|---|---|\n| V_OPEN_ORDERS | Lệnh đang mở |")
    assert doc.version.startswith("sha256:") and not doc.truncated
    assert db.calls == [("SQL_SCHEMA_COUNT", {"owner": "BOND"}), ("SQL_SCHEMA_OBJECTS", {"owner": "BOND", "lim": 300})]
    monkeypatch.setattr(mod, "MAX_SCHEMA_OBJECTS", 2)
    cut = await adapter.fetch(DocRef(ref="schema:BOND"), SCOPE)
    assert cut.truncated and cut.text.endswith("… còn 2 bảng/view chưa liệt kê (giới hạn 2); tìm theo tên bằng source_search") and "ORDER_ITEMS" not in cut.text


async def test_an_object_or_schema_that_is_not_there_is_not_found(db: FakeOracle) -> None:
    adapter = build(db, {"dsn": "db/SVC", "schemas": ["BOND", "EMPTY"]})
    for ref, said in (("table:BOND.NOPE", "không có đối tượng BOND.NOPE"), ("proc:BOND.NOPE", "BOND.NOPE"), ("schema:EMPTY", "không có quyền đọc schema EMPTY")):
        with pytest.raises(ConnectorError) as err:
            await adapter.fetch(DocRef(ref=ref), SCOPE)
        assert err.value.kind == "not_found" and said in str(err.value)
    assert db.connects == db.closed == 3, "the connection is closed after a failure too"


# ---- search -----------------------------------------------------------------------------------------------------------


async def test_search_finds_names_comments_columns_and_code_in_the_allowed_schemas_only(db: FakeOracle) -> None:
    adapter = build(db)
    hits = await adapter.search("lệnh", SCOPE, limit=10)
    assert [(h.ref.ref, h.title, h.snippet) for h in hits] == [
        ("table:BOND.ORDERS", "Bảng BOND.ORDERS", "Lệnh đặt mua trái phiếu"),
        ("view:BOND.V_OPEN_ORDERS", "View BOND.V_OPEN_ORDERS", "Lệnh đang mở"),
    ], "case-insensitive, one hit per object; HR.SALARIES mentions it too and is never asked about"
    assert all(h.ref.kind == "oracle_schema" and h.url == "" for h in hits)
    assert [(name, binds["owner"]) for name, binds in db.calls] == [
        ("SQL_SEARCH_TABLES", "BOND"), ("SQL_SEARCH_COLUMNS", "BOND"), ("SQL_SEARCH_PROCS", "BOND"), ("SQL_SEARCH_TABLES", "REF"), ("SQL_SEARCH_COLUMNS", "REF"), ("SQL_SEARCH_PROCS", "REF"),
    ]  # fmt: skip
    assert db.calls[0][1] == {"owner": "BOND", "lim": 10, "q1": "%LỆNH%", "q2": "%", "q3": "%"}

    by_column = await adapter.search("giá trị", SCOPE)
    assert [(h.ref.ref, h.snippet) for h in by_column] == [("table:BOND.ORDERS", "cột AMOUNT: Giá trị lệnh")], "both words, in a column's comment"
    by_name = await adapter.search("order", SCOPE, limit=20)
    assert [h.ref.ref for h in by_name] == [
        "table:BOND.ORDERS", "table:BOND.ORDER_ITEMS", "view:BOND.V_OPEN_ORDERS", "package:BOND.ORDER_API", "proc:BOND.PLACE_ORDER",
    ]  # fmt: skip
    assert by_name[-1].title == "Thủ tục BOND.PLACE_ORDER" and not any("HR" in h.ref.ref for h in by_name)
    assert [h.ref.ref for h in await adapter.search("calc fee", SCOPE)] == ["function:BOND.CALC_FEE"]
    assert len(await adapter.search("order", SCOPE, limit=2)) == 2
    for hit in by_name:
        assert adapter.in_scope(hit.ref, SCOPE) and adapter.recognize(hit.ref.ref) == [hit.ref], "a hit is a reference this adapter fetches"


async def test_search_words_are_binds_with_their_wildcards_escaped(db: FakeOracle) -> None:
    adapter = build(db)
    assert await adapter.search("   ", SCOPE) == [] and db.connects == 0
    assert await adapter.search("100% x_y a\\b fourth", SCOPE) == []
    assert db.calls[0][1] == {"owner": "BOND", "lim": 10, "q1": "%100\\%%", "q2": "%X\\_Y%", "q3": "%A\\\\B%"}, "% _ and \\ are literal; a fourth word is dropped"
    assert await adapter.search("%", SCOPE) == [], "a wildcard alone matches nothing instead of everything"
    underscored = {h.ref.ref for h in await adapter.search("_", SCOPE, limit=50)}
    assert "table:BOND.ORDER_ITEMS" in underscored and "table:BOND.CUSTOMERS" not in underscored and "table:REF.BRANCHES" not in underscored, "_ is a literal underscore"
    assert await adapter.search("' OR '1'='1", SCOPE) == []
    assert db.calls[-1][1]["q1"] == "%'%" and set(db.names()) == {"SQL_SEARCH_TABLES", "SQL_SEARCH_COLUMNS", "SQL_SEARCH_PROCS"}


# ---- the driver and its errors ----------------------------------------------------------------------------------------


async def test_the_driver_is_imported_on_first_use_and_a_missing_one_says_what_to_install(monkeypatch) -> None:
    monkeypatch.setitem(sys.modules, "oracledb", None)  # the import fails, as when the extra is not installed
    adapter = registry.build(ConnectionSpec(id="ora", use="oracle_schema", config=CONFIG), SECRET)
    assert [r.ref for r in adapter.recognize("table:BOND.ORDERS")] == ["table:BOND.ORDERS"], "no driver is needed to recognise a reference"
    for call in (adapter.fetch(DocRef(ref="table:BOND.ORDERS"), SCOPE), adapter.search("lệnh", SCOPE)):
        with pytest.raises(ConnectorError) as err:
            await call
        assert err.value.kind == "unconfigured" and str(err.value) == "chưa cài gói oracledb: uv sync --extra oracle"
    health = await adapter.test()
    assert health.state == "unconfigured" and "uv sync --extra oracle" in health.detail
    await adapter.close()


async def test_the_real_driver_is_given_discrete_connect_parameters_and_the_call_timeout(db: FakeOracle, monkeypatch) -> None:
    seen: list[dict[str, Any]] = []

    def connect(**kwargs: Any) -> FakeConnection:
        seen.append(kwargs)
        return db.connect()

    monkeypatch.setitem(sys.modules, "oracledb", types.SimpleNamespace(connect=connect))
    config = {"host": "db.acme.test", "port": 1522, "service_name": "BONDPDB", "schemas": ["BOND"], "statement_timeout_s": 7}
    adapter = registry.build(ConnectionSpec(id="ora", use="oracle_schema", config=config), SECRET)
    assert (await adapter.fetch(DocRef(ref="table:ORDERS"), SCOPE)).title == "Bảng BOND.ORDERS"
    sent = {"user": "app_ro", "password": PASSWORD, "host": "db.acme.test", "port": 1522, "service_name": "BONDPDB", "tcp_connect_timeout": 20.0}
    assert seen == [sent], "no DSN string with credentials is ever built"
    assert db.timeouts == [7000] and db.closed == 1


async def test_missing_secrets_are_a_normal_state(db: FakeOracle) -> None:
    with pytest.raises(ConnectorError) as err:
        build(db, secret={"user": "app_ro"})
    assert err.value.kind == "unconfigured" and "password" in str(err.value)
    bare = OracleSchema(registry.validate(ConnectionSpec(id="ora", use="oracle_schema", config=CONFIG)), {}, transport=db.connect)
    health = await bare.test()
    assert health.state == "unconfigured" and "user, password" in health.detail and db.connects == 0


ERRORS = [
    ("ORA-01017", "ORA-01017: invalid username/password; logon denied", "auth", "sai tên đăng nhập hoặc mật khẩu"),
    ("ORA-28000", "ORA-28000: The account is locked.", "auth", "bị khoá"),
    ("DPY-6005", f"DPY-6005: cannot connect to database (CONNECTION_ID=abc).\n[Errno 111] Connection refused app_ro/{PASSWORD}@db", "network", "không kết nối được tới listener"),
    ("ORA-12541", "ORA-12541: TNS:no listener", "network", "không kết nối được tới listener"),
    ("ORA-12514", "ORA-12514: TNS:listener does not currently know of service requested in connect descriptor", "not_found", "listener không biết service"),
    ("DPY-6001", 'DPY-6001: Service "BONDPDB" is not registered with the listener at host "db.acme.test" port 1521. (Similar to ORA-12514)', "not_found", "listener không biết service"),
    ("DPY-4024", "DPY-4024: call timeout of 30000 ms exceeded", "network", "statement_timeout_s"),
    ("DPY-4011", "DPY-4011: the database or network closed the connection", "network", "bị ngắt"),
    ("ORA-01031", "ORA-01031: insufficient privileges", "forbidden", "không đủ quyền"),
    ("ORA-00904", f'ORA-00904: "X": invalid identifier (password was {PASSWORD})', "invalid", "ORA-00904"),
]


@pytest.mark.parametrize(("code", "text", "kind", "said"), ERRORS)
@pytest.mark.parametrize("where", ["connect", "query"])
@pytest.mark.parametrize("shape", ["error_object", "text_only"])
async def test_driver_errors_become_kinds_and_never_carry_the_password(db: FakeOracle, code: str, text: str, kind: str, said: str, where: str, shape: str) -> None:
    error = DriverError(code, text) if shape == "error_object" else RuntimeError(text)
    setattr(db, f"{where}_error", error)
    adapter = build(db)
    messages: list[str] = []
    for call in (adapter.fetch(DocRef(ref="table:BOND.ORDERS"), SCOPE), adapter.search("lệnh", SCOPE)):
        with pytest.raises(ConnectorError) as err:
            await call
        assert err.value.kind == kind and err.value.__cause__ is None
        messages.append(str(err.value))
    health = await adapter.test()
    assert health.state == "error"
    messages.append(health.detail)
    for message in messages:
        assert said in message and code in message and message.startswith("Oracle db.acme.test:1521/BONDPDB: "), message
        assert PASSWORD not in message and "app_ro" not in message, message
    assert db.closed == (0 if where == "connect" else db.connects), "a connection that was opened is closed"


async def test_a_socket_error_without_a_code_is_a_network_error(db: FakeOracle) -> None:
    db.connect_error = TimeoutError("timed out")
    with pytest.raises(ConnectorError) as err:
        await build(db).fetch(DocRef(ref="schema:BOND"), SCOPE)
    assert err.value.kind == "network" and "listener" in str(err.value)


# ---- health -----------------------------------------------------------------------------------------------------------


async def test_health_says_who_connected_and_what_each_schema_shows(db: FakeOracle) -> None:
    ok = await build(db).test()
    assert (ok.state, ok.identity) == ("ok", "APP_RO") and ok.checked_at
    assert ok.detail == "Đã kết nối Oracle db.acme.test:1521/BONDPDB với tài khoản APP_RO. Đọc được BOND: 3 bảng; REF: 1 bảng."
    assert db.calls == [("SQL_WHOAMI", {}), ("SQL_COUNT_TABLES", {"owner": "BOND"}), ("SQL_COUNT_TABLES", {"owner": "REF"})] and db.closed == 1

    partly = await build(db, {"dsn": "db.acme.test/BONDPDB", "schemas": ["BOND", "FIN"]}).test()
    assert partly.state == "degraded" and "tài khoản không có quyền đọc schema FIN" in partly.detail and "Đọc được BOND: 3 bảng" in partly.detail
    blind = await build(db, {"dsn": "db.acme.test/BONDPDB", "schemas": ["FIN", "OPS"]}).test()
    assert blind.state == "error" and "không có quyền đọc schema FIN" in blind.detail and "không có quyền đọc schema OPS" in blind.detail

    for code, text, said in (
        ("ORA-01017", "ORA-01017: invalid username/password; logon denied", "sai tên đăng nhập hoặc mật khẩu"),
        ("DPY-6005", "DPY-6005: cannot connect to database", "không kết nối được tới listener"),
        ("ORA-12541", "ORA-12541: TNS:no listener", "không kết nối được tới listener"),
        ("ORA-12514", "ORA-12514: TNS:listener does not currently know of service", "listener không biết service"),
    ):
        db.connect_error = DriverError(code, text)
        health = await build(db).test()
        assert health.state == "error" and said in health.detail and code in health.detail and PASSWORD not in health.detail, code


# ---- through the office -----------------------------------------------------------------------------------------------


def tool(name: str):
    return next(t for t in SOURCE_TOOLS if t.name == name).handler


async def test_end_to_end_through_the_office_the_document_is_cached_and_the_read_is_outside_content(office: Office, db: FakeOracle, monkeypatch) -> None:
    real = registry.build
    monkeypatch.setattr(registry, "build", lambda spec, secret, **_: real(spec, secret, transport=db.connect))
    saved = await office.connections.save(ConnectionSpec(id="ora", use="oracle_schema", config={"dsn": "db.acme.test:1521/BONDPDB", "schemas": ["BOND", "REF"]}), secret=SECRET)
    stored = (await office.db.fetchone("SELECT config_json FROM connections WHERE id = 'ora'"))["config_json"]
    assert saved.secret_ref == "ora" and PASSWORD not in stored and "app_ro" not in stored
    ticket = await office.tickets.create("Thêm trạng thái lệnh")

    seen = await office.sourcehub.recognize(ticket, "Bảng table:bond.orders và table:HR.SALARIES")
    assert [(spec.id, ref.connection, ref.ref, ref.kind) for spec, _, ref in seen] == [("ora", "ora", "table:BOND.ORDERS", "oracle_schema"), ("ora", "ora", "table:HR.SALARIES", "oracle_schema")]

    fetched = await office.sourcehub.get(ticket, "table:BOND.ORDERS")
    assert (fetched.connection, fetched.use, fetched.ref.ref, fetched.content.title) == ("ora", "oracle_schema", "table:BOND.ORDERS", "Bảng BOND.ORDERS")
    row = await office.db.fetchone("SELECT version, title, body FROM source_cache WHERE connection_id = 'ora' AND ref = 'table:BOND.ORDERS'")
    assert (row["version"], row["title"]) == (fetched.content.version, "Bảng BOND.ORDERS") and "| STATUS | VARCHAR2(20) |" in row["body"], "kept by version"
    audit = await office.db.fetchone("SELECT action, target, detail_json FROM audit_log WHERE action = 'source:get' ORDER BY id DESC")
    assert audit["target"] == "ora:table:BOND.ORDERS" and json.loads(audit["detail_json"])["version"] == fetched.content.version

    # a changed table is a new version of the same cached row
    db.columns[("BOND", "ORDERS")].append(("NOTE", "VARCHAR2", 400, None, None, 400, "B", "Y", None, None))
    newer = await office.sourcehub.get(ticket, "table:bond.orders")
    row = await office.db.fetchone("SELECT COUNT(*) AS n, MAX(version) AS version FROM source_cache WHERE connection_id = 'ora'")
    assert newer.content.version != fetched.content.version and (row["n"], row["version"]) == (1, newer.content.version)

    found = await office.sourcehub.search(ticket, "lệnh", limit=5)
    assert found == [
        {"connection": "ora", "ref": "table:BOND.ORDERS", "title": "Bảng BOND.ORDERS", "snippet": "Lệnh đặt mua trái phiếu", "url": ""},
        {"connection": "ora", "ref": "view:BOND.V_OPEN_ORDERS", "title": "View BOND.V_OPEN_ORDERS", "snippet": "Lệnh đang mở", "url": ""},
    ]

    # the AI reads it with the same tools as any other source, and those mark the run as having read outside content
    ctx = ToolCtx(office, None, None, "run_1", ticket_id=ticket.id)  # type: ignore[arg-type]
    out = await tool("source_get")({"ref": "proc:BOND.PLACE_ORDER"}, ctx)
    assert out.startswith("# Thủ tục BOND.PLACE_ORDER\nNguồn: proc:BOND.PLACE_ORDER · phiên bản 20261101T080000-sha256:") and "qua kết nối ora" in out
    assert "<document>\n# Thủ tục BOND.PLACE_ORDER" in out and "PROCEDURE place_order" in out
    assert "table:BOND.ORDERS — Bảng BOND.ORDERS" in await tool("source_search")({"query": "lệnh"}, ctx)
    assert all(t.untrusted for t in SOURCE_TOOLS if t.name in ("source_get", "source_search")) and brings_untrusted("mcp__office__source_get")

    before = len(db.calls)
    with pytest.raises(ConnectorError) as outside:
        await office.sourcehub.get(ticket, "table:HR.SALARIES")
    assert outside.value.kind == "forbidden" and "ngoài phạm vi" in str(outside.value)
    with pytest.raises(ConnectorError) as unknown:
        await office.sourcehub.get(ticket, "table:BOND.X' OR '1'='1")
    assert unknown.value.kind == "not_found" and "nhận ra" in str(unknown.value)
    assert len(db.calls) == before, "neither reached the database"

    health = await office.connections.health("ora")
    assert health.state == "ok" and health.identity == "APP_RO"
    assert PASSWORD not in json.dumps(await office.connections.overview(), ensure_ascii=False)
