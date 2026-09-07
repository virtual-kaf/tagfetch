from datetime import datetime, timezone

import pytest

from nonebot_plugin_tagfetch.models import DownloadedImage, PreparedCandidate
from nonebot_plugin_tagfetch.models.tweet import (
    TweetAuthor,
    TweetConversation,
    TweetItem,
    TweetMedia,
)
from nonebot_plugin_tagfetch.services import pending_queue
from nonebot_plugin_tagfetch.storage import (
    claim_pending_dispatch_hour,
    enqueue_pending_candidate,
    get_pending_candidate_count,
    is_pending_candidate,
    move_pending_candidate_to_back,
    peek_pending_candidate,
    remove_pending_candidate,
)


def _candidate(tweet_id: str, local_path=None) -> PreparedCandidate:
    target = TweetItem(
        id=tweet_id,
        url=f"https://x.com/artist/status/{tweet_id}",
        author=TweetAuthor(id="7", name="Artist", screen_name="artist"),
        text="#art new work",
        created_at="2026-09-07 12:00:00 JST",
        media=[
            TweetMedia(
                url="https://pbs.twimg.com/media/example.jpg",
                width=1200,
                height=900,
            )
        ],
        likes=500,
    )
    return PreparedCandidate(
        tweet_id=tweet_id,
        url=target.url,
        conversation=TweetConversation(target=target),
        originals=[
            DownloadedImage(
                source_url="https://pbs.twimg.com/media/example.jpg?name=orig",
                data=b"original",
                mime_type="image/jpeg",
                source_tweet_id=tweet_id,
                author_handle="artist",
                media_index=0,
                is_original_photo=True,
                local_path=local_path,
            )
        ],
    )


def test_pending_candidates_are_durable_fifo_and_deduplicated(tmp_path):
    database = tmp_path / "state.sqlite3"
    image_path = tmp_path / "original.jpg"
    first = _candidate("101", image_path)
    second = _candidate("102")

    assert enqueue_pending_candidate(first, path=database)
    assert enqueue_pending_candidate(second, path=database)
    assert not enqueue_pending_candidate(first, path=database)
    assert get_pending_candidate_count(path=database) == 2
    assert is_pending_candidate("101", path=database)

    restored = peek_pending_candidate(path=database)
    assert restored is not None
    assert restored.tweet_id == "101"
    assert restored.conversation.target is not None
    assert restored.conversation.target.author.screen_name == "artist"
    assert restored.conversation.target.media[0].width == 1200
    assert restored.originals[0].data == b"original"
    assert restored.originals[0].local_path == image_path

    assert remove_pending_candidate("101", path=database)
    assert peek_pending_candidate(path=database).tweet_id == "102"


def test_incomplete_candidate_rotates_to_the_back(tmp_path):
    database = tmp_path / "state.sqlite3"
    assert enqueue_pending_candidate(_candidate("101"), path=database)
    assert enqueue_pending_candidate(_candidate("102"), path=database)

    assert move_pending_candidate_to_back("101", path=database)
    assert peek_pending_candidate(path=database).tweet_id == "102"


def test_dispatch_hour_can_only_be_claimed_once(tmp_path):
    database = tmp_path / "state.sqlite3"
    ten_cst = datetime(2026, 9, 7, 2, 10, tzinfo=timezone.utc)
    same_hour = datetime(2026, 9, 7, 2, 59, tzinfo=timezone.utc)
    next_hour = datetime(2026, 9, 7, 3, tzinfo=timezone.utc)

    assert claim_pending_dispatch_hour("101", at=ten_cst, path=database)
    assert not claim_pending_dispatch_hour("102", at=same_hour, path=database)
    assert claim_pending_dispatch_hour("102", at=next_hour, path=database)


@pytest.mark.asyncio
async def test_dispatch_sends_one_and_removes_it_after_all_groups(monkeypatch):
    candidate = _candidate("101")
    delivered = False
    removed = []
    cleaned = []

    async def broadcast(_bot, candidates, groups, *, cleanup):
        nonlocal delivered
        assert candidates == [candidate]
        assert groups == ["1", "2"]
        assert cleanup is False
        delivered = True

    monkeypatch.setattr(pending_queue, "peek_pending_candidate", lambda: candidate)
    monkeypatch.setattr(
        pending_queue,
        "has_pending_delivery",
        lambda _tweet_id, _groups: not delivered,
    )
    monkeypatch.setattr(
        pending_queue, "claim_pending_dispatch_hour", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(pending_queue, "broadcast_to_groups", broadcast)
    monkeypatch.setattr(
        pending_queue,
        "remove_pending_candidate",
        lambda tweet_id: removed.append(tweet_id) or True,
    )
    monkeypatch.setattr(
        pending_queue,
        "cleanup_spooled_originals",
        lambda candidates: cleaned.extend(candidates),
    )
    monkeypatch.setattr(pending_queue, "get_pending_candidate_count", lambda: 0)

    result = await pending_queue.dispatch_one_pending_candidate(
        object(), ["1", "2"]
    )

    assert result == "delivered"
    assert removed == ["101"]
    assert cleaned == [candidate]


@pytest.mark.asyncio
async def test_incomplete_dispatch_retains_queue_and_spooled_files(monkeypatch):
    candidate = _candidate("101")

    async def broadcast(_bot, _candidates, _groups, *, cleanup):
        assert cleanup is False

    monkeypatch.setattr(pending_queue, "peek_pending_candidate", lambda: candidate)
    monkeypatch.setattr(
        pending_queue, "has_pending_delivery", lambda _tweet_id, _groups: True
    )
    monkeypatch.setattr(
        pending_queue, "claim_pending_dispatch_hour", lambda *_args, **_kwargs: True
    )
    monkeypatch.setattr(pending_queue, "broadcast_to_groups", broadcast)
    monkeypatch.setattr(pending_queue, "get_pending_candidate_count", lambda: 1)
    rotated = []
    monkeypatch.setattr(
        pending_queue,
        "move_pending_candidate_to_back",
        lambda tweet_id: rotated.append(tweet_id) or True,
    )
    monkeypatch.setattr(
        pending_queue,
        "remove_pending_candidate",
        lambda _tweet_id: pytest.fail("incomplete candidate must remain queued"),
    )
    monkeypatch.setattr(
        pending_queue,
        "cleanup_spooled_originals",
        lambda _candidates: pytest.fail("retained candidate files must remain"),
    )

    result = await pending_queue.dispatch_one_pending_candidate(object(), ["1"])

    assert result == "retained"
    assert rotated == ["101"]


@pytest.mark.asyncio
async def test_dispatch_hour_gate_blocks_a_second_candidate(monkeypatch):
    candidate = _candidate("101")

    monkeypatch.setattr(pending_queue, "peek_pending_candidate", lambda: candidate)
    monkeypatch.setattr(
        pending_queue, "has_pending_delivery", lambda _tweet_id, _groups: True
    )
    monkeypatch.setattr(
        pending_queue, "claim_pending_dispatch_hour", lambda *_args, **_kwargs: False
    )

    async def forbidden_broadcast(*_args, **_kwargs):
        raise AssertionError("hourly gate must prevent a second send")

    monkeypatch.setattr(pending_queue, "broadcast_to_groups", forbidden_broadcast)

    result = await pending_queue.dispatch_one_pending_candidate(object(), ["1"])

    assert result == "rate_limited"
