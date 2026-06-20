"""Configuration loading, env-var substitution, and fail-fast validation."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

_ENV_PATTERN = re.compile(r"\$\{([A-Z0-9_]+)\}")


class ConfigError(RuntimeError):
    """Raised when configuration is missing or invalid."""


def _substitute_env(value: Any) -> Any:
    """Recursively replace ${VAR} tokens with environment values."""
    if isinstance(value, str):
        def repl(match: re.Match) -> str:
            var = match.group(1)
            env = os.environ.get(var)
            if env is None:
                raise ConfigError(f"Required environment variable not set: {var}")
            return env
        return _ENV_PATTERN.sub(repl, value)
    if isinstance(value, dict):
        return {k: _substitute_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_substitute_env(v) for v in value]
    return value


@dataclass
class Config:
    """Typed accessor over the raw config dict.

    Kept intentionally thin: the YAML is the source of truth, this just
    provides dotted-path access plus validation of the bits the agent
    cannot run without.
    """

    raw: dict[str, Any] = field(default_factory=dict)

    def get(self, path: str, default: Any = None) -> Any:
        node: Any = self.raw
        for part in path.split("."):
            if not isinstance(node, dict) or part not in node:
                return default
            node = node[part]
        return node

    # Convenience typed views -------------------------------------------------
    @property
    def repository(self) -> dict: return self.raw["repository"]

    @property
    def loki(self) -> dict: return self.raw["loki"]

    @property
    def claude(self) -> dict: return self.raw["claude"]

    @property
    def parcle(self) -> dict: return self.raw["parcle"]

    @property
    def autonomy(self) -> dict: return self.raw["autonomy"]

    @property
    def rules(self) -> dict: return self.raw["rules"]

    @property
    def context_retrieval(self) -> dict: return self.raw["context_retrieval"]

    @property
    def github(self) -> dict: return self.raw["github"]

    @property
    def code_style(self) -> dict: return self.raw["code_style"]

    @property
    def build(self) -> dict: return self.raw["build"]

    @property
    def test(self) -> dict: return self.raw["test"]

    @property
    def pattern_cache(self) -> dict: return self.raw["pattern_cache"]


_REQUIRED_SECTIONS = [
    "repository", "loki", "claude", "parcle", "autonomy",
    "rules", "context_retrieval", "github", "build", "test",
]


def load_config(path: str | Path = "config.yaml", *, validate: bool = True) -> Config:
    """Load YAML, substitute env vars, and validate required sections.

    Fails fast (raises ConfigError) on missing env vars or sections so the
    agent never starts in a half-configured state mid-incident.
    """
    p = Path(path)
    if not p.exists():
        raise ConfigError(f"Config file not found: {p}")

    raw = yaml.safe_load(p.read_text()) or {}
    raw = _substitute_env(raw)

    if validate:
        missing = [s for s in _REQUIRED_SECTIONS if s not in raw]
        if missing:
            raise ConfigError(f"Missing required config sections: {missing}")
        # token allocation must sum to ~1.0
        alloc = raw["context_retrieval"].get("token_allocation", {})
        total = sum(alloc.values())
        if alloc and abs(total - 1.0) > 0.01:
            raise ConfigError(f"token_allocation must sum to 1.0, got {total}")

    return Config(raw=raw)
