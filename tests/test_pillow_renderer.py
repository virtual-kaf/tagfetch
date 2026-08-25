from pathlib import Path

import pytest
from nonebot_plugin_tagfetch.models import PreparedCandidate
from nonebot_plugin_tagfetch.models.tweet import (
    TweetAuthor,
    TweetConversation,
    TweetItem,
)
from nonebot_plugin_tagfetch.renderer import engine
from nonebot_plugin_tagfetch.services import broadcaster
from PIL import Image


def _tweet(tweet_id: str, text: str = "原文") -> TweetItem:
    return TweetItem(
        id=tweet_id,
        url=f"https://x.com/test/status/{tweet_id}",
        author=TweetAuthor(name="作者", screen_name="author"),
        text=text,
        translated_text="不应该出现在 Tagfetch 卡片中",
    )


def _candidate(tweet_id: str) -> PreparedCandidate:
    return PreparedCandidate(
        tweet_id=tweet_id,
        url=f"https://x.com/test/status/{tweet_id}",
        conversation=TweetConversation(target=_tweet(tweet_id)),
    )


def _worker_spec(tmp_path: Path, max_height: int = 4096) -> dict:
    target = _tweet("tagfetch_target", "長い日本語の投稿です。" * 180)
    return {
        "kind": "conversation",
        "output_path": str(tmp_path / "card.jpg"),
        "format": "JPEG",
        "quality": 76,
        "font_path": str(engine.FONT_PATH),
        "emoji_paths": {},
        "memory_limit_mb": 384,
        "max_image_pixels": 12_000_000,
        "max_height": max_height,
        "ancestors": [],
        "target": engine._tweet_spec(target, {}),
        "quote": None,
    }


@pytest.mark.asyncio
async def test_tagfetch_worker_outputs_paginated_jpeg(tmp_path, monkeypatch):
    monkeypatch.setattr(engine, "TEMP_DIR", tmp_path / "worker-temp")

    paths = await engine._run_worker(_worker_spec(tmp_path, max_height=900))

    assert len(paths) >= 2
    for path in paths:
        with Image.open(path) as image:
            assert image.format == "JPEG"
            assert image.width == 800
            assert image.height <= 900


@pytest.mark.asyncio
async def test_tagfetch_renderer_never_passes_translation_to_worker(
    monkeypatch, tmp_path
):
    captured = {}

    async def resolve(_urls, _texts):
        return {}, {}

    async def run(spec):
        captured.update(spec)
        output = tmp_path / "card.jpg"
        Image.new("RGB", (20, 20)).save(output, format="JPEG")
        return [output]

    monkeypatch.setattr(engine, "CARD_DIR", tmp_path)
    monkeypatch.setattr(engine, "_resolve_assets", resolve)
    monkeypatch.setattr(engine, "_run_worker", run)

    paths = await engine.render_conversation_card(
        TweetConversation(target=_tweet("target"), quote=_tweet("quote"))
    )

    assert paths
    assert captured["target"]["translated_text"] == ""
    assert captured["quote"]["translated_text"] == ""


@pytest.mark.asyncio
async def test_direct_send_delivers_every_page(monkeypatch, tmp_path):
    pages = [tmp_path / "one.jpg", tmp_path / "two.jpg"]
    for page in pages:
        page.write_bytes(b"image")

    class Bot:
        self_id = "123"

        async def call_api(self, api, **kwargs):
            calls.append((api, kwargs))

    calls = []
    candidate = _candidate("tweet")
    monkeypatch.setattr(
        broadcaster, "image_segment_from_path", lambda path: Path(path).name
    )

    delivered = await broadcaster._send_cards(
        Bot(), "100", [candidate], {"tweet": pages}
    )

    assert delivered == [candidate]
    assert [call[1]["message"] for call in calls] == ["one.jpg", "two.jpg"]


@pytest.mark.asyncio
async def test_forward_send_flattens_pages_and_accounts_complete_candidates(
    monkeypatch, tmp_path
):
    first = _candidate("first")
    second = _candidate("second")
    cards = {
        "first": [tmp_path / "first-1.jpg", tmp_path / "first-2.jpg"],
        "second": [tmp_path / "second-1.jpg"],
    }
    for paths in cards.values():
        for path in paths:
            path.write_bytes(b"image")

    class Bot:
        self_id = "123"

        async def call_api(self, api, **kwargs):
            calls.append((api, kwargs))

    async def card_node(path, candidate, _bot_id):
        return {
            "type": "node",
            "data": {"content": path.name, "name": candidate.tweet_id},
        }

    calls = []
    monkeypatch.setattr(broadcaster, "_card_node", card_node)

    delivered = await broadcaster._send_cards(Bot(), "100", [first, second], cards)

    assert delivered == [first, second]
    nodes = [node for _, call in calls for node in call["messages"]]
    assert [node["data"]["content"] for node in nodes] == [
        "first-1.jpg",
        "first-2.jpg",
        "second-1.jpg",
    ]


def test_tagfetch_renderer_is_browser_free_and_independent():
    source = Path(engine.__file__).read_text(encoding="utf-8").casefold()
    assert "playwright" not in source
    assert "jinja" not in source
    assert "nonebot_plugin_xfetch" not in source
