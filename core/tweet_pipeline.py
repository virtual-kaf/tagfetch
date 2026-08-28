"""Remote discovery -> FxTwitter -> media -> Gemini pipeline."""

from __future__ import annotations

import asyncio
import re
from datetime import datetime, timedelta, timezone
from zoneinfo import ZoneInfo

from nonebot import logger

from ..clients.discovery import fetch_discovered_posts
from ..clients.fxtwitter import fetch_conversation
from ..clients.remote_grok import RemoteDiscoveryError
from ..config import FETCH_CONCURRENCY, LOOKBACK, MIN_LIKES
from ..models import DiscoveredPost, PreparedCandidate
from ..models.tweet import TweetConversation
from ..storage import has_pending_delivery, is_rejected, record_rejection
from .media import (
    MediaDownloadError,
    download_candidate_images,
    spool_originals,
)
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
            return (
                datetime.strptime(value, fmt)
                .replace(tzinfo=_JST)
                .astimezone(timezone.utc)
            )
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
    logger.info(
        "[TagfetchPipeline] run started tags={} target_groups={}",
        len(tags),
        len(group_ids),
    )
    if not tags or not group_ids:
        logger.info("[TagfetchPipeline] run skipped reason=missing_tags_or_groups")
        return []
    try:
        discovered = await fetch_discovered_posts(tags)
        logger.info(
            "[TagfetchPipeline] discovery finished candidates={}", len(discovered)
        )
    except RemoteDiscoveryError as exc:
        logger.warning(
            "[TagfetchPipeline] discovery unavailable: {}",
            exc.reason,
        )
        return []

    pending: list[DiscoveredPost] = []
    for post in discovered:
        if is_rejected(post.tweet_id):
            logger.info(
                "[TagfetchPipeline] candidate skipped tweet={} "
                "reason=permanently_rejected",
                post.tweet_id,
            )
            continue
        if not has_pending_delivery(post.tweet_id, group_ids):
            logger.info(
                "[TagfetchPipeline] candidate skipped tweet={} "
                "reason=delivered_to_all_active_groups",
                post.tweet_id,
            )
            continue
        pending.append(post)
    logger.info(
        "[TagfetchPipeline] discovery filtered discovered={} pending={}",
        len(discovered),
        len(pending),
    )
    if not pending:
        logger.info("[TagfetchPipeline] run finished reason=no_pending_candidates")
        return []

    fetched = await _fetch_conversations(pending)
    logger.info(
        "[TagfetchPipeline] FxTwitter batch finished requested={} usable={}",
        len(pending),
        len(fetched),
    )
    now = datetime.now(timezone.utc)
    valid: list[tuple[datetime, DiscoveredPost, TweetConversation]] = []
    for post, conversation in fetched:
        target = conversation.target
        if target is None or target.id != post.tweet_id:
            continue
        created_at = parse_created_at(target.created_at)
        if created_at is None:
            logger.warning(
                "[TagfetchPipeline] invalid timestamp tweet={}", post.tweet_id
            )
            continue
        age = now - created_at
        if not timedelta(0) <= age <= LOOKBACK:
            logger.info(
                "[TagfetchPipeline] candidate skipped tweet={} "
                "reason=outside_lookback age_seconds={:.0f}",
                post.tweet_id,
                age.total_seconds(),
            )
            continue
        if target.likes < MIN_LIKES:
            logger.info(
                "[TagfetchPipeline] candidate skipped tweet={} "
                "reason=likes_below_minimum likes={} minimum={}",
                post.tweet_id,
                target.likes,
                MIN_LIKES,
            )
            continue
        if not contains_requested_tag(target.text, tags):
            logger.info(
                "[TagfetchPipeline] candidate skipped tweet={} "
                "reason=target_hashtag_missing",
                post.tweet_id,
            )
            continue
        _clear_translations(conversation)
        valid.append((created_at, post, conversation))
        logger.info(
            "[TagfetchPipeline] candidate validated tweet={} likes={}",
            post.tweet_id,
            target.likes,
        )

    logger.info(
        "[TagfetchPipeline] local validation finished fetched={} valid={}",
        len(fetched),
        len(valid),
    )
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
        logger.info(
            "[TagfetchPipeline] media ready tweet={} originals={} "
            "video_thumbnails={} original_bytes={}",
            post.tweet_id,
            len(originals),
            len(thumbnails),
            sum(len(image.data) for image in originals),
        )
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
            logger.warning(
                "[TagfetchPipeline] candidate skipped tweet={} "
                "reason=review_inconclusive",
                post.tweet_id,
            )
            continue
        logger.info("[TagfetchPipeline] candidate approved tweet={}", post.tweet_id)
        try:
            spooled_originals = await asyncio.to_thread(spool_originals, originals)
        except MediaDownloadError as exc:
            logger.warning(
                "[TagfetchPipeline] approved original spool failed tweet={} reason={}",
                post.tweet_id,
                exc,
            )
            continue
        logger.info(
            "[TagfetchPipeline] originals moved to disk tweet={} count={}",
            post.tweet_id,
            len(spooled_originals),
        )
        prepared.append(
            PreparedCandidate(
                tweet_id=post.tweet_id,
                url=post.url,
                conversation=conversation,
                originals=spooled_originals,
            )
        )
    logger.info(
        "[TagfetchPipeline] run finished discovered={} valid={} prepared={}",
        len(discovered),
        len(valid),
        len(prepared),
    )
    return prepared
