"""Data structures used by the tagfetch pipeline."""

from dataclasses import dataclass, field
from pathlib import Path

from .tweet import TweetConversation


@dataclass(frozen=True)
class DiscoveredPost:
    tag: str
    url: str
    tweet_id: str


@dataclass(frozen=True)
class DownloadedImage:
    source_url: str
    data: bytes
    mime_type: str
    source_tweet_id: str
    author_handle: str
    media_index: int
    is_original_photo: bool = False
    local_path: Path | None = None


@dataclass
class PreparedCandidate:
    tweet_id: str
    url: str
    conversation: TweetConversation
    originals: list[DownloadedImage] = field(default_factory=list)


@dataclass(frozen=True)
class SafetyReview:
    status: str  # approved | rejected | inconclusive
    categories: tuple[str, ...] = ()
    reason: str = ""
