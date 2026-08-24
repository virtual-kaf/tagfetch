"""Standalone authenticated HTTP service for remote Grok hashtag discovery."""

from __future__ import annotations

import hmac
import ipaddress
import os
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from aiohttp import web
from nonebot import logger

POLL_PATH = "/api/tagfetch/poll"
Fetcher = Callable[..., Awaitable[list[dict[str, str]]]]


class RemoteServiceConfigError(RuntimeError):
    pass


@dataclass(frozen=True)
class RemoteServiceSettings:
    enabled: bool
    host: str
    port: int
    token: str

    @classmethod
    def from_raw(
        cls, *, enabled: str, host: str, port: str, token: str
    ) -> RemoteServiceSettings:
        normalized = enabled.strip().casefold()
        if normalized in {"1", "true", "yes", "on"}:
            parsed_enabled = True
        elif normalized in {"0", "false", "no", "off"}:
            parsed_enabled = False
        else:
            raise RemoteServiceConfigError(
                "KABUBU_TAGFETCH_REMOTE_ENABLED must be true or false"
            )
        if not parsed_enabled:
            raise RemoteServiceConfigError("tagfetch remote service is disabled")
        try:
            address = ipaddress.ip_address(host.strip())
            parsed_port = int(port)
        except ValueError as exc:
            raise RemoteServiceConfigError("invalid remote host or port") from exc
        if not 1 <= parsed_port <= 65535:
            raise RemoteServiceConfigError("remote port must be between 1 and 65535")
        normalized_token = token.strip()
        if not normalized_token:
            raise RemoteServiceConfigError("remote token is required")
        return cls(True, str(address), parsed_port, normalized_token)


SETTINGS_KEY = web.AppKey("tagfetch_remote_settings", RemoteServiceSettings)
FETCHER_KEY = web.AppKey("tagfetch_remote_fetcher", object)


def _project_root() -> Path:
    package_root = Path(__file__).resolve().parents[3]
    cwd = Path.cwd().resolve()
    return cwd if (cwd / ".env").is_file() else package_root


def load_remote_settings() -> RemoteServiceSettings:
    from nonebot.config import Config, Env

    root = _project_root()
    env_files: list[Path] = []
    base_env = root / ".env"
    if base_env.is_file():
        environment = Env(_env_file=base_env).environment
        env_files.append(base_env)
        selected = root / f".env.{environment}"
        if selected.is_file():
            env_files.append(selected)
    loaded = Config(_env_file=tuple(env_files), driver="~none")

    def read(name: str, default: str) -> str:
        value = os.getenv(name)
        return (
            value if value is not None else str(getattr(loaded, name.lower(), default))
        )

    return RemoteServiceSettings.from_raw(
        enabled=read("KABUBU_TAGFETCH_REMOTE_ENABLED", "false"),
        host=read("KABUBU_TAGFETCH_REMOTE_HOST", "100.98.44.83"),
        port=read("KABUBU_TAGFETCH_REMOTE_PORT", "8766"),
        token=read("KABUBU_TAGFETCH_REMOTE_TOKEN", ""),
    )


def _json_response(
    *,
    status: int,
    ok: bool,
    posts: list[dict[str, str]] | None = None,
    error: str | None = None,
) -> web.Response:
    return web.json_response(
        {
            "ok": ok,
            "posts": posts or [],
            "source": "grok" if ok else None,
            "error": error,
        },
        status=status,
    )


def _authorized(header: str | None, expected: str) -> bool:
    if not header:
        return False
    scheme, separator, token = header.partition(" ")
    return bool(
        separator
        and scheme.casefold() == "bearer"
        and token
        and hmac.compare_digest(token, expected)
    )


async def _poll(request: web.Request) -> web.Response:
    settings = request.app[SETTINGS_KEY]
    logger.info(
        "[TagfetchRemote] poll received peer={} content_length={}",
        request.remote or "unknown",
        request.content_length,
    )
    if not _authorized(request.headers.get("Authorization"), settings.token):
        logger.warning(
            "[TagfetchRemote] poll rejected reason=unauthorized peer={}",
            request.remote or "unknown",
        )
        response = _json_response(status=401, ok=False, error="unauthorized")
        response.headers["WWW-Authenticate"] = "Bearer"
        return response
    try:
        body: Any = await request.json()
    except ValueError:
        return _json_response(status=400, ok=False, error="invalid_request")
    if not isinstance(body, dict):
        return _json_response(status=400, ok=False, error="invalid_request")

    from .clients.grok import GrokDiscoveryError
    from .remote_fetch import (
        InvalidTagRequest,
        normalize_requested_tags,
        validate_query_options,
    )

    try:
        validate_query_options(body)
        tags = normalize_requested_tags(body.get("tags"))
        logger.info(
            "[TagfetchRemote] request validated tags={} lookback_hours={}",
            len(tags),
            body["lookback_hours"],
        )
        fetcher = cast(Fetcher, request.app[FETCHER_KEY])
        posts = await fetcher(
            tags,
            min_likes=body["min_likes"],
            lookback_hours=body["lookback_hours"],
            limit_per_tag=body["limit_per_tag"],
        )
    except (InvalidTagRequest, KeyError):
        return _json_response(status=400, ok=False, error="invalid_request")
    except GrokDiscoveryError as exc:
        logger.warning("[TagfetchRemote] Grok poll failed: {}", exc.reason)
        return _json_response(status=502, ok=False, error="grok_failed")
    except Exception:  # noqa: BLE001 - HTTP boundary must return a closed error
        logger.exception("[TagfetchRemote] unexpected poll failure")
        return _json_response(status=500, ok=False, error="internal_error")
    logger.info("[TagfetchRemote] poll succeeded posts={}", len(posts))
    return _json_response(status=200, ok=True, posts=posts)


def create_app(
    settings: RemoteServiceSettings, *, fetcher: Fetcher | None = None
) -> web.Application:
    if fetcher is None:
        from .remote_fetch import fetch_latest_urls

        fetcher = fetch_latest_urls
    app = web.Application(client_max_size=64 * 1024)
    app[SETTINGS_KEY] = settings
    app[FETCHER_KEY] = fetcher
    app.router.add_post(POLL_PATH, _poll)
    return app


def main() -> None:
    try:
        settings = load_remote_settings()
    except RemoteServiceConfigError as exc:
        logger.error("[TagfetchRemote] refusing to start: {}", exc)
        raise SystemExit(2) from exc
    logger.info(
        "[TagfetchRemote] listening on http://{}:{}{}",
        settings.host,
        settings.port,
        POLL_PATH,
    )
    web.run_app(create_app(settings), host=settings.host, port=settings.port)


if __name__ == "__main__":
    main()
