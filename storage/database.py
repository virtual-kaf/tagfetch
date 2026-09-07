"""SQLite-backed group switches, rejection ledger, and delivery ledger."""

from __future__ import annotations

import base64
import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..config import CST, STATE_DB
from ..models import DownloadedImage, PreparedCandidate
from ..models.tweet import TweetAuthor, TweetConversation, TweetItem, TweetMedia

_DB_LOCK = threading.RLock()


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _connect(path: Path = STATE_DB) -> sqlite3.Connection:
    path.parent.mkdir(parents=True, exist_ok=True)
    connection = sqlite3.connect(path, timeout=10)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA journal_mode=WAL")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def initialize_database(path: Path = STATE_DB) -> None:
    with _DB_LOCK, _connect(path) as connection:
        connection.executescript(
            """
            CREATE TABLE IF NOT EXISTS group_switches (
                group_id TEXT PRIMARY KEY,
                enabled INTEGER NOT NULL CHECK (enabled IN (0, 1)),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS rejections (
                tweet_id TEXT PRIMARY KEY,
                url TEXT NOT NULL,
                categories_json TEXT NOT NULL,
                reason TEXT NOT NULL,
                rejected_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS deliveries (
                tweet_id TEXT NOT NULL,
                group_id TEXT NOT NULL,
                card_sent_at TEXT NOT NULL,
                originals_sent INTEGER NOT NULL DEFAULT 0
                    CHECK (originals_sent IN (0, 1)),
                originals_sent_at TEXT,
                PRIMARY KEY (tweet_id, group_id)
            );
            CREATE TABLE IF NOT EXISTS discovery_health (
                source TEXT PRIMARY KEY,
                consecutive_failures INTEGER NOT NULL DEFAULT 0
                    CHECK (consecutive_failures >= 0),
                updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_candidates (
                sequence INTEGER PRIMARY KEY AUTOINCREMENT,
                tweet_id TEXT NOT NULL UNIQUE,
                url TEXT NOT NULL,
                payload_json TEXT NOT NULL,
                queued_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS pending_dispatch_hours (
                hour_key TEXT PRIMARY KEY,
                tweet_id TEXT NOT NULL,
                attempted_at TEXT NOT NULL
            );
            """
        )


def _candidate_json(candidate: PreparedCandidate) -> str:
    payload = asdict(candidate)
    for raw_image, image in zip(payload["originals"], candidate.originals):
        raw_image["data"] = base64.b64encode(image.data).decode("ascii")
        raw_image["local_path"] = (
            str(image.local_path) if image.local_path is not None else None
        )
    return json.dumps(
        {"version": 1, "candidate": payload},
        ensure_ascii=False,
        separators=(",", ":"),
    )


def _tweet_item(raw: Any) -> TweetItem | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ValueError("invalid queued tweet item")
    values = dict(raw)
    author = values.get("author")
    media = values.get("media")
    if not isinstance(author, dict) or not isinstance(media, list):
        raise ValueError("invalid queued tweet fields")
    values["author"] = TweetAuthor(**author)
    values["media"] = [TweetMedia(**item) for item in media]
    return TweetItem(**values)


def _candidate_from_json(raw: str) -> PreparedCandidate:
    document = json.loads(raw)
    if not isinstance(document, dict) or document.get("version") != 1:
        raise ValueError("unsupported queued candidate payload")
    payload = document.get("candidate")
    if not isinstance(payload, dict):
        raise ValueError("invalid queued candidate payload")
    conversation = payload.get("conversation")
    originals = payload.get("originals")
    if not isinstance(conversation, dict) or not isinstance(originals, list):
        raise ValueError("invalid queued candidate fields")
    restored_conversation = TweetConversation(
        root=_tweet_item(conversation.get("root")),
        ancestors=[_tweet_item(item) for item in conversation.get("ancestors", [])],
        target=_tweet_item(conversation.get("target")),
        quote=_tweet_item(conversation.get("quote")),
        replies=[_tweet_item(item) for item in conversation.get("replies", [])],
    )
    restored_originals: list[DownloadedImage] = []
    for raw_image in originals:
        if not isinstance(raw_image, dict):
            raise ValueError("invalid queued image")
        values = dict(raw_image)
        encoded = values.get("data", "")
        local_path = values.get("local_path")
        if not isinstance(encoded, str) or not (
            local_path is None or isinstance(local_path, str)
        ):
            raise ValueError("invalid queued image fields")
        values["data"] = base64.b64decode(encoded, validate=True)
        values["local_path"] = Path(local_path) if local_path else None
        restored_originals.append(DownloadedImage(**values))
    return PreparedCandidate(
        tweet_id=str(payload["tweet_id"]),
        url=str(payload["url"]),
        conversation=restored_conversation,
        originals=restored_originals,
    )


