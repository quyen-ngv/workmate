"""Descriptor files as graph facts: the YAML / JSON reading that keeps source lines, and the `deploy` extractor
(Kubernetes manifests, Helm values, docker-compose).

A descriptor says which services exist and which other hosts they are configured to reach. That a value such as
`ORDERS_URL: http://orders:8080` means "this service calls orders" is a guess, so those facts are `heuristic`; a
compose `depends_on` names the other service outright and is `exact`.
"""

from __future__ import annotations

import json
import re
from collections.abc import Iterator
from typing import Any

import yaml

from actoroffice.ports.intel import Fact, ParsedFile, RepoFacts


class Lined(dict[Any, Any]):
    """A YAML mapping that knows the line it starts on and the line of each key."""

    line: int = 1
    lines: dict[str, int]


class LinedList(list[Any]):
    """A YAML sequence that knows the line of each item."""

    lines: list[int]


class _Loader(getattr(yaml, "CSafeLoader", yaml.SafeLoader)):  # type: ignore[misc]
    """A safe loader that keeps source lines and reads every scalar as the text that was written: `8080:80` stays a
    port mapping (YAML 1.1 reads it as a base-60 number) and `2.0` stays a version."""


def _mapping(loader: Any, node: Any) -> Iterator[Lined]:
    data = Lined()
    data.line = node.start_mark.line + 1
    data.lines = {}
    yield data
    loader.flatten_mapping(node)  # `<<: *defaults`
    data.lines.update({key.value: key.start_mark.line + 1 for key, _ in node.value if isinstance(key, yaml.ScalarNode)})
    data.update(loader.construct_mapping(node))


def _sequence(loader: Any, node: Any) -> Iterator[LinedList]:
    data = LinedList()
    data.lines = [item.start_mark.line + 1 for item in node.value]
    yield data
    data.extend(loader.construct_sequence(node))


_Loader.add_constructor("tag:yaml.org,2002:map", _mapping)
_Loader.add_constructor("tag:yaml.org,2002:seq", _sequence)
for _tag in ("int", "float", "bool", "timestamp"):
    _Loader.add_constructor(f"tag:yaml.org,2002:{_tag}", lambda loader, node: node.value)


def documents(file: ParsedFile) -> list[Any]:
    """The documents of a YAML or JSON file: mappings are `Lined`, sequences `LinedList`, scalars `str` (or None).
    A file that does not parse (a Helm template with `{{ }}`, a broken file) has no documents."""
    text = file.text
    if file.lang == "json":  # JSON is YAML but for tabs and the `\/` escape
        text = re.sub(r"\\(.)", lambda m: "/" if m.group(1) == "/" else m.group(0), text.replace("\t", " "))
    try:
        return list(yaml.load_all(text, Loader=_Loader))  # _Loader is a safe loader
    except Exception:  # malformed input gives no facts, whatever the parser raised (YAMLError, RecursionError…)
        pass
    if file.lang == "json":
        try:
            return [json.loads(file.text)]  # no line numbers: facts point at line 1
        except (ValueError, RecursionError):
            pass
    return []


def key_line(mapping: Any, key: Any) -> int:
    """The line a key of a mapping was written on (the line of the mapping when that is not known)."""
    return getattr(mapping, "lines", {}).get(key) or getattr(mapping, "line", 1)


def items(sequence: Any) -> list[tuple[int, Any]]:
    """(line, item) of a sequence; nothing for any other value."""
    if not isinstance(sequence, list):
        return []
    lines = getattr(sequence, "lines", [])
    return [(lines[i] if i < len(lines) else 1, item) for i, item in enumerate(sequence)]


def dig(data: Any, *keys: str) -> Any:
    """`data[k1][k2]…` through mappings; None as soon as a step is missing or is not a mapping."""
    for key in keys:
        if not isinstance(data, dict):
            return None
        data = data.get(key)
    return data


def _text(value: Any) -> str:
    return str(value) if isinstance(value, (str, int, float)) and not isinstance(value, bool) else ""


URL = re.compile(r"^(?:jdbc:)?([a-z][a-z0-9+.-]*)://(?:[^@/\s]*@)?(\[[^\]]+\]|[^/:?#\s,;]+)(?::(\d+))?", re.I)
HOST_PORT = re.compile(r"^([a-z0-9](?:[a-z0-9.-]*[a-z0-9])?)(?::(\d+))?$", re.I)
HOST_KEY = re.compile(r"(?i)(host|hostname|addr|address|brokers?|servers?|endpoint|ur[li])$")
K8S_DNS = re.compile(r"^([a-z0-9-]+)\.[a-z0-9-]+\.svc(\.cluster\.local)?$")  # name.namespace.svc[.cluster.local]
LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "host.docker.internal"}
NOT_A_HOST = {"true", "false", "null", "none", "yes", "no", "on", "off", "enabled", "disabled"}
VALUES_SKIP = {"ingress", "image", "annotations", "labels", "podAnnotations", "podLabels"}  # the chart's own names, not what it reaches


