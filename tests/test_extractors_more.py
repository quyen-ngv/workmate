"""The extractors for descriptor files and for more frameworks: `openapi`, `express` (with NestJS), `jpa`, `deploy`.
Each on small samples: what it finds, what it must leave alone, and one pass of the real indexer over a sample project."""

from __future__ import annotations

from pathlib import Path

import pytest

from actoroffice.codeintel import extractors, parse
from actoroffice.codeintel.extractors_deploy import documents, pointers
from actoroffice.codeintel.index import index_repo
from actoroffice.codeintel.store import IndexStore
from actoroffice.codeintel.sync import FolderSource
from actoroffice.ports.intel import Fact, ParsedFile, RepoFacts

CHART_FILES = ["charts/orders/Chart.yaml", "charts/orders/values.yaml", "charts/orders/templates/deployment.yaml"]
FACTS = RepoFacts(slug="acme/shop", languages=["java", "javascript", "json", "python", "typescript", "yaml"], files=CHART_FILES)


def _run(extractor: object, path: str, text: str) -> list[Fact]:
    return extractor.extract(parse.parse(path, text), FACTS)  # type: ignore[attr-defined]


def _named(facts: list[Fact]) -> set[tuple[str, str, int, str]]:
    return {(f.edge, f.name, f.line, f.confidence) for f in facts}


def test_the_four_are_active_by_default() -> None:
    by_name = {e.name: e for e in extractors.load()}
    assert {"openapi", "express", "jpa", "deploy"} <= set(by_name)
    assert all(by_name[n].applies(FACTS) for n in ("openapi", "express", "jpa", "deploy"))
    python_only = RepoFacts(slug="x", languages=["python", "toml"])
    assert not any(by_name[n].applies(python_only) for n in ("openapi", "express", "jpa", "deploy"))


# ---- reading YAML and JSON ------------------------------------------------------------------------------------------------


def test_documents_keep_lines_and_the_text_of_scalars() -> None:
    text = 'base: &base\n  restart: always\nsvc:\n  <<: *base\n  ports:\n    - 8080:80\n    - "9090:90"\n  version: 2.0\n  empty:\n'
    (doc,) = documents(ParsedFile(path="a.yaml", lang="yaml", text=text))
    svc = doc["svc"]
    assert svc == {"restart": "always", "ports": ["8080:80", "9090:90"], "version": "2.0", "empty": None}, "a scalar is the text that was written"
    assert (doc.lines["svc"], svc.lines["ports"], svc["ports"].lines) == (3, 5, [6, 7])


@pytest.mark.parametrize("text", ["a: [1, 2", "a: !Ref b\n", "key: {{ .Values.x }}\n", "\tx: 1\n", "{{- if .Values.on }}\na: 1\n{{- end }}\n"])
def test_a_file_that_does_not_parse_has_no_documents(text: str) -> None:
    assert documents(ParsedFile(path="a.yaml", lang="yaml", text=text)) == []
    assert documents(ParsedFile(path="a.json", lang="json", text='{"a": ')) == []


# ---- openapi ----------------------------------------------------------------------------------------------------------------

OPENAPI = """openapi: 3.0.3
info: {title: Orders, version: 1.0.0}
servers:
  - url: https://api.example.com/v1
paths:
  /orders:
    get:
      operationId: listOrders
      summary: List orders
    post: {operationId: createOrder}
    parameters: []
  /orders/{orderId}:
    delete: {}
"""


def test_openapi_exposes_every_operation_under_the_server_path() -> None:
    facts = _run(extractors.OpenApi(), "api/openapi.yaml", OPENAPI)
    assert _named(facts) == {("exposes", "GET /v1/orders", 7, "exact"), ("exposes", "POST /v1/orders", 10, "exact"), ("exposes", "DELETE /v1/orders/{}", 13, "exact")}
    assert all(f.node_kind == "endpoint" and f.symbol == "" for f in facts)
    assert facts[0].meta == {"spec": True, "operationId": "listOrders", "summary": "List orders"}


