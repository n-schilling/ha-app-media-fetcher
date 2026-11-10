"""App options from /data/options.json."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

OPTIONS = Path("/data/options.json")
LEVELS = ("DEBUG", "INFO", "WARNING", "ERROR")


class ConfigError(Exception):
    """The options are unreadable or invalid."""


@dataclass(frozen=True)
class Config:
    token: str               # the API token; "" when none
    trust_home_assistant: bool  # Home Assistant Core gets in without the token
    max_jobs: int            # jobs that run at the same time
    keep_minutes: int        # minutes a finished job and its file stay
    max_size_mb: int         # megabytes (10^6 bytes) a job's file may have
    allow_any_site: bool     # also yt-dlp's generic extractor
    log_level: str


def _int(opts: dict, key: str, default: int, low: int, high: int) -> int:
    value = opts.get(key, default)
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"{key} must be a whole number from {low} to {high}")
    return value


def load(path: Path = OPTIONS) -> Config:
    try:
        opts = json.loads(path.read_text())
    except (OSError, ValueError) as err:
        raise ConfigError(f"{path} not readable: {err}") from err
    if not isinstance(opts, dict):
        raise ConfigError(f"{path} holds no options")
    level = str(opts.get("log_level") or "info").upper()
    if level not in LEVELS:
        raise ConfigError("log_level must be debug, info, warning or error")
    return Config(
        token=str(opts.get("token") or "").strip(),
        trust_home_assistant=bool(opts.get("trust_home_assistant", True)),
        max_jobs=_int(opts, "max_jobs", 2, 1, 8),
        keep_minutes=_int(opts, "keep_minutes", 60, 1, 1440),
        max_size_mb=_int(opts, "max_size_mb", 2000, 1, 100000),
        allow_any_site=bool(opts.get("allow_any_site", False)),
        log_level=level,
    )
