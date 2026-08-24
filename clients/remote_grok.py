"""Authenticated client for the standalone tagfetch Grok service."""

from __future__ import annotations

import ipaddress
from typing import Any

import httpx
from nonebot import logger
from nonebot_plugin_xfetch.clients.tweet_urls import TWEET_URL_RE

from ..config import (
    LIMIT_PER_TAG,
    LOOKBACK_HOURS,
    MIN_LIKES,
    REQUEST_TIMEOUT_SECONDS,
    TAGFETCH_REMOTE_ENABLED,
    TAGFETCH_REMOTE_HOST,
    TAGFETCH_REMOTE_PORT,
    TAGFETCH_REMOTE_TOKEN,
)
from ..models import DiscoveredPost

POLL_PATH = "/api/tagfetch/poll"


class RemoteDiscoveryError(RuntimeError):
    def __init__(self, reason: str, status_code: int | None = None):
        super().__init__(reason)
        self.reason = reason
        self.status_code = status_code


def _remote_url() -> str:
    enabled = TAGFETCH_REMOTE_ENABLED.casefold()
    if enabled not in {"1", "true", "yes", "on"}:
        raise RemoteDiscoveryError(
            "remote_disabled"
            if enabled in {"0", "false", "no", "off"}
            else "invalid_remote_config"
        )
    if not TAGFETCH_REMOTE_TOKEN:
        raise RemoteDiscoveryError("invalid_remote_config")
    try:
        address = ipaddress.ip_address(TAGFETCH_REMOTE_HOST)
        port = int(TAGFETCH_REMOTE_PORT)
    except ValueError as exc:
        raise RemoteDiscoveryError("invalid_remote_config") from exc
    if not 1 <= port <= 65535:
        raise RemoteDiscoveryError("invalid_remote_config")
    host = f"[{address}]" if address.version == 6 else str(address)
    return f"http://{host}:{port}{POLL_PATH}"


def _parse_posts(raw: Any, tags: tuple[str, ...]) -> list[DiscoveredPost]:
    if not isinstance(raw, list):
        raise RemoteDiscoveryError("invalid_remote_response")
    requested = {tag.casefold(): tag for tag in tags}
    counts: dict[str, int] = {}
    seen: set[str] = set()
    result: list[DiscoveredPost] = []
    for item in raw:
        if not isinstance(item, dict):
            raise RemoteDiscoveryError("invalid_remote_response")
        raw_tag, raw_url = item.get("tag"), item.get("url")
        if not isinstance(raw_tag, str) or not isinstance(raw_url, str):
            raise RemoteDiscoveryError("invalid_remote_response")
        tag = requested.get(raw_tag.strip().lstrip("#").casefold())
        match = TWEET_URL_RE.fullmatch(raw_url.strip())
        if tag is None or match is None:
            raise RemoteDiscoveryError("invalid_remote_response")
        key, tweet_id = tag.casefold(), match.group(2)
        counts[key] = counts.get(key, 0) + 1
        if counts[key] > LIMIT_PER_TAG:
            raise RemoteDiscoveryError("invalid_remote_response")
        if tweet_id in seen:
            continue
        seen.add(tweet_id)
        result.append(
            DiscoveredPost(
                tag=tag,
                url=f"https://x.com/{match.group(1)}/status/{tweet_id}",
                tweet_id=tweet_id,
            )
        )
    return result


async def remote_fetch_urls(tags: tuple[str, ...]) -> list[DiscoveredPost]:
    url = _remote_url()
    try:
        async with httpx.AsyncClient(
            timeout=REQUEST_TIMEOUT_SECONDS, trust_env=False
        ) as client:
            response = await client.post(
                url,
                headers={"Authorization": f"Bearer {TAGFETCH_REMOTE_TOKEN}"},
                json={
                    "tags": [f"#{tag}" for tag in tags],
                    "min_likes": MIN_LIKES,
                    "lookback_hours": LOOKBACK_HOURS,
                    "limit_per_tag": LIMIT_PER_TAG,
                },
            )
    except httpx.RequestError as exc:
        raise RemoteDiscoveryError("remote_request_failed") from exc
    if response.status_code != 200:
        raise RemoteDiscoveryError(
            f"http_{response.status_code}", status_code=response.status_code
        )
    try:
        body = response.json()
    except (TypeError, ValueError) as exc:
        raise RemoteDiscoveryError("invalid_remote_response") from exc
    if (
        not isinstance(body, dict)
        or body.get("ok") is not True
        or body.get("source") != "grok"
        or body.get("error") is not None
    ):
        raise RemoteDiscoveryError("invalid_remote_response")
    posts = _parse_posts(body.get("posts"), tags)
    logger.info(
        "[TagfetchDiscovery] source=remote_grok posts={} tags={}",
        len(posts),
        len(tags),
    )
    return posts