def test_openapi_reads_swagger_2_json() -> None:
    # tabs and the `\/` escape are JSON that YAML does not accept
    text = '{\n\t"swagger": "2.0",\n\t"basePath": "\\/api",\n\t"paths": {\n\t\t"\\/pets\\/{id}": {\n\t\t\t"get": {"operationId": "getPet"},\n\t\t\t"x-internal": true\n\t\t}\n\t}\n}\n'
    assert _named(_run(extractors.OpenApi(), "docs/swagger.json", text)) == {("exposes", "GET /api/pets/{}", 6, "exact")}
    minified = '{"paths":{"/a":{"put":{}}},"openapi":"3.1.0"}'
    assert _named(_run(extractors.OpenApi(), "o.json", minified)) == {("exposes", "PUT /a", 1, "exact")}


def test_openapi_server_variables() -> None:
    spec = "openapi: 3.0.0\nservers:\n  - url: '{scheme}://api.example.com/{base}'\n    variables:\n      scheme: {default: https}\n%spaths:\n  /a:\n    get: {}\n"
    with_default = _run(extractors.OpenApi(), "a.yaml", spec % "      base: {default: v2}\n")
    assert _named(with_default) == {("exposes", "GET /v2/a", 9, "exact")}
    unknown = _run(extractors.OpenApi(), "a.yaml", spec % "")
    assert _named(unknown) == {("exposes", "GET /a", 8, "heuristic")}, "the base path is not known: the endpoint is a guess"
    relative = _run(extractors.OpenApi(), "a.yaml", "openapi: 3.0.0\nservers: [{url: /internal}]\npaths:\n  /a:\n    get: {}\n")
    assert [f.name for f in relative] == ["GET /internal/a"]


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("config/application.yaml", "swagger:\n  enabled: true\npaths:\n  - /a\n"),  # a setting named swagger, not a spec
        ("config/routes.yaml", "paths:\n  /a:\n    get: {}\n"),  # paths without the openapi key
        ("api/broken.yaml", "openapi: 3.0.0\npaths:\n  /a: {get: {\n"),
        ("api/broken.json", '{"openapi": "3.0.0", "paths": {"/a": {"get": {}}'),
        ("api/odd.yaml", "openapi: 3.0.0\npaths: [1, 2]\n"),
        ("api/odd2.yaml", "openapi: 3.0.0\nservers: 7\npaths:\n  a: {get: {}}\n  /b: get\n  /c: {summary: x, parameters: []}\n"),
        ("src/app.py", "openapi = {'paths': {'/a': {'get': {}}}}\n"),
    ],
)
def test_openapi_ignores(path: str, text: str) -> None:
    assert _run(extractors.OpenApi(), path, text) == []


# ---- express, nestjs ------------------------------------------------------------------------------------------------------------

EXPRESS = """const express = require('express');
const app = express();
const router = express.Router();
const admin = express.Router();

app.get('/health', (req, res) => res.send('ok'));
router.get('/users/:id', auth, async (req, res) => {
  res.json(await find(req.params.id)); // don't (
});
router.post("/users", create);
router.route('/orders/:id')
  .get((req, res) => { res.json(load(req.params.id)); })
  .put(update)
  .all(log);
admin.delete('/cache', flush);
app.use('/api', auth, router);
router.use('/admin', admin);
app.set('port', 3000);
const port = app.get('port');
const x = cache.get('/key');
const y = axios.get('/remote', { params });
module.exports = app;
"""


