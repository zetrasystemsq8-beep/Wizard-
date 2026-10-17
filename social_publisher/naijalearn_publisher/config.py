"""All configuration comes from environment variables. Nothing secret is ever stored in the repo."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass
from typing import Mapping


class ConfigError(RuntimeError):
    pass


def _required(env: Mapping[str, str], name: str) -> str:
    value = (env.get(name) or "").strip()
    if not value:
        raise ConfigError(f"Missing required environment variable {name}")
    return value


def _int(env: Mapping[str, str], name: str, default: int, minimum: int = 0) -> int:
    raw = (env.get(name) or "").strip()
    if not raw:
        return default
    try:
        value = int(raw)
    except ValueError:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from None
    if value < minimum:
        raise ConfigError(f"{name} must be >= {minimum}, got {value}")
    return value


@dataclass(frozen=True)
class SupabaseSettings:
    url: str
    service_key: str


@dataclass(frozen=True)
class FacebookSettings:
    page_id: str
    page_token: str
    graph_version: str


@dataclass(frozen=True)
class WorkerSettings:
    max_per_run: int
    min_interval_seconds: int
    max_per_day: int
    retry_base_seconds: int
    retry_cap_seconds: int
    lock_timeout_minutes: int


def load_supabase_settings(env: Mapping[str, str] | None = None) -> SupabaseSettings:
    env = os.environ if env is None else env
    return SupabaseSettings(_required(env, "SUPABASE_URL"), _required(env, "SUPABASE_SERVICE_KEY"))


def graph_version(env: Mapping[str, str]) -> str:
    version = (env.get("FB_GRAPH_VERSION") or "v25.0").strip()
    if not re.fullmatch(r"v\d+\.\d+", version):
        raise ConfigError(f"FB_GRAPH_VERSION must look like v25.0, got {version!r}")
    return version


def load_facebook_settings(env: Mapping[str, str] | None = None) -> FacebookSettings:
    env = os.environ if env is None else env
    return FacebookSettings(
        page_id=_required(env, "FB_PAGE_ID"),
        page_token=_required(env, "FB_PAGE_ACCESS_TOKEN"),
        graph_version=graph_version(env),
    )


def load_worker_settings(env: Mapping[str, str] | None = None) -> WorkerSettings:
    env = os.environ if env is None else env
    return WorkerSettings(
        max_per_run=_int(env, "PUBLISH_MAX_PER_RUN", 3, 1),
        min_interval_seconds=_int(env, "PUBLISH_MIN_INTERVAL_SECONDS", 3600),
        max_per_day=_int(env, "PUBLISH_MAX_PER_DAY", 3, 1),
        retry_base_seconds=_int(env, "RETRY_BASE_SECONDS", 600, 1),
        retry_cap_seconds=_int(env, "RETRY_CAP_SECONDS", 21600, 1),
        lock_timeout_minutes=_int(env, "LOCK_TIMEOUT_MINUTES", 15, 1),
    )
