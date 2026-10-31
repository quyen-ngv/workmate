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
    max_unattended_agent_messages: int = 8  # actor↔actor chat messages in a row before pausing for the user
    web_host: str = "127.0.0.1"
    web_port: int = 8765
    # Team copilot
    ai_name: str = "Copilot"  # the single AI engineer every team works with
    ai_model: str | None = None  # None = default_model; "kiro" / "kiro:<id>" runs it on Kiro CLI
    jira_poll_s: float = 60  # Jira mirror interval in seconds (0 = off)
    stall_after_s: float = 900  # watchdog: a run with no event for this long is reported as stalled
    slow_command_s: float = 300  # watchdog: one command running longer than this is reported as slow

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
