import math
from pathlib import Path

import pytest
from nonebot_plugin_tagfetch.models import DownloadedImage, PreparedCandidate
from nonebot_plugin_tagfetch.models.tweet import (
    TweetAuthor,
    TweetConversation,
    TweetItem,
)
from nonebot_plugin_tagfetch.renderer import engine, pillow_worker
from nonebot_plugin_tagfetch.services import broadcaster
from PIL import Image, ImageDraw, ImageFont


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


def _hybrid_backend(known_emojis: set[str] | None = None):
    return pillow_worker._HybridTextBackend(
        engine.FONT_PATH, known_emojis or set(), Image, ImageFont
    )


class _FakePangoBackend:
    def __init__(self) -> None:
        self.grapheme_calls = 0
        self.cluster_calls = 0
        self.metric_calls = 0

    def graphemes(self, text: str) -> list[str]:
        self.grapheme_calls += 1
        return pillow_worker._fallback_graphemes(text)

    def run_metrics(self, text: str, size: int, _component: str):
        self.metric_calls += 1
        advance = len(pillow_worker._fallback_graphemes(text)) * size * 0.7
        return advance, (0.0, -size * 0.8, advance, size * 0.2)

    def cluster_advances(
        self, clusters: list[str], size: int, _component: str
    ) -> list[float]:
        self.cluster_calls += 1
        return [size * 0.7] * len(clusters)


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


@pytest.mark.asyncio
async def test_renderer_failure_fallback_forwards_prepared_text(monkeypatch):
    candidate = _candidate("fallback")
    candidate.conversation.target.translated_text = "翻译"
    candidate.conversation.quote = _tweet("quote", "引用")

    class Bot:
        self_id = "123"

        async def call_api(self, api, **kwargs):
            calls.append((api, kwargs))

    calls = []
    assert await broadcaster._send_renderer_fallback(Bot(), "100", candidate)
    assert [api for api, _kwargs in calls] == ["send_group_forward_msg"]
    nodes = calls[0][1]["messages"]
    assert [node["data"]["name"] for node in nodes] == ["@author", "@author"]
    assert "原文" in nodes[0]["data"]["content"]
    assert "翻译" in nodes[0]["data"]["content"]
    assert "引用" in nodes[1]["data"]["content"]


@pytest.mark.asyncio
async def test_renderer_failure_fallback_skips_one_unavailable_original(tmp_path):
    candidate = _candidate("fallback")
    candidate.originals = [
        DownloadedImage(
            source_url="https://pbs.twimg.com/media/missing.jpg",
            data=b"",
            mime_type="image/jpeg",
            source_tweet_id="fallback",
            author_handle="author",
            media_index=0,
            local_path=tmp_path / "missing.jpg",
        )
    ]

    nodes = await broadcaster._fallback_nodes(candidate, "123")

    assert len(nodes) == 1
    assert "原文" in nodes[0]["data"]["content"]


@pytest.mark.asyncio
async def test_renderer_failure_fallback_forward_failure_is_unsent():
    candidate = _candidate("fallback")

    class Bot:
        self_id = "123"

        async def call_api(self, _api, **_kwargs):
            raise RuntimeError("forward unavailable")

    assert not await broadcaster._send_renderer_fallback(Bot(), "100", candidate)


@pytest.mark.asyncio
async def test_renderer_failure_fallback_records_delivery_only_on_success(monkeypatch):
    candidate = _candidate("fallback")
    records = []

    async def render(_conversation):
        raise RuntimeError("renderer unavailable")

    async def shutdown():
        return None

    class Bot:
        self_id = "123"

        async def call_api(self, _api, **_kwargs):
            return None

    monkeypatch.setattr(broadcaster, "render_conversation_card", render)
    monkeypatch.setattr(broadcaster, "shutdown_renderer", shutdown)
    monkeypatch.setattr(broadcaster, "has_delivery", lambda *_args: False)
    monkeypatch.setattr(
        broadcaster,
        "record_card_delivery",
        lambda tweet_id, group_id, **kwargs: records.append(
            (tweet_id, group_id, kwargs["originals_sent"])
        ),
    )

    await broadcaster._broadcast_to_groups(Bot(), [candidate], ["100"])

    assert records == [("fallback", "100", True)]


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


def test_tagfetch_stats_row_uses_twitter_style_outline_icons():
    image = Image.new("L", (92, 24), 0)
    draw = ImageDraw.Draw(image)

    for index, kind in enumerate(("reply", "repost", "like", "views")):
        pillow_worker._draw_stats_icon(draw, kind, (index * 23, 1), 255)
        assert image.crop((index * 23, 0, index * 23 + 22, 24)).getbbox()

    source = Path(pillow_worker.__file__).read_text(encoding="utf-8")
    for old_icon in ('("💬"', '("🔁"', '("❤️"', '("👁"'):
        assert old_icon not in source


def test_tagfetch_hybrid_fast_path_matches_original_pillow_metrics_and_wrap():
    backend = _hybrid_backend()
    text_font = backend.font(21)
    sample = "普通中文 English 日本語 mixed text"
    max_width = 150

    expected_lines = []
    for paragraph in sample.replace("\t", " ").split("\n"):
        current = []
        current_width = 0
        for value in backend.tokens(paragraph):
            width = backend.token_width(value, text_font)
            if current and current_width + width > max_width:
                expected_lines.append(current)
                current = []
                current_width = 0
            current.append(value)
            current_width += width
        if current:
            expected_lines.append(current)

    assert backend.primary_covers(sample)
    assert backend.measure(sample, 21) == text_font.primary.getlength(sample)
    assert backend.bbox(sample, 21) == text_font.primary.getbbox(sample)
    assert backend.wrap(sample, text_font, max_width) == expected_lines
    assert not backend.pango_initialized


