"""Two-stage OneBot delivery using Tagfetch-owned rendering and image helpers."""

from __future__ import annotations

import asyncio
import copy
import random
import tempfile
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from nonebot import logger
from nonebot.adapters.onebot.v11 import Bot

from ..config import (
    MEDIA_CACHE_DIR,
    TAGFETCH_CARD_FORWARD_MAX_NODES,
    TAGFETCH_CARD_FORWARD_MAX_RAW_BYTES,
    TAGFETCH_ONEBOT_FORWARD_MAX_RAW_BYTES,
)
from ..models import DownloadedImage, PreparedCandidate
from ..renderer import render_conversation_card
from ..renderer import shutdown as shutdown_renderer
from ..storage import (
    has_delivery,
    mark_originals_sent,
    record_card_delivery,
)
from .image_sender import (
    image_cq_from_path,
    image_segment_from_path,
)

GROUP_PUSH_DELAY_MIN_SECONDS = 3.0
GROUP_PUSH_DELAY_MAX_SECONDS = 5.0
FORWARD_BATCH_DELAY_SECONDS = 1.0


async def _wait_between_group_pushes() -> None:
    delay = random.uniform(
        GROUP_PUSH_DELAY_MIN_SECONDS,
        GROUP_PUSH_DELAY_MAX_SECONDS,
    )
    logger.info(
        "[TagfetchBroadcast] waiting before next group delay_seconds={:.1f}", delay
    )
    await asyncio.sleep(delay)


def _card_media_url(url: str) -> str:
    """Request a card-sized pbs.twimg.com variant without touching QQ originals."""
    parsed = urlsplit(url)
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "pbs.twimg.com"
    ):
        return url
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["name"] = "small"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def conversation_without_avatars(candidate: PreparedCandidate):
    """Return an avatar-free renderer copy without mutating fetched data."""
    rendered = copy.deepcopy(candidate.conversation)
    items = [rendered.root, *rendered.ancestors, rendered.target, rendered.quote]
    for item in items:
        if item is not None:
            item.author.avatar_url = ""
            item.translated_text = ""
            for media in item.media:
                media.url = _card_media_url(media.url)
                media.thumbnail_url = _card_media_url(media.thumbnail_url)
    if rendered.target is not None:
        # Keep generated card filenames private to Tagfetch.
        rendered.target.id = f"tagfetch_{candidate.tweet_id}"
    return rendered


async def _render_cards(
    candidates: list[PreparedCandidate],
) -> dict[str, Path]:
    cards: dict[str, Path] = {}
    logger.info(
        "[TagfetchBroadcast] render batch starting candidates={}", len(candidates)
    )
    for candidate in candidates:
        logger.info(
            "[TagfetchBroadcast] card render starting tweet={}", candidate.tweet_id
        )
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
            logger.info(
                "[TagfetchBroadcast] card render finished tweet={}",
                candidate.tweet_id,
            )
        else:
            logger.warning(
                "[TagfetchBroadcast] card render returned no path tweet={}",
                candidate.tweet_id,
            )
    logger.info(
        "[TagfetchBroadcast] render batch finished requested={} rendered={}",
        len(candidates),
        len(cards),
    )
    return cards


async def _card_node(
    path: Path, candidate: PreparedCandidate, bot_id: str
) -> dict | None:
    content = await asyncio.to_thread(image_cq_from_path, path)
    if content is None:
        return None
    target = candidate.conversation.target
    handle = target.author.screen_name if target is not None else "tagfetch"
    return {
        "type": "node",
        "data": {"name": f"@{handle}", "uin": bot_id, "content": content},
    }


def _card_size(path: Path) -> int:
    try:
        return path.stat().st_size
    except OSError:
        return 0