def test_express_routes_chains_and_mounts() -> None:
    facts = _run(extractors.Express(), "src/server.js", EXPRESS)
    assert _named(facts) == {
        ("exposes", "GET /health", 6, "exact"),
        ("exposes", "GET /api/users/{}", 7, "exact"),
        ("exposes", "POST /api/users", 10, "exact"),
        ("exposes", "GET /api/orders/{}", 12, "exact"),
        ("exposes", "PUT /api/orders/{}", 13, "exact"),
        ("exposes", "ANY /api/orders/{}", 14, "exact"),
        ("exposes", "DELETE /api/admin/cache", 15, "exact"),
    }
    assert [f.line for f in facts] == sorted(f.line for f in facts)


def test_express_router_mounted_elsewhere_is_a_guess() -> None:
    src = "import { Router } from 'express';\nexport const usersRouter: Router = Router();\nusersRouter.get('/:id', show);\nusersRouter.patch<Params>('/:id/name', rename);\n"
    assert _named(_run(extractors.Express(), "src/users.ts", src)) == {("exposes", "GET /{}", 3, "heuristic"), ("exposes", "PATCH /{}/name", 4, "heuristic")}
    twice = "const r = express.Router();\nr.get('/x', h);\napp.use('/v1', r);\napp.use('/v2', r);\n"
    assert {f.name for f in _run(extractors.Express(), "a.js", twice)} == {"GET /v1/x", "GET /v2/x"}


def test_express_receiver_passed_in_and_template_path() -> None:
    src = "export function register(app) {\n  app.post(`/v1/things/${kind}`, create);\n  app.get('/v1/things', list);\n}\nclass Api {\n  mount() { this.router.delete('/v1/things/:id', drop); }\n}\n"
    facts = _run(extractors.Express(), "src/register.ts", src)
    assert {(f.name, f.line, f.symbol, f.confidence) for f in facts} == {
        ("POST /v1/things/{}", 2, "src/register.register", "heuristic"),  # the path is put together from a variable
        ("GET /v1/things", 3, "src/register.register", "exact"),
        ("DELETE /v1/things/{}", 6, "src/register.Api.mount", "heuristic"),  # a router: where it is mounted is not in this file
    }


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("src/client.ts", "const user = await api.get('/v1/users', { params });\nconst v = cache.get('/key');\nconst w = map.get('/a', fallback);\n"),
        ("src/settings.js", "const app = express();\napp.set('port', 3000);\nconst port = app.get('port');\napp.use(express.json());\n"),
        ("src/broken.js", "router.route('/x'\n  .get(\nconst r = express.Router(;\napp.use('/api',\n"),
        ("src/test.spec.ts", "await request(app).get('/v1/users').expect(200);\n"),
        ("src/app.py", "app.get('/v1/users', handler)\n"),
    ],
)
def test_express_ignores(path: str, text: str) -> None:
    assert _run(extractors.Express(), path, text) == []


NEST = """import { Controller, Get, Post, Param } from '@nestjs/common';

@Controller('users')
export class UsersController {
  constructor(private readonly svc: UsersService) {}

  @Get(':id')
  findOne(@Param('id') id: string) {
    return this.svc.find(id);
  }

  @Post()
  @HttpCode(201)
  create() {}
}

@Controller({ path: 'orders', version: '1' })
class OrdersController {
  @Get()
  list() {}
}

class NotAController {
  @Get('nope')
  x() {}
}
"""


def test_nest_controllers() -> None:
    facts = _run(extractors.Express(), "src/users.controller.ts", NEST)
    assert {(f.name, f.line, f.symbol, f.confidence) for f in facts} == {
        ("GET /users/{}", 7, "src/users.controller.UsersController.findOne", "exact"),
        ("POST /users", 12, "src/users.controller.UsersController.create", "exact"),
        ("GET /orders", 19, "src/users.controller.OrdersController.list", "exact"),
    }
    assert _run(extractors.Express(), "src/x.ts", "class A {\n  @Get('a')\n  a() {}\n}\n") == [], "no controller, no route"
    assert _run(extractors.Express(), "src/y.ts", "@Controller('a'\nexport class A {\n  @Get('b'\n  b() {}\n}\n") == []


