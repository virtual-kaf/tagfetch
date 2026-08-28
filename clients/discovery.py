"""Primary Grok discovery with a rate-limited twitterapi.io fallback."""

from __future__ import annotations

from datetime import datetime

from nonebot import logger

from ..config import CST
from ..models import DiscoveredPost
from ..storage import (
    record_remote_discovery_failure,
    reset_remote_discovery_failures,
)
from .remote_grok import RemoteDiscoveryError, remote_fetch_urls
from .twitterapi_io import TwitterApiDiscoveryError, twitterapi_fetch_urls

FALLBACK_FAILURE_THRESHOLD = 3
FALLBACK_START_HOUR = 8
FALLBACK_END_HOUR = 24


def _fallback_window_open(now: datetime) -> bool:
    hour = now.astimezone(CST).hour
    return FALLBACK_START_HOUR <= hour < FALLBACK_END_HOUR


def _is_service_failure(error: RemoteDiscoveryError) -> bool:
    if error.reason == "remote_request_failed":
        return True
    return error.status_code is not None and error.status_code >= 500


async def fetch_discovered_posts(
    tags: tuple[str, ...], *, now: datetime | None = None
) -> list[DiscoveredPost]:
    """Use Grok normally and make one fallback request per three failures."""
    current = now or datetime.now(CST)
    try:
        posts = await remote_fetch_urls(tags)
    except RemoteDiscoveryError as error:
        if not _is_service_failure(error):
            reset_remote_discovery_failures()
            raise

        failures = record_remote_discovery_failure()
        if failures < FALLBACK_FAILURE_THRESHOLD:
            logger.warning(
                "[TagfetchDiscovery] remote Grok unavailable failures={}/{} "
                "fallback=false reason={}",
                failures,
                FALLBACK_FAILURE_THRESHOLD,
                error.reason,
            )
            raise
        if not _fallback_window_open(current):
            logger.warning(
                "[TagfetchDiscovery] remote Grok unavailable failures={} "
                "fallback=false reason=outside_cst_window",
                failures,
            )
            raise

        # Consuming the threshold before the request keeps fallback attempts at
        # one per three Grok failures even when twitterapi.io is also unavailable.
        reset_remote_discovery_failures()
        logger.warning(
            "[TagfetchDiscovery] remote Grok unavailable failures={} "
            "fallback=twitterapi_io",
            failures,
        )
        try:
            return await twitterapi_fetch_urls(tags, now=current)
        except TwitterApiDiscoveryError as fallback_error:
            logger.warning(
                "[TagfetchDiscovery] twitterapi.io fallback failed reason={}",
                fallback_error.reason,
            )
            raise RemoteDiscoveryError(
                f"twitterapi_fallback_{fallback_error.reason}",
                status_code=fallback_error.status_code,
            ) from fallback_error
    else:
        reset_remote_discovery_failures()
        return posts
