"""Bounded remote-asset prefetch plus isolated Pillow card rendering."""

from __future__ import annotations

import asyncio
import hashlib
import json
import os
import sys
import time
import uuid
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import httpx
from nonebot import logger
from PIL import Image, UnidentifiedImageError

from ..config import (
    CARD_DIR,
    TAGFETCH_RENDER_IMAGE_CONCURRENCY,
    TAGFETCH_RENDER_IMAGE_MAX_BYTES,
    TAGFETCH_RENDER_IMAGE_TIMEOUT,
    TAGFETCH_RENDER_JPEG_QUALITY,
    TAGFETCH_RENDER_MAX_HEIGHT,
    TAGFETCH_RENDER_MEMORY_MB,
    TAGFETCH_RENDER_TIMEOUT,
)
from ..models.tweet import TweetConversation, TweetItem

WORKER_PATH = Path(__file__).with_name("pillow_worker.py")
ASSET_DIR = Path(__file__).with_name("assets")
FONT_PATH = ASSET_DIR / "HYSongYunLangHeiW.ttf"
RENDER_CACHE_DIR = CARD_DIR.parent / "renderer_cache"
MEDIA_CACHE_DIR = RENDER_CACHE_DIR / "media"
EMOJI_CACHE_DIR = RENDER_CACHE_DIR / "emoji"
TEMP_DIR = RENDER_CACHE_DIR / "tmp"

_MAX_IMAGE_PIXELS = 12_000_000
_MEDIA_CACHE_TTL = 24 * 3600
_EMOJI_CACHE_TTL = 30 * 24 * 3600
_render_gate = asyncio.Semaphore(1)
_image_gate = asyncio.Semaphore(TAGFETCH_RENDER_IMAGE_CONCURRENCY)
_active_processes: set[asyncio.subprocess.Process] = set()
_last_prune = 0.0


def _bounded_image_url(url: str) -> str:
    """Ask Twitter for card-sized media while leaving other hosts unchanged."""
    parts = urlsplit(url)
    if (
        parts.hostname or ""
    ).casefold() != "pbs.twimg.com" or not parts.path.startswith("/media/"):
        return url
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["name"] = "medium"
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def _validate_image(path: Path) -> None:
    Image.MAX_IMAGE_PIXELS = _MAX_IMAGE_PIXELS
    with Image.open(path) as image:
        width, height = image.size
        if width <= 0 or height <= 0 or width * height > _MAX_IMAGE_PIXELS:
            raise ValueError(f"unsafe decoded image dimensions: {width}x{height}")
        image.verify()


def _cache_path(cache_dir: Path, url: str) -> Path:
    return cache_dir / f"{hashlib.sha256(url.encode('utf-8')).hexdigest()}.img"


async def _download_image(client: httpx.AsyncClient, url: str, cache_dir: Path) -> Path:
    bounded_url = _bounded_image_url(url)
    destination = _cache_path(cache_dir, bounded_url)
    if destination.exists():
        try:
            await asyncio.to_thread(_validate_image, destination)
            os.utime(destination, None)
            return destination
        except (OSError, ValueError, UnidentifiedImageError):
            destination.unlink(missing_ok=True)

    cache_dir.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(f".download-{uuid.uuid4().hex}")
    try:
        async with _image_gate, client.stream("GET", bounded_url) as response:
            if not response.is_success:
                raise RuntimeError(f"HTTP {response.status_code}")
            content_type = response.headers.get("content-type", "").split(";", 1)[0]
            if not content_type.casefold().startswith("image/"):
                raise RuntimeError(f"unexpected content type {content_type!r}")
            raw_length = response.headers.get("content-length", "")
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError):
                content_length = 0
            if content_length > TAGFETCH_RENDER_IMAGE_MAX_BYTES:
                raise RuntimeError(
                    f"compressed image too large: {content_length} bytes"
                )
            total = 0
            with temporary.open("wb") as stream:
                async for chunk in response.aiter_bytes():
                    total += len(chunk)
                    if total > TAGFETCH_RENDER_IMAGE_MAX_BYTES:
                        raise RuntimeError(
                            f"compressed image exceeded {TAGFETCH_RENDER_IMAGE_MAX_BYTES} bytes"
                        )
                    stream.write(chunk)
        await asyncio.to_thread(_validate_image, temporary)
        os.replace(temporary, destination)
        return destination
    finally:
        temporary.unlink(missing_ok=True)


