"""One validated configuration for every provider and workflow."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import RESEARCH_TOPICS

DEFAULT_FOCUS = (
    "independent Israeli or Israeli-founded technology startups across enterprise software, "
    "cybersecurity, fintech, health and biotechnology, AI, robotics, semiconductors, "
    "climate, energy, water, agriculture, consumer technology, and other technology sectors"
)


class Settings(BaseModel):
    model_config = ConfigDict(extra="forbid")
    root: Path = Field(default_factory=Path.cwd)
    directory: Path = Path("data/startups")
    state_dir: Path = Path(".agent-runs")
    provider: Literal["codex", "copilot", "claude"] = "codex"
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh"] = "medium"
    focus: str = DEFAULT_FOCUS
    min_words: int = Field(default=900, ge=0)
    min_sources: int = Field(default=4, ge=1)
    duplicate_threshold: float = Field(default=0.9, gt=0, le=1)
    timeout: float = Field(default=900, ge=0)
    max_attempts: int = Field(default=3, ge=1)
    max_rate_limit_retries: int = Field(default=3, ge=0)
    rate_limit_default_wait: float = Field(default=300, ge=0)
    rate_limit_fudge: float = Field(default=60, ge=0)
    max_quota_wait: float = Field(default=86400, ge=1)
    failure_backoff: float = Field(default=10, ge=0)
    jobs: int = Field(default=1, ge=1, le=32)
    sleep: float = Field(default=2, ge=0)
    limit: int = Field(default=1, ge=0)
    only: str | None = None
    resume_from: str | None = None
    topics: list[str] = []
    due_only: bool = False
    stale_days: int = Field(default=30, ge=1)
    retry_failed_days: int = Field(default=7, ge=0)
    sandbox: Literal["read-only", "workspace-write", "danger-full-access"] = "workspace-write"
    approval_policy: Literal["never", "on-request"] = "never"
    search: bool = True
    ephemeral: bool = True
    silent: bool = False
    claude_dangerously_skip_permissions: bool = False

    @field_validator("root", "directory", "state_dir")
    @classmethod
    def expand_paths(cls, value: Path) -> Path:
        return value.expanduser()

    @field_validator("topics")
    @classmethod
    def known_topics(cls, value: list[str]) -> list[str]:
        if set(value) - set(RESEARCH_TOPICS):
            raise ValueError(
                f"Unknown research topics: {sorted(set(value) - set(RESEARCH_TOPICS))}"
            )
        return list(dict.fromkeys(value))

    def resolved(self) -> Settings:
        root = self.root.resolve()
        directory = (root / self.directory).resolve()
        state = (root / self.state_dir).resolve()
        if not directory.is_dir():
            raise ValueError(f"Startup directory does not exist: {directory}")
        if state == directory or directory in state.parents:
            raise ValueError("state_dir must be outside the startup directory")
        return self.model_copy(update={"root": root, "directory": directory, "state_dir": state})


def settings_from_file(
    path: Path | None, overrides: dict[str, object], defaults: dict[str, object] | None = None
) -> Settings:
    values: dict[str, object] = {}
    if path:
        with path.open("rb") as handle:
            document = tomllib.load(handle)
        values = document.get("agent", document)
    return Settings.model_validate({**(defaults or {}), **values, **overrides}).resolved()
