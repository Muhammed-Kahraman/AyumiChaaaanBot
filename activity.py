"""Persistent, non-monetary group activity scoring and moderation."""

from __future__ import annotations

import hashlib
import html
import json
import os
import random
import sqlite3
import time
from datetime import datetime, timedelta, timezone
from typing import Optional
from zoneinfo import ZoneInfo

from telegram.helpers import mention_html

from config import (
    ACTIVITY_DB_FILE,
    ACTIVITY_DEFAULT_GROUP_ID,
    ACTIVITY_LINK_DAILY_LIMIT,
    ACTIVITY_OWNER_USER_ID,
    ACTIVITY_OWNER_USERNAME,
    ACTIVITY_RECOMMENDATION_WEEKLY_LIMIT,
    ACTIVITY_TIMEZONE,
)


OWNER_TITLE = (
    "Anime Evrenleri 12. Boyut Baş Oyun Kurucusu, Spoiler Divanı "
    "Ebedî Genel Başkanı ve Kozmik Hakem-i Mutlak"
)


def _now() -> datetime:
    try:
        return datetime.now(ZoneInfo(ACTIVITY_TIMEZONE))
    except Exception:
        return datetime.now(timezone.utc)


def _periods() -> tuple[str, str]:
    current = _now()
    return current.date().isoformat(), current.strftime("%G-W%V")


def _is_group(chat_type: Optional[str]) -> bool:
    return chat_type in {"group", "supergroup"}


def _connect() -> sqlite3.Connection:
    directory = os.path.dirname(ACTIVITY_DB_FILE)
    if directory:
        os.makedirs(directory, exist_ok=True)
    db = sqlite3.connect(ACTIVITY_DB_FILE, timeout=15)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA busy_timeout=15000")
    db.executescript(
        """
        CREATE TABLE IF NOT EXISTS groups (
            chat_id INTEGER PRIMARY KEY,
            title TEXT NOT NULL DEFAULT '',
            chat_type TEXT NOT NULL DEFAULT '',
            last_seen REAL NOT NULL
        );
        CREATE TABLE IF NOT EXISTS members (
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            username TEXT NOT NULL DEFAULT '',
            display_name TEXT NOT NULL DEFAULT '',
            total_points INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            updated_at REAL NOT NULL,
            PRIMARY KEY (chat_id, user_id)
        );
        CREATE TABLE IF NOT EXISTS activity_events (
            event_key TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            user_id INTEGER NOT NULL,
            event_type TEXT NOT NULL,
            points INTEGER NOT NULL,
            source_id TEXT NOT NULL DEFAULT '',
            day_key TEXT NOT NULL DEFAULT '',
            week_key TEXT NOT NULL DEFAULT '',
            created_at REAL NOT NULL,
            metadata TEXT NOT NULL DEFAULT '{}'
        );
        CREATE INDEX IF NOT EXISTS idx_events_week
            ON activity_events(chat_id, week_key, event_type);
        CREATE TABLE IF NOT EXISTS penalty_reviews (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            chat_id INTEGER NOT NULL,
            vote_token TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            status TEXT NOT NULL DEFAULT 'pending',
            points_delta INTEGER NOT NULL DEFAULT -2,
            reason TEXT NOT NULL DEFAULT '',
            decided_by INTEGER NOT NULL DEFAULT 0,
            created_at REAL NOT NULL,
            decided_at REAL,
            UNIQUE(vote_token, user_id)
        );
        CREATE TABLE IF NOT EXISTS quiz_schedules (
            slot_key TEXT PRIMARY KEY,
            scheduled_at REAL NOT NULL,
            claimed_at REAL
        );
        CREATE TABLE IF NOT EXISTS quizzes (
            poll_id TEXT PRIMARY KEY,
            chat_id INTEGER NOT NULL,
            message_id INTEGER NOT NULL,
            correct_option_id INTEGER NOT NULL,
            expires_at REAL NOT NULL,
            deleted_at REAL
        );
        CREATE TABLE IF NOT EXISTS quiz_answers (
            poll_id TEXT NOT NULL,
            user_id INTEGER NOT NULL,
            selected_option_id INTEGER NOT NULL DEFAULT -1,
            answered_at REAL NOT NULL,
            PRIMARY KEY (poll_id, user_id)
        );
        CREATE INDEX IF NOT EXISTS idx_quizzes_expiry
            ON quizzes(expires_at, deleted_at);
        """
    )
    return db