# ---- jpa ----------------------------------------------------------------------------------------------------------------------

REPOSITORY = '''package com.acme.shop;

import org.springframework.data.jpa.repository.JpaRepository;
import org.springframework.data.jpa.repository.Query;

@Repository
public interface OrderItemRepository extends JpaRepository<OrderItem, Long>, JpaSpecificationExecutor<OrderItem> {
    @Query("select i from OrderItem i join fetch i.order o join Product p on p.id = i.productId where o.customer.id = :id")
    List<OrderItem> findByCustomer(Long id);

    @Modifying
    @Query(value = "UPDATE order_item SET qty = 0 WHERE order_id IN (SELECT id FROM orders WHERE status = 'X')", nativeQuery = true)
    int zero();

    @Query("""
        delete from AuditLog a
        where a.at < :before
        """)
    void purge(Instant before);
}
'''
REPOSITORY_PATH = "src/main/java/com/acme/shop/OrderItemRepository.java"


def test_jpa_repository_and_queries() -> None:
    facts = _run(extractors.Jpa(), REPOSITORY_PATH, REPOSITORY)
    repo = "com.acme.shop.OrderItemRepository"
    assert {(f.edge, f.name, f.line, f.symbol, f.confidence) for f in facts} == {
        ("reads", "order_item", 6, repo, "heuristic"),  # the repository itself: the table is the entity name in snake_case
        ("writes", "order_item", 6, repo, "heuristic"),
        ("reads", "order_item", 8, f"{repo}.findByCustomer", "heuristic"),  # JPQL names entities; `join fetch i.order` names none
        ("reads", "product", 8, f"{repo}.findByCustomer", "heuristic"),
        ("reads", "orders", 12, f"{repo}.zero", "exact"),  # native SQL names tables
        ("writes", "order_item", 12, f"{repo}.zero", "exact"),
        ("writes", "audit_log", 15, f"{repo}.purge", "heuristic"),
    }
    assert all(f.node_kind == "table" for f in facts)
    assert {f.meta.get("entity") for f in facts if f.confidence == "heuristic"} == {"OrderItem", "Product", "AuditLog"}


def test_jpa_other_repository_bases_and_names() -> None:
    src = "package a;\ninterface HTTPLogRepository extends CrudRepository<com.acme.model.HTTPLog, java.util.UUID> {}\ninterface P extends PagingAndSortingRepository<User2FA, Long> {}\n"
    assert {(f.edge, f.name) for f in _run(extractors.Jpa(), "A.java", src)} == {("reads", "http_log"), ("writes", "http_log"), ("reads", "user2_fa"), ("writes", "user2_fa")}
    assert extractors.snake_case("OrderItem") == "order_item" and extractors.snake_case("order_item") == "order_item"


@pytest.mark.parametrize(
    "text",
    [
        "package a;\n@NoRepositoryBean\npublic interface BaseRepository<T, ID> extends JpaRepository<T, ID> {}\n",  # a base for repositories: no entity
        "package a;\ninterface Generic<T> extends CrudRepository<T, Long> {}\n",
        "package a;\npublic interface RefundMapper {\n    Refund findByOrder(Long orderId);\n}\nclass RepositoryHelper {}\n",
        'package a;\ninterface R {\n    @Query(Queries.FIND)\n    List<A> a();\n    @QueryHints({})\n    List<A> b();\n    @Query("select 1"\n    List<A> c();\n}\n',
        "package a;\ninterface R extends JpaRepository<\n",
    ],
)
def test_jpa_ignores(text: str) -> None:
    assert _run(extractors.Jpa(), "src/main/java/a/R.java", text) == []
    assert _run(extractors.Jpa(), "src/r.ts", "interface R extends JpaRepository<Order, number> {}\n") == []


# ---- deploy -------------------------------------------------------------------------------------------------------------------