async def _safe_download(client: httpx.AsyncClient, url: str, cache_dir: Path) -> str:
    if not url or urlsplit(url).scheme.casefold() not in {"http", "https"}:
        return ""
    try:
        return str(await _download_image(client, url, cache_dir))
    except Exception as exc:  # noqa: BLE001 - each image degrades independently
        parts = urlsplit(url)
        label = f"{parts.netloc}{parts.path}"[:160]
        logger.warning(
            f"[Render] Remote image degraded: {label} ({type(exc).__name__}: {exc})"
        )
        return ""


def _is_emoji_base(codepoint: int) -> bool:
    return (
        0x1F000 <= codepoint <= 0x1FAFF
        or 0x2600 <= codepoint <= 0x27BF
        or codepoint
        in {
            0x00A9,
            0x00AE,
            0x203C,
            0x2049,
            0x2122,
            0x2139,
            0x3030,
            0x303D,
            0x3297,
            0x3299,
        }
    )


def _fallback_emoji_list(text: str) -> list[str]:
    def consume_component(start: int) -> int | None:
        codepoint = ord(text[start])
        if text[start] in "#*0123456789":
            end = start + 1
            if end < len(text) and ord(text[end]) == 0xFE0F:
                end += 1
            if end < len(text) and ord(text[end]) == 0x20E3:
                return end + 1
            return None
        if not _is_emoji_base(codepoint):
            return None

        end = start + 1
        if end < len(text) and ord(text[end]) == 0xFE0E:
            return None
        if 0x1F1E6 <= codepoint <= 0x1F1FF:
            if end < len(text) and 0x1F1E6 <= ord(text[end]) <= 0x1F1FF:
                return end + 1
            return end

        while end < len(text) and (
            ord(text[end]) == 0xFE0F
            or 0x1F3FB <= ord(text[end]) <= 0x1F3FF
        ):
            end += 1

        if codepoint == 0x1F3F4:
            tag_start = end
            while end < len(text) and 0xE0020 <= ord(text[end]) <= 0xE007E:
                end += 1
            if end > tag_start and end < len(text) and ord(text[end]) == 0xE007F:
                end += 1
            elif end > tag_start:
                end = tag_start
        return end

    found: list[str] = []
    index = 0
    while index < len(text):
        end = consume_component(index)
        if end is None:
            index += 1
            continue
        start = index
        while end < len(text) and ord(text[end]) == 0x200D:
            next_end = consume_component(end + 1) if end + 1 < len(text) else None
            if next_end is None:
                break
            end = next_end
        found.append(text[start:end])
        index = end
    return found


def _extract_emojis(texts: Iterable[str]) -> set[str]:
    result: set[str] = set()
    try:
        import emoji

        for text in texts:
            result.update(item["emoji"] for item in emoji.emoji_list(text))
    except ImportError:
        for text in texts:
            result.update(_fallback_emoji_list(text))
    return result


def _twemoji_url(value: str) -> str:
    raw_codepoints = [ord(char) for char in value]
    # Twemoji retains VS16 inside ZWJ graphemes (for example the rainbow flag)
    # but drops it from standalone emoji and keycap filenames.
    keep_vs16 = 0x200D in raw_codepoints
    codepoints = "-".join(
        f"{codepoint:x}"
        for codepoint in raw_codepoints
        if keep_vs16 or codepoint != 0xFE0F
    )
    return f"https://cdn.jsdelivr.net/gh/jdecked/twemoji@latest/assets/72x72/{codepoints}.png"


def _tweet_strings(item: TweetItem | None) -> list[str]:
    if item is None:
        return []
    return [item.author.name, item.author.screen_name, item.text, item.translated_text]


