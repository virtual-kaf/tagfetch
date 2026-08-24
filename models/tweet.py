"""Tweet conversation models owned by Tagfetch."""

from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class TweetAuthor:
    id: str = ""
    name: str = ""
    screen_name: str = ""
    avatar_url: str = ""


@dataclass
class TweetMedia:
    url: str = ""
    thumbnail_url: str = ""
    width: int = 0
    height: int = 0
    type: str = "photo"  # photo | video


@dataclass
class TweetItem:
    id: str
    url: str = ""
    author: TweetAuthor = field(default_factory=TweetAuthor)
    text: str = ""
    created_at: str = ""
    media: list[TweetMedia] = field(default_factory=list)
    likes: int = 0
    retweets: int = 0
    replies: int = 0
    views: int = 0
    is_reply: bool = False
    parent_id: str | None = None
    translated_text: str = ""
    is_valuable: bool = True


@dataclass
class TweetConversation:
    root: TweetItem | None = None
    ancestors: list[TweetItem] = field(default_factory=list)
    target: TweetItem | None = None
    quote: TweetItem | None = None
    replies: list[TweetItem] = field(default_factory=list)