K8S = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: orders
spec:
  template:
    spec:
      containers:
        - name: orders
          image: acme/orders:1.2
          ports:
            - containerPort: 8080
          env:
            - name: PAYMENTS_URL
              value: http://payments.prod.svc.cluster.local:8080/v1
            - name: SPRING_DATASOURCE_URL
              value: jdbc:postgresql://app:s3cret@orders-db:5432/orders
            - name: KAFKA_BROKERS
              value: kafka-0:9092,kafka-1:9092
            - name: SELF_URL
              value: http://orders:8080
            - name: LOG_LEVEL
              value: debug
            - name: TOKEN
              valueFrom: {secretKeyRef: {name: s, key: t}}
            - name: LOCAL_URL
              value: http://localhost:9000
            - name: PEER_URL
              value: http://$(PEER_HOST):8080
---
apiVersion: v1
kind: Service
metadata: {name: orders}
spec:
  ports:
    - port: 80
      targetPort: 8080
---
apiVersion: networking.k8s.io/v1
kind: Ingress
metadata: {name: public}
spec:
  rules:
    - host: api.example.com
      http:
        paths:
          - path: /orders
            backend: {service: {name: orders, port: {number: 80}}}
---
apiVersion: v1
kind: ConfigMap
metadata: {name: orders-config}
data:
  LEDGER_BASE_URL: https://ledger.internal
  application.yml: |
    client: http://not-a-setting:1
"""


def test_deploy_kubernetes_manifests() -> None:
    facts = _run(extractors.Deploy(), "deploy/k8s/orders.yaml", K8S)
    assert {(f.node_kind, f.edge, f.name, f.line, f.confidence) for f in facts} == {
        ("service", "contains", "orders", 4, "exact"),  # the Deployment
        ("external", "calls_http", "payments", 14, "heuristic"),  # the cluster DNS name is cut to the service
        ("external", "depends_on", "orders-db", 16, "heuristic"),
        ("external", "depends_on", "kafka-0", 18, "heuristic"),
        ("external", "depends_on", "kafka-1", 18, "heuristic"),
        ("service", "contains", "orders", 33, "exact"),  # the Service
        ("service", "contains", "orders", 47, "exact"),  # the Ingress route to it
        ("external", "calls_http", "ledger.internal", 54, "heuristic"),  # the ConfigMap
    }
    by_line = {f.line: f.meta for f in facts}
    assert by_line[4] == {"kind": "Deployment", "image": "acme/orders:1.2", "ports": ["8080"], "service": "orders"}
    assert by_line[14] == {"service": "orders", "key": "PAYMENTS_URL", "scheme": "http", "port": "8080"}
    assert by_line[16] == {"service": "orders", "key": "SPRING_DATASOURCE_URL", "scheme": "postgresql", "port": "5432"}, "no user, password or path"
    assert by_line[33] == {"kind": "Service", "ports": ["80"], "service": "orders"}
    assert by_line[47] == {"kind": "Ingress", "ingress": "public", "host": "api.example.com", "path": "/orders", "service": "orders"}
    assert by_line[54] == {"configmap": "orders-config", "key": "LEDGER_BASE_URL", "scheme": "https"}


COMPOSE = """version: "3.8"
x-common: &common
  restart: always
  environment:
    AUDIT_URL: http://audit:8080