def enqueue_pending_candidate(
    candidate: PreparedCandidate, *, path: Path = STATE_DB
) -> bool:
    """Append a candidate to the durable FIFO unless it is already queued."""
    initialize_database(path)
    with _DB_LOCK, _connect(path) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO pending_candidates
                (tweet_id, url, payload_json, queued_at)
            VALUES (?, ?, ?, ?)
            """,
            (
                str(candidate.tweet_id),
                str(candidate.url),
                _candidate_json(candidate),
                _utc_now(),
            ),
        )
    return cursor.rowcount == 1


def peek_pending_candidate(*, path: Path = STATE_DB) -> PreparedCandidate | None:
    initialize_database(path)
    with _connect(path) as connection:
        row = connection.execute(
            """
            SELECT payload_json
            FROM pending_candidates
            ORDER BY sequence
            LIMIT 1
            """
        ).fetchone()
    return _candidate_from_json(row["payload_json"]) if row is not None else None


def remove_pending_candidate(tweet_id: str, *, path: Path = STATE_DB) -> bool:
    initialize_database(path)
    with _DB_LOCK, _connect(path) as connection:
        cursor = connection.execute(
            "DELETE FROM pending_candidates WHERE tweet_id = ?",
            (str(tweet_id),),
        )
    return cursor.rowcount == 1


def move_pending_candidate_to_back(
    tweet_id: str, *, path: Path = STATE_DB
) -> bool:
    """Rotate an incomplete candidate so one failure cannot block the FIFO."""
    initialize_database(path)
    with _DB_LOCK, _connect(path) as connection:
        cursor = connection.execute(
            """
            UPDATE pending_candidates
            SET sequence = (
                SELECT COALESCE(MAX(sequence), 0) + 1
                FROM pending_candidates
            )
            WHERE tweet_id = ?
            """,
            (str(tweet_id),),
        )
    return cursor.rowcount == 1


def is_pending_candidate(tweet_id: str, *, path: Path = STATE_DB) -> bool:
    initialize_database(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT 1 FROM pending_candidates WHERE tweet_id = ?",
            (str(tweet_id),),
        ).fetchone()
    return row is not None


def get_pending_candidate_count(*, path: Path = STATE_DB) -> int:
    initialize_database(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT COUNT(*) AS count FROM pending_candidates"
        ).fetchone()
    return int(row["count"])


def claim_pending_dispatch_hour(
    tweet_id: str,
    *,
    at: datetime | None = None,
    path: Path = STATE_DB,
) -> bool:
    """Atomically reserve the current CST hour for one candidate attempt."""
    current = at or datetime.now(timezone.utc)
    hour_key = current.astimezone(CST).strftime("%Y-%m-%dT%H")
    initialize_database(path)
    with _DB_LOCK, _connect(path) as connection:
        cursor = connection.execute(
            """
            INSERT OR IGNORE INTO pending_dispatch_hours
                (hour_key, tweet_id, attempted_at)
            VALUES (?, ?, ?)
            """,
            (hour_key, str(tweet_id), current.astimezone(timezone.utc).isoformat()),
        )
    return cursor.rowcount == 1


def get_remote_discovery_failures(*, path: Path = STATE_DB) -> int:
    initialize_database(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT consecutive_failures FROM discovery_health WHERE source = ?",
            ("remote_grok",),
        ).fetchone()
    return int(row["consecutive_failures"]) if row is not None else 0


def record_remote_discovery_failure(*, path: Path = STATE_DB) -> int:
    initialize_database(path)
    with _DB_LOCK, _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO discovery_health
                (source, consecutive_failures, updated_at)
            VALUES (?, 1, ?)
            ON CONFLICT(source) DO UPDATE SET
                consecutive_failures = consecutive_failures + 1,
                updated_at = excluded.updated_at
            """,
            ("remote_grok", _utc_now()),
        )
        row = connection.execute(
            "SELECT consecutive_failures FROM discovery_health WHERE source = ?",
            ("remote_grok",),
        ).fetchone()
    return int(row["consecutive_failures"])