def _card_batches(
    candidates: list[PreparedCandidate], cards: dict[str, Path]
) -> tuple[list[list[PreparedCandidate]], list[PreparedCandidate]]:
    batches: list[list[PreparedCandidate]] = []
    skipped: list[PreparedCandidate] = []
    batch: list[PreparedCandidate] = []
    batch_size = 0
    for candidate in candidates:
        image_size = _card_size(cards[candidate.tweet_id])
        if image_size <= 0 or image_size > TAGFETCH_CARD_FORWARD_MAX_RAW_BYTES:
            skipped.append(candidate)
            continue
        if batch and (
            len(batch) >= TAGFETCH_CARD_FORWARD_MAX_NODES
            or batch_size + image_size > TAGFETCH_CARD_FORWARD_MAX_RAW_BYTES
        ):
            batches.append(batch)
            batch = []
            batch_size = 0
        batch.append(candidate)
        batch_size += image_size
    if batch:
        batches.append(batch)
    return batches, skipped


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
        image_size = _card_size(cards[candidate.tweet_id])
        if image_size <= 0 or image_size > TAGFETCH_CARD_FORWARD_MAX_RAW_BYTES:
            logger.warning(
                "[TagfetchBroadcast] direct card exceeds safe limit group={} "
                "tweet={} bytes={} limit_bytes={}",
                group_id,
                candidate.tweet_id,
                image_size,
                TAGFETCH_CARD_FORWARD_MAX_RAW_BYTES,
            )
            return []
        image = await asyncio.to_thread(
            image_segment_from_path, cards[candidate.tweet_id]
        )
        if image is None:
            return []
        try:
            await bot.call_api("send_group_msg", group_id=int(group_id), message=image)
        except Exception:  # noqa: BLE001 - adapter errors are isolated per group
            logger.exception(
                "[TagfetchBroadcast] direct card failed group={} tweet={}",
                group_id,
                candidate.tweet_id,
            )
            return []
        return [candidate]

    batches, skipped = _card_batches(available, cards)
    for candidate in skipped:
        logger.warning(
            "[TagfetchBroadcast] card skipped by safe limit group={} tweet={} "
            "bytes={} limit_bytes={}",
            group_id,
            candidate.tweet_id,
            _card_size(cards[candidate.tweet_id]),
            TAGFETCH_CARD_FORWARD_MAX_RAW_BYTES,
        )
    logger.info(
        "[TagfetchBroadcast] cards split group={} candidates={} batches={} "
        "max_nodes={} max_raw_bytes={}",
        group_id,
        len(available),
        len(batches),
        TAGFETCH_CARD_FORWARD_MAX_NODES,
        TAGFETCH_CARD_FORWARD_MAX_RAW_BYTES,
    )
    delivered: list[PreparedCandidate] = []
    for batch_index, batch in enumerate(batches, start=1):
        nodes: list[dict] = []
        included: list[PreparedCandidate] = []
        for candidate in batch:
            node = await _card_node(
                cards[candidate.tweet_id], candidate, str(bot.self_id)
            )
            if node is not None:
                nodes.append(node)
                included.append(candidate)
        if nodes:
            try:
                await bot.call_api(
                    "send_group_forward_msg",
                    group_id=int(group_id),
                    messages=nodes,
                )
            except Exception:  # noqa: BLE001 - isolate one forward batch
                logger.exception(
                    "[TagfetchBroadcast] merged cards batch failed group={} batch={}",
                    group_id,
                    batch_index,
                )
            else:
                delivered.extend(included)
                logger.info(
                    "[TagfetchBroadcast] cards batch sent group={} batch={}/{} "
                    "nodes={}",
                    group_id,
                    batch_index,
                    len(batches),
                    len(nodes),
                )
        if batch_index < len(batches):
            await asyncio.sleep(FORWARD_BATCH_DELAY_SECONDS)
    return delivered


_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


def _original_size(image: DownloadedImage) -> int:
    if image.local_path is not None:
        try:
            return image.local_path.stat().st_size
        except OSError:
            return 0
    return len(image.data)


def _original_batches(
    originals: list[DownloadedImage],
) -> list[list[DownloadedImage]]:
    batches: list[list[DownloadedImage]] = []
    batch: list[DownloadedImage] = []
    batch_size = 0
    for image in originals:
        image_size = _original_size(image)
        if image_size <= 0 or image_size > TAGFETCH_ONEBOT_FORWARD_MAX_RAW_BYTES:
            return []
        if batch and batch_size + image_size > TAGFETCH_ONEBOT_FORWARD_MAX_RAW_BYTES:
            batches.append(batch)
            batch = []
            batch_size = 0
        batch.append(image)
        batch_size += image_size
    if batch:
        batches.append(batch)
    return batches


def _original_path(
    image: DownloadedImage, directory: Path, position: int
) -> Path | None:
    if image.local_path is not None:
        return image.local_path if image.local_path.is_file() else None
    extension = _IMAGE_EXTENSIONS.get(image.mime_type, ".img")
    path = directory / f"{position:03d}{extension}"
    try:
        path.write_bytes(image.data)
    except OSError:
        return None
    return path