services:
  api:
    <<: *common
    image: acme/api
    ports: ["8080:80"]
    depends_on:
      - db
      - orders
    environment:
      - ORDERS_URL=${ORDERS_URL:-http://orders:8080}
      - DB_HOST=db
      - DEBUG=true
  orders:
    <<: *common
    build: ./orders
    depends_on:
      db: {condition: service_healthy}
  db:
    image: postgres:16
"""


def test_deploy_docker_compose() -> None:
    facts = _run(extractors.Deploy(), "docker-compose.yml", COMPOSE)
    assert {(f.node_kind, f.edge, f.name, f.line, f.confidence, f.meta.get("service")) for f in facts} == {
        ("service", "contains", "api", 7, "exact", "api"),
        ("external", "depends_on", "db", 12, "exact", "api"),
        ("external", "depends_on", "orders", 13, "exact", "api"),
        ("external", "calls_http", "orders", 15, "heuristic", "api"),  # the default of `${ORDERS_URL:-…}`
        ("external", "depends_on", "db", 16, "heuristic", "api"),
        ("service", "contains", "orders", 18, "exact", "orders"),
        ("external", "depends_on", "db", 22, "exact", "orders"),
        ("external", "calls_http", "audit", 5, "heuristic", "orders"),  # merged from `x-common`; `api` replaces that environment
        ("service", "contains", "db", 23, "exact", "db"),
    }
    assert next(f.meta for f in facts if f.name == "api" and f.node_kind == "service") == {"kind": "compose", "image": "acme/api", "ports": ["8080:80"], "service": "api"}
    assert {f.name for f in _run(extractors.Deploy(), "deploy/compose.prod.yaml", "services:\n  web:\n    depends_on: [api]\n")} == {"web", "api"}
    assert [f.name for f in _run(extractors.Deploy(), "docker-compose.yml", "services: &all\n  loop: *all\n")] == ["loop"], "a YAML alias to itself does not hang"


VALUES = """replicaCount: 2
image: {repository: acme/orders, tag: "1.2"}
ingress:
  hosts:
    - host: orders.example.com
env:
  - name: PAYMENTS_URL
    value: http://payments:8080
config:
  ledgerUrl: https://ledger.internal/v2
  database:
    host: orders-db
    port: 5432
  featureServer: enabled
"""


def test_deploy_helm_values() -> None:
    facts = _run(extractors.Deploy(), "charts/orders/values.yaml", VALUES)
    assert {(f.edge, f.name, f.line, f.confidence) for f in facts} == {
        ("calls_http", "payments", 8, "heuristic"),
        ("calls_http", "ledger.internal", 10, "heuristic"),
        ("depends_on", "orders-db", 12, "heuristic"),
    }
    assert all(f.node_kind == "external" and f.meta["chart"] == "orders" for f in facts)
    assert _run(extractors.Deploy(), "config/values.yaml", VALUES) == [], "a values.yaml that is not next to a Chart.yaml is not Helm"
    looped = "a: &a\n  b: *a\n  url: http://x:1\n"
    assert [f.name for f in _run(extractors.Deploy(), "charts/orders/values.yaml", looped)] == ["x"], "a YAML alias to itself is walked once"


def test_deploy_helm_template_that_parses_keeps_what_is_literal() -> None:
    text = (
        'apiVersion: apps/v1\nkind: Deployment\nmetadata:\n  name: "{{ .Release.Name }}-api"\nspec:\n  template:\n    spec:\n      containers:\n        - env:\n'
        '            - {name: LEDGER_URL, value: "http://ledger:8080"}\n            - {name: PEER_URL, value: "http://{{ .Values.peer }}:80"}\n'
    )
    facts = _run(extractors.Deploy(), "charts/orders/templates/api.yaml", text)
    assert [(f.node_kind, f.edge, f.name, f.line, f.meta) for f in facts] == [("external", "calls_http", "ledger", 10, {"chart": "orders", "key": "LEDGER_URL", "scheme": "http", "port": "8080"})], (
        "the templated name gives no service node and the templated host no edge"
    )


HELM_TEMPLATE = """apiVersion: apps/v1
kind: Deployment
metadata:
  name: {{ include "orders.fullname" . }}
spec:
  {{- if .Values.replicas }}
  replicas: {{ .Values.replicas }}
  {{- end }}
  template:
    spec:
      containers:
        - env:
            - name: PAYMENTS_URL
              value: http://payments:8080
"""


@pytest.mark.parametrize(
    ("path", "text"),
    [
        ("charts/orders/templates/deployment.yaml", HELM_TEMPLATE),  # `{{ }}` makes it not YAML: skipped, not an error
        (".github/workflows/ci.yml", "name: ci\non: push\njobs:\n  test:\n    services:\n      db: {image: postgres}\n    env:\n      API_URL: http://api:8080\n"),
        ("config/application.yaml", "server:\n  port: 8080\nclients:\n  ledger:\n    base-url: http://ledger:8080\n"),  # app config: not a deployment descriptor
        ("deploy/broken.yaml", "apiVersion: v1\nkind: ConfigMap\ndata:\n  A_URL: [http://a:1\n"),
        ("docker-compose.yml", "services:\n  - not\n  - a mapping\n"),
        ("docker-compose.yml", "services:\n  api:\n    depends_on: 7\n    environment: [3, {a: b}]\n    ports: {x: y}\n"),
        ("deploy/odd.yaml", "apiVersion: v1\nkind: Deployment\nmetadata: 3\nspec:\n  template:\n    spec:\n      containers: [1, {env: [2, {name: 4}, {name: X_URL}]}]\n"),
        ("deploy/k8s.json", '{"apiVersion": "v1", "kind": "ConfigMap", "data": {"A_URL": "http://a:1"}}'),  # only YAML descriptors are read
    ],
)
def test_deploy_ignores(path: str, text: str) -> None:
    facts = _run(extractors.Deploy(), path, text)
    assert [f for f in facts if f.node_kind == "external"] == []
    assert [f.name for f in facts if f.node_kind == "service"] in ([], ["api"])


@pytest.mark.parametrize(
    ("key", "value", "found"),
    [
        ("ORDERS_URL", "http://orders:8080/v1?x=1", [("calls_http", "orders", {"scheme": "http", "port": "8080"})]),
        ("ANY_NAME", "https://user:pw@api.vendor.com/hook", [("calls_http", "api.vendor.com", {"scheme": "https"})]),
        ("AMQP", "amqp://rabbit.mq.svc:5672", [("depends_on", "rabbit", {"scheme": "amqp", "port": "5672"})]),
        ("REDIS_HOST", "cache", [("depends_on", "cache", {})]),
        ("kafkaBrokers", "k1:9092, k2:9092", [("depends_on", "k1", {"port": "9092"}), ("depends_on", "k2", {"port": "9092"})]),
        ("LOG_LEVEL", "debug", []),  # not a key that names a host
        ("SMTP_HOST", "true", []),
        ("LISTEN_ADDR", "0.0.0.0:8080", []),
        ("SELF", "http://localhost:3000", []),
        ("PEER_URL", "http://${PEER}:80", []),
        ("PORT", 8080, []),
        ("NOTES_URL", "see\nhttp://a:1", []),
    ],
)
def test_pointers(key: str, value: object, found: list[tuple[str, str, dict[str, str]]]) -> None:
    assert pointers(key, value) == found


# ---- malformed input, and the indexer ---------------------------------------------------------------------------------------------

GARBAGE = ["", "\x00\x01", "{", "}{", "a: b: c", "- [", '"""', "@Query(", "@Controller(", "app.use(", "router.route('/x'", "kind: Deployment\napiVersion: [", "&a [*a]"]


@pytest.mark.parametrize("extractor", [extractors.OpenApi(), extractors.Express(), extractors.Jpa(), extractors.Deploy()], ids=lambda e: e.name)
def test_malformed_input_gives_no_facts_and_no_error(extractor: object) -> None:
    for path in ("a.yaml", "a.json", "docker-compose.yml", "charts/orders/values.yaml", "A.java", "a.js", "a.ts"):
        for text in GARBAGE:
            lang = parse.language_of(path)
            assert extractor.extract(ParsedFile(path=path, lang=lang, text=text), FACTS) == [], (path, text)  # type: ignore[attr-defined]
            assert _run(extractor, path, text) == [], (path, text)


PROJECT = {
    "api/openapi.yaml": OPENAPI,
    "src/server.js": EXPRESS,
    "src/users.controller.ts": NEST,
    REPOSITORY_PATH: REPOSITORY,
    "deploy/k8s/orders.yaml": K8S,
    "docker-compose.yml": COMPOSE,
    "charts/orders/Chart.yaml": "apiVersion: v2\nname: orders\nversion: 0.1.0\n",
    "charts/orders/values.yaml": VALUES,
    "charts/orders/templates/deployment.yaml": HELM_TEMPLATE,
    "config/application.yaml": "swagger:\n  enabled: true\npaths:\n  - /a\n",
}


def test_the_indexer_stores_the_facts_with_their_evidence_lines(tmp_path: Path) -> None:
    root = tmp_path / "shop"
    for path, text in PROJECT.items():
        (root / path).parent.mkdir(parents=True, exist_ok=True)
        (root / path).write_text(text, encoding="utf-8")
    store = IndexStore(tmp_path / "index.db")
    try:
        state = index_repo(store, "shop", FolderSource(root))
        assert state.state == "ready" and state.level == "graph", state
        assert '"errors": []' in store.one("SELECT stats_json FROM repos")[0], "a file an extractor cannot read is skipped, not an error"
        rows = store.all(
            "SELECT e.source, f.path, e.line, e.kind, e.confidence, s.qualified AS src, d.kind AS node_kind, d.name FROM edges e "
            "JOIN nodes s ON s.id = e.src JOIN nodes d ON d.id = e.dst JOIN files f ON f.id = e.file_id WHERE e.source IN ('openapi', 'express', 'jpa', 'deploy')"
        )
    finally:
        store.close()
    found = {(r["source"], r["path"], r["line"], r["kind"], r["node_kind"], r["name"], r["confidence"]) for r in rows}
    assert {
        ("openapi", "api/openapi.yaml", 7, "exposes", "endpoint", "GET /v1/orders", "exact"),
        ("openapi", "api/openapi.yaml", 13, "exposes", "endpoint", "DELETE /v1/orders/{}", "exact"),
        ("express", "src/server.js", 7, "exposes", "endpoint", "GET /api/users/{}", "exact"),
        ("express", "src/server.js", 13, "exposes", "endpoint", "PUT /api/orders/{}", "exact"),
        ("express", "src/users.controller.ts", 7, "exposes", "endpoint", "GET /users/{}", "exact"),
        ("jpa", REPOSITORY_PATH, 6, "writes", "table", "order_item", "heuristic"),
        ("jpa", REPOSITORY_PATH, 12, "reads", "table", "orders", "exact"),
        ("deploy", "deploy/k8s/orders.yaml", 4, "contains", "service", "orders", "exact"),
        ("deploy", "deploy/k8s/orders.yaml", 14, "calls_http", "external", "payments", "heuristic"),
        ("deploy", "docker-compose.yml", 12, "depends_on", "external", "db", "exact"),
        ("deploy", "docker-compose.yml", 15, "calls_http", "external", "orders", "heuristic"),
        ("deploy", "charts/orders/values.yaml", 12, "depends_on", "external", "orders-db", "heuristic"),
    } <= found
    assert not {r for r in found if r[1] in ("charts/orders/templates/deployment.yaml", "config/application.yaml", "charts/orders/Chart.yaml")}
    by_source = {(r["source"], r["path"], r["line"]): r["src"] for r in rows}
    assert by_source[("jpa", REPOSITORY_PATH, 12)] == "com.acme.shop.OrderItemRepository.zero", "a fact inside a method belongs to the method"
    assert by_source[("express", "src/users.controller.ts", 7)] == "src/users.controller.UsersController.findOne"
    assert by_source[("openapi", "api/openapi.yaml", 7)] == "api/openapi.yaml", "a fact of a descriptor belongs to the file"
