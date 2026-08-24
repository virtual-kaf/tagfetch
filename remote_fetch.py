"""Validation and orchestration for one stateless remote tag query."""

from __future__ import annotations

from collections.abc import Sequence

from .clients.grok import GrokDiscoveryError, grok_fetch_urls
from .config import LIMIT_PER_TAG, LOOKBACK_HOURS, MAX_TAGS, MIN_LIKES


class InvalidTagRequest(ValueError):
    pass


def normalize_requested_tags(raw_tags: object) -> list[str]:
    if (
        isinstance(raw_tags, (str, bytes))
        or not isinstance(raw_tags, Sequence)
        or not 1 <= len(raw_tags) <= MAX_TAGS
    ):
        raise InvalidTagRequest("tags must be a non-empty array")
    result: list[str] = []
    seen: set[str] = set()
    for value in raw_tags:
        if not isinstance(value, str):
            raise InvalidTagRequest("tag must be a string")
        tag = value.strip().lstrip("#")
        if (
            not tag
            or len(tag) > 64
            or not all(character.isalnum() or character == "_" for character in tag)
        ):
            raise InvalidTagRequest("invalid hashtag")
        key = tag.casefold()
        if key not in seen:
            seen.add(key)
            result.append(tag)
    if not result:
        raise InvalidTagRequest("tags cannot be empty")
    return result


def validate_query_options(body: dict) -> None:
    expected = {
        "min_likes": MIN_LIKES,
        "lookback_hours": LOOKBACK_HOURS,
        "limit_per_tag": LIMIT_PER_TAG,
    }
    if any(body.get(name) != value for name, value in expected.items()):
        raise InvalidTagRequest("unsupported query options")


async def fetch_latest_urls(
    tags: Sequence[str],
    *,
    min_likes: int = MIN_LIKES,
    lookback_hours: int = LOOKBACK_HOURS,
    limit_per_tag: int = LIMIT_PER_TAG,
) -> list[dict[str, str]]:
    body = {
        "min_likes": min_likes,
        "lookback_hours": lookback_hours,
        "limit_per_tag": limit_per_tag,
    }
    validate_query_options(body)
    normalized = normalize_requested_tags(tags)
    try:
        return await grok_fetch_urls(normalized)
    except GrokDiscoveryError:
        raise
    except Exception as exc:
        raise GrokDiscoveryError("grok_failed") from exc
