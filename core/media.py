"""Safe X-media download and Gemini audit-copy preparation."""

from __future__ import annotations

import base64
import io
import shutil
import tempfile
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

import httpx
from PIL import Image, ImageOps

from ..config import (
    CANDIDATE_ORIGINALS_MAX_BYTES,
    GEMINI_IMAGE_PAYLOAD_MAX_BYTES,
    MEDIA_CACHE_DIR,
    REQUEST_TIMEOUT_SECONDS,
    SINGLE_ORIGINAL_MAX_BYTES,
)
from ..models import DownloadedImage
from ..models.tweet import TweetConversation, TweetItem

_ALLOWED_IMAGE_TYPES = {
    "image/jpeg": "image/jpeg",
    "image/jpg": "image/jpeg",
    "image/png": "image/png",
    "image/webp": "image/webp",
    "image/gif": "image/gif",
}


_IMAGE_EXTENSIONS = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "image/gif": ".gif",
}


class MediaDownloadError(RuntimeError):
    pass


def normalize_original_url(url: str) -> str:
    parsed = urlsplit(url)
    if (
        parsed.scheme.casefold() != "https"
        or (parsed.hostname or "").casefold() != "pbs.twimg.com"
    ):
        raise MediaDownloadError("original_photo_host_not_allowed")
    query = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query["name"] = "orig"
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), ""))


def _allowed_twimg_url(url: str, *, original: bool) -> bool:
    parsed = urlsplit(url)
    if parsed.scheme.casefold() != "https":
        return False
    host = (parsed.hostname or "").casefold()
    if original:
        return host == "pbs.twimg.com"
    return host == "pbs.twimg.com" or host.endswith(".twimg.com")


async def _download_image(
    client: httpx.AsyncClient,
    url: str,
    *,
    max_bytes: int,
    original: bool,
    source_tweet_id: str,
    author_handle: str,
    media_index: int,
) -> DownloadedImage:
    if not _allowed_twimg_url(url, original=original):
        raise MediaDownloadError("media_host_not_allowed")
    current_url = url
    try:
        for _redirect_count in range(6):
            if not _allowed_twimg_url(current_url, original=original):
                raise MediaDownloadError("media_redirect_host_not_allowed")
            async with client.stream(
                "GET", current_url, follow_redirects=False
            ) as response:
                if response.status_code in {301, 302, 303, 307, 308}:
                    location = response.headers.get("location")
                    if not location:
                        raise MediaDownloadError("media_redirect_missing_location")
                    next_url = urljoin(str(response.url), location)
                    if not _allowed_twimg_url(next_url, original=original):
                        raise MediaDownloadError("media_redirect_host_not_allowed")
                    current_url = next_url
                    continue
                if response.status_code != 200:
                    raise MediaDownloadError(f"media_http_{response.status_code}")
                content_type = (
                    response.headers.get("content-type", "")
                    .split(";", 1)[0]
                    .strip()
                    .casefold()
                )
                mime_type = _ALLOWED_IMAGE_TYPES.get(content_type)
                if mime_type is None:
                    raise MediaDownloadError("media_content_type_not_allowed")
                content_encoding = (
                    response.headers.get("content-encoding", "").strip().casefold()
                )
                if content_encoding not in {"", "identity"}:
                    raise MediaDownloadError("media_content_encoding_not_allowed")
                raw_length = response.headers.get("content-length")
                declared_length: int | None = None
                if raw_length:
                    try:
                        declared_length = int(raw_length)
                    except ValueError as exc:
                        raise MediaDownloadError("invalid_content_length") from exc
                    if declared_length < 0:
                        raise MediaDownloadError("invalid_content_length")
                    if declared_length > max_bytes:
                        raise MediaDownloadError("media_too_large")
                chunks: list[bytes] = []
                size = 0
                async for chunk in response.aiter_raw():
                    size += len(chunk)
                    if size > max_bytes:
                        raise MediaDownloadError("media_too_large")
                    chunks.append(chunk)
                if declared_length is not None and size != declared_length:
                    raise MediaDownloadError("content_length_mismatch")
                break
        else:
            raise MediaDownloadError("too_many_redirects")
    except httpx.HTTPError as exc:
        raise MediaDownloadError("media_request_failed") from exc
    data = b"".join(chunks)
    if not data:
        raise MediaDownloadError("empty_media")
    return DownloadedImage(
        source_url=url,
        data=data,
        mime_type=mime_type,
        source_tweet_id=source_tweet_id,
        author_handle=author_handle,
        media_index=media_index,
        is_original_photo=original,
    )


def _displayed_items(conversation: TweetConversation) -> Iterable[TweetItem]:
    yield from conversation.ancestors
    if conversation.target is not None:
        yield conversation.target
    if conversation.quote is not None:
        yield conversation.quote


