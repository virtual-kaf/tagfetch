"""OneBot V11 base64 image helpers owned by Tagfetch."""

from pathlib import Path

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment


def image_segment_from_path(path: str | Path) -> MessageSegment | None:
    image_path = Path(path)
    try:
        image_bytes = image_path.read_bytes()
    except OSError as exc:
        logger.warning(
            "[TagfetchImageSend] cannot read {} error={}", image_path.name, exc
        )
        return None
    if not image_bytes:
        logger.warning("[TagfetchImageSend] refusing empty file {}", image_path.name)
        return None
    return MessageSegment.image(image_bytes)


def image_cq_from_path(path: str | Path) -> str | None:
    segment = image_segment_from_path(path)
    return str(segment) if segment is not None else None
