from datetime import datetime, timezone

import httpx
import pytest

from nonebot_plugin_tagfetch.clients import discovery, twitterapi_io
from nonebot_plugin_tagfetch.clients.remote_grok import RemoteDiscoveryError
from nonebot_plugin_tagfetch.models import DiscoveredPost
from nonebot_plugin_tagfetch.storage import (
    get_remote_discovery_failures,
    record_remote_discovery_failure,
    reset_remote_discovery_failures,
)


def test_remote_failure_counter_is_persistent(tmp_path):
    database = tmp_path / "state.sqlite3"

    assert get_remote_discovery_failures(path=database) == 0
    assert record_remote_discovery_failure(path=database) == 1
    assert record_remote_discovery_failure(path=database) == 2
    reset_remote_discovery_failures(path=database)
    assert get_remote_discovery_failures(path=database) == 0


def test_fallback_window_is_0800_through_2359_cst():
    assert not discovery._fallback_window_open(
        datetime(2026, 8, 27, 23, 59, tzinfo=timezone.utc)
    )  # 07:59 CST
    assert discovery._fallback_window_open(
        datetime(2026, 8, 28, 0, tzinfo=timezone.utc)
    )  # 08:00 CST
    assert discovery._fallback_window_open(
        datetime(2026, 8, 28, 15, 59, tzinfo=timezone.utc)
    )  # 23:59 CST
    assert not discovery._fallback_window_open(
        datetime(2026, 8, 28, 16, tzinfo=timezone.utc)
    )  # 00:00 CST


@pytest.mark.asyncio
async def test_third_remote_failure_uses_one_fallback_and_resets(monkeypatch):
    failures = 0
    fallback_calls = []

    async def remote(_tags):
        raise RemoteDiscoveryError("remote_request_failed")

    async def fallback(tags, *, now):
        fallback_calls.append((tags, now))
        return [DiscoveredPost("art", "https://x.com/user/status/1", "1")]

    def increment():
        nonlocal failures
        failures += 1
        return failures

    def reset():
        nonlocal failures
        failures = 0

    monkeypatch.setattr(discovery, "remote_fetch_urls", remote)
    monkeypatch.setattr(discovery, "twitterapi_fetch_urls", fallback)
    monkeypatch.setattr(discovery, "record_remote_discovery_failure", increment)
    monkeypatch.setattr(discovery, "reset_remote_discovery_failures", reset)
    now = datetime(2026, 8, 28, 4, tzinfo=timezone.utc)  # 12:00 CST

    with pytest.raises(RemoteDiscoveryError):
        await discovery.fetch_discovered_posts(("art",), now=now)
    with pytest.raises(RemoteDiscoveryError):
        await discovery.fetch_discovered_posts(("art",), now=now)
    posts = await discovery.fetch_discovered_posts(("art",), now=now)

    assert [post.tweet_id for post in posts] == ["1"]
    assert len(fallback_calls) == 1
    assert failures == 0


@pytest.mark.asyncio
async def test_fallback_waits_until_cst_window_opens(monkeypatch):
    failures = 2
    fallback_calls = 0

    async def remote(_tags):
        raise RemoteDiscoveryError("http_502", status_code=502)

    async def fallback(_tags, *, now):
        nonlocal fallback_calls
        fallback_calls += 1
        return []

    def increment():
        nonlocal failures
        failures += 1
        return failures

    def reset():
        nonlocal failures
        failures = 0

    monkeypatch.setattr(discovery, "remote_fetch_urls", remote)
    monkeypatch.setattr(discovery, "twitterapi_fetch_urls", fallback)
    monkeypatch.setattr(discovery, "record_remote_discovery_failure", increment)
    monkeypatch.setattr(discovery, "reset_remote_discovery_failures", reset)

    with pytest.raises(RemoteDiscoveryError):
        await discovery.fetch_discovered_posts(
            ("art",), now=datetime(2026, 8, 27, 23, 59, tzinfo=timezone.utc)
        )  # 07:59 CST
    assert failures == 3

    await discovery.fetch_discovered_posts(
        ("art",), now=datetime(2026, 8, 28, 0, tzinfo=timezone.utc)
    )  # 08:00 CST
    assert fallback_calls == 1
    assert failures == 0


@pytest.mark.asyncio
async def test_auth_error_does_not_trigger_fallback(monkeypatch):
    resets = 0

    async def remote(_tags):
        raise RemoteDiscoveryError("http_401", status_code=401)

    async def fallback(_tags, *, now):
        raise AssertionError("fallback must not run")

    def reset():
        nonlocal resets
        resets += 1

    monkeypatch.setattr(discovery, "remote_fetch_urls", remote)
    monkeypatch.setattr(discovery, "twitterapi_fetch_urls", fallback)
    monkeypatch.setattr(discovery, "reset_remote_discovery_failures", reset)

    with pytest.raises(RemoteDiscoveryError, match="http_401"):
        await discovery.fetch_discovered_posts(
            ("art",), now=datetime(2026, 8, 28, 12, tzinfo=timezone.utc)
        )
    assert resets == 1


@pytest.mark.asyncio
async def test_twitterapi_request_and_response_contract(monkeypatch):
    seen_request = None

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal seen_request
        seen_request = request
        return httpx.Response(
            200,
            json={
                "tweets": [
                    {
                        "id": "123",
                        "url": "https://twitter.com/artist/status/123",
                        "author": {"userName": "artist"},
                        "text": "new work #Art",
                        "likeCount": 350,
                        "entities": {"hashtags": [{"text": "Art"}]},
                    },
                    {
                        "id": "124",
                        "url": "https://x.com/artist/status/124",
                        "author": {"userName": "artist"},
                        "text": "not popular #Art",
                        "likeCount": 299,
                    },
                ],
                "has_next_page": False,
                "next_cursor": "",
            },
        )

    real_client = httpx.AsyncClient
    transport = httpx.MockTransport(handler)
    monkeypatch.setattr(twitterapi_io, "TWITTERAPI_IO_API_KEY", "secret")
    monkeypatch.setattr(
        twitterapi_io.httpx,
        "AsyncClient",
        lambda **_kwargs: real_client(transport=transport),
    )
    now = datetime(2026, 8, 28, 4, tzinfo=timezone.utc)

    posts = await twitterapi_io.twitterapi_fetch_urls(("Art",), now=now)

    assert [post.tweet_id for post in posts] == ["123"]
    assert posts[0].url == "https://x.com/artist/status/123"
    assert seen_request is not None
    assert seen_request.headers["X-API-Key"] == "secret"
    assert seen_request.url.params["queryType"] == "Latest"
    assert "#Art" in seen_request.url.params["query"]
    assert "min_faves:300" in seen_request.url.params["query"]
    assert "since_time:" in seen_request.url.params["query"]
    assert "until_time:" in seen_request.url.params["query"]
