"""Configuration loading for GigWatch.

Config is a JSON file. Environment variables can override sensitive values
(e.g. SMTP password, Slack token) so secrets never have to live in the file.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class SourceConfig:
    """A single job/gig feed to watch."""

    type: str                      # "remotive" | "wwr" | "remoteok" | "hn" | "rss" | "json"
    url: Optional[str] = None      # feed url (rss/json)
    limit: Optional[int] = None    # max items to fetch per source
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "SourceConfig":
        return cls(
            type=str(d.get("type", "remotive")).lower(),
            url=d.get("url"),
            limit=d.get("limit"),
            enabled=bool(d.get("enabled", True)),
        )


@dataclass
class Filters:
    """What counts as a match."""

    keywords: List[str] = field(default_factory=list)   # any-match
    require_all_keywords: bool = False                  # ...or all-match
    categories: List[str] = field(default_factory=list) # any-match (empty = any)
    locations: List[str] = field(default_factory=list)  # any-match (empty = any)
    min_score: float = 1.0                              # relevance floor
    exclude_keywords: List[str] = field(default_factory=list)

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Filters":
        return cls(
            keywords=[str(k).lower() for k in d.get("keywords", [])],
            require_all_keywords=bool(d.get("require_all_keywords", False)),
            categories=[str(c).lower() for c in d.get("categories", [])],
            locations=[str(l).lower() for l in d.get("locations", [])],
            min_score=float(d.get("min_score", 1.0)),
            exclude_keywords=[str(k).lower() for k in d.get("exclude_keywords", [])],
        )


@dataclass
class AlertConfig:
    """Where new matches are sent."""

    console: bool = True
    email: Dict[str, Any] = field(default_factory=dict)   # {to, from, smtp_host, smtp_port, use_tls, username, password_env}
    slack: Dict[str, Any] = field(default_factory=dict)   # {webhook_url} or {token, channel}
    max_per_alert: int = 25

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "AlertConfig":
        return cls(
            console=bool(d.get("console", True)),
            email=dict(d.get("email", {})),
            slack=dict(d.get("slack", {})),
            max_per_alert=int(d.get("max_per_alert", 25)),
        )


@dataclass
class Config:
    sources: List[SourceConfig]
    filters: Filters
    alerts: AlertConfig
    state_file: str
    poll_interval: int  # seconds, for `watch`
    profile: Dict[str, Any] = field(default_factory=dict)  # for `rank`

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "Config":
        return cls(
            sources=[SourceConfig.from_dict(s) for s in d.get("sources", [])],
            filters=Filters.from_dict(d.get("filters", {})),
            alerts=AlertConfig.from_dict(d.get("alerts", {})),
            state_file=d.get("state_file", "gigwatch-state.json"),
            poll_interval=int(d.get("poll_interval", 900)),
            profile=dict(d.get("profile", {})),
        )


import re as _re

_ENV_VAR_RE = _re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}|\$([A-Za-z_][A-Za-z0-9_]*)")


def _expand_env(value: Any) -> Any:
    """Recursively substitute ${VAR} / $VAR in string values from the env."""
    if isinstance(value, str):
        for m in set(_ENV_VAR_RE.findall(value)):
            var = m[0] or m[1]
            value = value.replace("${%s}" % var, os.environ.get(var, ""))
            value = value.replace("$%s" % var, os.environ.get(var, ""))
        return value
    if isinstance(value, dict):
        return {k: _expand_env(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_expand_env(v) for v in value]
    return value


def load_config(path: str) -> Config:
    """Load and validate a GigWatch config file."""
    with open(path, "r", encoding="utf-8") as fh:
        raw = json.load(fh)
    raw = _expand_env(raw)
    cfg = Config.from_dict(raw)

    if not cfg.sources:
        raise ValueError("config must define at least one source")
    for s in cfg.sources:
        if s.type not in ("remotive", "wwr", "remoteok", "hn", "jobicy", "rss", "json"):
            raise ValueError("unknown source type: %r" % s.type)
        if s.type in ("rss", "json") and not s.url:
            raise ValueError("source of type %r requires a url" % s.type)
    return cfg
