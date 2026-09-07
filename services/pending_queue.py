"""Durable prepared-candidate queue with a global one-per-hour dispatch gate."""

from __future__ import annotations

from datetime import datetime

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from ..models import PreparedCandidate
from ..storage import (
    claim_pending_dispatch_hour,
    enqueue_pending_candidate,
    get_pending_candidate_count,
    has_pending_delivery,
    move_pending_candidate_to_back,
    peek_pending_candidate,
    remove_pending_candidate,
)
from .broadcaster import broadcast_to_groups, cleanup_spooled_originals


def enqueue_prepared_candidates(
    candidates: list[PreparedCandidate],
) -> tuple[int, int]:
    """Persist a batch and release spool files produced for duplicate entries."""
    queued = 0
    duplicates: list[PreparedCandidate] = []
    for candidate in candidates:
        if enqueue_pending_candidate(candidate):
            queued += 1
        else:
            duplicates.append(candidate)
    if duplicates:
        cleanup_spooled_originals(duplicates)
    logger.info(
        "[TagfetchQueue] enqueue finished candidates={} queued={} "
        "duplicates={} pending_total={}",
        len(candidates),
        queued,
        len(duplicates),
        get_pending_candidate_count(),
    )
    return queued, len(duplicates)


async def dispatch_one_pending_candidate(
    bot: Bot,
    group_ids: list[str],
    *,
    now: datetime | None = None,
) -> str:
    """Attempt at most one queued candidate in the current CST hour."""
    if not group_ids:
        return "no_groups"

    candidate = peek_pending_candidate()
    while candidate is not None and not has_pending_delivery(
        candidate.tweet_id, group_ids
    ):
        if not remove_pending_candidate(candidate.tweet_id):
            return "contended"
        cleanup_spooled_originals([candidate])
        logger.info(
            "[TagfetchQueue] stale completed entry removed tweet={}",
            candidate.tweet_id,
        )
        candidate = peek_pending_candidate()

    if candidate is None:
        return "empty"
    if not claim_pending_dispatch_hour(candidate.tweet_id, at=now):
        return "rate_limited"

    logger.info(
        "[TagfetchQueue] dispatch starting tweet={} groups={} pending_total={}",
        candidate.tweet_id,
        len(group_ids),
        get_pending_candidate_count(),
    )
    await broadcast_to_groups(bot, [candidate], group_ids, cleanup=False)
    if has_pending_delivery(candidate.tweet_id, group_ids):
        move_pending_candidate_to_back(candidate.tweet_id)
        logger.warning(
            "[TagfetchQueue] dispatch incomplete tweet={} retained=true rotated=true",
            candidate.tweet_id,
        )
        return "retained"

    if remove_pending_candidate(candidate.tweet_id):
        cleanup_spooled_originals([candidate])
    logger.info(
        "[TagfetchQueue] dispatch completed tweet={} pending_total={}",
        candidate.tweet_id,
        get_pending_candidate_count(),
    )
    return "delivered"
