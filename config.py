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
JST = ZoneInfo("Asia/Tokyo")
PLUGIN_DIR = Path(__file__).resolve().parent
DATA_DIR = PLUGIN_DIR / "data"
STATE_DB = DATA_DIR / "tagfetch_state.sqlite3"
CARD_DIR = DATA_DIR / "cards"
MEDIA_CACHE_DIR = DATA_DIR / "media_cache"

MIN_LIKES = 300
LOOKBACK_HOURS = 24
LOOKBACK = timedelta(hours=LOOKBACK_HOURS)
LIMIT_PER_TAG = 3
MAX_TAGS = 32
REQUEST_TIMEOUT_SECONDS = 120.0
GEMINI_TIMEOUT_SECONDS = 60.0
FETCH_CONCURRENCY = 4
FXTWITTER_API_BASE = "https://api.fxtwitter.com"

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


def _get_config_int(name: str, default: int, minimum: int, maximum: int) -> int:
    try:
        value = int(_get_config_value(name, default))
    except (TypeError, ValueError):
        return default
    return max(minimum, min(value, maximum))


def _authorization_value(raw: str) -> str:
    value = raw.strip()
    if not value or value.casefold().startswith("bearer "):
        return value
    return f"Bearer {value}"


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


def parse_id_set(raw: object) -> frozenset[int]:
    """Parse an integer ID list from NoneBot config or a JSON environment value."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError:
            return frozenset()
    if not isinstance(raw, (list, tuple, set, frozenset)):
        return frozenset()
    values: set[int] = set()
    for item in raw:
        try:
            values.add(int(item))
        except (TypeError, ValueError):
            continue
    return frozenset(values)


TAGFETCH_RENDER_TIMEOUT = _get_config_int("KABUBU_TAGFETCH_RENDER_TIMEOUT", 90, 30, 180)
TAGFETCH_RENDER_BROWSER_MAX_USES = _get_config_int(
    "KABUBU_TAGFETCH_RENDER_BROWSER_MAX_USES", 20, 1, 100
)
TAGFETCH_RENDER_JPEG_QUALITY = _get_config_int(
    "KABUBU_TAGFETCH_RENDER_JPEG_QUALITY", 76, 60, 95
)
TAGFETCH_CARD_FORWARD_MAX_NODES = _get_config_int(
    "KABUBU_TAGFETCH_CARD_FORWARD_MAX_NODES", 4, 1, 12
)
TAGFETCH_CARD_FORWARD_MAX_RAW_BYTES = _get_config_int(
    "KABUBU_TAGFETCH_CARD_FORWARD_MAX_RAW_BYTES",
    4 * 1024 * 1024,
    1024 * 1024,
    12 * 1024 * 1024,
)
TAGFETCH_ONEBOT_FORWARD_MAX_RAW_BYTES = _get_config_int(
    "KABUBU_TAGFETCH_ONEBOT_FORWARD_MAX_RAW_BYTES",
    8 * 1024 * 1024,
    1024 * 1024,
    20 * 1024 * 1024,
)
ADMIN_IDS = parse_id_set(_get_config_value("KABUBU_ADMIN_LIST", []))
SUPER_ADMIN_IDS = parse_id_set(_get_config_value("KABUBU_SUPER_ADMIN", []))

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

GEMINI_API_KEY_RAW = _get_config_str("KABUBU_GEMINI_API_KEY", "").strip()
GEMINI_API_KEY = _authorization_value(GEMINI_API_KEY_RAW)
GEMINI_BASE_URL = (
    _get_config_str("KABUBU_GEMINI_BASE_URL", "https://api.vectorengine.cn/v1")
    .strip()
    .rstrip("/")
)
GEMINI_API_URL = (
    GEMINI_BASE_URL
    if GEMINI_BASE_URL.endswith("/chat/completions")
    else f"{GEMINI_BASE_URL}/chat/completions"
)
GEMINI_MODEL = _get_config_str("KABUBU_GEMINI_MODEL", "gemini-3.5-flash").strip()