def owner_id() -> int:
    return ACTIVITY_OWNER_USER_ID


def is_owner(user_id: int) -> bool:
    return bool(ACTIVITY_OWNER_USER_ID) and user_id == ACTIVITY_OWNER_USER_ID


def owner_label_html() -> str:
    if ACTIVITY_OWNER_USERNAME:
        return f"@{html.escape(ACTIVITY_OWNER_USERNAME)}"
    return mention_html(ACTIVITY_OWNER_USER_ID, "Yönetici")


def touch_group(chat_id: int, chat_type: str, title: str = "") -> None:
    if not _is_group(chat_type):
        return
    now = time.time()
    with _connect() as db:
        db.execute(
            """INSERT INTO groups(chat_id,title,chat_type,last_seen)
               VALUES(?,?,?,?)
               ON CONFLICT(chat_id) DO UPDATE SET
                 title=excluded.title, chat_type=excluded.chat_type,
                 last_seen=excluded.last_seen""",
            (chat_id, title or "", chat_type, now),
        )


def upsert_member(chat_id: int, user: object, chat_type: str) -> None:
    if not _is_group(chat_type) or not user:
        return
    username = str(getattr(user, "username", "") or "")
    first = str(getattr(user, "first_name", "") or "")
    last = str(getattr(user, "last_name", "") or "")
    display_name = " ".join(part for part in (first, last) if part).strip()
    now = time.time()
    with _connect() as db:
        db.execute(
            """INSERT INTO members(
                 chat_id,user_id,username,display_name,total_points,created_at,updated_at
               ) VALUES(?,?,?,?,0,?,?)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET
                 username=excluded.username,
                 display_name=excluded.display_name,
                 updated_at=excluded.updated_at""",
            (chat_id, int(user.id), username, display_name, now, now),
        )