def _tweet_spec(item: TweetItem, resolved: dict[str, str]) -> dict[str, Any]:
    media = []
    for value in item.media[:9]:
        source = value.thumbnail_url if value.type == "video" else value.url
        media.append({"path": resolved.get(source, ""), "video": value.type == "video"})
    return {
        "id": item.id,
        "url": item.url,
        "name": item.author.name,
        "screen_name": item.author.screen_name,
        "avatar_path": resolved.get(item.author.avatar_url, ""),
        "text": item.text,
        # Tagfetch deliberately renders the source post without translation.
        "translated_text": "",
        "created_at": item.created_at,
        "media": media,
        "likes": item.likes,
        "retweets": item.retweets,
        "replies": item.replies,
        "views": item.views,
    }


def _prune_dir(path: Path, ttl: int, now: float) -> None:
    if not path.exists():
        return
    for entry in path.iterdir():
        try:
            if entry.is_file() and now - entry.stat().st_mtime > ttl:
                entry.unlink()
        except OSError:
            continue


async def _prune_cache_if_due() -> None:
    global _last_prune
    now = time.time()
    if now - _last_prune < 3600:
        return
    _last_prune = now
    await asyncio.to_thread(_prune_dir, MEDIA_CACHE_DIR, _MEDIA_CACHE_TTL, now)
    await asyncio.to_thread(_prune_dir, EMOJI_CACHE_DIR, _EMOJI_CACHE_TTL, now)


def _http_client() -> httpx.AsyncClient:
    return httpx.AsyncClient(
        timeout=httpx.Timeout(TAGFETCH_RENDER_IMAGE_TIMEOUT),
        follow_redirects=True,
        max_redirects=3,
        headers={"User-Agent": "nonebot-plugin-tagfetch-pillow/1"},
    )


async def _resolve_assets(
    urls: Iterable[str], texts: Iterable[str]
) -> tuple[dict[str, str], dict[str, str]]:
    unique_urls = {url for url in urls if url}
    emoji_values = _extract_emojis(texts)
    async with _http_client() as client:
        url_list = list(unique_urls)
        url_paths = await asyncio.gather(
            *(_safe_download(client, url, MEDIA_CACHE_DIR) for url in url_list)
        )
        resolved = dict(zip(url_list, url_paths))
        emoji_list = list(emoji_values)
        emoji_paths = await asyncio.gather(
            *(
                _safe_download(client, _twemoji_url(value), EMOJI_CACHE_DIR)
                for value in emoji_list
            )
        )
    # Keep failed emoji keys so the offline worker can draw a placeholder.
    return resolved, dict(zip(emoji_list, emoji_paths))


def _base_spec(
    kind: str, output_path: Path, emoji_paths: dict[str, str]
) -> dict[str, Any]:
    return {
        "kind": kind,
        "output_path": str(output_path),
        "format": "JPEG",
        "quality": TAGFETCH_RENDER_JPEG_QUALITY,
        "font_path": str(FONT_PATH),
        "emoji_paths": emoji_paths,
        "memory_limit_mb": TAGFETCH_RENDER_MEMORY_MB,
        "max_image_pixels": _MAX_IMAGE_PIXELS,
        "max_height": TAGFETCH_RENDER_MAX_HEIGHT,
    }


async def _terminate_process(process: asyncio.subprocess.Process) -> None:
    if process.returncode is not None:
        return
    try:
        process.terminate()
    except ProcessLookupError:
        return
    try:
        await asyncio.wait_for(process.wait(), timeout=3)
    except asyncio.TimeoutError:
        try:
            process.kill()
        except ProcessLookupError:
            pass
        await process.wait()