async def _send_originals(
    bot: Bot, group_id: str, candidates: list[PreparedCandidate]
) -> bool:
    originals = [image for candidate in candidates for image in candidate.originals]
    if not originals:
        return True
    batches = _original_batches(originals)
    if not batches:
        logger.warning(
            "[TagfetchBroadcast] originals exceed safe forward batch group={} "
            "limit_bytes={}",
            group_id,
            TAGFETCH_ONEBOT_FORWARD_MAX_RAW_BYTES,
        )
        return False
    logger.info(
        "[TagfetchBroadcast] originals split group={} images={} batches={} "
        "batch_limit_bytes={}",
        group_id,
        len(originals),
        len(batches),
        TAGFETCH_ONEBOT_FORWARD_MAX_RAW_BYTES,
    )
    with tempfile.TemporaryDirectory(prefix="tagfetch_originals_") as directory:
        temporary_directory = Path(directory)
        position = 0
        for batch_index, batch in enumerate(batches, start=1):
            nodes: list[dict] = []
            for image in batch:
                position += 1
                path = await asyncio.to_thread(
                    _original_path, image, temporary_directory, position
                )
                content = (
                    await asyncio.to_thread(image_cq_from_path, path)
                    if path is not None
                    else None
                )
                if content is None:
                    logger.warning(
                        "[TagfetchBroadcast] failed to encode original "
                        "group={} index={}",
                        group_id,
                        position,
                    )
                    return False
                nodes.append(
                    {
                        "type": "node",
                        "data": {
                            "name": (
                                f"@{image.author_handle} 原图 {image.media_index + 1}"
                            ),
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
            except Exception:  # noqa: BLE001 - no per-image fallback by design
                logger.exception(
                    "[TagfetchBroadcast] originals batch failed group={} batch={}",
                    group_id,
                    batch_index,
                )
                return False
            logger.info(
                "[TagfetchBroadcast] originals batch sent group={} batch={}/{}",
                group_id,
                batch_index,
                len(batches),
            )
            if batch_index < len(batches):
                await asyncio.sleep(FORWARD_BATCH_DELAY_SECONDS)
    return True


def _cleanup_spooled_originals(candidates: list[PreparedCandidate]) -> None:
    cache_root = MEDIA_CACHE_DIR.resolve()
    directories: set[Path] = set()
    removed = 0
    for candidate in candidates:
        for image in candidate.originals:
            if image.local_path is None:
                continue
            resolved = image.local_path.resolve()
            try:
                resolved.relative_to(cache_root)
            except ValueError:
                logger.warning(
                    "[TagfetchBroadcast] refusing cleanup outside media cache path={}",
                    resolved,
                )
                continue
            directories.add(resolved.parent)
            try:
                resolved.unlink(missing_ok=True)
            except OSError as exc:
                logger.warning(
                    "[TagfetchBroadcast] original cleanup failed file={} error={}",
                    resolved.name,
                    exc,
                )
            else:
                removed += 1
    for directory in sorted(
        directories, key=lambda path: len(path.parts), reverse=True
    ):
        try:
            directory.rmdir()
        except OSError:
            pass
    if removed:
        logger.info("[TagfetchBroadcast] spooled originals cleaned count={}", removed)


async def _broadcast_to_groups(
    bot: Bot, candidates: list[PreparedCandidate], group_ids: list[str]
) -> None:
    if not candidates or not group_ids:
        logger.info(
            "[TagfetchBroadcast] broadcast skipped candidates={} groups={}",
            len(candidates),
            len(group_ids),
        )
        return
    logger.info(
        "[TagfetchBroadcast] broadcast starting candidates={} groups={}",
        len(candidates),
        len(group_ids),
    )
    try:
        cards = await _render_cards(candidates)
    finally:
        # Free Chromium before constructing large OneBot base64 payloads.
        await shutdown_renderer()
    has_previous_group = False
    for group_id in group_ids:
        try:
            pending = [
                candidate
                for candidate in candidates
                if not has_delivery(candidate.tweet_id, group_id)
            ]
            logger.info(
                "[TagfetchBroadcast] group starting group={} pending={} "
                "already_delivered={}",
                group_id,
                len(pending),
                len(candidates) - len(pending),
            )
            if not pending:
                continue
            if has_previous_group:
                await _wait_between_group_pushes()
            has_previous_group = True
            card_sent = await _send_cards(bot, group_id, pending, cards)
            if not card_sent:
                continue
            for candidate in card_sent:
                record_card_delivery(
                    candidate.tweet_id,
                    group_id,
                    originals_sent=not candidate.originals,
                )
            logger.info(
                "[TagfetchBroadcast] card deliveries recorded group={} count={}",
                group_id,
                len(card_sent),
            )
            with_originals = [
                candidate for candidate in card_sent if candidate.originals
            ]
            if with_originals:
                await asyncio.sleep(FORWARD_BATCH_DELAY_SECONDS)
            if with_originals and await _send_originals(bot, group_id, with_originals):
                mark_originals_sent(
                    [candidate.tweet_id for candidate in with_originals], group_id
                )
        except Exception:  # noqa: BLE001 - groups have independent accounting
            logger.exception(
                "[TagfetchBroadcast] isolated group failure group={}", group_id
            )


async def broadcast_to_groups(
    bot: Bot, candidates: list[PreparedCandidate], group_ids: list[str]
) -> None:
    """Broadcast a round and always release its spooled original files."""
    try:
        await _broadcast_to_groups(bot, candidates, group_ids)
    finally:
        _cleanup_spooled_originals(candidates)
