"""Settings: ~/.actoroffice/config.yaml (override the home with ACTOROFFICE_HOME)."""

from __future__ import annotations

import os
from pathlib import Path

import yaml
from pydantic import BaseModel


class Settings(BaseModel):
    data_dir: Path
    key_prefix: str = "AO"
    language: str = "Vietnamese"  # language actors use with the user
    max_concurrent_runs: int = 2  # ~0.4 GB RAM per run
    approval_wait_s: float = 90  # live wait before a tool call is parked for later
    default_model: str = "sonnet"
    meta_model: str = "haiku"
    claude_cli_path: str | None = None  # None = CLI bundled with the SDK
    kiro_cli_path: str | None = None  # None = kiro-cli on PATH (actors whose model is "kiro" or "kiro:<model>")
    kiro_engine: str = "v3"  # Kiro CLI ACP engine: "v3" (current) or "v2"
    corrective_turns: int = 1  # extra turns to make an actor set a final ticket status
    # Waits before re-running a turn that failed for a passing reason (start-up, network/API, stall): one per retry.
    transient_retry_delays_s: list[float] = [30, 120, 300]
    command_timeout_s: float = 900  # hard limit of one AI shell command; a project overrides it in its profile
    max_unattended_agent_messages: int = 8  # actor↔actor chat messages in a row before pausing for the user
    # A PEM file with the certificate authorities to trust for every outgoing connection (a company network that
    # inspects TLS). None = the operating system's certificate store. Verification is never switched off.
    ca_bundle: str | None = None
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    # Team copilot
    ai_name: str = "Copilot"  # the single AI engineer every team works with
    ai_model: str | None = None  # None = default_model; "kiro" / "kiro:<id>" runs it on Kiro CLI
    ai_effort: str | None = None  # low | medium | high | xhigh | max (how hard the model thinks); None = its default
    jira_poll_s: float = 60  # Jira mirror interval in seconds (0 = off)
    stall_after_s: float = 900  # watchdog: a run with no event for this long is stalled (stages not listed below)
    stall_after_s_by_stage: dict[str, float] = {"understand": 600, "build": 900}
    slow_command_s: float = 300  # watchdog: one command running longer than this is reported as slow
    # The standard pack of documents (Doc Core): a folder with pack.yaml, or a name under <data_dir>/packs/. None =
    # the built-in pack. A unit's pack usually says `extends: default` and changes only what differs.
    doc_pack: str | None = None
    doc_sweep_s: float = 600  # how often approved documents are compared with the code they cite (0 = off)
    # The documents repository (services/docrepo.py): every document, ticket card and knowledge item as a Markdown file
    # in a git folder, kept in step both ways. None = off. A relative path is under data_dir.
    docs_repo: str | None = None
    docs_branch: str = "main"
    docs_remote: str | None = None  # a git URL or `KEY/repo`; None = the folder's own `origin` remote, if any
    docs_connection: str | None = None  # the code-host connection whose credential pushes and fetches `docs_remote`
    docs_scan_s: float = 5  # how often the folder is looked over for files changed outside the office (0 = only before a commit)
    docs_commit_s: float = 20  # quiet seconds after the last change before a commit (an approval commits at once)
    docs_pull_s: float = 120  # how often the remote is fetched (0 = only when a code host announces a push)
    index_sweep_s: float = 300  # how often the code index is brought up to the main branches in the background (0 = off)
    code_api_connections: list[str] = []  # code-host connections that must not be cloned: read through the API instead
    ai_review: bool = True  # an independent reviewer session reads the diff before a build stage finishes
    # The office as a read-only MCP server at /mcp for developers' IDEs, each with a personal token (web/mcp_api.py).
    # Off unless asked for: it lets code and documents be read from outside the office's own runs.
    mcp_server: bool = False
    environment: dict = {}  # what this machine can run (written by `actoroffice doctor` / init)

    @property
    def db_path(self) -> Path:
        return self.data_dir / "office.db"

    @property
    def workspaces_dir(self) -> Path:
        return self.data_dir / "ws"

    @property
    def runs_dir(self) -> Path:
        """Large run outputs (diffs, command output, check logs) referenced from run events."""
        return self.data_dir / "runs"

    @property
    def doc_pack_path(self) -> Path | None:
        if not self.doc_pack:
            return None
        direct = Path(self.doc_pack).expanduser()
        return direct if direct.is_absolute() or direct.exists() else self.data_dir / "packs" / self.doc_pack

    @property
    def docs_repo_path(self) -> Path | None:
        if not self.docs_repo:
            return None
        path = Path(self.docs_repo).expanduser()
        return path if path.is_absolute() else self.data_dir / path

    @property
    def config_path(self) -> Path:
        return self.data_dir / "config.yaml"


def default_home() -> Path:
    return Path(os.environ.get("ACTOROFFICE_HOME") or Path.home() / ".actoroffice")


def load_settings(data_dir: Path | None = None) -> Settings:
    home = (data_dir or default_home()).expanduser().resolve()
    values: dict = {}
    config = home / "config.yaml"
    if config.exists():
        values = yaml.safe_load(config.read_text(encoding="utf-8")) or {}
    values["data_dir"] = home
    return Settings.model_validate(values)


def write_default_config(settings: Settings) -> None:
    settings.data_dir.mkdir(parents=True, exist_ok=True)
    if settings.config_path.exists():
        return
    data = settings.model_dump(mode="json", exclude={"data_dir"})
    settings.config_path.write_text(yaml.safe_dump(data, sort_keys=False, allow_unicode=True), encoding="utf-8")
