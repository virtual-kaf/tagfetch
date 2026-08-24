"""FxTwitter conversation client owned by Tagfetch."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from urllib.request import Request, urlopen

from nonebot import logger

from ..config import FXTWITTER_API_BASE, JST, REQUEST_TIMEOUT_SECONDS
from ..models.tweet import TweetAuthor, TweetConversation, TweetItem, TweetMedia


def _parse_date(raw: str) -> str:
    try:
        parsed = datetime.strptime(raw, "%a %b %d %H:%M:%S %z %Y")
    except ValueError:
        parsed = datetime.strptime(raw, "%a %b %d %H:%M:%S +0000 %Y").replace(
            tzinfo=timezone.utc
        )
    return parsed.astimezone(JST).strftime("%Y-%m-%d %H:%M:%S JST")


def _parse_parent_id(raw: dict) -> str | None:
    """Read reply parent IDs from both legacy and current FxTwitter fields."""
    reply_ref = raw.get("in_reply_to") or raw.get("replying_to")
    if isinstance(reply_ref, dict):
        reply_ref = reply_ref.get("status") or reply_ref.get("id")
    if reply_ref is None or reply_ref == "":
        return None
    return str(reply_ref)


def _parse_tweet(raw: dict) -> TweetItem:
    author_raw = raw.get("author", {})
    author = TweetAuthor(
        id=str(author_raw.get("id", "")),
        name=str(author_raw.get("name", "")),
        screen_name=str(author_raw.get("screen_name", "")),
        avatar_url=str(author_raw.get("avatar_url", "")),
    )
    media: list[TweetMedia] = []
    media_raw = raw.get("media")
    if isinstance(media_raw, dict):
        for photo in media_raw.get("photos", []):
            media.append(
                TweetMedia(
                    url=str(photo.get("url", "")),
                    width=int(photo.get("width", 0) or 0),
                    height=int(photo.get("height", 0) or 0),
                    type="photo",
                )
            )
        for video in media_raw.get("videos", []):
            media.append(
                TweetMedia(
                    url=str(video.get("url", "")),
                    thumbnail_url=str(video.get("thumbnail_url") or ""),
                    width=int(video.get("width", 0) or 0),
                    height=int(video.get("height", 0) or 0),
                    type="video",
                )
            )

    parent_id = _parse_parent_id(raw)
    return TweetItem(
        id=str(raw.get("id", "")),
        url=str(raw.get("url", "")),
        author=author,
        text=str(raw.get("text", "")),
        created_at=_parse_date(str(raw.get("created_at", ""))),
        media=media,
        likes=int(raw.get("likes", 0) or 0),
        retweets=int(raw.get("retweets", 0) or 0),
        replies=int(raw.get("replies", 0) or 0),
        views=int(raw.get("views", 0) or 0),
        is_reply=bool(parent_id),
        parent_id=parent_id,
    )


def fetch_conversation(tweet_id: str) -> TweetConversation:
    """Fetch a target tweet, ancestors, replies, quote, and media."""
    url = f"{FXTWITTER_API_BASE}/2/conversation/{tweet_id}?ranking_mode=likes"
    request = Request(url, headers={"User-Agent": "tagfetch/1.0"})
    with urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:
        data = json.loads(response.read())
    if data.get("code") != 200:
        raise RuntimeError(f"FxTwitter API code {data.get('code')}")

    tweet_raw = data.get("status") or data
    target = _parse_tweet(tweet_raw)
    ancestors = [
        _parse_tweet(item)
        for item in (data.get("thread") or [])
        if str(item.get("id", "")) != target.id
    ]
    ancestors.sort(key=lambda tweet: tweet.created_at)
    root = ancestors[0] if ancestors else None

    quote_raw = tweet_raw.get("quote")
    quote = (
        _parse_tweet(quote_raw)
        if isinstance(quote_raw, dict) and quote_raw.get("type") != "tombstone"
        else None
    )
    replies = [
        _parse_tweet(item)
        for item in (data.get("replies") or [])
        if item.get("type") != "tombstone"
    ]
    logger.info(
        "[TagfetchFxTwitter] target={} ancestors={} quote={} replies={}",
        target.id,
        len(ancestors),
        quote.id if quote else "none",
        len(replies),
    )
    return TweetConversation(
        root=root,
        ancestors=ancestors,
        target=target,
        quote=quote,
        replies=replies,
    )
