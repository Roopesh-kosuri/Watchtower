from __future__ import annotations

import os
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field


class ConfigValidationError(Exception):
    """Raised with ALL problems found, not just the first one -- fixing a
    config file one error at a time via repeated restarts is a bad first
    experience. __str__ renders a numbered list."""

    def __init__(self, errors: list[str]):
        self.errors = errors
        super().__init__(self._render())

    def _render(self) -> str:
        lines = [f"Configuration validation failed with {len(self.errors)} error(s):"]
        for i, err in enumerate(self.errors, 1):
            lines.append(f"  {i}. {err}")
        lines.append("Fix these and restart.")
        return "\n".join(lines)

    def __str__(self) -> str:
        return self._render()


class HTTPCheckConfig(BaseModel):
    url: str
    method: str = "GET"
    interval_seconds: float = Field(gt=0)
    timeout_seconds: float = Field(gt=0)
    expected_status: list[int] = Field(default_factory=lambda: [200])


class LogConfig(BaseModel):
    source: Literal["file", "push"]
    path: Optional[str] = None
    parser: str
    pattern: Optional[str] = None


class DetectorOverride(BaseModel):
    run_interval_seconds: Optional[float] = None
    ema_alpha: Optional[float] = None
    cold_start_min_samples: Optional[int] = None
    latency_stddev_threshold: Optional[float] = None
    error_rate_stddev_threshold: Optional[float] = None


class ServiceConfig(BaseModel):
    name: str
    type: Literal["http", "log"]
    enabled: bool = True
    check: Optional[HTTPCheckConfig] = None
    log: Optional[LogConfig] = None
    detector: Optional[DetectorOverride] = None
    notify: list[str] = Field(default_factory=list)


class DetectorConfig(BaseModel):
    run_interval_seconds: float = Field(default=30, gt=0)
    ema_alpha: float = Field(default=0.2, gt=0, le=1)
    cold_start_min_samples: int = Field(default=20, ge=1)
    latency_stddev_threshold: float = Field(default=3.0, gt=0)
    error_rate_stddev_threshold: float = Field(default=3.0, gt=0)


class NotificationChannelConfig(BaseModel):
    kind: Literal["webhook"]
    url: str
    format: Literal["discord", "generic"] = "generic"


class StorageConfig(BaseModel):
    path: str


class AuthConfig(BaseModel):
    """Off by default so a first-time local run isn't blocked at the door.
    A loud startup warning fires if this stays off -- see main.py -- since
    Watchtower is explicitly meant to be internet-facing eventually.
    `password_env` lets the actual secret live outside the yaml file if
    you don't want it checked into version control; `password` (literal)
    remains supported for the fastest possible quickstart."""

    enabled: bool = False
    username: str = "admin"
    password: Optional[str] = None
    password_env: Optional[str] = None


class WatchtowerConfig(BaseModel):
    storage: StorageConfig
    notifications: dict[str, NotificationChannelConfig] = Field(default_factory=dict)
    detector: DetectorConfig = DetectorConfig()
    auth: AuthConfig = AuthConfig()
    services: list[ServiceConfig]


def resolve_auth_password(auth: AuthConfig) -> Optional[str]:
    if auth.password_env:
        return os.environ.get(auth.password_env)
    return auth.password


def load_config(path: str) -> WatchtowerConfig:
    try:
        with open(path) as f:
            raw = yaml.safe_load(f)
    except FileNotFoundError:
        raise ConfigValidationError([f"config file not found at '{path}'"])
    except yaml.YAMLError as e:
        raise ConfigValidationError([f"'{path}' is not valid YAML: {e}"])

    if raw is None:
        raise ConfigValidationError([f"'{path}' is empty"])

    try:
        cfg = WatchtowerConfig.model_validate(raw)
    except Exception as e:
        # pydantic's own error already enumerates every field-level problem
        # in one shot -- pass it through as-is rather than re-wrapping it.
        raise ConfigValidationError([str(e)])

    errors: list[str] = []

    # --- cross-field checks pydantic's type system can't express alone ---
    for s in cfg.services:
        if s.type == "http" and s.check is None:
            errors.append(f"service '{s.name}' is type=http but has no `check:` block")
        if s.type == "log" and s.log is None:
            errors.append(f"service '{s.name}' is type=log but has no `log:` block")
        if s.type == "log" and s.log is not None:
            if s.log.source == "file" and not s.log.path:
                errors.append(f"service '{s.name}' has log.source=file but no `path:` given")
        if s.check is not None and not (s.check.url.startswith("http://") or s.check.url.startswith("https://")):
            errors.append(f"service '{s.name}' has check.url '{s.check.url}' -- must start with http:// or https://")
        for channel_name in s.notify:
            if channel_name not in cfg.notifications:
                known = ", ".join(sorted(cfg.notifications)) or "(none defined)"
                errors.append(
                    f"service '{s.name}' references unknown notification channel "
                    f"'{channel_name}' -- defined channels: {known}"
                )

    names = [s.name for s in cfg.services]
    dupes = sorted({n for n in names if names.count(n) > 1})
    if dupes:
        errors.append(f"duplicate service names in config: {dupes}")

    # --- auth ---
    if cfg.auth.enabled:
        resolved_password = resolve_auth_password(cfg.auth)
        if not resolved_password:
            if cfg.auth.password_env:
                errors.append(
                    f"auth.enabled is true and auth.password_env='{cfg.auth.password_env}' is set, "
                    f"but that environment variable is not set (or is empty) in this process"
                )
            else:
                errors.append("auth.enabled is true but no password is configured (set 'password' or 'password_env')")

    if errors:
        raise ConfigValidationError(errors)

    return cfg
