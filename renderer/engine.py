"""Jinja2 and Playwright renderer for Tagfetch conversation cards."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from jinja2 import Environment, FileSystemLoader, select_autoescape
from nonebot import logger

from ..config import (
    CARD_DIR,
    TAGFETCH_RENDER_BROWSER_MAX_USES,
    TAGFETCH_RENDER_IMAGE_CONCURRENCY,
    TAGFETCH_RENDER_IMAGE_MAX_BYTES,
    TAGFETCH_RENDER_IMAGE_TIMEOUT,
    TAGFETCH_RENDER_JPEG_QUALITY,
    TAGFETCH_RENDER_TIMEOUT,
)
from ..models.tweet import TweetConversation

TEMPLATE_DIR = Path(__file__).parent / "templates"
_env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)

_browser: Any = None
_playwright: Any = None
_browser_lock = asyncio.Lock()
_render_gate = asyncio.Semaphore(1)
_image_request_gate = asyncio.Semaphore(TAGFETCH_RENDER_IMAGE_CONCURRENCY)
_browser_uses = 0
_BROWSER_CLOSE_TIMEOUT_SECONDS = 5
_CONTEXT_CLOSE_TIMEOUT_SECONDS = 3

_TRANSPARENT_PNG = bytes.fromhex(
    "89504e470d0a1a0a0000000d4948445200000001000000010804000000b51c0c02"
    "0000000b4944415478da6364f80f00010501012718e3660000000049454e44ae426082"
)


def _bounded_image_url(url: str) -> str:
    """Ask Twitter's CDN for card-sized media instead of original files."""
    parts = urlsplit(url)
    if parts.hostname != "pbs.twimg.com" or not parts.path.startswith("/media/"):
        return url
    query = dict(parse_qsl(parts.query, keep_blank_values=True))
    query["name"] = "medium"
    return urlunsplit(
        (parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment)
    )


async def _fulfill_image_fallback(route) -> None:
    """Resolve a failed image without leaving networkidle pending."""
    try:
        await route.fulfill(
            status=200,
            content_type="image/png",
            body=_TRANSPARENT_PNG,
        )
    except Exception:  # noqa: BLE001 - page may already be closing
        pass


async def _allow_card_image_only(route) -> None:
    if route.request.resource_type != "image":
        await route.abort()
        return

    response = None
    source_url = route.request.url
    try:
        async with _image_request_gate:
            response = await route.fetch(
                url=_bounded_image_url(source_url),
                timeout=TAGFETCH_RENDER_IMAGE_TIMEOUT * 1000,
                max_redirects=3,
            )
            content_type = response.headers.get("content-type", "")
            raw_length = response.headers.get("content-length", "")
            try:
                content_length = int(raw_length)
            except (TypeError, ValueError):
                content_length = 0
            if (
                not response.ok
                or not content_type.casefold().startswith("image/")
                or content_length > TAGFETCH_RENDER_IMAGE_MAX_BYTES
            ):
                raise RuntimeError(
                    f"unsafe image response status={response.status} "
                    f"content_type={content_type!r} bytes={content_length}"
                )
            await route.fulfill(response=response)
    except Exception as exc:  # noqa: BLE001 - one image degrades independently
        parts = urlsplit(source_url)
        label = f"{parts.netloc}{parts.path}"[:160]
        reason = str(exc) if isinstance(exc, RuntimeError) else type(exc).__name__
        logger.warning(
            "[TagfetchRender] remote image degraded source={} reason={}",
            label,
            reason,
        )
        await _fulfill_image_fallback(route)
    finally:
        if response is not None:
            try:
                await response.dispose()
            except Exception:  # noqa: BLE001 - response may already be gone
                pass


