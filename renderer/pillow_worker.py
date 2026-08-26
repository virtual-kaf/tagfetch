"""Standalone Pillow renderer.

This file is executed by path in a short-lived subprocess. Keep it free of
NoneBot and network imports: the parent process resolves every remote asset
before starting the worker.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any

MAX_CANVAS_HEIGHT = 16_384
MAX_CANVAS_PIXELS = 12_000_000


class RenderSizeError(RuntimeError):
    """Raised before an unsafe renderer allocation is attempted."""


def _diagnostic(event: str, **fields: Any) -> None:
    details = " ".join(f"{key}={value}" for key, value in fields.items())
    print(f"[RenderSize] {event} {details}".rstrip(), file=sys.stderr, flush=True)


def _pixels_to_pango(value: float, scale: int) -> int:
    if scale <= 0:
        raise RenderSizeError(f"invalid Pango.SCALE: {scale}")
    return round(float(value) * scale)


def _pango_to_pixels(value: float, scale: int) -> float:
    if scale <= 0:
        raise RenderSizeError(f"invalid Pango.SCALE: {scale}")
    return float(value) / scale


def _validate_size(
    event: str, component: str, width: int, height: int, bytes_per_pixel: int
) -> tuple[int, int]:
    normalized_width = int(width)
    normalized_height = int(height)
    pixels = normalized_width * normalized_height
    estimated_bytes = pixels * int(bytes_per_pixel)
    _diagnostic(
        event,
        component=component,
        width=normalized_width,
        height=normalized_height,
        pixels=pixels,
        estimated_bytes=estimated_bytes,
    )
    if normalized_width <= 0 or normalized_height <= 0:
        raise RenderSizeError(
            f"invalid {component} dimensions: {normalized_width}x{normalized_height}"
        )
    if normalized_height > MAX_CANVAS_HEIGHT:
        raise RenderSizeError(
            f"{component} height {normalized_height} exceeds "
            f"MAX_CANVAS_HEIGHT={MAX_CANVAS_HEIGHT}"
        )
    if pixels > MAX_CANVAS_PIXELS:
        raise RenderSizeError(
            f"{component} pixels {pixels} exceeds "
            f"MAX_CANVAS_PIXELS={MAX_CANVAS_PIXELS}"
        )
    return normalized_width, normalized_height


def _new_pillow_image(image, component: str, mode: str, size, color=0):
    width, height = (int(size[0]), int(size[1]))
    bytes_per_pixel = {"1": 1, "L": 1, "RGB": 3, "RGBA": 4}.get(mode, 4)
    _validate_size("image-allocation", component, width, height, bytes_per_pixel)
    try:
        return image.new(mode, (width, height), color)
    except MemoryError as exc:
        raise RenderSizeError(
            f"Pillow allocation failed for {component}: {width}x{height} {mode}"
        ) from exc


def _new_cairo_surface(cairo, component: str, width: int, height: int):
    width, height = _validate_size(
        "surface-allocation", component, width, height, 4
    )
    try:
        return cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    except MemoryError as exc:
        raise RenderSizeError(
            f"Cairo allocation failed for {component}: {width}x{height} ARGB32"
        ) from exc


def _measure_component(component: str, height: int, **fields: Any) -> None:
    _diagnostic("component-measure", component=component, height=int(height), **fields)


def _apply_memory_limit(megabytes: int) -> None:
    if sys.platform != "linux" or megabytes <= 0:
        return
    try:
        import resource

        limit = megabytes * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, OSError, ValueError):
        pass


class _PangoFont:
    def __init__(self, backend: _PangoTextBackend, size: int) -> None:
        self.backend = backend
        self.size = int(size)

    def getlength(self, value: str) -> float:
        return float(self.backend.measure(value, self.size, "font.getlength"))


class _PangoTextBackend:
    """Shape and rasterize text with PangoCairo and fontconfig."""

    _OBJECT_REPLACEMENT = "\ufffc"

    def __init__(self, font_path: Path, known_emojis: set[str], image, image_font) -> None:
        self._Image = image
        self._known_emojis = known_emojis
        self._emoji_by_first: dict[str, list[str]] = {}
        for value in known_emojis:
            if value:
                self._emoji_by_first.setdefault(value[0], []).append(value)
        for values in self._emoji_by_first.values():
            values.sort(key=len, reverse=True)

        try:
            import cairo
            import gi

            gi.require_version("Pango", "1.0")
            gi.require_version("PangoCairo", "1.0")
            from gi.repository import Pango, PangoCairo
        except (ImportError, ValueError, OSError) as exc:
            raise RuntimeError(
                "Pango renderer unavailable; install cairo, pango, "
                "gobject-introspection, pycairo, and PyGObject"
            ) from exc

        if not font_path.is_file():
            raise RuntimeError(f"bundled renderer font missing: {font_path}")
        try:
            primary_font = image_font.truetype(str(font_path), 16)
            self.family = str(primary_font.getname()[0])
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"unable to load bundled renderer font: {font_path}") from exc

        self._register_fontconfig_font(font_path)
        self._cairo = cairo
        self._Pango = Pango
        self._PangoCairo = PangoCairo
        self._pango_scale = int(Pango.SCALE)
        if self._pango_scale <= 0:
            raise RenderSizeError(f"invalid Pango.SCALE: {self._pango_scale}")
        _diagnostic("pango-scale", value=self._pango_scale)
        self._measure_surface = _new_cairo_surface(cairo, "pango.measure", 1, 1)
        self._measure_context = cairo.Context(self._measure_surface)

        try:
            font_map = PangoCairo.FontMap.get_default()
            font_map.changed()
            families = {family.get_name().casefold() for family in font_map.list_families()}
        except (AttributeError, TypeError) as exc:
            raise RuntimeError("unable to initialize the PangoCairo font map") from exc
        if self.family.casefold() not in families:
            raise RuntimeError(
                f"fontconfig did not register bundled renderer font family {self.family!r}"
            )

    @staticmethod
    def _register_fontconfig_font(font_path: Path) -> None:
        import ctypes
        import ctypes.util

        library_name = ctypes.util.find_library("fontconfig")
        if not library_name:
            raise RuntimeError("fontconfig shared library not found")
        try:
            fontconfig = ctypes.CDLL(library_name)
            fontconfig.FcInit.argtypes = []
            fontconfig.FcInit.restype = ctypes.c_int
            fontconfig.FcConfigGetCurrent.argtypes = []
            fontconfig.FcConfigGetCurrent.restype = ctypes.c_void_p
            fontconfig.FcConfigAppFontAddFile.argtypes = [
                ctypes.c_void_p,
                ctypes.c_char_p,
            ]
            fontconfig.FcConfigAppFontAddFile.restype = ctypes.c_int
            fontconfig.FcConfigBuildFonts.argtypes = [ctypes.c_void_p]
            fontconfig.FcConfigBuildFonts.restype = ctypes.c_int
        except (AttributeError, OSError) as exc:
            raise RuntimeError("fontconfig application-font API unavailable") from exc
        if not fontconfig.FcInit():
            raise RuntimeError("fontconfig initialization failed")
        config = fontconfig.FcConfigGetCurrent()
        if not config:
            raise RuntimeError("fontconfig returned no current configuration")
        encoded_path = os.fsencode(font_path.resolve())
        if not fontconfig.FcConfigAppFontAddFile(config, encoded_path):
            raise RuntimeError(f"fontconfig rejected bundled font: {font_path}")
        if not fontconfig.FcConfigBuildFonts(config):
            raise RuntimeError("fontconfig failed to rebuild its application font set")

    def font(self, size: int) -> _PangoFont:
        return _PangoFont(self, size)

    def _prepare_text(
        self, text: str
    ) -> tuple[str, list[tuple[int, str]]]:
        result: list[str] = []
        slots: list[tuple[int, str]] = []
        byte_offset = 0
        index = 0
        while index < len(text):
            matched = None
            for candidate in self._emoji_by_first.get(text[index], ()):
                if text.startswith(candidate, index):
                    matched = candidate
                    break
            if matched is None:
                value = text[index]
                result.append(value)
                byte_offset += len(value.encode("utf-8"))
                index += 1
                continue
            result.append(self._OBJECT_REPLACEMENT)
            slots.append((byte_offset, matched))
            byte_offset += len(self._OBJECT_REPLACEMENT.encode("utf-8"))
            index += len(matched)
        return "".join(result), slots

    def _layout(
        self,
        text: str,
        size: int,
        max_width: int | None = None,
        component: str = "text",
    ):
        Pango = self._Pango
        prepared, slots = self._prepare_text(text)
        layout = self._PangoCairo.create_layout(self._measure_context)
        description = Pango.FontDescription()
        description.set_family(self.family)
        font_size_units = _pixels_to_pango(int(size), self._pango_scale)
        description.set_absolute_size(font_size_units)
        layout.set_font_description(description)
        layout.set_auto_dir(True)
        wrap_width_units = None
        if max_width is not None:
            wrap_width_pixels = max(1, int(max_width))
            wrap_width_units = _pixels_to_pango(
                wrap_width_pixels, self._pango_scale
            )
            layout.set_width(wrap_width_units)
            layout.set_wrap(Pango.WrapMode.WORD_CHAR)
        layout.set_text(prepared, -1)

        attributes = Pango.AttrList()
        fallback = Pango.attr_fallback_new(True)
        fallback.start_index = 0
        fallback.end_index = 0xFFFFFFFF
        attributes.insert(fallback)
        replacement_bytes = len(self._OBJECT_REPLACEMENT.encode("utf-8"))
        for byte_offset, _token in slots:
            ink = Pango.Rectangle()
            logical = Pango.Rectangle()
            ink.x = logical.x = 0
            emoji_size_units = _pixels_to_pango(int(size), self._pango_scale)
            ink.y = logical.y = -emoji_size_units
            ink.width = logical.width = emoji_size_units
            ink.height = logical.height = emoji_size_units
            shape = Pango.attr_shape_new(ink, logical)
            shape.start_index = byte_offset
            shape.end_index = byte_offset + replacement_bytes
            attributes.insert(shape)
        layout.set_attributes(attributes)
        pixel_width, pixel_height = layout.get_pixel_size()
        units_width, units_height = layout.get_size()
        _diagnostic(
            "pango-layout",
            component=component,
            text_bytes=len(prepared.encode("utf-8")),
            font_px=int(size),
            font_units=font_size_units,
            wrap_px="none" if max_width is None else int(max_width),
            wrap_units="none" if wrap_width_units is None else wrap_width_units,
            scale=self._pango_scale,
            pixel_width=int(pixel_width),
            pixel_height=int(pixel_height),
            units_width=int(units_width),
            units_height=int(units_height),
            units_height_px=round(
                _pango_to_pixels(units_height, self._pango_scale), 3
            ),
            lines=int(layout.get_line_count()),
        )
        _validate_size(
            "layout-measure",
            component,
            max(1, int(pixel_width)),
            max(1, int(pixel_height)),
            4,
        )
        return layout, prepared, slots

    @staticmethod
    def _restore_emojis(
        prepared: str, slots: list[tuple[int, str]], start: int, end: int
    ) -> str:
        raw = prepared.encode("utf-8")
        restored = raw[start:end].decode("utf-8")
        for byte_offset, token in slots:
            if start <= byte_offset < end:
                restored = restored.replace("\ufffc", token, 1)
        return restored

    def wrap(
        self, text: str, size: int, max_width: int, component: str = "text.wrap"
    ) -> list[str]:
        if not text:
            return []
        normalized = text.replace("\t", " ")
        layout, prepared, slots = self._layout(
            normalized, size, max_width, component
        )
        lines: list[str] = []
        for line in layout.get_lines_readonly():
            start = int(line.start_index)
            end = start + int(line.length)
            lines.append(self._restore_emojis(prepared, slots, start, end))
        return lines

    def measure(self, text: str, size: int, component: str = "text.measure") -> int:
        if not text:
            return 0
        layout, _prepared, _slots = self._layout(text, size, component=component)
        _ink, logical = layout.get_pixel_extents()
        return max(1, int(logical.width))

    def bbox(
        self, text: str, size: int, component: str = "text.bbox"
    ) -> tuple[int, int, int, int]:
        if not text:
            return (0, 0, 0, 0)
        layout, _prepared, _slots = self._layout(text, size, component=component)
        ink, logical = layout.get_pixel_extents()
        left = min(int(ink.x), int(logical.x))
        top = min(int(ink.y), int(logical.y))
        right = max(int(ink.x + ink.width), int(logical.x + logical.width))
        bottom = max(int(ink.y + ink.height), int(logical.y + logical.height))
        return left, top, right, bottom

    def unknown_glyphs(self, text: str, size: int = 24) -> int:
        layout, _prepared, _slots = self._layout(
            text, size, component="unknown-glyph-check"
        )
        return int(layout.get_unknown_glyphs_count())

    def render(
        self,
        text: str,
        size: int,
        fill: tuple[int, ...],
        component: str = "text.render",
    ) -> tuple[Any, tuple[int, int], list[tuple[str, int, int]]]:
        layout, _prepared, slots = self._layout(text, size, component=component)
        ink, logical = layout.get_pixel_extents()
        left = min(0, int(ink.x), int(logical.x))
        top = min(0, int(ink.y), int(logical.y))
        right = max(1, int(ink.x + ink.width), int(logical.x + logical.width))
        bottom = max(1, int(ink.y + ink.height), int(logical.y + logical.height))
        width = max(1, right - left)
        height = max(1, bottom - top)

        surface = _new_cairo_surface(
            self._cairo, f"{component}.surface", width, height
        )
        context = self._cairo.Context(surface)
        context.move_to(-left, -top)
        red, green, blue = (int(value) / 255 for value in fill[:3])
        alpha = int(fill[3]) / 255 if len(fill) > 3 else 1.0
        context.set_source_rgba(red, green, blue, alpha)
        self._PangoCairo.show_layout(context, layout)
        surface.flush()
        layer = self._Image.frombuffer(
            "RGBa",
            (width, height),
            bytes(surface.get_data()),
            "raw",
            "BGRa",
            surface.get_stride(),
            1,
        ).convert("RGBA")

        placements: list[tuple[str, int, int]] = []
        for byte_offset, token in slots:
            position = layout.index_to_pos(byte_offset)
            slot_x = round(_pango_to_pixels(position.x, self._pango_scale))
            slot_y = round(
                _pango_to_pixels(position.y, self._pango_scale)
                + max(
                    0,
                    _pango_to_pixels(position.height, self._pango_scale) - size,
                )
                / 2
            )
            placements.append((token, slot_x, slot_y))
        return layer, (left, top), placements


def _run(spec: dict[str, Any]) -> list[str]:
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    Image.MAX_IMAGE_PIXELS = int(spec.get("max_image_pixels", 12_000_000))

    font_path = Path(spec["font_path"])
    raw_emoji_paths = dict(spec.get("emoji_paths", {}))
    known_emojis = set(raw_emoji_paths)
    emoji_paths = {key: Path(value) for key, value in raw_emoji_paths.items() if value}
    emoji_cache: dict[tuple[str, int], Any] = {}

    text_backend = _PangoTextBackend(font_path, known_emojis, Image, ImageFont)
    font_cache: dict[int, _PangoFont] = {}

    def font(size: int) -> _PangoFont:
        normalized = int(size)
        if normalized not in font_cache:
            font_cache[normalized] = text_backend.font(normalized)
        return font_cache[normalized]

    def new_image(component: str, mode: str, size, color=0):
        return _new_pillow_image(Image, component, mode, size, color)

    def apply_rounded_corners(img: Image.Image, radius: int) -> Image.Image:
        mask = new_image("media.rounded-mask", "L", img.size, 0)
        mask_draw = ImageDraw.Draw(mask)
        mask_draw.rounded_rectangle((0, 0, img.width - 1, img.height - 1), radius=radius, fill=255)
        res = img.convert("RGBA")
        res.putalpha(mask)
        return res

    def emoji_image(token: str, size: int):
        cache_key = (token, size)
        if cache_key in emoji_cache:
            return emoji_cache[cache_key]
        path = emoji_paths.get(token)
        if path is None:
            emoji_cache[cache_key] = None
            return None
        try:
            with Image.open(path) as source:
                rendered = source.convert("RGBA").resize(
                    (size, size), Image.Resampling.LANCZOS
                )
        except (OSError, ValueError):
            rendered = None
        emoji_cache[cache_key] = rendered
        return rendered

    class PangoDraw:
        def __init__(self, image) -> None:
            self.image = image
            self.pillow_draw = ImageDraw.Draw(image)

        def __getattr__(self, name: str):
            return getattr(self.pillow_draw, name)

        def text(
            self,
            xy,
            value,
            *,
            font=None,
            fill=(0, 0, 0),
            component="text.render",
            **kwargs,
        ):
            if not isinstance(font, _PangoFont):
                return self.pillow_draw.text(
                    xy, value, font=font, fill=fill, **kwargs
                )
            layer, offset, placements = text_backend.render(
                str(value), font.size, tuple(fill), str(component)
            )
            origin_x = round(float(xy[0]))
            origin_y = round(float(xy[1]))
            self.image.paste(
                layer,
                (origin_x + offset[0], origin_y + offset[1]),
                layer,
            )
            for token, slot_x, slot_y in placements:
                icon = emoji_image(token, font.size)
                destination = (origin_x + slot_x, origin_y + slot_y)
                if icon is not None:
                    self.image.paste(icon, destination, icon)
                    continue
                x, y = destination
                size = font.size
                self.pillow_draw.rounded_rectangle(
                    (x + 2, y + 2, x + size - 2, y + size - 2),
                    radius=max(2, size // 6),
                    fill=(239, 243, 246),
                    outline=(160, 174, 184),
                )
            return None

        def textbbox(
            self, xy, value, *, font=None, component="text.bbox", **kwargs
        ):
            if not isinstance(font, _PangoFont):
                return self.pillow_draw.textbbox(xy, value, font=font, **kwargs)
            left, top, right, bottom = text_backend.bbox(
                str(value), font.size, str(component)
            )
            origin_x = round(float(xy[0]))
            origin_y = round(float(xy[1]))
            return (
                origin_x + left,
                origin_y + top,
                origin_x + right,
                origin_y + bottom,
            )

    def text_draw(image) -> PangoDraw:
        return PangoDraw(image)

    def wrap(
        text: str,
        text_font: _PangoFont,
        max_width: int,
        component: str = "text.wrap",
    ) -> list[str]:
        return text_backend.wrap(text, text_font.size, max_width, component)

    def draw_lines(
        image,
        draw,
        lines: list[str],
        xy: tuple[int, int],
        text_font,
        fill: tuple[int, int, int],
        line_height: int,
        component: str = "text.line",
    ) -> int:
        start_x, y = xy
        for line in lines:
            draw.text(
                (start_x, y),
                line,
                font=text_font,
                fill=fill,
                component=component,
            )
            y += line_height
        return y

    def draw_text(
        draw,
        xy,
        value: str,
        text_font,
        fill,
        component: str = "text",
    ) -> None:
        draw.text(
            xy, value, font=text_font, fill=fill, component=f"{component}.render"
        )

    def rounded_avatar(path_value: str, size: int, fallback_text: str):
        avatar = None
        if path_value:
            try:
                with Image.open(path_value) as source:
                    avatar = ImageOps.fit(
                        source.convert("RGBA"),
                        (size, size),
                        method=Image.Resampling.LANCZOS,
                    )
            except (OSError, ValueError):
                avatar = None
        if avatar is None:
            avatar = new_image(
                "avatar.placeholder", "RGBA", (size, size), (225, 232, 238, 255)
            )
            avatar_draw = text_draw(avatar)
            avatar_font = font(max(18, size // 2))
            letter = (fallback_text.strip() or "?")[:1]
            box = avatar_draw.textbbox(
                (0, 0), letter, font=avatar_font, component="avatar.initial"
            )
            avatar_draw.text(
                (
                    (size - (box[2] - box[0])) // 2,
                    (size - (box[3] - box[1])) // 2 - box[1],
                ),
                letter,
                font=avatar_font,
                fill=(83, 100, 113),
                component="avatar.initial",
            )
        mask = new_image("avatar.mask", "L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        avatar.putalpha(mask)
        return avatar

    def media_layout(media: list[dict[str, Any]], content_width: int):
        display = media[:9]
        count = len(display)
        if count == 0:
            return [], 0, []
        cols = 1 if count == 1 else (2 if count in (2, 4) else 3)
        gap = 6
        cell_width = (content_width - gap * (cols - 1)) // cols
        rows = (count + cols - 1) // cols
        cell_height = min(480, int(cell_width * 0.75)) if count == 1 else cell_width
        return (
            display,
            rows * cell_height + (rows - 1) * gap,
            [
                (index % cols, index // cols, cell_width, cell_height)
                for index in range(count)
            ],
        )

    def draw_media_grid(image, draw, media, x: int, y: int, content_width: int):
        display, total_height, layout = media_layout(media, content_width)
        gap = 6
        safe: list[int] = []
        for item, (col, row, cell_width, cell_height) in zip(display, layout):
            left = x + col * (cell_width + gap)
            top = y + row * (cell_height + gap)
            rendered = None
            path_value = item.get("path", "")
            if path_value:
                try:
                    with Image.open(path_value) as source:
                        rendered = ImageOps.fit(
                            source.convert("RGBA"),
                            (cell_width, cell_height),
                            method=Image.Resampling.LANCZOS,
                        )
                        rendered = apply_rounded_corners(rendered, 12)
                except (OSError, ValueError):
                    rendered = None
            if rendered is None:
                draw.rounded_rectangle(
                    (left, top, left + cell_width, top + cell_height),
                    radius=12,
                    fill=(239, 243, 246),
                )
                placeholder = "视频（请查看原文）" if item.get("video") else "图片加载失败"
                placeholder_font = font(18)
                lines = wrap(
                    placeholder,
                    placeholder_font,
                    cell_width - 24,
                    "media.placeholder",
                )
                text_height = len(lines) * 24
                draw_lines(
                    image,
                    draw,
                    lines,
                    (left + 12, top + (cell_height - text_height) // 2),
                    placeholder_font,
                    (83, 100, 113),
                    24,
                )
            else:
                image.paste(rendered, (left, top), rendered)

            if item.get("video"):
                radius = max(20, min(36, cell_width // 8))
                center = (left + cell_width // 2, top + cell_height // 2)
                draw.ellipse(
                    (
                        center[0] - radius,
                        center[1] - radius,
                        center[0] + radius,
                        center[1] + radius,
                    ),
                    fill=(0, 0, 0, 160),
                )
                draw.polygon(
                    (
                        (center[0] - radius // 3, center[1] - radius // 2),
                        (center[0] - radius // 3, center[1] + radius // 2),
                        (center[0] + radius // 2, center[1]),
                    ),
                    fill=(255, 255, 255),
                )
            safe.append(top + cell_height)
        return y + total_height, safe

    def render_tweet_card(
        item: dict[str, Any],
        quote: dict[str, Any] | None = None,
        width: int = 736,
        target: bool = False,
    ):
        padding = 24
        content_width = width - padding * 2

        body_font = font(21)
        body_line = 30
        muted_font = font(18)
        name_font = font(22)
        trans_font = font(20)
        trans_line = 28

        item_key = str(item.get("id", "target" if target else "tweet"))[:40]
        component_prefix = f"tweet[{item_key}]"
        text_lines = wrap(
            str(item.get("text", "")),
            body_font,
            content_width,
            f"{component_prefix}.body",
        )
        translation = str(item.get("translated_text", ""))
        trans_lines = wrap(
            translation,
            trans_font,
            content_width - 32,
            f"{component_prefix}.translation",
        )
        media = list(item.get("media", []))
        _, media_height, _ = media_layout(media, content_width)

        avatar_size = 52
        header_height = avatar_size

        height = padding + header_height + 16
        _measure_component(
            f"{component_prefix}.header", height, avatar_height=avatar_size
        )
        if text_lines:
            body_height = len(text_lines) * body_line + 12
            _measure_component(
                f"{component_prefix}.body",
                body_height,
                lines=len(text_lines),
                line_height=body_line,
            )
            height += body_height

        if trans_lines:
            translation_height = 28 + len(trans_lines) * trans_line + 20
            _measure_component(
                f"{component_prefix}.translation",
                translation_height,
                lines=len(trans_lines),
                line_height=trans_line,
            )
            height += translation_height

        if media:
            _measure_component(
                f"{component_prefix}.media",
                media_height + 16,
                items=min(len(media), 9),
            )
            height += media_height + 16

        quote_card = None
        if quote:
            quote_card, _ = render_tweet_card(
                quote, width=content_width, target=False
            )
            _measure_component(
                f"{component_prefix}.quote", quote_card.height + 16
            )
            height += quote_card.height + 16

        _measure_component(f"{component_prefix}.stats", 36)
        height += 36

        if target and item.get("url"):
            _measure_component(f"{component_prefix}.link", 36)
            height += 36

        height += padding
        _measure_component(
            f"{component_prefix}.total",
            height,
            width=width,
            body_lines=len(text_lines),
            translation_lines=len(trans_lines),
        )

        card = new_image(
            f"{component_prefix}.canvas",
            "RGBA",
            (width, height),
            (0, 0, 0, 0),
        )
        draw = text_draw(card)

        draw.rounded_rectangle(
            (0, 0, width - 1, height - 1),
            radius=18,
            fill=(255, 255, 255),
            outline=(225, 232, 237),
            width=1,
        )

        safe_breaks: list[int] = []
        y = padding

        avatar = rounded_avatar(
            str(item.get("avatar_path", "")), avatar_size, str(item.get("name", ""))
        )
        card.paste(avatar, (padding, y), avatar)
        text_x = padding + avatar_size + 14

        draw_text(
            draw,
            (text_x, y + 2),
            str(item.get("name", "")),
            name_font,
            (15, 20, 25),
            f"{component_prefix}.name",
        )

        handle = str(item.get("screen_name", ""))
        created = str(item.get("created_at", ""))[:16]
        meta = f"@{handle}" if handle else ""
        if created:
            meta = f"{meta} · {created}" if meta else created

        draw_text(
            draw,
            (text_x, y + name_font.size + 8),
            meta,
            muted_font,
            (83, 100, 113),
            f"{component_prefix}.meta",
        )

        y += header_height + 14
        safe_breaks.append(y)

        if text_lines:
            for line in text_lines:
                y = draw_lines(
                    card,
                    draw,
                    [line],
                    (padding, y),
                    body_font,
                    (15, 20, 25),
                    body_line,
                    f"{component_prefix}.body-line",
                )
                safe_breaks.append(y)
            y += 12

        if trans_lines:
            box_top = y
            box_height = 28 + len(trans_lines) * trans_line + 16
            draw.rounded_rectangle(
                (padding, box_top, width - padding, box_top + box_height),
                radius=10,
                fill=(235, 245, 255),
            )
            draw.rounded_rectangle(
                (padding, box_top, padding + 4, box_top + box_height),
                radius=2,
                fill=(29, 155, 240),
            )

            draw.text(
                (padding + 16, y + 8),
                "翻译自DEEPSEEK",
                font=font(15),
                fill=(29, 155, 240),
            )
            y += 30
            for line in trans_lines:
                y = draw_lines(
                    card,
                    draw,
                    [line],
                    (padding + 16, y),
                    trans_font,
                    (15, 20, 25),
                    trans_line,
                    f"{component_prefix}.translation-line",
                )
                safe_breaks.append(y)
            y = box_top + box_height + 12

        if media:
            y, media_breaks = draw_media_grid(
                card, draw, media, padding, y, content_width
            )
            safe_breaks.extend(media_breaks)
            y += 16

        if quote_card is not None:
            card.paste(quote_card, (padding, y), quote_card)
            y += quote_card.height + 16
            safe_breaks.append(y)

        # 优化 Stats 栏：使用 draw_lines 绘制图标 + 文字组合
        stats_font = font(18)
        replies = item.get("replies", 0)
        retweets = item.get("retweets", 0)
        likes = item.get("likes", 0)
        views = item.get("views", 0)

        stats_items = [
            ("💬", str(replies)),
            ("🔁", str(retweets)),
            ("❤️", str(likes)),
            ("👁", str(views)),
        ]
        
        cur_x = padding
        spacing = content_width // 4
        for icon_str, val_str in stats_items:
            line_tokens = wrap(
                f"{icon_str} {val_str}",
                stats_font,
                spacing,
                f"{component_prefix}.stats-item",
            )
            if line_tokens:
                draw_lines(card, draw, [line_tokens[0]], (cur_x, y + 4), stats_font, (83, 100, 113), 24)
            cur_x += spacing

        y += 32
        safe_breaks.append(y)

        if target and item.get("url"):
            link_font = font(18)
            draw.text(
                (width // 2 - 40, y + 2),
                "查看原文",
                font=link_font,
                fill=(29, 155, 240),
            )
            y += 32
            safe_breaks.append(y)

        return card, safe_breaks

    def paginate(image, safe_breaks: list[int], max_height: int):
        if image.height <= max_height:
            return [image]
        footer_height = 36
        content_limit = max(400, max_height - footer_height)
        pages = []
        start = 0
        while start < image.height:
            hard_end = min(image.height, start + content_limit)
            candidates = [
                value for value in safe_breaks if start + 120 <= value <= hard_end
            ]
            end = max(candidates) if candidates else hard_end
            if image.height - end < 120:
                end = image.height
            if end <= start:
                end = hard_end
            crop_height = end - start
            _validate_size(
                "image-allocation",
                "pagination.crop",
                image.width,
                crop_height,
                4,
            )
            try:
                crop = image.crop((0, start, image.width, end))
            except MemoryError as exc:
                raise RenderSizeError(
                    f"Pillow crop failed: {image.width}x{crop_height}"
                ) from exc
            page = new_image(
                "pagination.page",
                "RGB",
                (image.width, crop.height + footer_height),
                (245, 247, 248),
            )
            page.paste(crop, (0, 0))
            pages.append(page)
            start = end
        page_font = font(16)
        total = len(pages)
        for index, page in enumerate(pages, 1):
            page_draw = text_draw(page)
            page_draw.text(
                (page.width - 80, page.height - 27),
                f"{index}/{total}",
                font=page_font,
                fill=(113, 118, 123),
            )
        return pages

    def conversation_pages():
        width = 800
        card_width = 736
        card_x = (width - card_width) // 2

        blocks: list[tuple[Any, list[int]]] = []
        ancestors = list(spec.get("ancestors", []))
        for item in ancestors:
            blocks.append(render_tweet_card(item, width=card_width))

        target_item = spec["target"]
        blocks.append(
            render_tweet_card(
                target_item,
                quote=spec.get("quote"),
                width=card_width,
                target=True,
            )
        )

        badge_height = 40
        gap = 16
        total_height = 24 + badge_height + sum(block.height for block, _ in blocks)
        total_height += gap * (len(blocks) - 1) + 32
        for index, (block, _) in enumerate(blocks):
            _measure_component(
                "conversation.block", block.height, index=index, width=block.width
            )
        _measure_component(
            "conversation.canvas", total_height, blocks=len(blocks), width=width
        )

        canvas = new_image(
            "conversation.canvas", "RGB", (width, total_height), (245, 247, 248)
        )
        canvas_draw = text_draw(canvas)
        y = 20

        if ancestors:
            label_font = font(16)
            badge_w, badge_h = 100, 28
            bx = (width - badge_w) // 2
            canvas_draw.rounded_rectangle(
                (bx, y, bx + badge_w, y + badge_h), radius=14, fill=(232, 236, 239)
            )
            canvas_draw.text(
                (bx + 20, y + 5), "THREAD", font=label_font, fill=(83, 100, 113)
            )
            y += badge_height

        safe: list[int] = []
        for index, (block, block_safe) in enumerate(blocks):
            canvas.paste(block, (card_x, y), block)
            safe.extend(y + value for value in block_safe)
            y += block.height
            safe.append(y)

            if index < len(blocks) - 1:
                y += gap // 2
                badge_w, badge_h = 80, 26
                bx = (width - badge_w) // 2
                canvas_draw.rounded_rectangle(
                    (bx, y - 13, bx + badge_w, y - 13 + badge_h),
                    radius=13,
                    fill=(232, 236, 239),
                )
                canvas_draw.text(
                    (bx + 18, y - 9), "REPLY", font=font(14), fill=(83, 100, 113)
                )
                y += gap // 2

        return paginate(canvas, safe, int(spec.get("max_height", 4096)))

    def sublist_image():
        width = 800
        sections = [
            ("核心成员（默认推送）", spec.get("active_core", []), (29, 155, 240)),
            ("已屏蔽核心成员", spec.get("muted_core", []), (113, 118, 123)),
            ("额外订阅", spec.get("extra_subs", []), (0, 186, 124)),
        ]
        visible = [
            (title, values, color) for title, values, color in sections if values
        ]
        height = 120 + sum(54 + len(values) * 36 for _, values, _ in visible)
        if not visible:
            height += 50
        for title, values, _ in visible:
            _measure_component(
                "sublist.section", 54 + len(values) * 36, title=title, rows=len(values)
            )
        _measure_component("sublist.canvas", height, sections=len(visible))
        image = new_image(
            "sublist.canvas", "RGB", (width, height), (245, 247, 248)
        )
        draw = text_draw(image)
        draw.rounded_rectangle(
            (24, 24, width - 24, height - 24),
            radius=18,
            fill=(255, 255, 255),
            outline=(225, 232, 237),
        )
        draw.text((52, 48), "本群订阅", font=font(30), fill=(15, 20, 25))
        y = 102
        for title, values, color in visible:
            draw.text((52, y), title, font=font(22), fill=color)
            y += 38
            for value in values:
                draw_text(
                    draw, (72, y), f"@{value}", font(20), (15, 20, 25)
                )
                y += 36
            y += 16
        if not visible:
            draw.text((52, y), "暂无订阅", font=font(20), fill=(113, 118, 123))
        return [image]

    def live_image():
        width = 800
        title_font = font(32)
        member_font = font(22)
        title_lines = wrap(
            str(spec.get("title", "")), title_font, width - 100, "live.title"
        )
        member_lines = wrap(
            "、".join(spec.get("members", [])),
            member_font,
            width - 100,
            "live.members",
        )
        height = 210 + len(title_lines) * 44 + len(member_lines) * 32
        _measure_component("live.title", len(title_lines) * 44, lines=len(title_lines))
        _measure_component(
            "live.members", len(member_lines) * 32, lines=len(member_lines)
        )
        _measure_component("live.canvas", height)
        image = new_image(
            "live.canvas", "RGB", (width, height), (245, 247, 248)
        )
        draw = text_draw(image)
        draw.rounded_rectangle(
            (24, 24, width - 24, height - 24),
            radius=18,
            fill=(255, 255, 255),
            outline=(225, 232, 237),
        )
        draw.ellipse((52, 55, 78, 81), fill=(244, 33, 46))
        draw.text((94, 48), "直播即将开始", font=font(24), fill=(244, 33, 46))
        y = 105
        y = draw_lines(image, draw, title_lines, (52, y), title_font, (15, 20, 25), 44)
        y += 14
        y = draw_lines(
            image, draw, member_lines, (52, y), member_font, (83, 100, 113), 32
        )
        y += 20
        draw.rounded_rectangle(
            (52, y, width - 52, y + 50), radius=12, fill=(15, 20, 25)
        )
        draw_text(
            draw,
            (70, y + 10),
            str(spec.get("start_time_display", "")),
            font(22),
            (255, 255, 255),
        )
        return [image]

    def calendar_image():
        width = 1680
        weeks = list(spec.get("calendar", {}).get("weeks", []))
        row_height = 230
        header_height = 142
        weekday_height = 55
        footer_height = 52
        height = (
            26 * 2
            + header_height
            + weekday_height
            + row_height * len(weeks)
            + footer_height
        )
        _measure_component("calendar.header", header_height)
        _measure_component("calendar.weekdays", weekday_height)
        _measure_component(
            "calendar.weeks", row_height * len(weeks), rows=len(weeks)
        )
        _measure_component("calendar.footer", footer_height)
        _measure_component("calendar.canvas", height, width=width)
        image = new_image(
            "calendar.canvas", "RGB", (width, height), (245, 245, 243)
        )
        draw = text_draw(image)
        cal = spec.get("calendar", {})
        left, right = 26, width - 26
        draw.rectangle(
            (left, 26, right, height - 26),
            fill=(255, 255, 255),
            outline=(17, 17, 17),
            width=1,
        )
        draw.text((52, 60), "KABUBU", font=font(28), fill=(17, 17, 17))
        draw.text((52, 96), "ACTIVITY CALENDAR", font=font(15), fill=(102, 102, 102))
        month_label = str(cal.get("month_label", ""))
        month_font = font(42)
        month_box = draw.textbbox((0, 0), month_label, font=month_font)
        draw.text(
            ((width - (month_box[2] - month_box[0])) // 2, 54),
            month_label,
            font=month_font,
            fill=(17, 17, 17),
        )
        title = str(cal.get("month_title", ""))
        title_font = font(19)
        title_box = draw.textbbox((0, 0), title, font=title_font)
        draw.text(
            ((width - (title_box[2] - title_box[0])) // 2, 105),
            title,
            font=title_font,
            fill=(17, 17, 17),
        )
        draw.rounded_rectangle((1260, 54, 1600, 124), radius=12, fill=(5, 5, 5))
        draw.text(
            (1315, 67),
            str(cal.get("date_range", "")),
            font=font(27),
            fill=(255, 255, 255),
        )
        draw.text((1378, 101), "CST / UTC+8", font=font(12), fill=(204, 204, 204))
        y = 26 + header_height
        cell_width = (right - left) // 7
        weekdays = [
            "Monday",
            "Tuesday",
            "Wednesday",
            "Thursday",
            "Friday",
            "Saturday",
            "Sunday",
        ]
        for index, weekday in enumerate(weekdays):
            x1 = left + index * cell_width
            x2 = right if index == 6 else x1 + cell_width
            draw.rectangle(
                (x1 + 3, y + 7, x2 - 3, y + weekday_height - 7), fill=(5, 5, 5)
            )
            box = draw.textbbox((0, 0), weekday, font=font(18))
            draw.text(
                (x1 + (cell_width - (box[2] - box[0])) // 2, y + 17),
                weekday,
                font=font(18),
                fill=(255, 255, 255),
            )
        y += weekday_height
        for week in weeks:
            for index, day in enumerate(week):
                x1 = left + index * cell_width
                x2 = right if index == 6 else x1 + cell_width
                fill = (255, 255, 255) if day.get("in_month") else (245, 245, 243)
                draw.rectangle(
                    (x1, y, x2, y + row_height),
                    fill=fill,
                    outline=(160, 160, 160),
                    width=1,
                )
                if not day.get("in_month"):
                    continue
                number = str(day.get("day", ""))
                if day.get("is_today"):
                    draw.ellipse((x1 + 10, y + 8, x1 + 48, y + 46), fill=(17, 17, 17))
                    draw.text(
                        (x1 + 21, y + 11), number, font=font(22), fill=(255, 255, 255)
                    )
                else:
                    draw.text(
                        (x1 + 14, y + 10), number, font=font(24), fill=(17, 17, 17)
                    )
                events = list(day.get("events", []))
                if events:
                    draw.text(
                        (x2 - 75, y + 18),
                        f"{len(events)} EVENTS",
                        font=font(11),
                        fill=(102, 102, 102),
                    )
                event_y = y + 55
                for event in events[:3]:
                    card_height = 68 if event.get("show_image") else 48
                    _measure_component(
                        "calendar.event-card",
                        card_height,
                        show_image=bool(event.get("show_image")),
                    )
                    draw.rectangle(
                        (x1 + 9, event_y, x2 - 9, event_y + card_height),
                        fill=(237, 237, 237),
                    )
                    draw.rectangle(
                        (x1 + 9, event_y, x1 + 13, event_y + card_height),
                        fill=(17, 17, 17),
                    )
                    text_x = x1 + 19
                    cover_path = str(event.get("cover_path", ""))
                    if event.get("show_image") and cover_path:
                        try:
                            with Image.open(cover_path) as source:
                                cover = ImageOps.fit(
                                    source.convert("RGB"),
                                    (58, 58),
                                    method=Image.Resampling.LANCZOS,
                                )
                            image.paste(cover, (x1 + 17, event_y + 5))
                            text_x = x1 + 84
                        except (OSError, ValueError):
                            pass
                    draw_text(
                        draw,
                        (text_x, event_y + 5),
                        str(event.get("time_disp", "")),
                        font(13),
                        (76, 76, 76),
                    )
                    title_lines = wrap(
                        str(event.get("title", "")),
                        font(13),
                        x2 - text_x - 15,
                        "calendar.event-title",
                    )[:2]
                    draw_lines(
                        image,
                        draw,
                        title_lines,
                        (text_x, event_y + 23),
                        font(13),
                        (17, 17, 17),
                        17,
                    )
                    event_y += card_height + 7
                    if event_y + 45 > y + row_height:
                        break
            y += row_height
        draw.text(
            (45, y + 16),
            f"PAGE {cal.get('page', 1)} / {cal.get('total_pages', 1)}",
            font=font(14),
            fill=(102, 102, 102),
        )
        footer = f"本月 {cal.get('month_event_count', 0)} 场 · 未来共 {cal.get('total_event_count', 0)} 场"
        footer_font = font(14)
        footer_box = draw.textbbox((0, 0), footer, font=footer_font)
        draw.text(
            (right - (footer_box[2] - footer_box[0]) - 20, y + 16),
            footer,
            font=footer_font,
            fill=(102, 102, 102),
        )
        return [image]

    kind = spec.get("kind")
    if kind == "conversation":
        pages = conversation_pages()
    elif kind == "sublist":
        pages = sublist_image()
    elif kind == "live":
        pages = live_image()
    elif kind == "calendar":
        pages = calendar_image()
    else:
        raise ValueError(f"unsupported render kind: {kind!r}")

    output = Path(spec["output_path"])
    output.parent.mkdir(parents=True, exist_ok=True)
    output_format = str(spec.get("format", "PNG")).upper()
    quality = int(spec.get("quality", 85))
    paths: list[str] = []
    multi = len(pages) > 1
    for index, page in enumerate(pages, 1):
        final_path = (
            output.with_name(f"{output.stem}_{index}{output.suffix}")
            if multi
            else output
        )
        temporary = final_path.with_suffix(final_path.suffix + f".{os.getpid()}.tmp")
        save_kwargs: dict[str, Any] = {}
        if output_format == "JPEG":
            save_kwargs.update(quality=quality, optimize=True, progressive=True)
            page = page.convert("RGB")
        else:
            save_kwargs.update(optimize=True)
        page.save(temporary, format=output_format, **save_kwargs)
        os.replace(temporary, final_path)
        paths.append(str(final_path))
    return paths


def main() -> int:
    if len(sys.argv) != 3:
        print("usage: pillow_worker.py SPEC_JSON RESULT_JSON", file=sys.stderr)
        return 2
    spec_path = Path(sys.argv[1])
    result_path = Path(sys.argv[2])
    try:
        spec = json.loads(spec_path.read_text(encoding="utf-8"))
        _apply_memory_limit(int(spec.get("memory_limit_mb", 384)))
        paths = _run(spec)
        temporary = result_path.with_suffix(result_path.suffix + ".tmp")
        temporary.write_text(
            json.dumps({"paths": paths}, ensure_ascii=False), encoding="utf-8"
        )
        os.replace(temporary, result_path)
        return 0
    except MemoryError:
        print(
            "RenderSizeError: renderer memory exhausted outside a guarded "
            "allocation; inspect preceding [RenderSize] diagnostics",
            file=sys.stderr,
        )
        return 1
    except Exception as exc:  # noqa: BLE001 - process boundary reports all errors
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