def _add_event(
    db: sqlite3.Connection,
    chat_id: int,
    user_id: int,
    event_key: str,
    event_type: str,
    points: int,
    source_id: str = "",
    metadata: Optional[dict] = None,
) -> bool:
    day_key, week_key = _periods()
    cursor = db.execute(
        """INSERT OR IGNORE INTO activity_events(
             event_key,chat_id,user_id,event_type,points,source_id,
             day_key,week_key,created_at,metadata
           ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
        (
            event_key,
            chat_id,
            user_id,
            event_type,
            points,
            source_id,
            day_key,
            week_key,
            time.time(),
            json.dumps(metadata or {}, ensure_ascii=False),
        ),
    )
    if cursor.rowcount != 1:
        return False
    db.execute(
        """UPDATE members
           SET total_points=MAX(0,total_points+?), updated_at=?
           WHERE chat_id=? AND user_id=?""",
        (points, time.time(), chat_id, user_id),
    )
    return True


def record_link(chat_id: int, chat_type: str, user: object, message_id: int, url: str) -> bool:
    if not _is_group(chat_type) or not user or is_owner(int(user.id)):
        return False
    upsert_member(chat_id, user, chat_type)
    day_key, _ = _periods()
    digest = hashlib.sha256(url.split("?", 1)[0].encode()).hexdigest()[:24]
    event_key = f"link:{chat_id}:{message_id}:{digest}"
    with _connect() as db:
        if ACTIVITY_LINK_DAILY_LIMIT:
            count = db.execute(
                """SELECT COUNT(*) FROM activity_events
                   WHERE chat_id=? AND user_id=? AND event_type='link_share'
                     AND day_key=?""",
                (chat_id, int(user.id), day_key),
            ).fetchone()[0]
            if count >= ACTIVITY_LINK_DAILY_LIMIT:
                return False
        return _add_event(
            db, chat_id, int(user.id), event_key, "link_share", 2,
            source_id=str(message_id), metadata={"url": url.split("?", 1)[0]},
        )


def record_spoiler_vote(
    chat_id: int, chat_type: str, user: object, vote_token: str
) -> bool:
    if not _is_group(chat_type) or not user or is_owner(int(user.id)):
        return False
    upsert_member(chat_id, user, chat_type)
    with _connect() as db:
        return _add_event(
            db,
            chat_id,
            int(user.id),
            f"spoiler-vote:{vote_token}:{int(user.id)}",
            "spoiler_vote",
            1,
            source_id=vote_token,
        )


def record_resolution_bonus(
    chat_id: int, chat_type: str, vote_token: str, user_id: Optional[int]
) -> None:
    if not _is_group(chat_type) or not user_id or is_owner(user_id):
        return
    with _connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO members(chat_id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
            (chat_id, user_id, time.time(), time.time()),
        )
        _add_event(
            db,
            chat_id,
            user_id,
            f"spoiler-resolution:{vote_token}:{user_id}",
            "spoiler_resolution",
            2,
            source_id=vote_token,
        )


def recommendation_status(
    chat_id: int, chat_type: str, user: object, message_id: int, text: str
) -> str:
    """Return recorded, duplicate, limit, or ignored for one recommendation."""
    if not _is_group(chat_type) or not user or is_owner(int(user.id)):
        return "ignored"
    upsert_member(chat_id, user, chat_type)
    _, week_key = _periods()
    event_key = f"recommendation:{chat_id}:{message_id}"
    with _connect() as db:
        if db.execute(
            "SELECT 1 FROM activity_events WHERE event_key=?", (event_key,)
        ).fetchone():
            return "duplicate"
        if ACTIVITY_RECOMMENDATION_WEEKLY_LIMIT:
            count = db.execute(
                """SELECT COUNT(*) FROM activity_events
                   WHERE chat_id=? AND user_id=? AND event_type='recommendation'
                     AND week_key=?""",
                (chat_id, int(user.id), week_key),
            ).fetchone()[0]
            if count >= ACTIVITY_RECOMMENDATION_WEEKLY_LIMIT:
                return "limit"
        recorded = _add_event(
            db,
            chat_id,
            int(user.id),
            event_key,
            "recommendation",
            2,
            source_id=str(message_id),
            metadata={"text": text[:200]},
        )
        return "recorded" if recorded else "duplicate"


def record_recommendation(
    chat_id: int, chat_type: str, user: object, message_id: int, text: str
) -> bool:
    return recommendation_status(chat_id, chat_type, user, message_id, text) == "recorded"


def quiz_group_ids() -> list[int]:
    """Return groups that should receive quizzes, including the configured fallback."""
    ids = group_ids()
    if ACTIVITY_DEFAULT_GROUP_ID and ACTIVITY_DEFAULT_GROUP_ID not in ids:
        ids.append(ACTIVITY_DEFAULT_GROUP_ID)
    return ids


def ensure_quiz_schedule(now: Optional[datetime] = None) -> list[sqlite3.Row]:
    """Create two persistent random quiz slots for the current local week."""
    current = now or _now()
    week_key = current.strftime("%G-W%V")
    with _connect() as db:
        existing = db.execute(
            "SELECT * FROM quiz_schedules WHERE slot_key LIKE ? ORDER BY scheduled_at",
            (f"{week_key}:%",),
        ).fetchall()
        if not existing:
            week_start = current.replace(
                hour=0, minute=0, second=0, microsecond=0
            ) - timedelta(days=current.weekday())
            next_week = week_start + timedelta(days=7)
            lower = max(
                time.time() + 15 * 60,
                week_start.timestamp() + 60 * 60,
            )
            upper = next_week.timestamp() - 60 * 60
            if lower < upper:
                rng = random.SystemRandom()
                minute_start = int(lower // 60)
                minute_end = int(upper // 60)
                candidates = list(range(minute_start, minute_end + 1))
                if len(candidates) >= 2:
                    first, second = sorted(rng.sample(candidates, 2))
                    if second - first < 360:
                        second = min(minute_end, first + 360)
                    if second <= first:
                        second = first + max(1, (minute_end - first) // 2)
                    for index, minute in enumerate((first, second), start=1):
                        db.execute(
                            "INSERT INTO quiz_schedules(slot_key,scheduled_at) VALUES(?,?)",
                            (f"{week_key}:{index}", minute * 60),
                        )
                elif candidates:
                    db.execute(
                        "INSERT INTO quiz_schedules(slot_key,scheduled_at) VALUES(?,?)",
                        (f"{week_key}:1", candidates[0] * 60),
                    )
        return db.execute(
            "SELECT * FROM quiz_schedules WHERE slot_key LIKE ? ORDER BY scheduled_at",
            (f"{week_key}:%",),
        ).fetchall()


def next_quiz_slot(now_timestamp: Optional[float] = None) -> Optional[sqlite3.Row]:
    now_timestamp = time.time() if now_timestamp is None else now_timestamp
    ensure_quiz_schedule()
    with _connect() as db:
        return db.execute(
            """SELECT * FROM quiz_schedules
               WHERE claimed_at IS NULL AND scheduled_at <= ?
               ORDER BY scheduled_at LIMIT 1""",
            (now_timestamp,),
        ).fetchone()


def upcoming_quiz_slot() -> Optional[sqlite3.Row]:
    ensure_quiz_schedule()
    with _connect() as db:
        return db.execute(
            """SELECT * FROM quiz_schedules
               WHERE claimed_at IS NULL
               ORDER BY scheduled_at LIMIT 1"""
        ).fetchone()


def claim_quiz_slot(slot_key: str) -> bool:
    with _connect() as db:
        cursor = db.execute(
            """UPDATE quiz_schedules SET claimed_at=?
               WHERE slot_key=? AND claimed_at IS NULL""",
            (time.time(), slot_key),
        )
        return cursor.rowcount == 1


def register_quiz(
    poll_id: str,
    chat_id: int,
    message_id: int,
    correct_option_id: int,
    expires_at: float,
) -> None:
    with _connect() as db:
        db.execute(
            """INSERT OR REPLACE INTO quizzes(
                 poll_id,chat_id,message_id,correct_option_id,expires_at,deleted_at
               ) VALUES(?,?,?,?,?,NULL)""",
            (poll_id, chat_id, message_id, correct_option_id, expires_at),
        )


def record_quiz_answer(poll_id: str, user: object, option_ids: object) -> bool:
    """Record one answer and award +3 only when the first answer is correct."""
    if not user:
        return False
    selected = list(option_ids or [])
    selected_id = int(selected[0]) if selected else -1
    with _connect() as db:
        quiz = db.execute(
            "SELECT * FROM quizzes WHERE poll_id=?", (poll_id,)
        ).fetchone()
        if not quiz or time.time() > float(quiz["expires_at"]):
            return False
        cursor = db.execute(
            """INSERT OR IGNORE INTO quiz_answers(
                 poll_id,user_id,selected_option_id,answered_at
               ) VALUES(?,?,?,?)""",
            (poll_id, int(user.id), selected_id, time.time()),
        )
        if cursor.rowcount != 1:
            return False
        if is_owner(int(user.id)) or selected_id != int(quiz["correct_option_id"]):
            return False
        username = str(getattr(user, "username", "") or "")
        first = str(getattr(user, "first_name", "") or "")
        last = str(getattr(user, "last_name", "") or "")
        now = time.time()
        db.execute(
            """INSERT INTO members(
                 chat_id,user_id,username,display_name,total_points,created_at,updated_at
               ) VALUES(?,?,?,?,0,?,?)
               ON CONFLICT(chat_id,user_id) DO UPDATE SET
                 username=excluded.username,display_name=excluded.display_name,
                 updated_at=excluded.updated_at""",
            (quiz["chat_id"], int(user.id), username, " ".join(p for p in (first, last) if p), now, now),
        )
        return _add_event(
            db,
            int(quiz["chat_id"]),
            int(user.id),
            f"quiz:{poll_id}:{int(user.id)}",
            "quiz_correct",
            3,
            source_id=poll_id,
        )


def expired_quizzes(now_timestamp: Optional[float] = None) -> list[sqlite3.Row]:
    now_timestamp = time.time() if now_timestamp is None else now_timestamp
    with _connect() as db:
        return db.execute(
            """SELECT * FROM quizzes
               WHERE deleted_at IS NULL AND expires_at <= ?
               ORDER BY expires_at""",
            (now_timestamp,),
        ).fetchall()


def mark_quiz_deleted(poll_id: str) -> None:
    with _connect() as db:
        db.execute(
            "UPDATE quizzes SET deleted_at=? WHERE poll_id=?",
            (time.time(), poll_id),
        )


def find_member(chat_id: int, identifier: str) -> Optional[sqlite3.Row]:
    username = identifier.strip().lstrip("@").casefold()
    with _connect() as db:
        return db.execute(
            """SELECT user_id,username,display_name,total_points
               FROM members WHERE chat_id=? AND lower(username)=?""",
            (chat_id, username),
        ).fetchone()


def member_label(chat_id: int, user_id: int) -> str:
    with _connect() as db:
        row = db.execute(
            "SELECT user_id,username,display_name FROM members WHERE chat_id=? AND user_id=?",
            (chat_id, user_id),
        ).fetchone()
    if row:
        return _label(row)
    return mention_html(user_id, f"Kullanıcı {user_id}")


def manual_adjust(
    chat_id: int,
    user_id: int,
    points: int,
    reason: str,
    decided_by: int,
) -> bool:
    if not is_owner(decided_by) or points == 0:
        return False
    with _connect() as db:
        db.execute(
            "INSERT OR IGNORE INTO members(chat_id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
            (chat_id, user_id, time.time(), time.time()),
        )
        return _add_event(
            db,
            chat_id,
            user_id,
            f"manual:{chat_id}:{user_id}:{time.time_ns()}",
            "manual_adjustment",
            points,
            metadata={"reason": reason},
        )


def statistics(chat_id: int, user_id: int) -> dict[str, int]:
    _, week_key = _periods()
    with _connect() as db:
        rows = db.execute(
            """SELECT event_type,COUNT(*) AS count
               FROM activity_events
               WHERE chat_id=? AND user_id=? AND week_key=?
               GROUP BY event_type""",
            (chat_id, user_id, week_key),
        ).fetchall()
    return {str(row["event_type"]): int(row["count"]) for row in rows}


def link_message_sources(url: str) -> list[tuple[int, int]]:
    """Find legacy link message IDs whose activity metadata matches a URL."""
    normalized = url.split("?", 1)[0].rstrip("/")
    matches: list[tuple[int, int]] = []
    with _connect() as db:
        rows = db.execute(
            """SELECT chat_id,source_id,metadata FROM activity_events
               WHERE event_type='link_share'"""
        ).fetchall()
    for row in rows:
        try:
            stored_url = json.loads(row["metadata"]).get("url", "")
        except (TypeError, ValueError):
            stored_url = ""
        if str(stored_url).rstrip("/") == normalized:
            try:
                matches.append((int(row["chat_id"]), int(row["source_id"])))
            except (TypeError, ValueError):
                continue
    return matches


def create_penalty_reviews(chat_id: int, vote_token: str, user_ids: set[int]) -> list[int]:
    created: list[int] = []
    with _connect() as db:
        for user_id in sorted(user_ids):
            if is_owner(user_id):
                continue
            cursor = db.execute(
                """INSERT OR IGNORE INTO penalty_reviews(
                     chat_id,vote_token,user_id,points_delta,created_at
                   ) VALUES(?,?,?,?,?)""",
                (chat_id, vote_token, user_id, -2, time.time()),
            )
            if cursor.rowcount == 1:
                created.append(int(cursor.lastrowid))
    return created


def apply_penalty(review_id: int, decided_by: int, reason: str = "Hatalı spoiler butonu kullanımı") -> bool:
    if not is_owner(decided_by):
        return False
    with _connect() as db:
        db.execute("BEGIN IMMEDIATE")
        review = db.execute(
            "SELECT * FROM penalty_reviews WHERE id=? AND status='pending'",
            (review_id,),
        ).fetchone()
        if not review:
            db.rollback()
            return False
        db.execute(
            "INSERT OR IGNORE INTO members(chat_id,user_id,created_at,updated_at) VALUES(?,?,?,?)",
            (review["chat_id"], review["user_id"], time.time(), time.time()),
        )
        applied = _add_event(
            db,
            review["chat_id"],
            review["user_id"],
            f"penalty:{review_id}",
            "penalty",
            review["points_delta"],
            source_id=str(review["vote_token"]),
            metadata={"reason": reason},
        )
        db.execute(
            """UPDATE penalty_reviews
               SET status='approved',reason=?,decided_by=?,decided_at=?
               WHERE id=?""",
            (reason, decided_by, time.time(), review_id),
        )
        db.commit()
        return applied


def reject_penalty(review_id: int, decided_by: int) -> bool:
    if not is_owner(decided_by):
        return False
    with _connect() as db:
        cursor = db.execute(
            """UPDATE penalty_reviews
               SET status='rejected',decided_by=?,decided_at=?
               WHERE id=? AND status='pending'""",
            (decided_by, time.time(), review_id),
        )
        return cursor.rowcount == 1


def pending_reviews(limit: int = 20) -> list[sqlite3.Row]:
    with _connect() as db:
        return db.execute(
            "SELECT * FROM penalty_reviews WHERE status='pending' ORDER BY created_at LIMIT ?",
            (limit,),
        ).fetchall()


def penalty_history(chat_id: int, user_id: int, limit: int = 20) -> list[sqlite3.Row]:
    with _connect() as db:
        return db.execute(
            """SELECT id,status,points_delta,reason,decided_by,created_at,decided_at
               FROM penalty_reviews WHERE chat_id=? AND user_id=?
               ORDER BY created_at DESC LIMIT ?""",
            (chat_id, user_id, limit),
        ).fetchall()


def _label(row: sqlite3.Row) -> str:
    username = str(row["username"] or "")
    if username:
        return f"@{html.escape(username)}"
    name = html.escape(str(row["display_name"] or "Kullanıcı"))
    return mention_html(int(row["user_id"]), name)


def leaderboard(chat_id: int, week_only: bool = True, limit: int = 10) -> list[sqlite3.Row]:
    _, week_key = _periods()
    with _connect() as db:
        if week_only:
            return db.execute(
                """SELECT m.user_id,m.username,m.display_name,
                          MAX(0,COALESCE(SUM(e.points),0)) AS points
                   FROM members m LEFT JOIN activity_events e
                     ON e.chat_id=m.chat_id AND e.user_id=m.user_id AND e.week_key=?
                   WHERE m.chat_id=? AND m.user_id != ?
                   GROUP BY m.chat_id,m.user_id
                   HAVING points > 0 ORDER BY points DESC, m.user_id LIMIT ?""",
                (week_key, chat_id, ACTIVITY_OWNER_USER_ID, limit),
            ).fetchall()
        return db.execute(
            """SELECT user_id,username,display_name,total_points AS points
               FROM members WHERE chat_id=? AND user_id != ? AND total_points > 0
               ORDER BY total_points DESC,user_id LIMIT ?""",
            (chat_id, ACTIVITY_OWNER_USER_ID, limit),
        ).fetchall()


def member_summary(chat_id: int, user_id: int) -> Optional[sqlite3.Row]:
    _, week_key = _periods()
    with _connect() as db:
        row = db.execute(
            """SELECT m.user_id,m.username,m.display_name,m.total_points,
                      MAX(0,COALESCE(SUM(CASE WHEN e.week_key=? THEN e.points ELSE 0 END),0)) AS week_points
               FROM members m LEFT JOIN activity_events e
                 ON e.chat_id=m.chat_id AND e.user_id=m.user_id
               WHERE m.chat_id=? AND m.user_id=?
               GROUP BY m.chat_id,m.user_id""",
            (week_key, chat_id, user_id),
        ).fetchone()
        return row


def badges(points: int) -> list[str]:
    result: list[str] = []
    for threshold, badge in (
        (10, "Anime Çırağı"),
        (30, "Sezon Takipçisi"),
        (75, "Spoiler Avcısı"),
        (150, "Topluluk Rehberi"),
        (300, "Anime Bilgesi"),
    ):
        if points >= threshold:
            result.append(badge)
    return result


def group_ids() -> list[int]:
    with _connect() as db:
        return [int(row[0]) for row in db.execute("SELECT chat_id FROM groups").fetchall()]


def latest_group_id(user_id: int = 0) -> Optional[int]:
    """Return a group for private commands.

    Prefer the user's most recently active group, but fall back to the most
    recently seen group so a user can use commands in private before earning
    any activity points there.
    """
    with _connect() as db:
        row = None
        if user_id:
            row = db.execute(
                """SELECT g.chat_id
                   FROM groups g JOIN members m ON m.chat_id=g.chat_id
                   WHERE m.user_id=?
                ORDER BY MAX(g.last_seen, m.updated_at) DESC LIMIT 1""",
                (user_id,),
            ).fetchone()
        if not row:
            row = db.execute(
                "SELECT chat_id FROM groups ORDER BY last_seen DESC LIMIT 1"
            ).fetchone()
    return int(row[0]) if row else (ACTIVITY_DEFAULT_GROUP_ID or None)


def report_html(chat_id: int) -> str:
    lines = [
        f"👑 Yönetici: {owner_label_html()}",
        f"<b>{html.escape(OWNER_TITLE)}</b>",
        "",
        "🏆 <b>Haftanın Anime Aktivite Raporu</b>",
    ]
    rows = leaderboard(chat_id, week_only=True, limit=10)
    if not rows:
        lines.append("Henüz bu hafta puan kazanan olmadı.")
    else:
        medals = ("🥇", "🥈", "🥉")
        for index, row in enumerate(rows):
            medal = medals[index] if index < 3 else f"{index + 1}."
            lines.append(f"{medal} {_label(row)} — <b>{row['points']} puan</b>")
    return "\n".join(lines)