def pointers(key: str, value: Any) -> list[tuple[str, str, dict[str, str]]]:
    """(edge, host, meta) for each host a config value points to: a URL (`http://orders:8080/v1`,
    `jdbc:postgresql://db:5432/app`), or a bare `host[:port]` under a key named like one (`DB_HOST`, `kafkaBrokers`).
    HTTP URLs are `calls_http`, the rest `depends_on`. Userinfo, path and query of the URL are not kept."""
    if not isinstance(value, str) or "\n" in value:
        return []
    default = re.match(r"^\$\{\w+:?-(.+)\}$", value.strip())  # compose: `${ORDERS_URL:-http://orders:8080}`
    found: list[tuple[str, str, dict[str, str]]] = []
    for part in re.split(r"\s*,\s*", (default.group(1) if default else value).strip()):
        m = URL.match(part)
        if m:
            scheme, host, port = m.group(1).lower(), m.group(2), m.group(3) or ""
        else:
            m = HOST_PORT.match(part) if HOST_KEY.search(key) and part.lower() not in NOT_A_HOST and not part.isdigit() else None
            if not m:
                continue
            scheme, host, port = "", m.group(1), m.group(2) or ""
        host = host.strip("[]").lower().rstrip(".")
        host = K8S_DNS.sub(r"\1", host)
        if host in LOCAL_HOSTS or re.search(r"[${}()<>%]", host):  # itself, or a name only known at deploy time
            continue
        meta = {k: v for k, v in (("scheme", scheme), ("port", port)) if v}
        found.append(("calls_http" if scheme in ("http", "https") else "depends_on", host, meta))
    return found


