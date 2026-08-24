"""Configuration for the independent tagfetch plugin and remote service."""

from __future__ import annotations

import json
import os
from datetime import timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

try:
    from nonebot import get_driver
except ImportError:  # pragma: no cover - standalone remote helper
    get_driver = None

CST = ZoneInfo("Asia/Shanghai")
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"
STATE_DB = DATA_DIR / "tagfetch_state.sqlite3"

MIN_LIKES = 300
LOOKBACK_HOURS = 2
LOOKBACK = timedelta(hours=LOOKBACK_HOURS)
LIMIT_PER_TAG = 3
MAX_TAGS = 32
REQUEST_TIMEOUT_SECONDS = 120.0
GEMINI_TIMEOUT_SECONDS = 60.0
FETCH_CONCURRENCY = 4

SINGLE_ORIGINAL_MAX_BYTES = 20 * 1024 * 1024
CANDIDATE_ORIGINALS_MAX_BYTES = 60 * 1024 * 1024
GEMINI_IMAGE_PAYLOAD_MAX_BYTES = 18 * 1024 * 1024


def _get_config_value(name: str, default: object = "") -> object:
    value = os.getenv(name)
    if value is not None:
        return value
    if get_driver is not None:
        try:
            return getattr(get_driver().config, name.lower(), default)
        except ValueError:
            pass
    return getattr(_standalone_config(), name.lower(), default)


def _env_root() -> Path:
    cwd = Path.cwd().resolve()
    if (cwd / ".env").is_file():
        return cwd
    for parent in (PLUGIN_DIR, *PLUGIN_DIR.parents):
        if (parent / ".env").is_file():
            return parent
    return cwd


@lru_cache(maxsize=1)
def _standalone_config() -> Any:
    """Load project dotenv files when NoneBot's driver is not initialized."""
    from nonebot.config import Config, Env

    root = _env_root()
    env_files: list[Path] = []
    base_env = root / ".env"
    if base_env.is_file():
        env_files.append(base_env)
        environment = Env(_env_file=base_env).environment
        selected = root / f".env.{environment}"
        if selected.is_file():
            env_files.append(selected)
    return Config(_env_file=tuple(env_files), driver="~none")


def _get_config_str(name: str, default: str = "") -> str:
    value = _get_config_value(name, default)
    return default if value is None else str(value)


def _normalize_tag(value: object) -> str | None:
    if not isinstance(value, str):
        return None
    tag = value.strip().lstrip("#")
    if not tag or len(tag) > 64:
        return None
    if not all(character.isalnum() or character == "_" for character in tag):
        return None
    return tag


def parse_tags(raw: object) -> tuple[str, ...]:
    """Parse a JSON/list tag setting and return canonical tags without '#'."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return ()
    if not isinstance(raw, (list, tuple, set)):
        return ()

    tags: list[str] = []
    seen: set[str] = set()
    for item in raw:
        tag = _normalize_tag(item)
        if tag is None:
            continue
        key = tag.casefold()
        if key in seen:
            continue
        seen.add(key)
        tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    return tuple(tags)


TAGS = parse_tags(_get_config_value("KABUBU_TAGFETCH_TAGS", "[]"))

TAGFETCH_REMOTE_ENABLED = _get_config_str(
    "KABUBU_TAGFETCH_REMOTE_ENABLED", "false"
).strip()
TAGFETCH_REMOTE_HOST = _get_config_str(
    "KABUBU_TAGFETCH_REMOTE_HOST", "100.98.44.83"
).strip()
TAGFETCH_REMOTE_PORT = _get_config_str("KABUBU_TAGFETCH_REMOTE_PORT", "8766").strip()
TAGFETCH_REMOTE_TOKEN = _get_config_str("KABUBU_TAGFETCH_REMOTE_TOKEN", "").strip()

GROK_API_URL = _get_config_str(
    "KABUBU_GROK_API_URL", "http://127.0.0.1:8000/v1/chat/completions"
).strip()
GROK_API_KEY_RAW = _get_config_str("KABUBU_GROK_API_KEY", "").strip()
GROK_API_KEY = (
    GROK_API_KEY_RAW
    if GROK_API_KEY_RAW.casefold().startswith("bearer ")
    else f"Bearer {GROK_API_KEY_RAW}"
    if GROK_API_KEY_RAW
    else ""
)
GROK_MODEL = _get_config_str(
    "KABUBU_GROK_MODEL", "grok-4.20-0309-non-reasoning"
).strip()
