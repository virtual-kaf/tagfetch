"""Jinja2 and Playwright renderer for Tagfetch conversation cards."""

from __future__ import annotations

import asyncio
from pathlib import Path
from typing import Any

from jinja2 import Environment, FileSystemLoader, select_autoescape
from nonebot import logger

from ..config import (
    CARD_DIR,
    TAGFETCH_RENDER_BROWSER_MAX_USES,
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
_browser_uses = 0


async def _allow_card_image_only(route) -> None:
    if route.request.resource_type == "image":
        await route.continue_()
    else:
        await route.abort()


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
            await browser.close()
        except Exception as exc:  # noqa: BLE001 - browser may be disconnected
            logger.warning("[TagfetchRender] browser close failed error={}", exc)
    if playwright is not None:
        try:
            await playwright.stop()
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
                    "--disable-extensions",
                    "--disable-background-networking",
                    "--disable-sync",
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
    page = None
    try:
        page = await browser.new_page(
            viewport={"width": width, "height": 900},
            device_scale_factor=1,
            java_script_enabled=False,
        )
        await page.route("**/*", _allow_card_image_only)
        await page.set_content(
            html,
            wait_until="networkidle",
            timeout=TAGFETCH_RENDER_TIMEOUT * 1000,
        )
        set_viewport_size = getattr(page, "set_viewport_size", None)
        if set_viewport_size is not None:
            await set_viewport_size({"width": width, "height": 1})
        await page.screenshot(path=str(output_path), full_page=True, type="png")
    finally:
        if page is not None:
            try:
                await page.close()
            except Exception as exc:  # noqa: BLE001 - page may be disconnected
                logger.debug("[TagfetchRender] page close failed error={}", exc)


async def _html_to_png(html: str, output_path: Path, width: int = 650) -> None:
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
        path = CARD_DIR / f"{conversation.target.id}.png"
        await _html_to_png(_render_conversation_html(conversation), path)
        logger.info("[TagfetchRender] card ready path={}", path)
        return [path]
    except Exception:  # noqa: BLE001 - caller skips a failed candidate
        logger.exception("[TagfetchRender] card render failed")
        return []


async def shutdown() -> None:
    async with _render_gate:
        await _close_browser("shutdown")