async def download_candidate_images(
    conversation: TweetConversation,
) -> tuple[list[DownloadedImage], list[DownloadedImage]]:
    """Return (QQ originals, extra video thumbnails for Gemini)."""
    originals: list[DownloadedImage] = []
    thumbnails: list[DownloadedImage] = []
    total_original_bytes = 0
    seen_photos: set[str] = set()
    seen_thumbnails: set[str] = set()
    timeout = httpx.Timeout(REQUEST_TIMEOUT_SECONDS)

    async with httpx.AsyncClient(
        timeout=timeout,
        trust_env=False,
        follow_redirects=False,
        headers={"Accept-Encoding": "identity"},
    ) as client:
        for item in _displayed_items(conversation):
            handle = item.author.screen_name
            for index, media in enumerate(item.media):
                if media.type == "photo" and media.url:
                    original_url = normalize_original_url(media.url)
                    if original_url in seen_photos:
                        continue
                    remaining = CANDIDATE_ORIGINALS_MAX_BYTES - total_original_bytes
                    if remaining <= 0:
                        raise MediaDownloadError("candidate_originals_too_large")
                    image = await _download_image(
                        client,
                        original_url,
                        max_bytes=min(SINGLE_ORIGINAL_MAX_BYTES, remaining),
                        original=True,
                        source_tweet_id=item.id,
                        author_handle=handle,
                        media_index=index,
                    )
                    total_original_bytes += len(image.data)
                    if total_original_bytes > CANDIDATE_ORIGINALS_MAX_BYTES:
                        raise MediaDownloadError("candidate_originals_too_large")
                    seen_photos.add(original_url)
                    originals.append(image)
                elif media.type == "video":
                    if item is conversation.quote and index != 0:
                        continue
                    if not media.thumbnail_url:
                        raise MediaDownloadError("video_thumbnail_missing")
                    if media.thumbnail_url in seen_thumbnails:
                        continue
                    thumbnail = await _download_image(
                        client,
                        media.thumbnail_url,
                        max_bytes=SINGLE_ORIGINAL_MAX_BYTES,
                        original=False,
                        source_tweet_id=item.id,
                        author_handle=handle,
                        media_index=index,
                    )
                    seen_thumbnails.add(media.thumbnail_url)
                    thumbnails.append(thumbnail)
    return originals, thumbnails


def spool_originals(images: list[DownloadedImage]) -> list[DownloadedImage]:
    """Persist approved QQ originals and release their in-memory byte payloads."""
    if not images:
        return []
    MEDIA_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    try:
        directory = Path(
            tempfile.mkdtemp(prefix="candidate_", dir=str(MEDIA_CACHE_DIR))
        )
    except OSError as exc:
        raise MediaDownloadError("original_spool_directory_failed") from exc

    spooled: list[DownloadedImage] = []
    try:
        for position, image in enumerate(images, start=1):
            if not image.data:
                raise MediaDownloadError("original_spool_empty")
            extension = _IMAGE_EXTENSIONS.get(image.mime_type, ".img")
            path = directory / f"{position:03d}{extension}"
            written = path.write_bytes(image.data)
            if written != len(image.data):
                raise MediaDownloadError("original_spool_incomplete")
            spooled.append(replace(image, data=b"", local_path=path))
    except OSError as exc:
        shutil.rmtree(directory, ignore_errors=True)
        raise MediaDownloadError("original_spool_write_failed") from exc
    except MediaDownloadError:
        shutil.rmtree(directory, ignore_errors=True)
        raise
    return spooled


def _encoded_size(images: Iterable[DownloadedImage]) -> int:
    return sum(4 * ((len(image.data) + 2) // 3) for image in images)


def _jpeg_copy(data: bytes, max_edge: int, quality: int) -> bytes:
    try:
        with Image.open(io.BytesIO(data)) as source:
            image = ImageOps.exif_transpose(source)
            image.seek(0)
            image = image.convert("RGB")
            image.thumbnail((max_edge, max_edge), Image.Resampling.LANCZOS)
            output = io.BytesIO()
            image.save(output, format="JPEG", quality=quality, optimize=True)
            return output.getvalue()
    except Exception as exc:
        raise MediaDownloadError("audit_image_decode_failed") from exc


def prepare_audit_images(images: list[DownloadedImage]) -> list[DownloadedImage]:
    """Fit every image into one Gemini request without dropping any image."""
    if _encoded_size(images) <= GEMINI_IMAGE_PAYLOAD_MAX_BYTES:
        return images
    attempts = (
        (2048, 85),
        (1536, 80),
        (1280, 75),
        (1024, 70),
        (768, 65),
        (512, 60),
    )
    for max_edge, quality in attempts:
        resized = [
            DownloadedImage(
                source_url=image.source_url,
                data=_jpeg_copy(image.data, max_edge, quality),
                mime_type="image/jpeg",
                source_tweet_id=image.source_tweet_id,
                author_handle=image.author_handle,
                media_index=image.media_index,
                is_original_photo=image.is_original_photo,
            )
            for image in images
        ]
        if _encoded_size(resized) <= GEMINI_IMAGE_PAYLOAD_MAX_BYTES:
            return resized
    raise MediaDownloadError("gemini_image_budget_exceeded")


def image_data_url(image: DownloadedImage) -> str:
    encoded = base64.b64encode(image.data).decode("ascii")
    return f"data:{image.mime_type};base64,{encoded}"
