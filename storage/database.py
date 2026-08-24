"""SQLite-backed group switches, rejection ledger, and delivery ledger."""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import datetime, timezone
from pathlib import Path

from ..config import STATE_DB

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
            """
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
