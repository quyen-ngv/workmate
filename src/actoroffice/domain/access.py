"""Shared resources (services, databases, brokers, log stores) and time-limited grants for a ticket.

Levels, from least to most: none < read < write-sandboxed < write-shared. Production is never a resource.
"""

from __future__ import annotations

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field


class AccessLevel(StrEnum):
    READ = "read"
    WRITE_SANDBOXED = "write-sandboxed"
    WRITE_SHARED = "write-shared"


LEVEL_ORDER = {AccessLevel.READ: 1, AccessLevel.WRITE_SANDBOXED: 2, AccessLevel.WRITE_SHARED: 3}
LEVEL_VI = {AccessLevel.READ: "chỉ đọc", AccessLevel.WRITE_SANDBOXED: "ghi trong vùng riêng", AccessLevel.WRITE_SHARED: "ghi vào chung"}


class ResourceKind(StrEnum):
    HTTP = "http"  # a REST/HTTP service: the run gets a local proxy URL that adds the credentials
    DB = "db"  # a database: tools (db_query…) and, when granted, a connection string in the run's env
    KAFKA = "kafka"
    MQ = "mq"  # RabbitMQ (management API)
    REDIS = "redis"
    LOGS = "logs"  # Elasticsearch / OpenSearch / Loki / files


class Resource(BaseModel):
    id: str
    team_id: str | None = None
    project_id: str | None = None
    kind: ResourceKind
    name: str  # handle the AI uses, e.g. "payments-api", "orders-db"
    env: str = "dev"  # dev | staging | local (never prod)
    config: dict[str, Any] = Field(default_factory=dict)  # base_url, headers_secret, allow_paths, dsn_secret…
    max_level: AccessLevel = AccessLevel.READ
    created_at: str = ""

    @property
    def env_name(self) -> str:
        return "AO_" + "".join(c if c.isalnum() else "_" for c in self.name.upper())


class Grant(BaseModel):
    id: str
    ticket_id: int
    resource_id: str
    level: AccessLevel
    reason: str = ""
    granted_by: str | None = None
    created_at: str = ""
    expires_at: str
    revoked_at: str | None = None
    envelope: dict[str, Any] = Field(default_factory=dict)  # e.g. {"port": 21007, "url": "http://127.0.0.1:21007"}
