"""Two-stage OneBot delivery using xfetch rendering and base64 helpers."""

from __future__ import annotations

import copy
import tempfile
from pathlib import Path

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot
from nonebot_plugin_xfetch.renderer import render_conversation_card
from nonebot_plugin_xfetch.services.image_sender import (
    image_cq_from_path,
    image_segment_from_path,
)

from ..models import PreparedCandidate
from ..storage import (
    has_delivery,
    mark_originals_sent,
    record_card_delivery,
)


def conversation_without_avatars(candidate: PreparedCandidate):
    """Return a renderer-only copy; xfetch's original conversation is untouched."""
    rendered = copy.deepcopy(candidate.conversation)
    items = [rendered.root, *rendered.ancestors, rendered.target, rendered.quote]
    for item in items:
        if item is not None:
            item.author.avatar_url = ""
            item.translated_text = ""
    if rendered.target is not None:
        # xfetch uses target.id only as its cache filename. A private prefix keeps
        # tagfetch's avatar-free card from overwriting xfetch's normal card.
        rendered.target.id = f"tagfetch_{candidate.tweet_id}"
    return rendered


async def _render_cards(
    candidates: list[PreparedCandidate],
) -> dict[str, Path]:
    cards: dict[str, Path] = {}
    for candidate in candidates:
        try:
            paths = await render_conversation_card(
                conversation_without_avatars(candidate)
            )
        except Exception:  # noqa: BLE001 - one render failure skips that candidate
            logger.exception(
                "[TagfetchBroadcast] card render crashed tweet={}",
                candidate.tweet_id,
            )
            continue
        if paths:
            cards[candidate.tweet_id] = Path(paths[0])
    return cards


def _card_node(path: Path, candidate: PreparedCandidate, bot_id: str) -> dict | None:
    content = image_cq_from_path(path)
    if content is None:
        return None
    target = candidate.conversation.target
    handle = target.author.screen_name if target is not None else "tagfetch"
    return {
        "type": "node",
        "data": {"name": f"@{handle}", "uin": bot_id, "content": content},
    }


async def _send_cards(
    bot: Bot,
    group_id: str,
    candidates: list[PreparedCandidate],
    cards: dict[str, Path],
) -> list[PreparedCandidate]:
    available = [candidate for candidate in candidates if candidate.tweet_id in cards]
    if not available:
        return []
    if len(available) == 1:
        candidate = available[0]
        image = image_segment_from_path(cards[candidate.tweet_id])
        if image is None:
            return []
        try:
            await bot.call_api(
                "send_group_msg", group_id=int(group_id), message=image
            )
        except Exception:  # noqa: BLE001 - adapter errors are isolated per group
            logger.exception(
                "[TagfetchBroadcast] direct card failed group={} tweet={}",
                group_id,
                candidate.tweet_id,
            )
            return []
        return [candidate]

    nodes: list[dict] = []
    included: list[PreparedCandidate] = []
    for candidate in available:
        node = _card_node(cards[candidate.tweet_id], candidate, str(bot.self_id))
        if node is not None:
            nodes.append(node)
            included.append(candidate)
    if not nodes:
        return []
    try:
        await bot.call_api(
            "send_group_forward_msg", group_id=int(group_id), messages=nodes
        )
    except Exception:  # noqa: BLE001 - adapter errors are isolated per group
        logger.exception(
            "[TagfetchBroadcast] merged cards failed group={}", group_id
        )
        return []
    return included


async def _send_originals(
    bot: Bot, group_id: str, candidates: list[PreparedCandidate]
) -> bool:
    originals = [
        image for candidate in candidates for image in candidate.originals
    ]
    if not originals:
        return True
    with tempfile.TemporaryDirectory(prefix="tagfetch_originals_") as directory:
        nodes: list[dict] = []
        for position, image in enumerate(originals, start=1):
            extension = {
                "image/jpeg": ".jpg",
                "image/png": ".png",
                "image/webp": ".webp",
                "image/gif": ".gif",
            }.get(image.mime_type, ".img")
            path = Path(directory) / f"{position:03d}{extension}"
            path.write_bytes(image.data)
            content = image_cq_from_path(path)
            if content is None:
                logger.warning(
                    "[TagfetchBroadcast] failed to encode original group={} index={}",
                    group_id,
                    position,
                )
                return False
            nodes.append(
                {
                    "type": "node",
                    "data": {
                        "name": f"@{image.author_handle} 原图 {image.media_index + 1}",
                        "uin": str(bot.self_id),
                        "content": content,
                    },
                }
            )
        try:
            await bot.call_api(
                "send_group_forward_msg",
                group_id=int(group_id),
                messages=nodes,
            )
        except Exception:  # noqa: BLE001 - album failure has no fallback by design
            logger.exception(
                "[TagfetchBroadcast] originals merge failed group={}", group_id
            )
            return False
    return True


async def broadcast_to_groups(
    bot: Bot, candidates: list[PreparedCandidate], group_ids: list[str]
) -> None:
    if not candidates or not group_ids:
        return
    cards = await _render_cards(candidates)
    for group_id in group_ids:
        try:
            pending = [
                candidate
                for candidate in candidates
                if not has_delivery(candidate.tweet_id, group_id)
            ]
            card_sent = await _send_cards(bot, group_id, pending, cards)
            if not card_sent:
                continue
            for candidate in card_sent:
                record_card_delivery(
                    candidate.tweet_id,
                    group_id,
                    originals_sent=not candidate.originals,
                )
            with_originals = [
                candidate for candidate in card_sent if candidate.originals
            ]
            if with_originals and await _send_originals(
                bot, group_id, with_originals
            ):
                mark_originals_sent(
                    [candidate.tweet_id for candidate in with_originals], group_id
                )
        except Exception:  # noqa: BLE001 - groups have independent accounting
            logger.exception(
                "[TagfetchBroadcast] isolated group failure group={}", group_id
            )