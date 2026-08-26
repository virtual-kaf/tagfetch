from pathlib import Path

import pytest
from nonebot_plugin_tagfetch.models import PreparedCandidate
from nonebot_plugin_tagfetch.models.tweet import (
    TweetAuthor,
    TweetConversation,
    TweetItem,
)
from nonebot_plugin_tagfetch.renderer import engine, pillow_worker
from nonebot_plugin_tagfetch.services import broadcaster
from PIL import Image, ImageFont


def _pango_runtime_available() -> bool:
    try:
        import cairo  # noqa: F401
        import gi

        gi.require_version("Pango", "1.0")
        gi.require_version("PangoCairo", "1.0")
        from gi.repository import Pango, PangoCairo  # noqa: F401
    except (ImportError, ValueError, OSError):
        return False
    return True


requires_pango = pytest.mark.skipif(
    not _pango_runtime_available(),
    reason="PangoCairo/fontconfig integration tests require the target Linux runtime",
)


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


@requires_pango
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


def test_tagfetch_render_spec_delegates_fallback_to_pango(tmp_path):
    spec = engine._base_spec("conversation", tmp_path / "card.jpg", {})

    assert spec["font_path"] == str(engine.FONT_PATH)
    assert "fallback_font_paths" not in spec
    assert not hasattr(engine, "FALLBACK_FONT_PATHS")

    renderer_source = Path(pillow_worker.__file__).read_text(encoding="utf-8")
    engine_source = Path(engine.__file__).read_text(encoding="utf-8")
    for category_font in (
        "NotoSansMath-Regular.ttf",
        "NotoSansGujarati-Regular.ttf",
        "NotoSerifTibetan-Regular.ttf",
        "NotoSansGeorgian-Regular.ttf",
    ):
        assert category_font not in renderer_source
        assert category_font not in engine_source


def test_tagfetch_twemoji_fallback_keeps_complete_graphemes():
    values = engine._fallback_emoji_list(
        "release * 6 of 9; *️⃣ 6️⃣ 9️⃣ 👨🏽‍💻 🏳️‍🌈 🇨🇳"
    )

    assert "*" not in values
    assert "6" not in values
    assert "9" not in values
    assert {"*️⃣", "6️⃣", "9️⃣", "👨🏽‍💻", "🏳️‍🌈", "🇨🇳"} <= set(values)
    assert engine._twemoji_url("*️⃣").endswith("/2a-20e3.png")
    assert engine._twemoji_url("❤️").endswith("/2764.png")
    assert engine._twemoji_url("🏳️‍🌈").endswith(
        "/1f3f3-fe0f-200d-1f308.png"
    )


def test_tagfetch_renderer_size_budget_fails_before_allocation(capsys):
    with pytest.raises(pillow_worker.RenderSizeError, match="MAX_CANVAS_HEIGHT"):
        pillow_worker._validate_size(
            "test", "too-tall", 800, pillow_worker.MAX_CANVAS_HEIGHT + 1, 4
        )
    with pytest.raises(pillow_worker.RenderSizeError, match="MAX_CANVAS_PIXELS"):
        pillow_worker._validate_size(
            "test", "too-many-pixels", pillow_worker.MAX_CANVAS_PIXELS + 1, 1, 4
        )

    diagnostics = capsys.readouterr().err
    assert "width=800" in diagnostics
    assert "estimated_bytes=" in diagnostics
    assert pillow_worker._pixels_to_pango(688, 1024) == 688 * 1024
    assert pillow_worker._pango_to_pixels(688 * 1024, 1024) == 688

    class OutOfMemoryImage:
        @staticmethod
        def new(_mode, _size, _color):
            raise MemoryError

    with pytest.raises(pillow_worker.RenderSizeError, match="Pillow allocation"):
        pillow_worker._new_pillow_image(
            OutOfMemoryImage, "test.image", "RGB", (800, 100), "white"
        )


@requires_pango
def test_tagfetch_pango_fontconfig_covers_mixed_scripts():
    backend = pillow_worker._PangoTextBackend(
        engine.FONT_PATH, set(), Image, ImageFont
    )
    sample = "中文 Devanagari देवनागरी ગુજરાતી བོད་ཡིག ქართული ∑√∞ العربية"

    assert backend.unknown_glyphs(sample) == 0


@requires_pango
def test_tagfetch_pango_wrap_width_uses_pango_units_once():
    backend = pillow_worker._PangoTextBackend(
        engine.FONT_PATH, set(), Image, ImageFont
    )
    layout, _prepared, _slots = backend._layout(
        "Pango width 中文 العربية " * 20,
        21,
        688,
        "test.wrap-units",
    )

    assert layout.get_width() == 688 * backend._pango_scale
    pixel_width, pixel_height = layout.get_pixel_size()
    assert 0 < pixel_width <= 688
    assert 0 < pixel_height < pillow_worker.MAX_CANVAS_HEIGHT


@requires_pango
@pytest.mark.asyncio
async def test_tagfetch_pango_renders_mixed_scripts_as_jpeg(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(engine, "TEMP_DIR", tmp_path / "worker-temp")
    spec = _worker_spec(tmp_path)
    spec["output_path"] = str(tmp_path / "pango-mixed.jpg")
    spec["target"]["name"] = "明透 ქართული"
    spec["target"]["text"] = (
        "देवनागरी ગુજરાતી བོད་ཡིག ქართული ∑√∞ العربية"
    )

    output_path = (await engine._run_worker(spec))[0]

    with Image.open(output_path) as rendered:
        assert rendered.format == "JPEG"
        assert rendered.width == 800
        assert rendered.height <= spec["max_height"]


def test_tagfetch_alinux3_installer_pins_runtime_and_font_packages():
    source = (
        Path(__file__).parents[1]
        / "tools"
        / "install_alinux3_renderer_deps.sh"
    ).read_text(encoding="utf-8")

    assert "pycairo==1.29.0" in source
    assert "PyGObject==3.44.2" in source
    for package in (
        "pango",
        "cairo-gobject",
        "gobject-introspection",
        "google-noto-sans-devanagari-fonts",
        "google-noto-sans-gujarati-fonts",
        "google-noto-sans-tibetan-fonts",
        "google-noto-sans-georgian-fonts",
        "google-noto-sans-symbols-fonts",
        "stix-math-fonts",
    ):
        assert package in source