async def _close_browser(reason: str) -> None:
    global _browser, _playwright, _browser_uses
    async with _browser_lock:
        browser = _browser
        playwright = _playwright
        _browser = None
        _playwright = None
        _browser_uses = 0

    if browser is not None or playwright is not None:
        logger.info("[TagfetchRender] releasing browser reason={}", reason)
    if browser is not None:
        try:
            await asyncio.wait_for(
                browser.close(),
                timeout=_BROWSER_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("[TagfetchRender] browser close timed out")
        except Exception as exc:  # noqa: BLE001 - browser may be disconnected
            logger.warning("[TagfetchRender] browser close failed error={}", exc)
    if playwright is not None:
        try:
            await asyncio.wait_for(
                playwright.stop(),
                timeout=_BROWSER_CLOSE_TIMEOUT_SECONDS,
            )
        except asyncio.TimeoutError:
            logger.warning("[TagfetchRender] Playwright stop timed out")
        except Exception as exc:  # noqa: BLE001 - driver may already be gone
            logger.warning("[TagfetchRender] Playwright stop failed error={}", exc)


async def _get_browser():
    global _browser, _playwright, _browser_uses
    async with _browser_lock:
        try:
            healthy = _browser is not None and _browser.is_connected()
        except Exception:  # noqa: BLE001 - browser health probe
            healthy = False
        if healthy:
            return _browser

        stale_browser = _browser
        stale_playwright = _playwright
        _browser = None
        _playwright = None
        _browser_uses = 0
        if stale_browser is not None:
            try:
                await stale_browser.close()
            except Exception as exc:  # noqa: BLE001 - stale browser cleanup
                logger.debug(
                    "[TagfetchRender] stale browser close failed error={}", exc
                )
        if stale_playwright is not None:
            try:
                await stale_playwright.stop()
            except Exception as exc:  # noqa: BLE001 - stale driver cleanup
                logger.debug(
                    "[TagfetchRender] stale Playwright stop failed error={}", exc
                )

        from playwright.async_api import async_playwright

        playwright = await async_playwright().start()
        try:
            browser = await playwright.chromium.launch(
                headless=True,
                args=[
                    "--no-sandbox",
                    "--disable-setuid-sandbox",
                    "--disable-gpu",
                    "--disable-dev-shm-usage",
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-breakpad",
                    "--disable-component-update",
                    "--disable-sync",
                    "--renderer-process-limit=1",
                    "--disk-cache-size=1",
                    "--media-cache-size=1",
                    "--no-first-run",
                    "--no-default-browser-check",
                    "--mute-audio",
                ],
            )
        except Exception:
            await playwright.stop()
            raise
        _browser = browser
        _playwright = playwright
        logger.info("[TagfetchRender] Chromium started")
        return browser


async def _render_once(html: str, output_path: Path, width: int) -> None:
    browser = await _get_browser()
    context = None
    try:
        context = await browser.new_context(
            viewport={"width": width, "height": 900},
            device_scale_factor=1,
            java_script_enabled=False,
            service_workers="block",
        )
        page = await context.new_page()
        await page.route("**/*", _allow_card_image_only)
        await page.set_content(
            html,
            wait_until="networkidle",
            timeout=TAGFETCH_RENDER_TIMEOUT * 1000,
        )
        set_viewport_size = getattr(page, "set_viewport_size", None)
        if set_viewport_size is not None:
            await set_viewport_size({"width": width, "height": 1})
        await page.screenshot(
            path=str(output_path),
            full_page=True,
            type="jpeg",
            quality=TAGFETCH_RENDER_JPEG_QUALITY,
            animations="disabled",
        )
    finally:
        if context is not None:
            try:
                await asyncio.wait_for(
                    context.close(),
                    timeout=_CONTEXT_CLOSE_TIMEOUT_SECONDS,
                )
            except asyncio.TimeoutError:
                logger.warning("[TagfetchRender] browser context close timed out")
            except Exception as exc:  # noqa: BLE001 - context may be disconnected
                logger.debug(
                    "[TagfetchRender] browser context close failed error={}", exc
                )


async def _html_to_image(html: str, output_path: Path, width: int = 650) -> None:
    global _browser_uses
    async with _render_gate:
        try:
            await asyncio.wait_for(
                _render_once(html, output_path, width),
                timeout=TAGFETCH_RENDER_TIMEOUT,
            )
        except asyncio.TimeoutError as exc:
            await _close_browser("render_timeout")
            raise RuntimeError(
                f"card rendering exceeded {TAGFETCH_RENDER_TIMEOUT}s"
            ) from exc
        except Exception:
            await _close_browser("render_error")
            raise

        _browser_uses += 1
        if _browser_uses >= TAGFETCH_RENDER_BROWSER_MAX_USES:
            await _close_browser("periodic_recycle")


def _render_conversation_html(conversation: TweetConversation) -> str:
    template = _env.get_template("conversation.html")
    return template.render(
        target=conversation.target,
        ancestors=conversation.ancestors,
        quote=conversation.quote,
    )


async def render_conversation_card(conversation: TweetConversation) -> list[Path]:
    CARD_DIR.mkdir(parents=True, exist_ok=True)
    if conversation.target is None:
        logger.warning("[TagfetchRender] missing target")
        return []
    try:
        path = CARD_DIR / f"{conversation.target.id}.jpg"
        await _html_to_image(_render_conversation_html(conversation), path)
        logger.info("[TagfetchRender] card ready path={}", path)
        return [path]
    except Exception:  # noqa: BLE001 - caller skips a failed candidate
        logger.exception("[TagfetchRender] card render failed")
        return []


async def shutdown() -> None:
    async with _render_gate:
        await _close_browser("shutdown")
