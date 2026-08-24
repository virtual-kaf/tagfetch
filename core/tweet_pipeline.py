"""Remote discovery -> FxTwitter -> media -> Gemini pipeline."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from nonebot import logger
from nonebot_plugin_xfetch.clients.fxtwitter import fetch_conversation
from nonebot_plugin_xfetch.models.tweet import TweetConversation

from ..clients.remote_grok import RemoteDiscoveryError, remote_fetch_urls
from ..config import FETCH_CONCURRENCY, LOOKBACK, MIN_LIKES
from ..models import DiscoveredPost, PreparedCandidate
from ..storage import has_pending_delivery, is_rejected, record_rejection
from .media import MediaDownloadError, download_candidate_images
from .safety import review_candidate

_HASHTAG_RE = re.compile(r"(?<![\w#])#([\w]+)", re.UNICODE)
_JST = ZoneInfo("Asia/Tokyo")


def contains_requested_tag(text: str, tags: tuple[str, ...]) -> bool:
    found = {match.casefold() for match in _HASHTAG_RE.findall(text or "")}
    return any(tag.casefold() in found for tag in tags)


def parse_created_at(raw: str) -> datetime | None:
    value = str(raw).strip()
    for fmt in ("%Y-%m-%d %H:%M:%S JST", "%Y-%m-%d %H:%M JST"):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=_JST).astimezone(timezone.utc)
        except ValueError:
            continue
    return None


def _clear_translations(conversation: TweetConversation) -> None:
    for item in [*conversation.ancestors, conversation.target, conversation.quote]:
        if item is not None:
            item.translated_text = ""


async def _fetch_conversations(
    posts: list[DiscoveredPost],
) -> list[tuple[DiscoveredPost, TweetConversation]]:
    gate = asyncio.Semaphore(FETCH_CONCURRENCY)

    async def fetch_one(post: DiscoveredPost):
        try:
            async with gate:
                conversation = await asyncio.to_thread(
                    fetch_conversation, post.tweet_id
                )
            return post, conversation
        except Exception as exc:  # noqa: BLE001 - FxTwitter failure skips candidate
            logger.warning(
                "[TagfetchPipeline] FxTwitter failed tweet={} error={}",
                post.tweet_id,
                exc,
            )
            return post, None

    fetched = await asyncio.gather(*(fetch_one(post) for post in posts))
    return [
        (post, conversation)
        for post, conversation in fetched
        if conversation is not None and conversation.target is not None
    ]


async def run_tagfetch_pipeline(
    tags: tuple[str, ...], group_ids: list[str]
) -> list[PreparedCandidate]:
    if not tags or not group_ids:
        return []
    try:
        discovered = await remote_fetch_urls(tags)
    except RemoteDiscoveryError as exc:
        logger.warning(
            "[TagfetchPipeline] remote Grok failed without fallback: {}",
            exc.reason,
        )
        return []

    pending = [
        post
        for post in discovered
        if not is_rejected(post.tweet_id)
        and has_pending_delivery(post.tweet_id, group_ids)
    ]
    if not pending:
        return []

    fetched = await _fetch_conversations(pending)
    now = datetime.now(timezone.utc)
    valid: list[tuple[datetime, DiscoveredPost, TweetConversation]] = []
    for post, conversation in fetched:
        target = conversation.target
        if target is None or target.id != post.tweet_id:
            continue
        created_at = parse_created_at(target.created_at)
        if created_at is None:
            logger.warning("[TagfetchPipeline] invalid timestamp tweet={}", post.tweet_id)
            continue
        age = now - created_at
        if not timedelta(0) <= age <= LOOKBACK:
            continue
        if target.likes < MIN_LIKES:
            continue
        if not contains_requested_tag(target.text, tags):
            continue
        _clear_translations(conversation)
        valid.append((created_at, post, conversation))

    prepared: list[PreparedCandidate] = []
    for _created_at, post, conversation in sorted(valid, key=lambda item: item[0]):
        try:
            originals, thumbnails = await download_candidate_images(conversation)
        except MediaDownloadError as exc:
            logger.warning(
                "[TagfetchPipeline] media rejected tweet={} reason={}",
                post.tweet_id,
                exc,
            )
            continue
        review = await review_candidate(conversation, [*originals, *thumbnails])
        if review.status == "rejected":
            record_rejection(
                post.tweet_id,
                post.url,
                review.categories,
                review.reason,
            )
            logger.warning(
                "[TagfetchPipeline] Gemini rejected tweet={} categories={}",
                post.tweet_id,
                ",".join(review.categories),
            )
            continue
        if review.status != "approved":
            continue
        prepared.append(
            PreparedCandidate(
                tweet_id=post.tweet_id,
                url=post.url,
                conversation=conversation,
                originals=originals,
            )
        )
    return prepared