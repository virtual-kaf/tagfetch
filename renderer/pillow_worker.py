"""Standalone Pillow renderer.

This file is executed by path in a short-lived subprocess.  Keep it free of
NoneBot and network imports: the parent process resolves every remote asset
before starting the worker.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path
from typing import Any


def _apply_memory_limit(megabytes: int) -> None:
    if sys.platform != "linux" or megabytes <= 0:
        return
    try:
        import resource

        limit = megabytes * 1024 * 1024
        resource.setrlimit(resource.RLIMIT_AS, (limit, limit))
    except (ImportError, OSError, ValueError):
        # Rendering still has the parent timeout and pixel limits as a backstop.
        pass


def _run(spec: dict[str, Any]) -> list[str]:
    from PIL import Image, ImageDraw, ImageFont, ImageOps

    Image.MAX_IMAGE_PIXELS = int(spec.get("max_image_pixels", 12_000_000))

    font_path = Path(spec["font_path"])
    raw_emoji_paths = dict(spec.get("emoji_paths", {}))
    known_emojis = set(raw_emoji_paths)
    emoji_paths = {key: Path(value) for key, value in raw_emoji_paths.items() if value}
    emoji_cache: dict[tuple[str, int], Any] = {}

    def font(size: int):
        return ImageFont.truetype(str(font_path), size)

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

    emoji_by_first: dict[str, list[str]] = {}
    for value in known_emojis:
        if value:
            emoji_by_first.setdefault(value[0], []).append(value)
    for values in emoji_by_first.values():
        values.sort(key=len, reverse=True)

    def tokens(text: str) -> list[str]:
        result: list[str] = []
        index = 0
        while index < len(text):
            matched = None
            for candidate in emoji_by_first.get(text[index], ()):
                if text.startswith(candidate, index):
                    matched = candidate
                    break
            if matched is not None:
                result.append(matched)
                index += len(matched)
            else:
                result.append(text[index])
                index += 1
        return result

    def token_width(value: str, text_font) -> int:
        if value in known_emojis:
            return text_font.size
        drawable = value.replace("\ufe0f", "").replace("\u200d", "")
        try:
            return max(1, round(text_font.getlength(drawable)))
        except (OSError, ValueError):
            return text_font.size

    def wrap(text: str, text_font, max_width: int) -> list[list[str]]:
        if not text:
            return []
        lines: list[list[str]] = []
        for paragraph in text.replace("\t", " ").split("\n"):
            if not paragraph:
                lines.append([])
                continue
            current: list[str] = []
            current_width = 0
            for value in tokens(paragraph):
                width = token_width(value, text_font)
                if current and current_width + width > max_width:
                    lines.append(current)
                    current = []
                    current_width = 0
                current.append(value)
                current_width += width
            if current:
                lines.append(current)
        return lines

    def draw_lines(
        image,
        draw,
        lines: list[list[str]],
        xy: tuple[int, int],
        text_font,
        fill: tuple[int, int, int],
        line_height: int,
    ) -> int:
        start_x, y = xy
        for line in lines:
            x = start_x
            for value in line:
                icon = emoji_image(value, text_font.size)
                if icon is not None:
                    image.paste(
                        icon, (x, y + max(0, (line_height - icon.height) // 2)), icon
                    )
                    x += text_font.size
                    continue
                if value in known_emojis:
                    size = text_font.size
                    top = y + max(1, (line_height - size) // 2)
                    draw.rounded_rectangle(
                        (x + 2, top + 2, x + size - 2, top + size - 2),
                        radius=max(2, size // 6),
                        fill=(239, 243, 246),
                        outline=(160, 174, 184),
                    )
                    draw.text(
                        (x + size // 4, top - 1),
                        "◇",
                        font=text_font,
                        fill=(83, 100, 113),
                    )
                    x += size
                    continue
                drawable = value.replace("\ufe0f", "").replace("\u200d", "")
                try:
                    draw.text((x, y), drawable, font=text_font, fill=fill)
                except (OSError, UnicodeEncodeError):
                    draw.text((x, y), "?", font=text_font, fill=fill)
                x += token_width(value, text_font)
            y += line_height
        return y

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
            avatar = Image.new("RGBA", (size, size), (225, 232, 238, 255))
            avatar_draw = ImageDraw.Draw(avatar)
            fallback_font = font(max(18, size // 2))
            letter = (fallback_text.strip() or "?")[:1]
            box = avatar_draw.textbbox((0, 0), letter, font=fallback_font)
            avatar_draw.text(
                (
                    (size - (box[2] - box[0])) // 2,
                    (size - (box[3] - box[1])) // 2 - box[1],
                ),
                letter,
                font=fallback_font,
                fill=(83, 100, 113),
            )
        mask = Image.new("L", (size, size), 0)
        ImageDraw.Draw(mask).ellipse((0, 0, size - 1, size - 1), fill=255)
        avatar.putalpha(mask)
        return avatar

    def media_layout(media: list[dict[str, Any]], content_width: int):
        display = media[:9]
        count = len(display)
        if count == 0:
            return [], 0, []
        cols = 1 if count == 1 else (2 if count in (2, 4) else 3)
        gap = 4
        cell_width = (content_width - gap * (cols - 1)) // cols
        rows = (count + cols - 1) // cols
        cell_height = min(520, int(cell_width * 0.72)) if count == 1 else cell_width
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
        gap = 4
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
                            source.convert("RGB"),
                            (cell_width, cell_height),
                            method=Image.Resampling.LANCZOS,
                        )
                except (OSError, ValueError):
                    rendered = None
            if rendered is None:
                draw.rounded_rectangle(
                    (left, top, left + cell_width, top + cell_height),
                    radius=8,
                    fill=(239, 243, 246),
                )
                placeholder = (
                    "视频（请查看原文）" if item.get("video") else "图片加载失败"
                )
                placeholder_font = font(20)
                lines = wrap(placeholder, placeholder_font, cell_width - 24)
                text_height = len(lines) * 28
                draw_lines(
                    image,
                    draw,
                    lines,
                    (left + 12, top + (cell_height - text_height) // 2),
                    placeholder_font,
                    (83, 100, 113),
                    28,
                )
            else:
                image.paste(rendered, (left, top))
            if item.get("video"):
                radius = max(20, min(42, cell_width // 8))
                center = (left + cell_width // 2, top + cell_height // 2)
                draw.ellipse(
                    (
                        center[0] - radius,
                        center[1] - radius,
                        center[0] + radius,
                        center[1] + radius,
                    ),
                    fill=(0, 0, 0, 150),
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

    def render_tweet(
        item: dict[str, Any],
        quote: dict[str, Any] | None = None,
        width: int = 800,
        nested: bool = False,
        target: bool = False,
    ):
        padding = 22 if nested else 25
        content_width = width - padding * 2
        body_font = font(22 if nested else 24)
        body_line = 31 if nested else 34
        muted_font = font(18 if nested else 20)
        name_font = font(24 if nested else 28)
        translation_font = font(21 if nested else 23)
        translation_line = 30 if nested else 33
        text_lines = wrap(str(item.get("text", "")), body_font, content_width)
        translation = str(item.get("translated_text", ""))
        translation_lines = wrap(translation, translation_font, content_width - 24)
        media = list(item.get("media", []))
        _, media_height, _ = media_layout(media, content_width)

        header_height = 62 if nested else 82
        height = padding + header_height
        height += len(text_lines) * body_line + (14 if text_lines else 0)
        if translation_lines:
            height += 28 + len(translation_lines) * translation_line + 20
        if media:
            height += media_height + 16

        quote_image = None
        quote_safe: list[int] = []
        if quote:
            quote_image, quote_safe = render_tweet(
                quote,
                width=content_width - 16,
                nested=True,
                target=False,
            )
            height += quote_image.height + 28
        if not nested:
            height += 48
        if target and item.get("url"):
            height += 30
        height += padding

        background = (247, 247, 247) if nested else (255, 255, 255)
        image = Image.new("RGB", (width, max(height, 180)), background)
        draw = ImageDraw.Draw(image, "RGBA")
        safe_breaks: list[int] = []
        y = padding

        avatar_size = 52 if nested else 68
        avatar = rounded_avatar(
            str(item.get("avatar_path", "")), avatar_size, str(item.get("name", ""))
        )
        image.paste(avatar, (padding, y), avatar)
        text_x = padding + avatar_size + 14
        draw.text(
            (text_x, y + 2),
            str(item.get("name", "")),
            font=name_font,
            fill=(0, 122, 255),
        )
        handle = str(item.get("screen_name", ""))
        created = str(item.get("created_at", ""))[:25]
        meta = f"@{handle}" if handle else ""
        if created:
            meta = f"{meta}  ·  {created}" if meta else created
        draw.text(
            (text_x, y + name_font.size + 7), meta, font=muted_font, fill=(83, 100, 113)
        )
        if not nested:
            logo_font = font(34)
            draw.text(
                (width - padding - 28, y + 8), "X", font=logo_font, fill=(15, 20, 25)
            )
        y += header_height
        safe_breaks.append(y)

        if text_lines:
            for line in text_lines:
                y = draw_lines(
                    image,
                    draw,
                    [line],
                    (padding, y),
                    body_font,
                    (15, 20, 25),
                    body_line,
                )
                safe_breaks.append(y)
            y += 14

        if translation_lines:
            box_top = y
            box_height = 28 + len(translation_lines) * translation_line + 12
            draw.rounded_rectangle(
                (padding, box_top, width - padding, box_top + box_height),
                radius=10,
                fill=(235, 246, 255),
            )
            label_font = font(16 if nested else 18)
            draw.text(
                (padding + 12, y + 7), "翻译", font=label_font, fill=(0, 122, 255)
            )
            y += 28
            for line in translation_lines:
                y = draw_lines(
                    image,
                    draw,
                    [line],
                    (padding + 12, y),
                    translation_font,
                    (37, 72, 104),
                    translation_line,
                )
                safe_breaks.append(y)
            y = box_top + box_height + 8

        if media:
            y, media_breaks = draw_media_grid(
                image, draw, media, padding, y, content_width
            )
            safe_breaks.extend(media_breaks)
            y += 16

        if quote_image is not None:
            quote_x = padding + 8
            draw.rounded_rectangle(
                (padding, y, width - padding, y + quote_image.height + 16),
                radius=12,
                fill=(247, 247, 247),
                outline=(220, 226, 230),
                width=1,
            )
            image.paste(quote_image, (quote_x, y + 8))
            safe_breaks.extend(y + 8 + value for value in quote_safe)
            y += quote_image.height + 28
            safe_breaks.append(y)

        if not nested:
            stats_font = font(20)
            stats = (
                f"回复 {item.get('replies', 0)}     "
                f"转推 {item.get('retweets', 0)}     "
                f"喜欢 {item.get('likes', 0)}     "
                f"浏览 {item.get('views', 0)}"
            )
            draw.line((padding, y, width - padding, y), fill=(225, 232, 237), width=1)
            draw.text((padding, y + 13), stats, font=stats_font, fill=(83, 100, 113))
            y += 48
            safe_breaks.append(y)
        if target and item.get("url"):
            draw.text(
                (padding, y),
                "查看原文  " + str(item["url"]),
                font=muted_font,
                fill=(0, 122, 255),
            )
            y += 30
            safe_breaks.append(y)

        final_height = min(image.height, y + padding)
        return image.crop((0, 0, width, final_height)), [
            value for value in safe_breaks if 0 < value < final_height
        ]

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
            crop = image.crop((0, start, image.width, end))
            page = Image.new(
                "RGB", (image.width, crop.height + footer_height), (255, 255, 255)
            )
            page.paste(crop, (0, 0))
            pages.append(page)
            start = end
        page_font = font(16)
        total = len(pages)
        for index, page in enumerate(pages, 1):
            page_draw = ImageDraw.Draw(page)
            page_draw.text(
                (page.width - 80, page.height - 27),
                f"{index}/{total}",
                font=page_font,
                fill=(113, 118, 123),
            )
        return pages

    def conversation_pages():
        width = 800
        blocks: list[tuple[Any, list[int]]] = []
        ancestors = list(spec.get("ancestors", []))
        for item in ancestors:
            blocks.append(render_tweet(item, width=width))
        target_item = spec["target"]
        blocks.append(
            render_tweet(
                target_item,
                quote=spec.get("quote"),
                width=width,
                target=True,
            )
        )
        label_height = 38 if ancestors else 0
        gap = 14
        total_height = 24 + label_height + sum(block.height for block, _ in blocks)
        total_height += gap * max(0, len(blocks) - 1) + 24
        canvas = Image.new("RGB", (width, total_height), (242, 244, 245))
        canvas_draw = ImageDraw.Draw(canvas)
        y = 24
        safe: list[int] = []
        if ancestors:
            label_font = font(19)
            canvas_draw.text((25, y), "THREAD", font=label_font, fill=(83, 100, 113))
            y += label_height
        for index, (block, block_safe) in enumerate(blocks):
            canvas.paste(block, (0, y))
            safe.extend(y + value for value in block_safe)
            y += block.height
            safe.append(y)
            if index < len(blocks) - 1:
                y += gap
                arrow_font = font(16)
                canvas_draw.text(
                    (30, y - 2), "↓ Reply", font=arrow_font, fill=(113, 118, 123)
                )
        return paginate(canvas, safe, int(spec.get("max_height", 4096)))

    def sublist_image():
        width = 800
        sections = [
            ("核心成员（默认推送）", spec.get("active_core", []), (0, 122, 255)),
            ("已屏蔽核心成员", spec.get("muted_core", []), (113, 118, 123)),
            ("额外订阅", spec.get("extra_subs", []), (0, 186, 124)),
        ]
        visible = [
            (title, values, color) for title, values, color in sections if values
        ]
        height = 120 + sum(54 + len(values) * 36 for _, values, _ in visible)
        if not visible:
            height += 50
        image = Image.new("RGB", (width, height), (245, 247, 249))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (24, 24, width - 24, height - 24), radius=16, fill=(255, 255, 255)
        )
        draw.text((52, 48), "本群订阅", font=font(32), fill=(15, 20, 25))
        y = 102
        for title, values, color in visible:
            draw.text((52, y), title, font=font(22), fill=color)
            y += 38
            for value in values:
                draw.text((72, y), f"@{value}", font=font(22), fill=(15, 20, 25))
                y += 36
            y += 16
        if not visible:
            draw.text((52, y), "暂无订阅", font=font(22), fill=(113, 118, 123))
        return [image]

    def live_image():
        width = 800
        title_font = font(34)
        member_font = font(24)
        title_lines = wrap(str(spec.get("title", "")), title_font, width - 100)
        member_lines = wrap(
            "、".join(spec.get("members", [])), member_font, width - 100
        )
        height = 210 + len(title_lines) * 47 + len(member_lines) * 34
        image = Image.new("RGB", (width, height), (248, 249, 250))
        draw = ImageDraw.Draw(image)
        draw.rounded_rectangle(
            (24, 24, width - 24, height - 24), radius=18, fill=(255, 255, 255)
        )
        draw.ellipse((52, 55, 78, 81), fill=(244, 33, 46))
        draw.text((94, 48), "直播即将开始", font=font(25), fill=(244, 33, 46))
        y = 105
        y = draw_lines(image, draw, title_lines, (52, y), title_font, (15, 20, 25), 47)
        y += 14
        y = draw_lines(
            image, draw, member_lines, (52, y), member_font, (83, 100, 113), 34
        )
        y += 20
        draw.rounded_rectangle(
            (52, y, width - 52, y + 50), radius=10, fill=(15, 20, 25)
        )
        draw.text(
            (70, y + 10),
            str(spec.get("start_time_display", "")),
            font=font(23),
            fill=(255, 255, 255),
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
        image = Image.new("RGB", (width, height), (245, 245, 243))
        draw = ImageDraw.Draw(image)
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
                    draw.text(
                        (text_x, event_y + 5),
                        str(event.get("time_disp", "")),
                        font=font(13),
                        fill=(76, 76, 76),
                    )
                    title_lines = wrap(
                        str(event.get("title", "")), font(13), x2 - text_x - 15
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
    except Exception as exc:  # noqa: BLE001 - process boundary reports all errors
        print(f"{type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
