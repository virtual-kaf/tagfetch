"""OneBot V11 shared-file image helpers owned by Tagfetch."""

import hashlib
import os
import shutil
import threading
import time
from pathlib import Path, PurePosixPath
from urllib.parse import quote

from nonebot import logger
from nonebot.adapters.onebot.v11 import MessageSegment

from ..config import (
    TAGFETCH_SHARED_IMAGE_CONTAINER_DIR,
    TAGFETCH_SHARED_IMAGE_HOST_DIR,
    TAGFETCH_SHARED_IMAGE_TTL_SECONDS,
)


_cleanup_worker_started = False
_cleanup_worker_lock = threading.Lock()
_SHARED_FILE_PREFIX = "tagfetch-"


def _shared_image_uri(filename: str) -> str:
    container_path = PurePosixPath(
        TAGFETCH_SHARED_IMAGE_CONTAINER_DIR,
        filename,
    )
    encoded_path = quote(container_path.as_posix(), safe="/")
    return f"file://{encoded_path}"


def cleanup_expired_shared_images() -> int:
    """Remove this plugin's shared copies after their grace period."""
    shared_dir = TAGFETCH_SHARED_IMAGE_HOST_DIR
    if not shared_dir.exists():
        return 0

    removed = 0
    now = time.time()
    try:
        files = list(shared_dir.iterdir())
    except OSError as exc:
        logger.warning(
            "[TagfetchImageSend] cannot scan shared directory error={}", exc
        )
        return 0

    owned_prefixes = (_SHARED_FILE_PREFIX, f".{_SHARED_FILE_PREFIX}")
    for shared_path in files:
        if not shared_path.is_file() or not shared_path.name.startswith(owned_prefixes):
            continue
        try:
            age = max(0.0, now - shared_path.stat().st_mtime)
            if age >= TAGFETCH_SHARED_IMAGE_TTL_SECONDS:
                shared_path.unlink(missing_ok=True)
                removed += 1
        except OSError as exc:
            logger.warning(
                "[TagfetchImageSend] cannot clean {} error={}",
                shared_path.name,
                exc,
            )
    return removed


def _cleanup_worker() -> None:
    """Periodically sweep shared copies using one daemon thread."""
    while True:
        interval = min(
            300.0,
            max(10.0, float(TAGFETCH_SHARED_IMAGE_TTL_SECONDS) / 4),
        )
        time.sleep(interval)
        removed = cleanup_expired_shared_images()
        if removed:
            logger.info(
                "[TagfetchImageSend] removed {} expired shared image(s)", removed
            )


def _ensure_cleanup_worker() -> None:
    """Start delayed cleanup once, including recovery after process restarts."""
    global _cleanup_worker_started
    with _cleanup_worker_lock:
        if _cleanup_worker_started:
            return
        _cleanup_worker_started = True

    removed = cleanup_expired_shared_images()
    if removed:
        logger.info(
            "[TagfetchImageSend] removed {} expired shared image(s)", removed
        )
    threading.Thread(
        target=_cleanup_worker,
        name="tagfetch-shared-image-cleanup",
        daemon=True,
    ).start()


def _shared_filename(image_path: Path) -> str:
    """Avoid overwriting a delayed copy with another same-named image."""
    metadata = image_path.stat()
    source_key = os.fsencode(
        f"{image_path.resolve()}\0{metadata.st_mtime_ns}\0{metadata.st_size}"
    )
    fingerprint = hashlib.sha256(source_key).hexdigest()[:12]
    return (
        f"{_SHARED_FILE_PREFIX}{image_path.stem}-"
        f"{fingerprint}{image_path.suffix}"
    )


def _copy_to_shared_directory(image_path: Path) -> Path | None:
    """Atomically copy one image into SnowLuma's shared directory."""
    shared_dir = TAGFETCH_SHARED_IMAGE_HOST_DIR
    try:
        shared_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "[TagfetchImageSend] cannot create shared directory error={}", exc
        )
        return None

    temporary_path: Path | None = None
    try:
        _ensure_cleanup_worker()
        shared_name = _shared_filename(image_path)
        shared_path = shared_dir / shared_name
        temporary_path = shared_dir / (
            f".{shared_name}.{os.getpid()}.{threading.get_ident()}.tmp"
        )
        if image_path.resolve() == shared_path.resolve():
            os.utime(shared_path, None)
        else:
            shutil.copyfile(image_path, temporary_path)
            temporary_path.chmod(0o644)
            temporary_path.replace(shared_path)
    except OSError as exc:
        logger.warning(
            "[TagfetchImageSend] cannot copy {} error={}", image_path.name, exc
        )
        if temporary_path is not None:
            try:
                temporary_path.unlink(missing_ok=True)
            except OSError:
                pass
        return None

    return shared_path


def image_segment_from_path(path: str | Path) -> MessageSegment | None:
    image_path = Path(path)
    try:
        is_valid = image_path.is_file() and image_path.stat().st_size > 0
    except OSError as exc:
        logger.warning(
            "[TagfetchImageSend] cannot inspect {} error={}", image_path.name, exc
        )
        return None
    if not is_valid:
        logger.warning(
            "[TagfetchImageSend] refusing missing or empty file {}", image_path.name
        )
        return None

    shared_path = _copy_to_shared_directory(image_path)
    if shared_path is None:
        return None
    return MessageSegment.image(_shared_image_uri(shared_path.name))


def image_cq_from_path(path: str | Path) -> str | None:
    segment = image_segment_from_path(path)
    return str(segment) if segment is not None else None
