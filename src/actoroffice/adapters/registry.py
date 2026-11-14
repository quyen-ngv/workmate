"""Which adapters exist and how one is built from a connection.

Built-in adapters and plugins register the same way: a name → a class. Built-ins are named here by import path (loaded
on first use, so a missing optional dependency of one adapter never breaks the others); plugins come from the entry
point group `actoroffice.adapters`.
"""

from __future__ import annotations

import importlib
from importlib import metadata
from typing import Any

from pydantic import BaseModel, ValidationError

from actoroffice.ports.connection import Capability, ConnectionSpec, ConnectorError

BUILTIN = {
    "jira_cloud": "actoroffice.adapters.atlassian.jira_cloud:JiraCloud",
    "confluence_cloud": "actoroffice.adapters.atlassian.confluence_cloud:ConfluenceCloud",
    "bitbucket_cloud": "actoroffice.adapters.bitbucket_cloud:BitbucketCloud",
    "github": "actoroffice.adapters.github:GitHub",
    # generic: an outside system connected by configuration only
    "http": "actoroffice.adapters.generic.http:GenericHttp",
    "mcp": "actoroffice.adapters.generic.mcp:GenericMcp",
    "command": "actoroffice.adapters.generic.command:GenericCommand",
}
ENTRY_POINT_GROUP = "actoroffice.adapters"

_extra: dict[str, type] = {}  # registered at run time (tests, embedding)


def register(name: str, cls: type) -> None:
    _extra[name] = cls


def unregister(name: str) -> None:
    _extra.pop(name, None)


def names() -> list[str]:
    found = set(BUILTIN) | set(_extra)
    found |= {ep.name for ep in metadata.entry_points(group=ENTRY_POINT_GROUP)}
    return sorted(found)


def adapter_class(name: str) -> type:
    if name in _extra:
        return _extra[name]
    for ep in metadata.entry_points(group=ENTRY_POINT_GROUP):
        if ep.name == name:
            return ep.load()
    target = BUILTIN.get(name)
    if target is None:
        raise ConnectorError("invalid", f"không có adapter tên '{name}'. Có: {', '.join(names())}")
    module, _, attr = target.partition(":")
    try:
        return getattr(importlib.import_module(module), attr)
    except (ImportError, AttributeError) as exc:
        raise ConnectorError("invalid", f"adapter '{name}' không nạp được: {exc}") from exc


def describe(name: str) -> dict[str, Any]:
    """What the settings page needs to draw one adapter: title, capabilities, the config form, the secret fields."""
    cls = adapter_class(name)
    schema: type[BaseModel] = cls.config_schema
    return {
        "name": name,
        "title": getattr(cls, "title", name),
        "capabilities": sorted(str(c) for c in cls.capabilities),
        "config_schema": schema.model_json_schema(),
        "secret_fields": list(getattr(cls, "secret_fields", ())),
        "required_scopes": {str(k): v for k, v in getattr(cls, "required_scopes", {}).items()},
    }


def validate(spec: ConnectionSpec) -> BaseModel:
    """The adapter's config model for this connection. Raises ConnectorError("invalid") naming every bad field."""
    cls = adapter_class(spec.use)
    schema: type[BaseModel] = cls.config_schema
    values = dict(spec.config)
    if "write" in schema.model_fields:
        values["write"] = spec.write  # one switch, on the connection
    try:
        return schema.model_validate(values)
    except ValidationError as exc:
        problems = "; ".join(f"{'.'.join(str(p) for p in e['loc']) or 'config'}: {e['msg']}" for e in exc.errors())
        raise ConnectorError("invalid", f"cấu hình của kết nối '{spec.id}' ({spec.use}) không hợp lệ: {problems}") from exc


def build(spec: ConnectionSpec, secret: dict[str, str], *, transport: Any = None) -> Any:
    cls = adapter_class(spec.use)
    config = validate(spec)
    optional = set(getattr(cls, "optional_secret_fields", ())) | {"webhook_secret"}
    missing = [f for f in getattr(cls, "secret_fields", ()) if f not in optional and not secret.get(f)]
    if missing:
        raise ConnectorError("unconfigured", f"kết nối '{spec.id}' thiếu bí mật: {', '.join(missing)}")
    return cls(config, secret, transport=transport) if transport is not None else cls(config, secret)


def capabilities(name: str) -> frozenset[Capability]:
    return frozenset(adapter_class(name).capabilities)