async def _run_worker(spec: dict[str, Any]) -> list[Path]:
    if not FONT_PATH.is_file():
        raise RuntimeError(f"bundled renderer font missing: {FONT_PATH}")
    TEMP_DIR.mkdir(parents=True, exist_ok=True)
    token = uuid.uuid4().hex
    spec_path = TEMP_DIR / f"{token}.spec.json"
    result_path = TEMP_DIR / f"{token}.result.json"
    spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    process: asyncio.subprocess.Process | None = None
    try:
        process = await asyncio.create_subprocess_exec(
            sys.executable,
            str(WORKER_PATH),
            str(spec_path),
            str(result_path),
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        _active_processes.add(process)
        try:
            stdout, stderr = await asyncio.wait_for(
                process.communicate(), timeout=TAGFETCH_RENDER_TIMEOUT
            )
        except (asyncio.TimeoutError, asyncio.CancelledError):
            await _terminate_process(process)
            raise
        if process.returncode != 0:
            detail = stderr.decode("utf-8", errors="replace")[-12000:]
            raise RuntimeError(f"Pillow worker exited {process.returncode}: {detail}")
        if not result_path.exists():
            raise RuntimeError("Pillow worker produced no result manifest")
        payload = json.loads(result_path.read_text(encoding="utf-8"))
        paths = [Path(value) for value in payload.get("paths", [])]
        if not paths:
            raise RuntimeError("Pillow worker produced no images")
        output_parent = Path(spec["output_path"]).parent.resolve()
        for path in paths:
            if path.resolve().parent != output_parent:
                raise RuntimeError(f"worker returned an unexpected path: {path}")
            await asyncio.to_thread(_validate_image, path)
        if stdout:
            logger.debug(
                f"[Render] Pillow worker: {stdout.decode(errors='replace')[-1000:]}"
            )
        if stderr:
            logger.debug(
                f"[Render] Pillow worker diagnostics: "
                f"{stderr.decode(errors='replace')[-12000:]}"
            )
        return paths
    finally:
        if process is not None:
            _active_processes.discard(process)
        spec_path.unlink(missing_ok=True)
        result_path.unlink(missing_ok=True)


async def _render_with_timeout(factory) -> list[Path]:
    async with _render_gate:
        await _prune_cache_if_due()
        try:
            return await asyncio.wait_for(factory(), timeout=TAGFETCH_RENDER_TIMEOUT)
        except asyncio.TimeoutError as exc:
            raise RuntimeError(
                f"card rendering exceeded {TAGFETCH_RENDER_TIMEOUT}s"
            ) from exc


async def render_conversation_card(conv: TweetConversation) -> list[Path]:
    """Render a complete conversation to one or more PNG files."""
    if conv.target is None:
        logger.warning("render_conversation_card: 无 target 推文")
        return []
    CARD_DIR.mkdir(parents=True, exist_ok=True)

    async def render() -> list[Path]:
        items = [*conv.ancestors, conv.target]
        if conv.quote is not None:
            items.append(conv.quote)
        urls: list[str] = []
        texts: list[str] = []
        for item in items:
            urls.append(item.author.avatar_url)
            texts.extend(_tweet_strings(item))
            for media in item.media[:9]:
                urls.append(media.thumbnail_url if media.type == "video" else media.url)
        resolved, emoji_paths = await _resolve_assets(urls, texts)
        output = CARD_DIR / f"{conv.target.id}.jpg"
        spec = _base_spec("conversation", output, emoji_paths)
        spec.update(
            ancestors=[_tweet_spec(item, resolved) for item in conv.ancestors],
            target=_tweet_spec(conv.target, resolved),
            quote=_tweet_spec(conv.quote, resolved) if conv.quote else None,
        )
        return await _run_worker(spec)

    try:
        paths = await _render_with_timeout(render)
        logger.info("[TagfetchRender] card ready path={}", ", ".join(map(str, paths)))
        return paths
    except Exception as exc:  # noqa: BLE001 - broadcaster skips one failed card
        logger.error("[TagfetchRender] card render failed: {}", exc, exc_info=True)
        return []


async def shutdown() -> None:
    """Terminate any renderer subprocess still active during bot shutdown."""
    processes = list(_active_processes)
    if processes:
        logger.info(f"[Render] Terminating {len(processes)} active Pillow worker(s)")
    await asyncio.gather(
        *(_terminate_process(process) for process in processes), return_exceptions=True
    )