def test_tagfetch_hybrid_twemoji_run_does_not_initialize_pango():
    backend = _hybrid_backend({"❤️"})

    line = backend.layout_line("中文 ❤️ English", 21)

    assert [run.renderer for run in line.runs] == ["pillow", "emoji", "pillow"]
    assert not backend.pango_initialized


def test_tagfetch_hybrid_mixed_layout_can_run_without_local_pango():
    backend = _hybrid_backend()
    backend._pango_backend = _FakePangoBackend()
    text_font = backend.font(24)

    line = backend.layout_line("中文 कि देवनागरी English", 24)
    wrapped = backend.wrap(
        "中文 कि देवनागरी English العربية end", text_font, 220
    )

    run_kinds = [run.renderer for run in line.runs]
    assert run_kinds[0] == "pillow"
    assert "pango" in run_kinds
    assert run_kinds[-1] == "pillow"
    assert any("कि" in run.text for run in line.runs if run.renderer == "pango")
    assert all(isinstance(value, pillow_worker._HybridLine) for value in wrapped)
    assert all(value.advance <= 220 for value in wrapped)


def test_tagfetch_hybrid_mixed_wrap_splits_an_oversized_ascii_token():
    backend = _hybrid_backend()
    backend._pango_backend = _FakePangoBackend()
    text_font = backend.font(24)

    lines = backend.wrap("中文 देवनागरी " + "longword" * 12, text_font, 120)

    assert len(lines) > 2
    assert all(isinstance(line, pillow_worker._HybridLine) for line in lines)
    assert all(line.advance <= 120 for line in lines)


def test_tagfetch_hybrid_mixed_wrap_keeps_pango_layout_calls_linear():
    backend = _hybrid_backend()
    fake_pango = _FakePangoBackend()
    backend._pango_backend = fake_pango
    text_font = backend.font(24)
    sample = "中文 " + "देवनागरी" * 24 + " English"

    lines = backend.wrap(sample, text_font, 220)

    assert len(lines) > 4
    assert fake_pango.grapheme_calls == 1
    assert fake_pango.cluster_calls == 1
    assert fake_pango.metric_calls <= len(lines) * 2
    assert all(line.advance <= 220 for line in lines)

    calls = (
        fake_pango.grapheme_calls,
        fake_pango.cluster_calls,
        fake_pango.metric_calls,
    )
    assert backend.wrap(sample, text_font, 220) == lines
    assert (
        fake_pango.grapheme_calls,
        fake_pango.cluster_calls,
        fake_pango.metric_calls,
    ) == calls


def test_tagfetch_fallback_graphemes_keep_marks_zwj_and_flags_together():
    clusters = pillow_worker._fallback_graphemes("कि क्\u200dष 👨🏽\u200d💻 🇨🇳")

    assert "कि" in clusters
    assert "क्\u200dष" in clusters
    assert "👨🏽\u200d💻" in clusters
    assert "🇨🇳" in clusters


@requires_pango
def test_tagfetch_hybrid_line_interleaves_pillow_and_pango_runs():
    backend = _hybrid_backend()
    sample = "中文 कि देवनागरी ગુજરાતી བོད་ཡིག ქართული العربية English"

    line = backend.layout_line(sample, 24)
    run_kinds = [run.renderer for run in line.runs]

    assert run_kinds[0] == "pillow"
    assert "pango" in run_kinds
    assert run_kinds[-1] == "pillow"
    assert any("कि" in run.text for run in line.runs if run.renderer == "pango")
    assert backend._pango().unknown_glyphs(sample) == 0


@requires_pango
def test_tagfetch_hybrid_wrap_and_render_share_advance_baseline_and_bbox():
    backend = _hybrid_backend()
    text_font = backend.font(24)
    lines = backend.wrap(
        "中文 देवनागरी English العربية ગુજરાતી end" * 3,
        text_font,
        260,
        "test.hybrid-wrap",
    )

    assert lines
    assert all(isinstance(line, pillow_worker._HybridLine) for line in lines)
    assert all(line.advance <= 260 for line in lines)

    line = lines[0]
    image = Image.new("RGBA", (340, 90), (0, 0, 0, 0))
    draw = ImageDraw.Draw(image)
    backend.render_line(
        image,
        draw,
        line,
        (20, 18),
        text_font,
        (15, 20, 25),
        lambda _token, _size: None,
        line_height=36,
        component="test.hybrid-render",
    )
    actual = image.getchannel("A").getbbox()
    ascent, _descent = text_font.primary.getmetrics()
    expected = (
        20 + math.floor(line.bbox[0]),
        18 + ascent + math.floor(line.bbox[1]),
        20 + math.ceil(line.bbox[2]),
        18 + ascent + math.ceil(line.bbox[3]),
    )

    assert actual is not None
    assert expected[0] <= actual[0] <= actual[2] <= expected[2]
    assert expected[1] <= actual[1] <= actual[3] <= expected[3]


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