class Deploy:
    """Deployment descriptors.

    Kubernetes manifest (also a Helm template, when it parses as plain YAML): a Deployment / StatefulSet / DaemonSet
    and a Service give a `service` node; an Ingress gives the `service` it routes to (host and path in `meta`); the
    `env` of the containers and the `data` of a ConfigMap give edges to the hosts they point to.
    docker-compose: every entry of `services` is a `service` node; `depends_on` and `environment` give edges.
    Helm `values.yaml` (next to a Chart.yaml): every value that points to a host gives an edge.

    Edges go to an `external` node named by the host; `meta.service` is the service the descriptor describes, when
    it names one (`meta.chart`: the Helm chart the file belongs to)."""

    name = "deploy"
    COMPOSE = re.compile(r"(?i)^(docker-)?compose([.\-][\w.\-]+)?\.ya?ml$")
    VALUES = re.compile(r"(?i)^values([.\-][\w.\-]+)?\.ya?ml$")

    def applies(self, repo: RepoFacts) -> bool:
        return "yaml" in repo.languages

    def extract(self, file: ParsedFile, repo: RepoFacts) -> list[Fact]:
        if file.lang != "yaml":
            return []
        base = file.path.rsplit("/", 1)[-1]
        if self.COMPOSE.match(base):
            return [f for doc in documents(file) for f in self._compose(doc)] if "services" in file.text else []
        chart = self._chart(file.path, repo)
        if chart and self.VALUES.match(base):
            seen: set[int] = set()
            return [f for doc in documents(file) for f in self._values(doc, {"chart": chart}, seen)]
        if "apiVersion" not in file.text or "kind" not in file.text:
            return []
        facts: list[Fact] = []
        for doc in documents(file):
            listed = [item for _, item in items(doc.get("items"))] if isinstance(doc, dict) and doc.get("kind") == "List" else [doc]
            for manifest in listed:
                facts += self._manifest(manifest, chart)
        return facts

    @staticmethod
    def _chart(path: str, repo: RepoFacts) -> str:
        """The Helm chart a file belongs to: the nearest folder above it that holds a Chart.yaml ("" = none)."""
        parts = path.split("/")[:-1]
        for depth in range(len(parts), -1, -1):
            if "/".join([*parts[:depth], "Chart.yaml"]) in repo.files:
                return parts[depth - 1] if depth else repo.slug.rsplit("/", 1)[-1]
        return ""

    @staticmethod
    def _edges(key: Any, value: Any, line: int, own: dict[str, str]) -> list[Fact]:
        return [
            Fact(node_kind="external", name=host, edge=edge, line=line, confidence="heuristic", meta={**own, "key": str(key), **more})
            for edge, host, more in pointers(str(key), value)
            if host != own.get("service")
        ]

    def _manifest(self, doc: Any, chart: str) -> list[Fact]:
        if not isinstance(doc, dict) or "apiVersion" not in doc or not isinstance(doc.get("kind"), str):
            return []
        kind = doc["kind"]
        name = dig(doc, "metadata", "name")
        name = name if isinstance(name, str) and "{" not in name else ""
        line = key_line(doc.get("metadata"), "name") if name else key_line(doc, "kind")
        own = {k: v for k, v in (("service", name), ("chart", chart)) if v}
        facts: list[Fact] = []
        pod = dig(doc, "spec", "template", "spec")
        if isinstance(pod, dict) and isinstance(pod.get("containers"), list):
            containers = [c for _, c in items(pod["containers"]) if isinstance(c, dict)]
            if name:
                image = next((_text(c.get("image")) for c in containers if _text(c.get("image"))), "")
                ports = [_text(p.get("containerPort")) for c in containers for _, p in items(c.get("ports")) if isinstance(p, dict)]
                facts.append(Fact(node_kind="service", name=name, edge="contains", line=line, meta={"kind": kind, "image": image, "ports": ports, **own}))
            for c in containers:
                for env_line, env in items(c.get("env")):
                    if isinstance(env, dict) and isinstance(env.get("name"), str):
                        facts += self._edges(env["name"], env.get("value"), env_line, own)
        elif kind == "Service" and name:
            ports = [_text(p.get("port")) for _, p in items(dig(doc, "spec", "ports")) if isinstance(p, dict)]
            facts.append(Fact(node_kind="service", name=name, edge="contains", line=line, meta={"kind": kind, "ports": ports, **own}))
        elif kind == "Ingress":
            for _, rule in items(dig(doc, "spec", "rules")):
                for path_line, route in items(dig(rule, "http", "paths")):
                    backend = dig(route, "backend", "service", "name") or dig(route, "backend", "serviceName")
                    if isinstance(backend, str) and "{" not in backend:
                        route_meta = {"kind": kind, "ingress": name, "host": _text(dig(rule, "host")), "path": _text(dig(route, "path")), "service": backend}
                        facts.append(Fact(node_kind="service", name=backend, edge="contains", line=path_line, meta=route_meta))
        elif kind == "ConfigMap" and isinstance(doc.get("data"), dict):
            own = {k: v for k, v in (("configmap", name), ("chart", chart)) if v}
            for key, value in doc["data"].items():
                facts += self._edges(key, value, key_line(doc["data"], key), own)
        return facts

    def _compose(self, doc: Any) -> list[Fact]:
        services = doc.get("services") if isinstance(doc, dict) else None
        if not isinstance(services, dict):
            return []
        facts: list[Fact] = []
        for name, svc in services.items():
            if not isinstance(name, str) or not isinstance(svc, dict):
                continue
            own = {"service": name}
            ports = [_text(p.get("target")) if isinstance(p, dict) else _text(p) for _, p in items(svc.get("ports"))]
            meta = {"kind": "compose", "image": _text(svc.get("image")), "ports": ports, **own}
            facts.append(Fact(node_kind="service", name=name, edge="contains", line=key_line(services, name), meta=meta))
            needs = svc.get("depends_on")
            named = [(key_line(needs, dep), dep) for dep in needs] if isinstance(needs, dict) else items(needs)
            for dep_line, dep in named:
                if isinstance(dep, str) and dep != name:
                    facts.append(Fact(node_kind="external", name=dep, edge="depends_on", line=dep_line, meta=own))
            env = svc.get("environment")
            if isinstance(env, dict):
                pairs = [(key_line(env, key), key, value) for key, value in env.items()]
            else:  # the list form: `- ORDERS_URL=http://orders:8080`
                pairs = [(env_line, *item.partition("=")[::2]) for env_line, item in items(env) if isinstance(item, str)]
            for env_line, key, value in pairs:
                facts += self._edges(key, value, env_line, own)
        return facts

    def _values(self, node: Any, own: dict[str, str], seen: set[int]) -> list[Fact]:
        """Every `key: value` and every `{name: …, value: …}` entry of a Helm values tree. `seen` keeps a YAML alias
        from being walked twice (or for ever)."""
        if not isinstance(node, (dict, list)) or id(node) in seen:
            return []
        seen.add(id(node))
        facts: list[Fact] = []
        if isinstance(node, list):
            for _, item in items(node):
                facts += self._values(item, own, seen)
        elif isinstance(node.get("name"), str) and isinstance(node.get("value"), str):  # an env entry
            facts += self._edges(node["name"], node["value"], key_line(node, "value"), own)
        else:
            for key, value in node.items():
                if key not in VALUES_SKIP:
                    facts += self._edges(key, value, key_line(node, key), own) if isinstance(value, str) else self._values(value, own, seen)
        return facts