def reset_remote_discovery_failures(*, path: Path = STATE_DB) -> None:
    initialize_database(path)
    with _DB_LOCK, _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO discovery_health
                (source, consecutive_failures, updated_at)
            VALUES (?, 0, ?)
            ON CONFLICT(source) DO UPDATE SET
                consecutive_failures = 0,
                updated_at = excluded.updated_at
            """,
            ("remote_grok", _utc_now()),
        )


def is_group_enabled(group_id: int | str, *, path: Path = STATE_DB) -> bool:
    initialize_database(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT enabled FROM group_switches WHERE group_id = ?",
            (str(group_id),),
        ).fetchone()
    return bool(row["enabled"]) if row is not None else False


def set_group_enabled(
    group_id: int | str, enabled: bool, *, path: Path = STATE_DB
) -> None:
    initialize_database(path)
    with _DB_LOCK, _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO group_switches (group_id, enabled, updated_at)
            VALUES (?, ?, ?)
            ON CONFLICT(group_id) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (str(group_id), int(enabled), _utc_now()),
        )


def get_enabled_group_ids(*, path: Path = STATE_DB) -> list[str]:
    initialize_database(path)
    with _connect(path) as connection:
        rows = connection.execute(
            "SELECT group_id FROM group_switches WHERE enabled = 1 ORDER BY group_id"
        ).fetchall()
    return [str(row["group_id"]) for row in rows]


def is_rejected(tweet_id: str, *, path: Path = STATE_DB) -> bool:
    initialize_database(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT 1 FROM rejections WHERE tweet_id = ?", (str(tweet_id),)
        ).fetchone()
    return row is not None


def record_rejection(
    tweet_id: str,
    url: str,
    categories: Iterable[str],
    reason: str,
    *,
    path: Path = STATE_DB,
) -> None:
    initialize_database(path)
    payload = json.dumps(list(categories), ensure_ascii=False)
    with _DB_LOCK, _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO rejections
                (tweet_id, url, categories_json, reason, rejected_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tweet_id) DO NOTHING
            """,
            (str(tweet_id), str(url), payload, str(reason), _utc_now()),
        )


def has_delivery(tweet_id: str, group_id: int | str, *, path: Path = STATE_DB) -> bool:
    initialize_database(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT 1 FROM deliveries WHERE tweet_id = ? AND group_id = ?",
            (str(tweet_id), str(group_id)),
        ).fetchone()
    return row is not None


def has_pending_delivery(
    tweet_id: str, group_ids: Iterable[int | str], *, path: Path = STATE_DB
) -> bool:
    return any(
        not has_delivery(tweet_id, group_id, path=path) for group_id in group_ids
    )


def record_card_delivery(
    tweet_id: str,
    group_id: int | str,
    *,
    originals_sent: bool,
    path: Path = STATE_DB,
) -> None:
    initialize_database(path)
    now = _utc_now()
    with _DB_LOCK, _connect(path) as connection:
        connection.execute(
            """
            INSERT INTO deliveries
                (tweet_id, group_id, card_sent_at, originals_sent,
                 originals_sent_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT(tweet_id, group_id) DO NOTHING
            """,
            (
                str(tweet_id),
                str(group_id),
                now,
                int(originals_sent),
                now if originals_sent else None,
            ),
        )


def mark_originals_sent(
    tweet_ids: Iterable[str], group_id: int | str, *, path: Path = STATE_DB
) -> None:
    values = [(str(tweet_id), str(group_id)) for tweet_id in tweet_ids]
    if not values:
        return
    initialize_database(path)
    now = _utc_now()
    with _DB_LOCK, _connect(path) as connection:
        connection.executemany(
            """
            UPDATE deliveries
            SET originals_sent = 1, originals_sent_at = ?
            WHERE tweet_id = ? AND group_id = ?
            """,
            [(now, tweet_id, group_id_value) for tweet_id, group_id_value in values],
        )


def get_delivery(
    tweet_id: str, group_id: int | str, *, path: Path = STATE_DB
) -> dict[str, object] | None:
    initialize_database(path)
    with _connect(path) as connection:
        row = connection.execute(
            "SELECT * FROM deliveries WHERE tweet_id = ? AND group_id = ?",
            (str(tweet_id), str(group_id)),
        ).fetchone()
    return dict(row) if row is not None else None
