"""Bounded twitterapi.io discovery used only after repeated Grok failures."""

from __future__ import annotations

import asyncio
import random
import re
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any

import httpx
from nonebot import logger

from ..config import (
    LIMIT_PER_TAG,
    LOOKBACK,
    MIN_LIKES,
    TWITTERAPI_IO_API_BASE,
    TWITTERAPI_IO_API_KEY,
)
from ..models import DiscoveredPost
from .tweet_urls import TWEET_URL_RE

_HASHTAG_RE = re.compile(r"(?<![\w#])#([\w]+)", re.UNICODE)
_HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
_TWEET_ID_RE = re.compile(r"^[0-9]+$")
TWITTERAPI_IO_MAX_PAGES = 3
TWITTERAPI_IO_MAX_ATTEMPTS = 3
TWITTERAPI_IO_REQUEST_TIMEOUT = 30.0
TWITTERAPI_IO_DEFAULT_RATE_LIMIT_DELAY = 5.0


class TwitterApiDiscoveryError(RuntimeError):
    def __init__(self, reason: str, status_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def _search_query(tags: tuple[str, ...], now: datetime) -> str:
    current = now.astimezone(timezone.utc)
    since_time = int((current - LOOKBACK).timestamp())
    until_time = int(current.timestamp())
    hashtags = " OR ".join(f"#{tag}" for tag in tags)
    return (
        f"({hashtags}) min_faves:{MIN_LIKES} "
        f"since_time:{since_time} until_time:{until_time}"
    )


def _tweet_hashtags(item: dict[str, Any]) -> set[str]:
    found = {
        match.casefold()
        for match in _HASHTAG_RE.findall(str(item.get("text") or ""))
    }
    entities = item.get("entities")
    raw_hashtags = entities.get("hashtags") if isinstance(entities, dict) else None
    if isinstance(raw_hashtags, list):
        for hashtag in raw_hashtags:
            if isinstance(hashtag, dict) and isinstance(hashtag.get("text"), str):
                found.add(hashtag["text"].strip().lstrip("#").casefold())
    return found


def _retry_after_seconds(value: str | None) -> float | None:
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        retry_at = parsedate_to_datetime(value)
        if retry_at.tzinfo is None:
            retry_at = retry_at.replace(tzinfo=timezone.utc)
        return max(
            0.0,
            (retry_at - datetime.now(timezone.utc)).total_seconds(),
        )
    except (TypeError, ValueError, OverflowError):
        return None


def _transient_retry_delay(attempt: int) -> float:
    return (2 ** (attempt - 1)) + random.uniform(0.0, 0.25)


async def _request_page(
    client: httpx.AsyncClient, params: dict[str, str]
) -> dict[str, Any]:
    url = f"{TWITTERAPI_IO_API_BASE}/twitter/tweet/advanced_search"
    headers = {"X-API-Key": TWITTERAPI_IO_API_KEY}
    for attempt in range(1, TWITTERAPI_IO_MAX_ATTEMPTS + 1):
        delay: float | None = None
        try:
            response = await client.get(url, headers=headers, params=params)
        except httpx.RequestError as exc:
            if attempt >= TWITTERAPI_IO_MAX_ATTEMPTS:
                raise TwitterApiDiscoveryError("request_failed") from exc
            delay = _transient_retry_delay(attempt)
            logger.warning(
                "[TagfetchDiscovery] source=twitterapi_io retry={} "
                "reason=network delay={:.2f}s",
                attempt,
                delay,
            )
        else:
            if response.status_code == 200:
                try:
                    body = response.json()
                except (TypeError, ValueError) as exc:
                    raise TwitterApiDiscoveryError("invalid_response") from exc
                if not isinstance(body, dict) or not isinstance(
                    body.get("tweets"), list
                ):
                    raise TwitterApiDiscoveryError("invalid_response")
                return body
            if response.status_code == 429:
                if attempt >= TWITTERAPI_IO_MAX_ATTEMPTS:
                    raise TwitterApiDiscoveryError("http_429", status_code=429)
                delay = _retry_after_seconds(response.headers.get("Retry-After"))
                if delay is None:
                    delay = TWITTERAPI_IO_DEFAULT_RATE_LIMIT_DELAY
            elif 500 <= response.status_code < 600:
                if attempt >= TWITTERAPI_IO_MAX_ATTEMPTS:
                    raise TwitterApiDiscoveryError(
                        f"http_{response.status_code}",
                        status_code=response.status_code,
                    )
                delay = _transient_retry_delay(attempt)
            else:
                raise TwitterApiDiscoveryError(
                    f"http_{response.status_code}",
                    status_code=response.status_code,
                )
            logger.warning(
                "[TagfetchDiscovery] source=twitterapi_io retry={} "
                "reason=http_{} delay={:.2f}s",
                attempt,
                response.status_code,
                delay,
            )
        if delay is not None:
            await asyncio.sleep(delay)
    raise TwitterApiDiscoveryError("request_failed")


def _parse_posts(
    raw: Any,
    tags: tuple[str, ...],
    *,
    counts: dict[str, int] | None = None,
    seen: set[str] | None = None,
) -> list[DiscoveredPost]:
    if not isinstance(raw, dict) or not isinstance(raw.get("tweets"), list):
        raise TwitterApiDiscoveryError("invalid_response")

    requested = {tag.casefold(): tag for tag in tags}
    if counts is None:
        counts = {}
    if seen is None:
        seen = set()
    result: list[DiscoveredPost] = []
    for item in raw["tweets"]:
        if not isinstance(item, dict):
            continue
        likes = item.get("likeCount")
        if type(likes) is not int or likes < MIN_LIKES:
            continue
        matched_tag = next(
            (
                requested[key]
                for key in requested
                if key in _tweet_hashtags(item)
                and counts.get(key, 0) < LIMIT_PER_TAG
            ),
            None,
        )
        if matched_tag is None:
            continue

        tweet_id = str(item.get("id", "")).strip()
        author = item.get("author")
        raw_handle = author.get("userName") if isinstance(author, dict) else None
        if (
            not _TWEET_ID_RE.fullmatch(tweet_id)
            or not isinstance(raw_handle, str)
            or not _HANDLE_RE.fullmatch(raw_handle.strip().lstrip("@"))
        ):
            continue
        if tweet_id in seen:
            continue

        handle = raw_handle.strip().lstrip("@")
        raw_url = item.get("url")
        match = (
            TWEET_URL_RE.fullmatch(raw_url.strip())
            if isinstance(raw_url, str)
            else None
        )
        if match is not None and match.group(2) != tweet_id:
            continue
        if match is not None:
            handle = match.group(1)

        key = matched_tag.casefold()
        counts[key] = counts.get(key, 0) + 1
        seen.add(tweet_id)
        result.append(
            DiscoveredPost(
                tag=matched_tag,
                url=f"https://x.com/{handle}/status/{tweet_id}",
                tweet_id=tweet_id,
            )
        )
    return result


async def twitterapi_fetch_urls(
    tags: tuple[str, ...], *, now: datetime | None = None
) -> list[DiscoveredPost]:
    """Run one latest-search request for all configured hashtags."""
    if not TWITTERAPI_IO_API_KEY:
        raise TwitterApiDiscoveryError("api_key_missing")
    if not tags:
        return []
    current = now or datetime.now(timezone.utc)
    query = _search_query(tags, current)
    counts: dict[str, int] = {}
    seen: set[str] = set()
    posts: list[DiscoveredPost] = []
    cursor = ""
    async with httpx.AsyncClient(
        timeout=TWITTERAPI_IO_REQUEST_TIMEOUT, trust_env=False
    ) as client:
        for _page_number in range(1, TWITTERAPI_IO_MAX_PAGES + 1):
            params = {"query": query, "queryType": "Latest"}
            if cursor:
                params["cursor"] = cursor
            body = await _request_page(client, params)
            posts.extend(_parse_posts(body, tags, counts=counts, seen=seen))
            if all(
                counts.get(tag.casefold(), 0) >= LIMIT_PER_TAG for tag in tags
            ):
                break
            next_cursor = body.get("next_cursor")
            if (
                body.get("has_next_page") is not True
                or not isinstance(next_cursor, str)
                or not next_cursor
            ):
                break
            cursor = next_cursor
    logger.info(
        "[TagfetchDiscovery] response accepted source=twitterapi_io "
        "posts={} tags={}",
        len(posts),
        len(tags),
    )
    return posts
