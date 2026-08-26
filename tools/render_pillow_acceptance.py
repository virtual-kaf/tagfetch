"""Generate the untranslated Tagfetch Pillow acceptance card for ASU."""

from __future__ import annotations

import argparse
import asyncio
from pathlib import Path

from nonebot_plugin_tagfetch.clients.fxtwitter import fetch_conversation
from nonebot_plugin_tagfetch.models import PreparedCandidate
from nonebot_plugin_tagfetch.renderer import engine as renderer
from nonebot_plugin_tagfetch.services.broadcaster import conversation_without_avatars

TWEET_ID = "2091513722498105515"
TWEET_URL = f"https://x.com/asu_virtual/status/{TWEET_ID}"


def _use_acceptance_directories(root: Path) -> None:
    renderer.CARD_DIR = root
    renderer.RENDER_CACHE_DIR = root / ".cache" / "tagfetch"
    renderer.MEDIA_CACHE_DIR = renderer.RENDER_CACHE_DIR / "media"
    renderer.EMOJI_CACHE_DIR = renderer.RENDER_CACHE_DIR / "emoji"
    renderer.TEMP_DIR = renderer.RENDER_CACHE_DIR / "tmp"


async def render(output_dir: Path) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    _use_acceptance_directories(output_dir)

    conversation = await asyncio.to_thread(fetch_conversation, TWEET_ID)
    if conversation.target is None:
        raise RuntimeError("FxTwitter did not return the acceptance target")
    candidate = PreparedCandidate(
        tweet_id=TWEET_ID,
        url=TWEET_URL,
        conversation=conversation,
    )
    paths = await renderer.render_conversation_card(
        conversation_without_avatars(candidate)
    )
    if not paths:
        raise RuntimeError("tagfetch acceptance render failed")
    return paths


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(__file__).resolve().parents[1] / "acceptance",
    )
    args = parser.parse_args()
    paths = asyncio.run(render(args.output_dir.resolve()))
    print(*(str(path) for path in paths), sep="\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
